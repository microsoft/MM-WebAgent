import sys
sys.path.append('.')

import json
import os
import threading
from datetime import datetime, timezone
from collections import defaultdict
from planner.evaluation_manager import EvaluationManager
from planner.config import *
from utils.parse_scores import get_issues_from_penalties

from utils import (
    parse_score,
    request_chatgpt_t2i_until_success,
    request_chatgpt_t2t_until_success,
    request_chatgpt_i2i_until_success,
    request_chatgpt_i2t_until_success,
)

from utils.mm_utils import (
    get_main_html,
    get_user_prompt,
    parse_html_file,
    read_html_file,
    make_safe_id,
    download_media,
    sample_frames_from_video,
    extract_html_excerpt,
    apply_webpage_css_fixes,
    extract_webpage_css_excerpt,
    extract_design_prompt_excerpt,
    get_iframe_height_from_html_sync,
    load_standalone_image_as_png_bytes,
)

from prompt.reflection_prompts import REFLECTION_PROMPTS

_WARNING_LOG_LOCK = threading.Lock()


def _append_warning_log(project_dir: str, record: dict) -> None:
    """
    Append a JSON-line record into <project_dir>/warning.log.
    Never raises. Thread-safe for concurrent tasks.
    """
    try:
        if not project_dir:
            return
        p = os.path.join(project_dir, "warning.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rec = dict(record or {})
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(rec, ensure_ascii=False)
        with _WARNING_LOG_LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def _load_reflection_state(project_dir: str) -> dict:
    """
    Per-project state used to keep reflection behavior stable across rounds.
    Stored as JSON at <project_dir>/.reflection_state.json.
    """
    try:
        p = os.path.join(project_dir, ".reflection_state.json")
        if not os.path.exists(p):
            return {}
        return json.loads(open(p, "r", encoding="utf-8").read() or "{}") or {}
    except Exception:
        return {}


def _save_reflection_state(project_dir: str, state: dict) -> None:
    try:
        p = os.path.join(project_dir, ".reflection_state.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(state or {}, ensure_ascii=False, indent=2))
    except Exception:
        return


class ReflectionManager(EvaluationManager):
    def __init__(
        self,
        project_dirs: list,
        api_config: dict=API_CONFIG,
        main_name: str="main.html",
        debug: bool=False,
        chart_mode: str="subpage",
        is_chart_subpage: bool | None = None,
    ):
        super().__init__(
            project_dirs=project_dirs,
            api_config=api_config,
            main_name=main_name,
            debug=debug,
            chart_mode=chart_mode,
            is_chart_subpage=is_chart_subpage,
        )

    def _process_one_task(self, meta_query: MetaQuery):
        """process one task based on its type."""
        task_type = meta_query.type

        if task_type == "take_screenshot":
            return self._take_screenshot_task(meta_query)
        elif task_type == "global_evaluation":
            return self._evaluate_global_evaluation_task(meta_query)
        elif task_type == "image_evaluation":
            return self._evaluate_image_task(meta_query)
        elif task_type == "video_evaluation":
            return self._evaluate_video_task(meta_query)
        elif task_type == "chart_evaluation":
            return self._evaluate_chart_task(meta_query)
        elif task_type == "missing_evaluation":
            return self._evaluate_missing_evaluation_task(meta_query)
        elif task_type == "mm_split":
            return self._do_mm_split_task(meta_query)
        elif task_type == "global_reflection":
            return self._do_global_reflection_task(meta_query)
        elif task_type == "image_local_reflection":
            return self._do_image_local_reflection_task(meta_query)
        elif task_type == "image_global_reflection":
            return self._do_image_global_reflection_task(meta_query)
        elif task_type == "chart_local_reflection":
            return self._do_chart_local_reflection_task(meta_query)
        elif task_type == "chart_global_reflection":
            return self._do_chart_global_reflection_task(meta_query)
        else:
            print(f"Unknown task type: {task_type}")
            return False


    def prepare_initial_reflection_tasks(self, reflection_type: str="global"):
        """prepare reflection tasks."""
        if reflection_type == "global":
            self._prepare_global_reflection_tasks()
        elif reflection_type == "local_image":
            self._preprare_image_local_reflection_tasks()
        elif reflection_type == "local_chart":
            self._prepare_chart_local_reflection_tasks()
        elif reflection_type == "global_image":
            self._preprare_image_global_reflection_tasks()
        elif reflection_type == "global_chart":
            self._prepare_chart_global_reflection_tasks()
        else:
            print(f"Unknown reflection type: {reflection_type}")

    def _prepare_global_reflection_tasks(self):
        """prepare global reflection tasks based on previous evaluation results."""
        new_task_queue = defaultdict(list)
        issue_lists = defaultdict(list)
        for eval_meta in self.task_queue['plain']:
            project_dir = eval_meta.project_dir
            parsed_result = (eval_meta.extra_info or {}).get("parsed_result") or {}
            if parsed_result.get("eval_type") in ("layout", "style"):
                raw_output = parsed_result.get("raw_output", "")
                if raw_output is None:
                    continue
                if isinstance(raw_output, str):
                    raw_text = raw_output
                else:
                    _append_warning_log(
                        project_dir,
                        {
                            "event": "non_string_raw_output",
                            "task_type": getattr(eval_meta, "type", None),
                            "eval_type": parsed_result.get("eval_type", None),
                            "raw_output_type": type(raw_output).__name__,
                        },
                    )
                    try:
                        raw_text = json.dumps(raw_output, ensure_ascii=False)
                    except Exception:
                        raw_text = str(raw_output)
                if raw_text:
                    issue_lists[project_dir].append(raw_text)
            
        for project_dir, issues in issue_lists.items():
            if not issues:
                continue
            issues = get_issues_from_penalties("\n".join(issues))
            html_path = os.path.join(project_dir, self.main_name)
            meta_query = MetaQuery(
                type="global_reflection",
                extra_info={
                    "input_html": html_path,
                    "issues": issues,
                },
                save_path=html_path,    # directly overwrite the main html
                project_dir=project_dir,
            )
            new_task_queue["plain"].append(meta_query)

        self.task_queue = new_task_queue

    def _preprare_image_local_reflection_tasks(self, score_thr: float=0.8):
        """prepare image reflection tasks based on previous evaluation results.
        eval_meta: MetaQuery
         - extra_info: dict_keys(['project_dir', 'fullpage_screenshot', 'embedded_screenshot', 'embedded_info', 'html_excerpt', 'design_prompt_excerpt', 'image_path', 'image_ref', 'parsed_result'])
         - project_dir
         - save_path: `.jsonl` result path
        """
        
        new_task_queue = defaultdict(list)
        for eval_meta in self.task_queue['plain']:
            parsed_result = (eval_meta.extra_info or {}).get("parsed_result") or {}
            eval_type = parsed_result.get("eval_type", "")
            if eval_type != "image":
                continue

            score = self.calculate_score(parsed_result.get("score", 0.0), eval_type)

            project_dir = eval_meta.project_dir
            image_ref = (eval_meta.extra_info or {}).get("image_ref", "")

            # If an image ever reaches a perfect score, freeze it for the rest of the run:
            # do not reflect/modify it in later rounds even if evaluator noise changes scores.
            state = _load_reflection_state(project_dir)
            frozen_images = set((state.get("frozen_images") or []))
            if isinstance(image_ref, str) and image_ref and image_ref in frozen_images:
                continue
            if score >= 0.999:
                if isinstance(image_ref, str) and image_ref:
                    frozen_images.add(image_ref)
                    state["frozen_images"] = sorted(frozen_images)
                    _save_reflection_state(project_dir, state)
                continue
            
            if score >= score_thr:
                continue

            pi = parsed_result.get("parsed_info", {}) or {}
            image_issues = pi.get("image_issues", pi.get("issues", [])) or []
            image_solutions = pi.get("image_solutions", pi.get("solutions", [])) or []
            webpage_issues = pi.get("webpage_issues", []) or []
            webpage_solutions = pi.get("webpage_solutions", []) or []

            image_issues = [s for s in image_issues if isinstance(s, str) and s.strip()]
            image_solutions = [s for s in image_solutions if isinstance(s, str) and s.strip()]
            # Some evaluators only produce issues but no explicit solutions. Convert issues to actionable edits.
            if not image_solutions and image_issues:
                image_solutions = [f"Fix: {issue.strip()}" for issue in image_issues if issue.strip()]
            # If we still have no instruction, skip to avoid calling image edit with empty prompt.
            if not image_solutions:
                continue

            meta_query = MetaQuery(
                type="image_local_reflection",
                extra_info={
                    "image_ref": image_ref,
                    "image_path": eval_meta.extra_info["image_path"],
                    "image_issues": image_issues,
                    "image_solutions": image_solutions,
                    "webpage_solutions": webpage_solutions,
                },
                save_path=eval_meta.extra_info["image_path"],    # directly overwrite the image
                project_dir=project_dir,
            )
            new_task_queue["imedit"].append(meta_query)

        self.task_queue = new_task_queue

    def _preprare_image_global_reflection_tasks(self):
        """prepare image reflection tasks based on previous evaluation results."""
        project_solutions = defaultdict(list)

        # find solutions by project
        for eval_meta in self.task_queue['imedit']:
            if eval_meta.type == "image_local_reflection":
                project_dir = eval_meta.project_dir
                webpage_solutions = eval_meta.extra_info.get("webpage_solutions", [])
                image_ref = eval_meta.extra_info.get("image_ref", "")
                project_solutions[project_dir].append( (image_ref, webpage_solutions) )

        # prepare for global reflection tasks
        new_task_queue = defaultdict(list)
        for project_dir, solution_metas in project_solutions.items():
            meta_query = MetaQuery(
                type="image_global_reflection",
                extra_info={
                    "webpage_solutions": solution_metas,
                },
                save_path=os.path.join(project_dir, self.main_name),    # directly overwrite the main html
                project_dir=project_dir,
            )
            new_task_queue["imwebfix"].append(meta_query)

        self.task_queue = new_task_queue


    def _prepare_chart_local_reflection_tasks(self, score_thr: float=0.8):
        """prepare chart reflection tasks based on previous evaluation results.
        eval_meta: MetaQuery
         - extra_info: dict_keys(['project_dir', 'fullpage_screenshot', 'embedded_screenshot', 'html_excerpt', 'design_prompt', 'echart_html', 'iframe_height', 'chart_ref', 'parsed_result'])
         - project_dir
         - save_path: `.jsonl` result path
        """
        new_task_queue = defaultdict(list)
        for eval_meta in self.task_queue['plain']:
            parsed_result = (eval_meta.extra_info or {}).get("parsed_result") or {}
            eval_type = parsed_result.get("eval_type", "")
            if eval_type != "chart":
                continue

            score = self.calculate_score(parsed_result.get("score", 0.0), eval_type)
            
            if score >= score_thr:
                continue

            project_dir = eval_meta.project_dir
            chart_ref = eval_meta.extra_info.get("chart_ref", "")
            # Local chart reflection only applies to iframe/subpage charts that are actual files.
            if not isinstance(chart_ref, str) or not chart_ref.lower().endswith(".html"):
                continue
            if not os.path.exists(os.path.join(project_dir, chart_ref)):
                continue
            meta_query = MetaQuery(
                type="chart_local_reflection",
                extra_info=eval_meta.extra_info,
                save_path=os.path.join(project_dir, chart_ref),    # directly overwrite the echart html
                project_dir=project_dir,
            )
            new_task_queue["plain"].append(meta_query)
        
        self.task_queue = new_task_queue


    def _prepare_chart_global_reflection_tasks(self):
        """prepare chart reflection tasks based on previous evaluation results."""

        # assign tasks by project
        # as the global reflection needs to handle charts one by one
        project_queries = defaultdict(list)
        for eval_meta in self.task_queue['plain']:
            parsed = (eval_meta.extra_info or {}).get("parsed_result") or {}
            if parsed.get("eval_type") != "chart":
                continue
            project_dir = eval_meta.project_dir
            pi = parsed.get("parsed_info", {}) or {}
            if pi.get("webpage_solutions", []):
                project_queries[project_dir].append(eval_meta)

        # prepare for global reflection tasks
        new_task_queue = defaultdict(list)
        for project_dir, meta_queries in project_queries.items():
            meta_query = MetaQuery(
                type="chart_global_reflection",
                extra_info={
                    "chart_queries": meta_queries,
                },
                save_path=os.path.join(project_dir, self.main_name),    # directly overwrite the main html
                project_dir=project_dir,
            )
            new_task_queue["plain"].append(meta_query)

        self.task_queue = new_task_queue


    def _do_global_reflection_task(self, meta_query: MetaQuery):
        """do global reflection task."""
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        system_prompt = REFLECTION_PROMPTS["global_system"]
        user_prompt_template = REFLECTION_PROMPTS["global_user"]

        project_dir = meta_query.project_dir
        html_path = meta_query.extra_info["input_html"]
        issues = meta_query.extra_info["issues"]
        save_path = meta_query.save_path

        generated_html = read_html_file(html_path)
        design_prompt = get_user_prompt(project_dir)
        
        user_prompt = user_prompt_template.format(
            design_prompt=design_prompt,
            generated_html=generated_html,
            issues_list=issues
        )

        generated_html, success = request_chatgpt_t2t_until_success(
            user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(project_dir, "error.log"),
        )
        if not success or not isinstance(generated_html, str):
            meta_query.extra_info["request_error"] = generated_html
            return False
        generated_html = generated_html.replace("```html", "").replace("```", "").strip()
        
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(generated_html)

        return success

    def _do_image_local_reflection_task(self, meta_query: MetaQuery):
        """do image local reflection task."""
        url = self.api_keys["imedit"]["url"]
        api_key = self.api_keys["imedit"]["api_key"]
        model = self.api_keys["imedit"]["model"]
        reasoning_effort = self.api_keys["imedit"].get("reasoning_effort", None)

        image_path = meta_query.extra_info["image_path"]
        image_solutions = meta_query.extra_info.get("image_solutions", []) or []
        image_issues = meta_query.extra_info.get("image_issues", []) or []
        image_solutions = [s for s in image_solutions if isinstance(s, str) and s.strip()]
        image_issues = [s for s in image_issues if isinstance(s, str) and s.strip()]
        user_prompt = "\n".join(image_solutions).strip()
        if not user_prompt and image_issues:
            user_prompt = "Fix the following issues in the image (keep other content unchanged):\n" + "\n".join(
                [f"- {s.strip()}" for s in image_issues]
            )
            _append_warning_log(
                meta_query.project_dir,
                {
                    "event": "fallback_prompt_from_issues",
                    "situation": "image_solutions_empty_but_image_issues_present",
                    "action": "use_issues_to_build_prompt",
                    "task_type": meta_query.type,
                    "image_ref": meta_query.extra_info.get("image_ref", ""),
                    "image_path": image_path,
                    "save_path": meta_query.save_path,
                    "request": {
                        "url": url,
                        "prompt": user_prompt,
                        "timeout": TIMEOUT,
                        "max_retries": MAX_RETRIES,
                        "sleep_time": SLEEP_TIME,
                        "mask_path": None,
                    },
                },
            )
        if not user_prompt:
            meta_query.extra_info["request_error"] = {
                "error_type": "empty_prompt_skipped",
                "message": "Skipping image edit because no image_solutions/image_issues were provided.",
            }
            _append_warning_log(
                meta_query.project_dir,
                {
                    "event": "skip_empty_prompt",
                    "situation": "image_solutions_empty_and_image_issues_empty_after_filtering",
                    "action": "skip_image_edit",
                    "task_type": meta_query.type,
                    "image_ref": meta_query.extra_info.get("image_ref", ""),
                    "image_path": image_path,
                    "save_path": meta_query.save_path,
                    "request": {
                        "url": url,
                        "prompt": "",
                        "timeout": TIMEOUT,
                        "max_retries": MAX_RETRIES,
                        "sleep_time": SLEEP_TIME,
                        "mask_path": None,
                    },
                },
            )
            return True

        output, success = request_chatgpt_i2i_until_success(
            image_path, user_prompt, None,
            save_path=meta_query.save_path,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success:
            meta_query.extra_info["request_error"] = output

        return success

    def _do_image_global_reflection_task(self, meta_query: MetaQuery):
        """do image global reflection task."""
        
        html_path = meta_query.save_path
        solutions_metas = meta_query.extra_info["webpage_solutions"]  # a list of (image_ref, solutions)
        
        # fix problems one by one
        # as it will modify the html file each time
        for image_ref, solution in solutions_metas:
            image_id = make_safe_id(image_ref)
            apply_webpage_css_fixes(
                webpage_html_path=html_path,
                image_ref=image_ref,
                image_id=image_id,
                webpage_solutions=solution,
            )

        return True

    def _do_chart_local_reflection_task(self, meta_query: MetaQuery):
        """do chart local reflection task."""
        
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        system_prompt = REFLECTION_PROMPTS["local_chart_system"]
        user_prompt_template = REFLECTION_PROMPTS["local_chart_user"]
        save_path = meta_query.save_path

        pi = meta_query.extra_info["parsed_result"].get("parsed_info", {})
        chart_solutions = pi.get("chart_solutions", pi.get("solutions", [])) or []
        chart_solutions = [s for s in chart_solutions if isinstance(s, str) and s.strip()]
        iframe_height = meta_query.extra_info.get("iframe_height", 180)
        source = meta_query.extra_info["embedded_screenshot"]

        user_prompt = user_prompt_template.format(
                background=pi.get("description", ""),
                design_prompt=pi.get("user_prompt", ""),
                generated_html=meta_query.extra_info["echart_html"],
                suggestions="\n".join(chart_solutions),
                iframe_height=iframe_height,
            )

        generated_html, success = request_chatgpt_i2t_until_success(
            source, user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug, reasoning_effort=reasoning_effort,
            model=model,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success or not isinstance(generated_html, str):
            meta_query.extra_info["request_error"] = generated_html
            return False
        generated_html = generated_html.replace("```html", "").replace("```", "").strip()
        
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(generated_html)

        return success

    def _do_chart_global_reflection_task(self, meta_query: MetaQuery):
        """do chart global reflection task."""
        
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        system_prompt = REFLECTION_PROMPTS["global_chart_system"]
        user_prompt_template = REFLECTION_PROMPTS["global_chart_user"]

        chart_queries = meta_query.extra_info["chart_queries"]
        save_path = meta_query.save_path

        # Fix charts one by one for the project.
        # This modifies the main HTML each time; keep it sequential.
        for chart_query in chart_queries:
            chart_ref = chart_query.extra_info.get("chart_ref", "")
            iframe_height = chart_query.extra_info.get("iframe_height", 180)
            source = chart_query.extra_info.get("embedded_screenshot", None)
            pi = (chart_query.extra_info.get("parsed_result") or {}).get("parsed_info", {}) or {}

            webpage_issues = pi.get("webpage_issues", []) or []
            webpage_solutions = pi.get("webpage_solutions", []) or []
            if not webpage_solutions and not webpage_issues:
                continue

            # Prefer direct CSS patching when solutions are ready-to-paste CSS rules.
            patched = False
            if webpage_solutions:
                chart_id = make_safe_id(f"chart:{chart_ref}")
                patched = apply_webpage_css_fixes(
                    webpage_html_path=save_path,
                    image_ref=chart_ref,
                    image_id=chart_id,
                    webpage_solutions=webpage_solutions,
                )
            if patched:
                continue

            # Fallback: ask the model to rewrite the full HTML based on issues/solutions + chart screenshot.
            current_webpage_html = read_html_file(save_path)
            user_prompt = user_prompt_template.format(
                chart_path=chart_ref,
                iframe_height=iframe_height,
                webpage_issues="\n".join([f"- {issue}" for issue in webpage_issues]) if webpage_issues else "(none)",
                webpage_solutions="\n".join([f"- {sol}" for sol in webpage_solutions]) if webpage_solutions else "(none)",
                webpage_html=current_webpage_html,
            )

            generated_html, success = request_chatgpt_i2t_until_success(
                source, user_prompt, system_prompt,
                url=url, api_key=api_key, debug=self.debug, reasoning_effort=reasoning_effort,
                model=model,
                timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
                log_path=os.path.join(meta_query.project_dir, "error.log"),
            )
            if not success or not isinstance(generated_html, str):
                self.failed_tasks.append(
                    {
                        "type": "chart_global_reflection_item",
                        "project_dir": meta_query.project_dir,
                        "save_path": save_path,
                        "chart_ref": chart_ref,
                        "error": generated_html,
                    }
                )
                continue
            generated_html = generated_html.replace("```html", "").replace("```", "").strip()
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(generated_html)

        return True
