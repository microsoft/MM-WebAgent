import sys
sys.path.append('.')

import os
import json
import random
import datetime
import traceback
import time
import threading
import importlib
from typing import List
import concurrent.futures
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from utils import (
    parse_score,
    generate_video,
    generate_video_until_success,
    request_chatgpt_t2i_until_success,
    request_chatgpt_t2t,
    request_chatgpt_t2t_until_success,
    request_chatgpt_i2t_until_success,
    get_openai_request_config
)
from prompt.planner_prompts import AGENTS_PROMPT_V5 as AGENTS_PROMPT
from agent.screenshot_html import screenshot_main_html_dirs_http
from planner.config import *
from planner.reflection import validate_parsed_plan


class _JsonlRunLogger:
    def __init__(self, path: str, *, enabled: bool = True):
        self.path = str(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    def log(self, event: dict) -> None:
        if not self.enabled:
            return
        try:
            rec = dict(event or {})
            rec.setdefault("ts", datetime.datetime.utcnow().isoformat() + "Z")
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            line = json.dumps(rec, ensure_ascii=False)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            return

def _safe_write_json(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        return


def get_unique_subdir(project_root):
    time_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = len(os.listdir(project_root)) + 1 if os.path.exists(project_root) else 1
    random_id = random.randint(1000, 9999)
    project_dir = os.path.join(project_root, f"{unique_id}_{time_str}_{random_id}")
    os.makedirs(project_dir, exist_ok=True)
    
    return project_dir


class GenerationManager:
    """Unified generation manager for planning, task parsing, and execution."""

    AGENTS_PROMPT = None

    def __init__(
        self,
        *,
        save_dir: str = "outputs",
        plan_strs: list | None = None,
        user_prompts: list | None = None,
        file_ids: list | None = None,
        planner_api_model: str = "5.2",
        planner_max_workers: int = 8,
        planner_max_retries: int = 3,
        planner_timeout: int = 180,
        planner_sleep_time: int = 5,
        planner_max_tokens: int = 8192,
        run_log_path: str | None = None,
        run_id: str | None = None,
        api_config: dict = API_CONFIG,
        main_name: str = "main.html",
        enable_video: bool = False,
        mini_flux_bs: int = 4,
        agent_version: str = "AGENTS_PROMPT_V5",
        debug: bool = False,
    ):
        self.save_dir = save_dir
        self.api_keys = {}
        self.main_name = main_name
        self.mini_flux_bs = mini_flux_bs
        self.debug = debug
        self.enable_video = enable_video

        self.task_queue = defaultdict(list)
        self.failed_tasks = []
        self.project_dirs = []
        self.project_dir_by_file_id: dict[str, str] = {}

        os.makedirs(save_dir, exist_ok=True)
        if run_log_path is None:
            run_log_path = os.path.join(save_dir, "run_log.jsonl")
        self.run_id = run_id or datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self._logger = _JsonlRunLogger(run_log_path, enabled=True)
        self._proj_summary: dict[str, dict] = {}

        module = importlib.import_module("prompt.planner_prompts")
        self.AGENTS_PROMPT = getattr(module, agent_version)
        type(self).AGENTS_PROMPT = self.AGENTS_PROMPT

        has_user_prompts = user_prompts is not None and len(user_prompts) > 0
        if file_ids is None:
            if has_user_prompts:
                file_ids = [str(i + 1).zfill(3) for i in range(len(user_prompts))]
            else:
                file_ids = []
        self.file_ids = file_ids

        if has_user_prompts and len(self.file_ids) != len(user_prompts):
            raise ValueError(f"file_ids length {len(self.file_ids)} must match user_prompts length {len(user_prompts)}.")

        if has_user_prompts:
            for fid, up in zip(self.file_ids, user_prompts):
                self._init_project_summary(str(fid), str(up))
        elif self.file_ids:
            summary_prompts = user_prompts or [""] * len(self.file_ids)
            for fid, up in zip(self.file_ids, summary_prompts):
                self._init_project_summary(str(fid), str(up))

        self.config_apis(api_config)

        resolved_plan_strs = list(plan_strs) if plan_strs else []
        should_plan = has_user_prompts and not resolved_plan_strs
        if should_plan:
            resolved_plan_strs = self._plan_all(
                user_prompts=user_prompts,
                file_ids=self.file_ids,
                planner_api_model=planner_api_model,
                planner_max_workers=planner_max_workers,
                planner_max_retries=planner_max_retries,
                planner_timeout=planner_timeout,
                planner_sleep_time=planner_sleep_time,
                planner_max_tokens=planner_max_tokens,
                logger=self._logger,
                run_id=self.run_id,
                debug=debug,
            )
            for fid, plan_str in zip(self.file_ids, resolved_plan_strs):
                try:
                    obj = json.loads(plan_str) if isinstance(plan_str, str) else {}
                except Exception:
                    obj = {}
                summary = self._proj_summary.get(str(fid))
                if summary is None:
                    continue
                if isinstance(obj, dict) and obj.get("planning_error"):
                    summary["planning"] = {"status": "failed", "error": obj.get("planning_error")}
                else:
                    summary["planning"] = {"status": "ok"}
                _safe_write_json(self._summary_path(str(fid)), summary)
        elif resolved_plan_strs and self.file_ids:
            for fid in self.file_ids:
                summary = self._proj_summary.get(str(fid))
                if summary is None:
                    continue
                summary["planning"] = {"status": "provided"}
                _safe_write_json(self._summary_path(str(fid)), summary)

        plan_user_prompts = user_prompts if has_user_prompts and len(user_prompts) == len(self.file_ids) else None
        self.parse_all_plans(resolved_plan_strs, self.file_ids, plan_user_prompts)

    def _summary_path(self, file_id: str) -> str:
        return os.path.join(self.save_dir, str(file_id), "run_summary.json")

    def _init_project_summary(self, file_id: str, user_prompt: str) -> None:
        if file_id in self._proj_summary:
            return
        self._proj_summary[file_id] = {
            "run_id": self.run_id,
            "file_id": file_id,
            "project_dir": os.path.join(self.save_dir, file_id),
            "user_prompt": user_prompt,
            "planning": {"status": "unknown"},
            "tasks": {
                "total": 0,
                "success": 0,
                "failed": 0,
                "failed_tasks": [],
            },
        }

    def _update_project_summary_task(self, *, file_id: str, meta_query: MetaQuery, success: bool, error: dict | None) -> None:
        summary = self._proj_summary.get(file_id)
        if not summary:
            return

        summary["tasks"]["total"] += 1
        if success:
            summary["tasks"]["success"] += 1
        else:
            summary["tasks"]["failed"] += 1
            summary["tasks"]["failed_tasks"].append(
                {
                    "type": getattr(meta_query, "type", None),
                    "save_path": getattr(meta_query, "save_path", None),
                    "extra_info": getattr(meta_query, "extra_info", None),
                    "error": error,
                }
            )

        _safe_write_json(self._summary_path(file_id), summary)

    def _finalize_summaries(self) -> None:
        for file_id, summary in (self._proj_summary or {}).items():
            _safe_write_json(self._summary_path(file_id), summary)

    @staticmethod
    def _strip_fences(text: str) -> str:
        if not isinstance(text, str):
            return ""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.replace("```json", "", 1).replace("```", "", 1).strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        return stripped.strip()

    @classmethod
    def _plan_one(
        cls,
        *,
        file_id: str,
        user_prompt: str,
        url: str,
        api_key: str,
        model: str,
        planner_system_prompt: str,
        planner_max_retries: int,
        planner_timeout: int,
        planner_sleep_time: int,
        planner_max_tokens: int,
        logger: _JsonlRunLogger | None,
        run_id: str,
        debug: bool,
    ) -> str:
        prompt = user_prompt
        for attempt in range(max(1, int(planner_max_retries))):
            if logger:
                logger.log(
                    {
                        "run_id": run_id,
                        "phase": "plan",
                        "event": "plan_attempt_start",
                        "file_id": file_id,
                        "attempt": attempt + 1,
                    }
                )
            try:
                out, success = request_chatgpt_t2t(
                    user_prompt=prompt,
                    system_prompt=planner_system_prompt,
                    url=url,
                    api_key=api_key,
                    model=model,
                    max_tokens=int(planner_max_tokens),
                    debug=debug,
                    timeout=int(planner_timeout),
                    log_path=(logger.path if logger else None),
                )
                if not success:
                    if logger:
                        logger.log(
                            {
                                "run_id": run_id,
                                "phase": "plan",
                                "event": "plan_attempt_failed",
                                "file_id": file_id,
                                "attempt": attempt + 1,
                                "error": out,
                            }
                        )
                    time.sleep(int(planner_sleep_time))
                    continue

                plan_text = cls._strip_fences(out)
                valid_plan, plan_obj = validate_parsed_plan(plan_text, check_content=True, debug=debug)
                if not valid_plan:
                    raise ValueError("planner returned invalid plan schema or asset references")
                if logger:
                    logger.log(
                        {
                            "run_id": run_id,
                            "phase": "plan",
                            "event": "plan_success",
                            "file_id": file_id,
                        }
                    )
                return json.dumps(plan_obj, ensure_ascii=False)
            except Exception as e:
                err = {"error_type": type(e).__name__, "message": str(e)[:2000]}
                if logger:
                    logger.log(
                        {
                            "run_id": run_id,
                            "phase": "plan",
                            "event": "plan_attempt_exception",
                            "file_id": file_id,
                            "attempt": attempt + 1,
                            "error": err,
                        }
                    )
                prompt = (
                    user_prompt
                    + "\n\nIMPORTANT: Return ONLY valid JSON per the required schema. "
                    "The plan must include a non-empty code_generation list, valid asset fields, "
                    "and every referenced assets/*.png|.mp4|.html path must have a matching generation entry. "
                    "No markdown fences, no extra text."
                )
                time.sleep(int(planner_sleep_time))
                continue

        if logger:
            logger.log(
                {
                    "run_id": run_id,
                    "phase": "plan",
                    "event": "plan_exhausted",
                    "file_id": file_id,
                    "attempts": int(planner_max_retries),
                }
            )
        return json.dumps({"planning_error": "planner_failed"}, ensure_ascii=False)

    @classmethod
    def _plan_all(
        cls,
        *,
        user_prompts: list,
        file_ids: list,
        planner_api_model: str,
        planner_max_workers: int,
        planner_max_retries: int,
        planner_timeout: int,
        planner_sleep_time: int,
        planner_max_tokens: int,
        logger: _JsonlRunLogger | None,
        run_id: str,
        debug: bool,
    ) -> list:
        if cls.AGENTS_PROMPT is None:
            print("[plan] using default AGENTS_PROMPT from prompts.agent_prompts")
            from prompt.planner_prompts import AGENTS_PROMPT_V5 as AGENTS_PROMPT
        else:
            AGENTS_PROMPT = cls.AGENTS_PROMPT

        planner_system_prompt = AGENTS_PROMPT["planner"]
        request_cfg = get_openai_request_config(planner_api_model)
        url = request_cfg["url"]
        api_key = request_cfg["headers"]
        model = request_cfg["model"]

        plan_strs: list[str] = [""] * len(user_prompts)

        def _job(i: int):
            plan_strs[i] = cls._plan_one(
                file_id=str(file_ids[i]),
                user_prompt=user_prompts[i],
                url=url,
                api_key=api_key,
                model=model,
                planner_system_prompt=planner_system_prompt,
                planner_max_retries=planner_max_retries,
                planner_timeout=planner_timeout,
                planner_sleep_time=planner_sleep_time,
                planner_max_tokens=planner_max_tokens,
                logger=logger,
                run_id=run_id,
                debug=debug,
            )

        max_workers = max(1, int(planner_max_workers))
        print(f"[plan] planning {len(user_prompts)} prompts with {max_workers} workers via model={planner_api_model}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_job, i): file_ids[i] for i in range(len(user_prompts))}
            try:
                from tqdm import tqdm

                for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="planning"):
                    future.result()
            except Exception:
                done = 0
                for future in concurrent.futures.as_completed(futures):
                    future.result()
                    done += 1
                    if done % 10 == 0 or done == len(futures):
                        print(f"[plan] {done}/{len(futures)} done")

        return plan_strs

    def run_all_tasks(self, pipes=None, only_tasks: list[str] | None = None):
        """
        Run all tasks with per-queue progress bars and structured logging.
        Failures are logged, but the overall run continues.
        """
        if len(self) == 0:
            self._finalize_summaries()
            return

        try:
            for fid in (self.file_ids or []):
                self._init_project_summary(str(fid), user_prompt="")
        except Exception:
            pass

        def _process_and_log(meta_query: MetaQuery) -> bool:
            fid = str(getattr(meta_query, "file_id", "") or "")
            try:
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_start",
                            "file_id": fid,
                            "task_type": getattr(meta_query, "type", None),
                            "save_path": getattr(meta_query, "save_path", None),
                        }
                    )
                ok = bool(self._process_one_task(meta_query))
                if not ok and self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_failed",
                            "file_id": fid,
                            "task_type": getattr(meta_query, "type", None),
                            "save_path": getattr(meta_query, "save_path", None),
                            "error": {"error_type": "task_returned_false"},
                        }
                    )
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_done",
                            "file_id": fid,
                            "task_type": getattr(meta_query, "type", None),
                            "save_path": getattr(meta_query, "save_path", None),
                            "success": ok,
                        }
                    )
                self._update_project_summary_task(
                    file_id=fid,
                    meta_query=meta_query,
                    success=ok,
                    error=None if ok else {"error_type": "task_returned_false"},
                )
                return ok
            except Exception as e:
                err = {
                    "error_type": type(e).__name__,
                    "message": str(e)[:2000],
                    "traceback": traceback.format_exc()[:8000],
                }
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_exception",
                            "file_id": fid,
                            "task_type": getattr(meta_query, "type", None),
                            "save_path": getattr(meta_query, "save_path", None),
                            "error": err,
                        }
                    )
                self._update_project_summary_task(file_id=fid, meta_query=meta_query, success=False, error=err)
                return False

        def _flux_batch_and_log(meta_queries: list, pipes):
            fid = str(getattr(meta_queries[0], "file_id", "") or "") if meta_queries else ""
            save_paths = [getattr(mq, "save_path", None) for mq in (meta_queries or [])]
            try:
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_start",
                            "file_id": fid,
                            "task_type": "imgen_flux_batch",
                            "save_paths": save_paths,
                        }
                    )
                ok = bool(self._image_batch_generation_task(meta_queries, pipes))
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_done",
                            "file_id": fid,
                            "task_type": "imgen_flux_batch",
                            "save_paths": save_paths,
                            "success": ok,
                        }
                    )
                for mq in meta_queries or []:
                    self._update_project_summary_task(
                        file_id=str(getattr(mq, "file_id", "") or ""),
                        meta_query=mq,
                        success=ok,
                        error=None if ok else {"error_type": "flux_batch_failed"},
                    )
                return ok
            except Exception as e:
                err = {
                    "error_type": type(e).__name__,
                    "message": str(e)[:2000],
                    "traceback": traceback.format_exc()[:8000],
                }
                if self._logger:
                    self._logger.log(
                        {
                            "run_id": self.run_id,
                            "phase": "task",
                            "event": "task_exception",
                            "file_id": fid,
                            "task_type": "imgen_flux_batch",
                            "save_paths": save_paths,
                            "error": err,
                        }
                    )
                for mq in meta_queries or []:
                    self._update_project_summary_task(
                        file_id=str(getattr(mq, "file_id", "") or ""),
                        meta_query=mq,
                        success=False,
                        error=err,
                    )
                return False

        executors: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
        futures_map: dict[str, list] = {}

        for qtype, tasks in self.task_queue.items():
            if not tasks or (only_tasks is not None and qtype not in only_tasks):
                continue
            max_workers = self.api_keys.get(qtype, {}).get("max_workers", 4)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            executors[qtype] = executor

            if qtype == "imgen" and pipes is not None:
                futures = [executor.submit(_flux_batch_and_log, tasks, pipes)]
            else:
                futures = [executor.submit(_process_and_log, meta_query) for meta_query in tasks]
            futures_map[qtype] = futures

        for qtype, futures in futures_map.items():
            try:
                from tqdm import tqdm

                for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc=f"tasks:{qtype}"):
                    try:
                        future.result()
                    except Exception:
                        if self._logger:
                            self._logger.log(
                                {
                                    "run_id": self.run_id,
                                    "phase": "task",
                                    "event": "future_exception",
                                    "queue": qtype,
                                    "error": traceback.format_exc()[:8000],
                                }
                            )
                        continue
            except Exception:
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        continue

        for executor in executors.values():
            executor.shutdown(wait=True)

        self._finalize_summaries()

    def _take_screenshot_task(self, meta_query: MetaQuery):
        """execute take screenshot task."""
        project_dir = meta_query.extra_info["project_dir"]
        main_name = meta_query.extra_info["main_name"]
        save_path = meta_query.save_path
        success = False

        for _ in range(MAX_RETRIES):
            if os.path.exists(save_path):
                return True
            try:
                _, _ = screenshot_main_html_dirs_http(
                    root=project_dir,
                    out_file=save_path,
                    fname=main_name,
                    debug=self.debug,
                )
                success = os.path.exists(save_path)
            except Exception:
                if self.debug:
                    traceback.print_exc()

        return success

    def _clean_task_queue(self):
        """clean the task queue."""
        self.task_queue = defaultdict(list)

    def _get_project_dir(self, project_root: str, file_id: str) -> str:
        os.makedirs(project_root, exist_ok=True)
        return project_root

    def _process_one_task(self, meta_query: MetaQuery):
        """process one task based on its type."""
        task_type = meta_query.type

        if task_type == "data_visualization":
            return self._chart_generation_task(meta_query)
        if task_type == "image_generation":
            return self._image_generation_task(meta_query)
        if task_type == "video_generation":
            if self.enable_video:
                return self._video_generation_task(meta_query)
            return self._image_generation_task(meta_query)
        if task_type == "code_generation":
            return self._layout_generation_task(meta_query)
        if task_type == "take_screenshot":
            return self._take_screenshot_task(meta_query)

        print(f"Unknown task type: {task_type}")
        return False

    def _image_batch_generation_task(self, meta_queries: list, pipes=None):
        """
        Execute batch image generation task using multiple FluxPipeline instances (pipes).
        Each pipe should be on a different GPU for parallel inference.
        """
        if not isinstance(pipes, list):
            pipes = [pipes]
        elif len(pipes) == 0:
            return False

        prompts = [mq.extra_info["prompt"] for mq in meta_queries]
        sizes = [mq.extra_info.get("size", "1024x1024") for mq in meta_queries]
        save_paths = [mq.save_path for mq in meta_queries]

        task_group_by_size = defaultdict(list)
        for i, size in enumerate(sizes):
            task_group_by_size[size].append((prompts[i], save_paths[i]))

        def find_nearest_multiple(n, multiple):
            return n if n % multiple == 0 else n + (multiple - n % multiple)

        num_pipes = len(pipes)

        for size, tasks in task_group_by_size.items():
            w, h = map(int, size.lower().split("x"))
            w = find_nearest_multiple(w, 16)
            h = find_nearest_multiple(h, 16)
            sub_prompts, sub_save_paths = zip(*tasks)

            split_size = (len(sub_prompts) + num_pipes - 1) // num_pipes
            pipe_tasks = [
                (
                    sub_prompts[idx * split_size:(idx + 1) * split_size],
                    sub_save_paths[idx * split_size:(idx + 1) * split_size],
                )
                for idx in range(num_pipes)
            ]

            def worker(pipe_idx, prompts_batch, save_paths_batch):
                pipe = pipes[pipe_idx]
                for i in range(0, len(prompts_batch), self.mini_flux_bs):
                    mini_prompts = prompts_batch[i:i + self.mini_flux_bs]
                    mini_save_paths = save_paths_batch[i:i + self.mini_flux_bs]

                    images = pipe(
                        list(mini_prompts),
                        height=h,
                        width=w,
                        guidance_scale=3.5,
                        num_inference_steps=28,
                        max_sequence_length=512,
                    ).images

                    for img, save_path in zip(images, mini_save_paths):
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        img.save(save_path)

            with ThreadPoolExecutor(max_workers=num_pipes) as executor:
                for idx, (prompts_batch, save_paths_batch) in enumerate(pipe_tasks):
                    executor.submit(worker, idx, prompts_batch, save_paths_batch)

        return True

    def _image_generation_task(self, meta_query: MetaQuery):
        """execute image generation task."""
        prompt = meta_query.extra_info["prompt"]
        save_path = meta_query.save_path

        url = self.api_keys["imgen"]["url"]
        api_key = self.api_keys["imgen"]["api_key"]
        model = self.api_keys["imgen"]["model"]

        if os.path.exists(save_path):
            return True
        _, success = request_chatgpt_t2i_until_success(
            user_prompt=prompt,
            system_prompt=self.AGENTS_PROMPT["imagen"],
            url=url,
            api_key=api_key,
            model=model,
            save_path=save_path,
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
            debug=self.debug,
        )
        return success

    def _video_generation_task(self, meta_query: MetaQuery):
        """
        Video generation via `utils.generate_video` (Sora-compatible endpoint).
        Normalizes size/seconds to supported values to avoid hard assertion failures.
        """
        prompt = meta_query.extra_info["prompt"]
        size = str(meta_query.extra_info.get("size", "1792x1024"))
        seconds = str(meta_query.extra_info.get("seconds", "4"))
        save_path = meta_query.save_path

        allowed_sizes = ["720x1280", "1280x720", "1024x1792", "1792x1024"]
        allowed_seconds = ["4", "8", "12"]

        def _norm_size(value: str) -> str:
            if value in allowed_sizes:
                return value
            if "x" in value:
                try:
                    w, h = [int(x) for x in value.lower().split("x", 1)]
                    if w >= h:
                        return "1792x1024"
                    return "1024x1792"
                except Exception:
                    pass
            return "1792x1024"

        def _norm_seconds(value: str) -> str:
            if value in allowed_seconds:
                return value
            try:
                parsed = int(float(value))
            except Exception:
                return "4"
            return min(allowed_seconds, key=lambda candidate: abs(int(candidate) - parsed))

        size = _norm_size(size)
        seconds = _norm_seconds(seconds)

        if not str(save_path).lower().endswith(".mp4"):
            save_path = str(save_path).rsplit(".", 1)[0] + ".mp4"
            meta_query.save_path = save_path

        if os.path.exists(save_path):
            return True

        video_file_path, success = generate_video_until_success(
            prompt=prompt,
            seconds=seconds,
            size=size,
            save_path=save_path,
            debug=self.debug,
            max_retries=10,
        )
        return bool(success and video_file_path)

    def _chart_generation_task(self, meta_query: MetaQuery):
        prompt = meta_query.extra_info["prompt"]
        source_data = meta_query.extra_info["source_data"]
        user_prompt = f"{prompt}. The source data is as follows: \n {source_data}."
        save_path = meta_query.save_path

        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        chart_data, success = request_chatgpt_t2t_until_success(
            user_prompt=user_prompt,
            system_prompt=self.AGENTS_PROMPT.get("vis", ""),
            url=url,
            api_key=api_key,
            model=model,
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
            reasoning_effort=reasoning_effort,
            debug=self.debug,
            log_path=(self._logger.path if self._logger else None),
        )
        try:
            chart_data = chart_data.strip("```html\n").strip("```")
            with open(save_path, "w", encoding="utf-8") as handle:
                handle.write(chart_data)
        except Exception:
            success = False
        return success

    def _layout_generation_task(self, meta_query: MetaQuery):
        prompt = meta_query.extra_info["prompt"]
        save_path = meta_query.save_path

        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        html_data, success = request_chatgpt_t2t_until_success(
            user_prompt=prompt,
            system_prompt=self.AGENTS_PROMPT.get("html", ""),
            url=url,
            api_key=api_key,
            model=model,
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
            debug=self.debug,
            reasoning_effort=reasoning_effort,
            log_path=(self._logger.path if self._logger else None),
        )
        try:
            html_data = html_data.strip("```html\n").strip("```")
            with open(save_path, "w", encoding="utf-8") as handle:
                handle.write(html_data)
        except Exception:
            success = False
        return success

    def parse_all_plans(self, plan_strs: list, file_ids: list, user_prompts: list = None):
        """parse all planning results to generate tasks."""
        if not plan_strs or not file_ids:
            return

        assert len(plan_strs) == len(file_ids)
        for i in range(len(plan_strs)):
            plan_str = plan_strs[i]
            file_id = file_ids[i]
            user_prompt = user_prompts[i] if user_prompts is not None else None
            self._parse_one_plan(plan_str, file_id, user_prompt)

    def _parse_one_plan(self, plan_str, file_id, user_prompt=None):
        """parse one planning result to generate tasks."""
        project_root = os.path.join(self.save_dir, str(file_id))
        project_dir = self._get_project_dir(project_root, str(file_id))
        self.project_dirs.append(project_dir)
        self.project_dir_by_file_id[str(file_id)] = project_dir

        try:
            planner_json = json.loads(plan_str)
        except Exception:
            self.failed_tasks.append(file_id)
            return

        if user_prompt is not None:
            planner_json["user_prompt"] = user_prompt

        with open(os.path.join(project_dir, "planner_output.json"), "w", encoding="utf-8") as handle:
            json.dump(planner_json, handle, ensure_ascii=False, indent=4)

        img_vis_list = planner_json.get("data_visualization", [])
        for vis_item in img_vis_list:
            try:
                vis_prompt = vis_item["prompt"]
                source_data = vis_item["source_data"]
                vis_save_path = os.path.join(project_dir, vis_item["save_path"])
                context = vis_item.get("context", None)
                compiled_attributes = vis_item.get("compiled_attributes", None)
                vis_prompt = self._get_meta_prompt(vis_prompt, context, compiled_attributes, meta_type="chart")

                meta_query = MetaQuery(
                    type="data_visualization",
                    file_id=file_id,
                    extra_info={"prompt": vis_prompt, "source_data": source_data},
                    save_path=vis_save_path,
                )
                self.task_queue["plain"].append(meta_query)
            except Exception:
                continue

        img_gen_list = planner_json.get("image_generation", [])
        for img_item in img_gen_list:
            try:
                img_prompt = img_item["prompt"]
                img_size = img_item.get("size", "1024x1024")
                img_save_path = os.path.join(project_dir, img_item["save_path"])
                if not img_save_path.lower().endswith((".png", ".jpg", ".jpeg")):
                    continue
                context = img_item.get("context", None)
                compiled_attributes = img_item.get("compiled_attributes", None)
                img_prompt = self._get_meta_prompt(img_prompt, context, compiled_attributes, meta_type="image")

                meta_query = MetaQuery(
                    type="image_generation",
                    file_id=file_id,
                    extra_info={"prompt": img_prompt, "size": img_size},
                    save_path=img_save_path,
                )
                self.task_queue["imgen"].append(meta_query)
            except Exception:
                continue

        video_gen_list = planner_json.get("video_generation", [])
        for video_item in video_gen_list:
            try:
                video_prompt = video_item["prompt"]
                video_size = video_item["size"]
                video_seconds = video_item["seconds"]
                video_save_path = os.path.join(project_dir, video_item["save_path"])
                if not video_save_path.lower().endswith((".mp4", ".mov", ".avi")):
                    continue
                if not self.enable_video:
                    video_save_path = video_save_path.rsplit(".", 1)[0] + ".png"
                context = video_item.get("context", None)
                compiled_attributes = video_item.get("compiled_attributes", None)
                video_prompt = self._get_meta_prompt(video_prompt, context, compiled_attributes, meta_type="video")

                meta_query = MetaQuery(
                    type="video_generation",
                    file_id=file_id,
                    extra_info={"prompt": video_prompt, "size": video_size, "seconds": video_seconds},
                    save_path=video_save_path,
                )
                if self.enable_video:
                    self.task_queue["vidgen"].append(meta_query)
                else:
                    self.task_queue["imgen"].append(meta_query)
            except Exception:
                continue

        code_gen_list = planner_json.get("code_generation", [])
        for code_item in code_gen_list:
            try:
                code_prompt = code_item["prompt"]
                if not self.enable_video:
                    code_prompt = code_prompt.replace(".mp4", ".png").replace(".mov", ".png").replace(".avi", ".png")

                code_save_path = os.path.join(project_dir, self.main_name)
                meta_query = MetaQuery(
                    type="code_generation",
                    file_id=file_id,
                    extra_info={"prompt": code_prompt},
                    save_path=code_save_path,
                )
                self.task_queue["plain"].append(meta_query)
            except Exception:
                continue

    def __len__(self):
        return sum(len(v) for v in self.task_queue.values())

    def _get_meta_prompt(self, prompt, context=None, compiled_attributes=None, meta_type="image") -> str:
        """get meta prompt for a given meta query."""
        context_key = ("page_style", "role", "section")
        if meta_type == "image":
            attr_key = ("visual_style", "color_tone", "composition", "lighting")
        elif meta_type == "video":
            attr_key = ("visual_style", "motion_intensity", "camera_behavior", "loopability")
        elif meta_type == "chart":
            attr_key = ("chart_style", "chart_type", "color_palette", "visual_emphasis")
        else:
            attr_key = ()

        try:
            if context:
                context_template = self.AGENTS_PROMPT.get(f"meta_context_{meta_type}", "")
                context_str = context_template.format(**{k: context.get(k, "") for k in context_key})
                prompt += context_str

            if compiled_attributes:
                attr_template = self.AGENTS_PROMPT.get(f"meta_attr_{meta_type}", "")
                attr_str = attr_template.format(**{k: compiled_attributes.get(k, "") for k in attr_key})
                prompt += attr_str
        except Exception:
            traceback.print_exc()
            print(context, compiled_attributes, meta_type)

        return prompt

    def _summerize_task_queue(self):
        """summarize current task queue."""
        print("=== Task Queue Summary ===")
        print(f"Total number of tasks: {len(self)}")
        for task_type, tasks in self.task_queue.items():
            print(f"Task type: {task_type}, Number of tasks: {len(tasks)}")

    def config_apis(self, api_config: dict):
        """configure api keys for different task types."""
        for task_type, config_info in api_config.items():
            api_model, max_workers = config_info
            reasoning_effort = "medium" if api_model in ["5.1", "5.2", "gpt-5.1", "gpt-5.2", "gpt5"] else None

            request_cfg = get_openai_request_config(api_model)
            self.api_keys[task_type] = {
                "url": request_cfg["url"],
                "api_key": request_cfg["headers"],
                "model": request_cfg["model"],
                "max_workers": max_workers,
                "reasoning_effort": reasoning_effort,
            }


