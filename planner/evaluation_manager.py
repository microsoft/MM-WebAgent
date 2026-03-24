import sys
sys.path.append('.')

import os
import json
import shutil
import asyncio
import traceback
import re
from urllib.parse import urlparse
import concurrent.futures
from collections import defaultdict

from bs4 import BeautifulSoup

from utils import (
    parse_score,
    request_chatgpt_t2i_until_success,
    request_chatgpt_t2t_until_success,
    request_chatgpt_i2t_until_success,
    get_openai_request_config
)
from utils.mm_utils import (
    get_main_html,
    get_user_prompt,
    parse_html_file,
    read_html_file,
    sample_frames_from_video,
    download_media,
    extract_html_excerpt,
    extract_webpage_css_excerpt,
    extract_design_prompt_excerpt,
    get_iframe_height_from_html_sync,
    load_standalone_image_as_png_bytes,
    extract_inline_chart_targets_from_html,
)
from prompt.evaluation_prompts import EVAL_PARSER_V4 as EVAL_PARSER
from prompt.evaluation_prompts import EVAL_PROMPTS_V4 as EVAL_PROMPTS
from agent.screenshot_webpage import screenshot_webpage_and_embedded_images
from planner.config import *


def should_include_standalone_image_for_eval(image_path: str) -> bool:
    SUPPORTED_EDIT_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

    ext = os.path.splitext(image_path)[1].lower()
    return ext in SUPPORTED_EDIT_EXTS



class EvaluationManager:
    def __init__(
        self,
        project_dirs: list,
        api_config: dict=API_CONFIG,
        main_name: str="main.html",
        debug: bool=False,
        chart_mode: str="subpage",
        is_chart_subpage: bool | None = None,
    ):
        self.project_dirs = project_dirs
        self.api_keys = {}
        self.main_name = main_name
        self.debug = debug
        # Backward compatibility: older callers may pass is_chart_subpage.
        if is_chart_subpage is not None:
            chart_mode = "subpage" if bool(is_chart_subpage) else "inline"
        self.chart_mode = chart_mode

        self.task_queue = defaultdict(list)
        self.failed_tasks = []

        self.eval_types = {
            "global": ["layout", "style", "aes"],
            "meta": ["image", "video", "chart"]
        }

        self.config_apis(api_config)
        self.set_eval_status()

    def set_eval_status(self, global_eval=True, image_eval=True, video_eval=True, chart_eval=True):
        """set evaluation status for different types."""
        self.do_global_eval = global_eval
        self.do_image_eval = image_eval
        self.do_video_eval = video_eval
        self.do_chart_eval = chart_eval


    def _extend_task_queue(self, new_task_queue):
        """extend existing task queue with new tasks."""
        for qtype, tasks in new_task_queue.items():
            self.task_queue[qtype].extend(tasks)

    def _append_task_to_queue(self, meta_query, qtype: str="plain"):
        """append one task to existing task queue."""
        self.task_queue[qtype].append(meta_query)
    

    def run_all_tasks(self, do_clean: bool=True):
        """
        Run all tasks in both queues ('plain' and 'imgen') concurrently.
        Each queue uses its own max_workers for parallel processing.
        """
        if len(self) == 0:
            return

        def _worker(meta_query):
            """Wrapper for executing a single task (never raises)."""
            try:
                ok = bool(self._process_one_task(meta_query))
                if not ok:
                    extra = getattr(meta_query, "extra_info", None) or {}
                    self.failed_tasks.append(
                        {
                            "type": getattr(meta_query, "type", None),
                            "project_dir": getattr(meta_query, "project_dir", None),
                            "save_path": getattr(meta_query, "save_path", None),
                            "error": extra.get("request_error", None),
                        }
                    )
                return ok
            except Exception as e:
                # Never abort the whole run due to one failed task.
                self.failed_tasks.append(
                    {
                        "type": getattr(meta_query, "type", None),
                        "project_dir": getattr(meta_query, "project_dir", None),
                        "save_path": getattr(meta_query, "save_path", None),
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
                if self.debug:
                    traceback.print_exc()
                return False

        executors = {}
        futures_map = {}

        for qtype, tasks in self.task_queue.items():
            if len(tasks) == 0:
                continue

            max_workers = self.api_keys[qtype]["max_workers"]

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            executors[qtype] = executor

            futures = [executor.submit(_worker, meta_query) for meta_query in tasks]
            futures_map[qtype] = futures

        for qtype, futures in futures_map.items():
            for fut in concurrent.futures.as_completed(futures):
                try:
                    _ = fut.result()
                except Exception as e:
                    self.failed_tasks.append({"type": "unknown", "error": f"{type(e).__name__}: {e}"})
                    if self.debug:
                        traceback.print_exc()

        for ex in executors.values():
            ex.shutdown(wait=True)

        if do_clean:
            self._clean_task_queue()

    def parse_eval_results(self, save_name="eval_result.json", eval_fname="eval_result.jsonl"):
        """parse all evaluation results from jsonl files."""

        def parse_scores(eval_data, mm_split_data=None):
            """parse scores from eval data lines."""
            score_lists = defaultdict(list)
            for line in eval_data:
                try:
                    data = json.loads(line.strip())
                    eval_type = data["eval_type"]
                    if data.get("success", False):
                        score_lists[eval_type].append(self.calculate_score(data["score"], eval_type))
                    elif eval_type in self.eval_types["global"]:
                        score_lists[eval_type].append(0.0)
                except:
                    continue
            
            final_result = {}
            for eval_type, scores in score_lists.items():
                if len(scores) == 0:
                    continue
                avg_score = sum(scores) / len(scores)
                final_result[eval_type] = round(avg_score, 4)

            return dict(final_result=final_result, score_lists=score_lists)            

        for i in range(len(self.project_dirs)):
            project_dir = self.project_dirs[i]
            eval_result_path = os.path.join(project_dir, eval_fname)
            mm_split_path = os.path.join(project_dir, "mm_split_result.json")
            final_result_path = os.path.join(project_dir, save_name)
            final_eval_result_path = final_result_path + "l"

            if not os.path.exists(eval_result_path):
                continue
            with open(eval_result_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            mm_split_data = None
            if os.path.exists(mm_split_path):
                with open(mm_split_path, "r", encoding="utf-8") as f:
                    mm_split_data = json.load(f)

            score_lists = parse_scores(lines, mm_split_data=mm_split_data)

            with open(final_result_path, "w", encoding="utf-8") as f:
                json.dump(score_lists, f, ensure_ascii=False, indent=2)
            
            shutil.copy(eval_result_path, final_eval_result_path)

            print(f"Saved final evaluation results to {final_result_path}")

    def calculate_score(self, raw_score, eval_type):
        """calculate final score based on eval type."""
        def clip_score(s, min_v=0.0, max_v=1.0):
            return max(min(s, max_v), min_v)
        if eval_type == "layout":
            return clip_score(1 - raw_score * 0.5, 0, 1)
        elif eval_type == "style":
            return clip_score(1 - raw_score * 0.5, 0, 1)
        else:
            return clip_score(raw_score, 0, 1)

    def _get_screenshot_max_workers(self, num_projects: int) -> int:
        raw = os.getenv("WEBAGENT_SCREENSHOT_MAX_WORKERS", "").strip()
        if raw:
            try:
                return max(1, min(num_projects, int(raw)))
            except Exception:
                pass

        cpu_count = os.cpu_count() or 4
        return max(1, min(num_projects, cpu_count, 4))

    def prepare_initial_evaluation_tasks(self, do_mm_split: bool=True):
        """parse all planning results to generate evaluation tasks."""

        def _local_chart_html_path(project_dir: str, chart_ref: str):
            """
            Resolve an iframe/object/embed reference to a local HTML file under project_dir.
            - Skips remote URLs like https://... (no local file to read).
            - Strips query/fragment (?/#) via urlparse().path.
            - Prevents path traversal outside project_dir.
            """
            if not isinstance(chart_ref, str):
                return None
            chart_ref = chart_ref.strip()
            if not chart_ref:
                return None

            parsed = urlparse(chart_ref)
            if parsed.scheme or parsed.netloc:
                return None

            rel = (parsed.path or "").lstrip("/")
            if not rel:
                return None
            if not rel.lower().endswith((".html", ".htm")):
                return None

            project_norm = os.path.normpath(project_dir)
            resolved = os.path.normpath(os.path.join(project_norm, rel))
            try:
                if os.path.commonpath([resolved, project_norm]) != project_norm:
                    return None
            except Exception:
                return None
            return resolved

        def _build_project_context(project_dir: str) -> dict:
            eval_result_path = os.path.join(project_dir, "eval_result.jsonl")
            if os.path.exists(eval_result_path):
                os.remove(eval_result_path)

            html_path = os.path.join(project_dir, self.main_name)
            html_text, subfiles = parse_html_file(html_path)
            design_prompt = get_user_prompt(project_dir)

            do_subpage_charts = self.chart_mode in ("subpage", "auto")
            do_inline_charts = self.chart_mode in ("inline", "auto")

            inline_chart_targets = []
            if self.do_chart_eval and do_inline_charts:
                inline_chart_targets = extract_inline_chart_targets_from_html(html_text)
            return {
                "project_dir": project_dir,
                "html_text": html_text,
                "subfiles": subfiles,
                "design_prompt": design_prompt,
                "inline_chart_targets": inline_chart_targets,
                "do_subpage_charts": do_subpage_charts,
                "do_inline_charts": do_inline_charts,
            }

        def _take_project_screenshot(project_ctx: dict) -> tuple[str, object]:
            project_dir = project_ctx["project_dir"]
            return project_dir, self._take_screenshot_task(
                project_dir,
                inline_chart_targets=project_ctx["inline_chart_targets"],
            )

        project_contexts = [ _build_project_context(project_dir) for project_dir in self.project_dirs ]
        screenshot_results: dict[str, tuple[bytes, dict, dict] | None] = {}

        if project_contexts:
            screenshot_workers = self._get_screenshot_max_workers(len(project_contexts))
            with concurrent.futures.ThreadPoolExecutor(max_workers=screenshot_workers) as executor:
                future_map = {
                    executor.submit(_take_project_screenshot, project_ctx): project_ctx["project_dir"]
                    for project_ctx in project_contexts
                }
                for fut in concurrent.futures.as_completed(future_map):
                    project_dir = future_map[fut]
                    try:
                        _, result = fut.result()
                        screenshot_results[project_dir] = result
                    except Exception as e:
                        screenshot_results[project_dir] = None
                        if self.debug:
                            print(f"[screenshot] failed project_dir={project_dir} error={type(e).__name__}: {e}")
                            traceback.print_exc()

        for project_ctx in project_contexts:
            project_dir = project_ctx["project_dir"]
            screenshot_result = screenshot_results.get(project_dir)
            if screenshot_result is None:
                continue

            fullpage_bytes, embedded_map, embedded_info_map = screenshot_result
            html_text = project_ctx["html_text"]
            subfiles = project_ctx["subfiles"]
            design_prompt = project_ctx["design_prompt"]
            inline_chart_targets = project_ctx["inline_chart_targets"]
            do_subpage_charts = project_ctx["do_subpage_charts"]
            do_inline_charts = project_ctx["do_inline_charts"]

            results_path = os.path.join(project_dir, "eval_result.jsonl")
            mm_split_path = os.path.join(project_dir, "mm_split_result.json")

            if do_mm_split and not os.path.exists(mm_split_path):
                meta_query = MetaQuery(
                    type="mm_split",
                    extra_info={
                        "design_prompt": design_prompt,
                    },
                    save_path=mm_split_path,
                    project_dir=project_dir,
                )
                self.task_queue["plain"].append(meta_query)
                self.task_queue["plain"].append(meta_query) # to ensure mm_split is done before other evals

            if self.do_global_eval:
                for eval_type in self.eval_types["global"]:
                    meta_query = MetaQuery(
                        type="global_evaluation",
                        extra_info={
                            "eval_type": eval_type,
                            "user_prompt": design_prompt,
                            "input_image": fullpage_bytes,
                            "input_html": os.path.join(project_dir, self.main_name),
                        },
                        save_path=results_path,
                        project_dir=project_dir,
                    )

                    self.task_queue["plain"].append(meta_query)

            if self.do_image_eval:
                img_refs = subfiles["image"]
                for img_ref in img_refs:
                    if img_ref not in embedded_map:
                        continue
                    if embedded_map.get(img_ref) is None:
                        # Screenshot capture failed (element not found / not visible).
                        continue

                    html_excerpt = extract_html_excerpt(html_text, img_ref, max_length=4500, context_chars=800)
                    design_prompt_excerpt = extract_design_prompt_excerpt(design_prompt, img_ref, max_length=4500, context_chars=800)

                    meta_query = MetaQuery(
                        type="image_evaluation",
                        extra_info={
                            "project_dir": project_dir,
                            "fullpage_screenshot": fullpage_bytes,
                            "embedded_screenshot": embedded_map[img_ref],
                            "embedded_info": embedded_info_map.get(img_ref, None),
                            "html_excerpt": html_excerpt,
                            "design_prompt_excerpt": design_prompt_excerpt,
                            "image_path": os.path.join(project_dir, img_ref),
                            "image_ref": img_ref,
                        },
                        save_path=results_path,
                        project_dir=project_dir,
                    )

                    self.task_queue["plain"].append(meta_query)

            if self.do_chart_eval:
                if do_subpage_charts:
                    chart_refs = subfiles["chart"]
                    for chart_ref in chart_refs:
                        if chart_ref not in embedded_map:
                            continue
                        if embedded_map.get(chart_ref) is None:
                            continue

                        html_excerpt = extract_webpage_css_excerpt(html_text)
                        chart_path = _local_chart_html_path(project_dir, chart_ref)
                        if not chart_path or not os.path.exists(chart_path):
                            if self.debug:
                                print(f"[chart] skip non-local/missing subpage ref={chart_ref}")
                            continue
                        echart_html = read_html_file(chart_path)

                        iframe_height = get_iframe_height_from_html_sync(chart_path)
                        meta_query = MetaQuery(
                            type="chart_evaluation",
                            extra_info={
                                "project_dir": project_dir,
                                "chart_mode": "subpage",
                                "fullpage_screenshot": fullpage_bytes,
                                "embedded_screenshot": embedded_map[chart_ref],
                                "html_excerpt": html_excerpt,
                                "design_prompt": design_prompt,
                                "echart_html": echart_html,
                                "iframe_height": iframe_height,
                                "chart_ref": chart_ref,
                            },
                            save_path=results_path,
                            project_dir=project_dir,
                        )
                        self.task_queue["plain"].append(meta_query)
                if do_inline_charts:
                    # Inline charts (<canvas>/<svg>/echarts-init containers) are evaluated via element screenshot.
                    for t in inline_chart_targets:
                        chart_ref = t["key"]
                        if chart_ref not in embedded_map:
                            continue
                        if embedded_map.get(chart_ref) is None:
                            continue

                        hint = t.get("hint", "") or chart_ref
                        html_excerpt = extract_html_excerpt(html_text, hint, max_length=4500, context_chars=900)
                        embed_info = embedded_info_map.get(chart_ref, None)
                        iframe_height = None
                        try:
                            iframe_height = int(((embed_info or {}).get("rect") or {}).get("height") or 0) or None
                        except Exception:
                            iframe_height = None

                        meta_query = MetaQuery(
                            type="chart_evaluation",
                            extra_info={
                                "project_dir": project_dir,
                                "chart_mode": "inline",
                                "fullpage_screenshot": fullpage_bytes,
                                "embedded_screenshot": embedded_map[chart_ref],
                                "embed_info": embed_info,
                                "html_excerpt": html_excerpt,
                                "design_prompt": design_prompt,
                                "echart_html": "",
                                "iframe_height": iframe_height or 0,
                                "chart_ref": chart_ref,
                            },
                            save_path=results_path,
                            project_dir=project_dir,
                        )
                        self.task_queue["plain"].append(meta_query)

            if self.do_video_eval:
                video_refs = subfiles["video"]
                # Be defensive: some generators mistakenly put non-video assets in <video src=...>
                # (e.g., .png). Decord will crash on these.
                SUPPORTED_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".gif"}
                for video_ref in video_refs:
                    raw_video_ref = video_ref
                    if not isinstance(video_ref, str) or not video_ref.strip():
                        continue
                    if not video_ref.startswith("http"):
                        ext = os.path.splitext(video_ref)[1].lower()
                        if ext and ext not in SUPPORTED_VIDEO_EXTS:
                            if self.debug:
                                print(f"[video] skip non-video ref={video_ref}")
                            continue
                    if video_ref.startswith("http"):
                        content, _ = download_media(video_ref, debug=self.debug)
                    else:
                        content = os.path.join(project_dir, video_ref)
                    try:
                        content_frames = sample_frames_from_video(content, K=3, max_side=384)
                    except Exception as e:
                        if self.debug:
                            print(f"[video] failed to sample frames ref={video_ref} error={type(e).__name__}: {e}")
                            traceback.print_exc()
                        continue
                    if not content_frames:
                        continue

                    html_excerpt = extract_html_excerpt(html_text, video_ref, max_length=4500, context_chars=800)
                    design_prompt_excerpt = extract_design_prompt_excerpt(design_prompt, video_ref, max_length=4500, context_chars=800)

                    meta_query = MetaQuery(
                        type="video_evaluation",
                        extra_info={
                            "project_dir": project_dir,
                            "content_frames": content_frames,
                            "video_path": video_ref,
                            "video_ref": raw_video_ref,
                            "html_excerpt": html_excerpt,
                            "design_prompt_excerpt": design_prompt_excerpt,
                        },
                        save_path=results_path,
                        format_err=(content_frames is None),
                        project_dir=project_dir,
                    )
                    self.task_queue["plain"].append(meta_query)


    def prepare_final_evaluation_tasks(self):
        """prepare evaluation tasks."""
        self._clean_task_queue()
        def _dedupe_str_list(items):
            out = []
            seen = set()
            for item in items or []:
                if not isinstance(item, str):
                    continue
                s = item.strip()
                if not s:
                    continue
                # Remove common surrounding quotes to reduce spurious mismatch.
                s = s.strip().strip('"').strip("“”").strip()
                if not s or s in seen:
                    continue
                seen.add(s)
                out.append(s)
            return out
            
        def parse_existing_eval_results(file_path):
            meta_results = defaultdict(list)
            if file_path is None or not os.path.exists(file_path):
                return meta_results
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    data = json.loads(line)
                    user_prompt = data.get("parsed_info", {}).get("user_prompt", "") \
                            + data.get("parsed_info", {}).get("description", "")
                    eval_type = data["eval_type"]
                    if data.get("success", False) and user_prompt and eval_type in self.eval_types["meta"]:
                        meta_results[eval_type].append(user_prompt)

            return meta_results

        def extract_existing_elements_from_html(project_dir: str, out: defaultdict(list) =None):
            """
            Extract lightweight evidence of existing multimodal elements from HTML to
            reduce false positives in missing-check prompts.
            """
            out = defaultdict(list) if out is None else out
            html_path = os.path.join(project_dir, self.main_name)
            if not os.path.exists(html_path):
                return out
            try:
                html_text = read_html_file(html_path) or ""
            except Exception:
                return out

            try:
                soup = BeautifulSoup(html_text, "html.parser")
            except Exception:
                return out

            # <img alt="..."> is often closer to the prompt than URLs.
            for img in soup.find_all("img"):
                alt = img.get("alt", None)
                if not isinstance(alt, str):
                    continue
                alt = alt.strip()
                if alt:
                    out["image"].append(f'HTML <img alt>: {alt}')

            # aria-labels commonly annotate inline SVG icons and UI elements.
            for el in soup.find_all(attrs={"aria-label": True}):
                label = el.get("aria-label", None)
                if label is None:
                    continue
                label = str(label).strip()
                if not label:
                    continue
                low = label.lower()
                if "video" in low:
                    out["video"].append(f"HTML aria-label: {label}")
                elif any(k in low for k in ("chart", "graph", "plot", "visualization")):
                    out["chart"].append(f"HTML aria-label: {label}")
                elif any(k in low for k in ("icon", "logo", "avatar", "image", "photo", "illustration", "thumbnail")):
                    out["image"].append(f"HTML aria-label: {label}")

            # Inline SVG titles can help identify chart/icon intent.
            for title in soup.select("svg title"):
                t = title.get_text(strip=True)
                if t:
                    out["image"].append(f"HTML svg title: {t}")

            # Dedupe + clip to keep prompt size bounded.
            MAX_ITEMS_PER_TYPE = 100
            MAX_CHARS_PER_ITEM = 420
            for k in list(out.keys()):
                items = _dedupe_str_list(out[k])
                clipped = []
                for s in items:
                    if len(s) > MAX_CHARS_PER_ITEM:
                        s = s[: MAX_CHARS_PER_ITEM - 3] + "..."
                    clipped.append(s)
                out[k] = clipped[:MAX_ITEMS_PER_TYPE]
            return out

        for i in range(len(self.project_dirs)):
            project_dir = self.project_dirs[i]
            design_prompt = get_user_prompt(project_dir)
            eval_result_path = os.path.join(project_dir, "eval_result.jsonl")
            existing_eval_prompts = parse_existing_eval_results(eval_result_path)
            existing_eval_prompts = extract_existing_elements_from_html(project_dir, out=existing_eval_prompts)

            mm_split_path = os.path.join(project_dir, "mm_split_result.json")

            eval_meta = MetaQuery(
                type="missing_evaluation",
                extra_info={
                    "eval_type": "missing",
                    "user_prompt": design_prompt,
                    "existing_elements": json.dumps(existing_eval_prompts, ensure_ascii=False, indent=2),
                    "mm_split_path": mm_split_path
                },
                save_path=eval_result_path,
                project_dir=project_dir,
            )
            self.task_queue["plain"].append(eval_meta)

    def _do_mm_split_task(self, meta_query: MetaQuery):
        """execute multimodal split task."""
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        system_prompt = EVAL_PROMPTS["mm_split_system"]
        user_prompt_template = EVAL_PROMPTS["mm_split_user"]

        design_prompt = meta_query.extra_info["design_prompt"]
        saved_path = meta_query.save_path

        user_prompt = user_prompt_template.format(
            design_prompt=design_prompt,
        )

        output, success = request_chatgpt_t2t_until_success(
            user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )

        if not success:
            meta_query.extra_info["request_error"] = output
            return False

        try:
            parsed_result, _ = parse_score(output, EVAL_PARSER["mm_split"], debug=self.debug)
            if saved_path is not None:
                with open(saved_path, "w", encoding="utf-8") as f:
                    json.dump(parsed_result, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            if self.debug:
                traceback.print_exc()
            return False

    def _evaluate_missing_evaluation_task(self, meta_query: MetaQuery):
        """execute missing evaluation task (find missing items)."""
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)
        
        meta_eval_type = meta_query.extra_info["eval_type"]

        system_prompt = EVAL_PROMPTS[f"check_{meta_eval_type}_system"]
        user_prompt_template = EVAL_PROMPTS[f"check_{meta_eval_type}_user"]
        parser_mode = EVAL_PARSER[f"check_{meta_eval_type}"]

        design_prompt = meta_query.extra_info["user_prompt"]
        existing_elements = meta_query.extra_info["existing_elements"]
        mm_split_path = meta_query.extra_info["mm_split_path"]
        save_path = meta_query.save_path
        project_dir = meta_query.project_dir
        success = False

        if not os.path.exists(mm_split_path):
            for i in range(MAX_RETRIES):
                print(f"[missing_eval] mm_split not found, re-running mm_split for project_dir={project_dir} attempt={i+1}")
                mm_split_path = os.path.join(project_dir, "mm_split_result.json")
                meta_query = MetaQuery(
                        type="mm_split",
                        extra_info={
                            "design_prompt": design_prompt,
                        },
                        save_path=mm_split_path,
                        project_dir=project_dir,
                )
                success = self._do_mm_split_task(meta_query)
                if success:
                    break
        if os.path.exists(mm_split_path):
            with open(mm_split_path, "r", encoding="utf-8") as f:
                extracted_elements = f.read()
        else:
            extracted_elements = ""

        user_prompt = user_prompt_template.format(
            design_prompt=design_prompt,
            existing_elements=existing_elements,
            extracted_elements=extracted_elements,
        )

        output, success = request_chatgpt_t2t_until_success(
            user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success:
            meta_query.extra_info["request_error"] = output

        try:
            parsed_result, _ = parse_score(output, parser_mode, debug=self.debug)
            missing_items = parsed_result.get("parsed_info", {})
            missing_metas = []

            for meta_eval_type, meta_dict in missing_items.items():
                if not meta_dict:
                    continue
                if meta_eval_type == "image" and not self.do_image_eval:
                    continue
                if meta_eval_type == "video" and not self.do_video_eval:
                    continue
                if meta_eval_type == "chart" and not self.do_chart_eval:
                    continue
                missing_metas.extend([
                    {
                        "score": 0,
                        "success": True,
                        "eval_type": meta_eval_type,
                        "parsed_info": {"user_prompt": f"{k}: {v}"},
                    }
                    for k, v in meta_dict.items()
                ])
            if save_path is not None:
                with open(save_path, "a", encoding="utf-8") as f:
                    for meta in missing_metas:
                        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except:
            if self.debug:
                traceback.print_exc()
        
        return success
        

    def _evaluate_image_task(self, meta_query: MetaQuery):
        """execute image evaluation task."""
        
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)
        
        system_prompt = EVAL_PROMPTS["sub_image_system"]
        user_prompt_template = EVAL_PROMPTS["sub_image_user"]
        parser_mode = EVAL_PARSER["image"]

        design_prompt_excerpt = meta_query.extra_info["design_prompt_excerpt"]
        html_excerpt = meta_query.extra_info["html_excerpt"]
        fullpage_screenshot = meta_query.extra_info["fullpage_screenshot"]
        embedded_screenshot = meta_query.extra_info["embedded_screenshot"]
        image_ref = meta_query.extra_info["image_ref"]
        image_path = meta_query.extra_info["image_path"]
        embedded_info = meta_query.extra_info["embedded_info"]
        save_path = meta_query.save_path
        success = False

        embed_info_str = json.dumps(embedded_info, ensure_ascii=False, indent=2)[:1800] if embedded_info else "(none)"
        
        user_prompt=user_prompt_template.format(
            image_path=image_ref,
            embed_info=embed_info_str,
            html_excerpt=html_excerpt,
            design_prompt=design_prompt_excerpt,
        )
        sources = [fullpage_screenshot, embedded_screenshot]
        # Be defensive: screenshot capture may fail for some refs; skip instead of crashing.
        sources = [s for s in sources if s is not None]
        if len(sources) < 2:
            meta_query.extra_info["parsed_result"] = {
                "eval_type": "image",
                "success": False,
                "score": 0.0,
                "raw_output": "missing screenshot(s) for image evaluation",
                "parsed_info": {},
            }
            return False

        if should_include_standalone_image_for_eval(image_path):
            standalone_image = load_standalone_image_as_png_bytes(image_path, max_side=384)
            if standalone_image is not None:
                sources.append(standalone_image)

        output, success = request_chatgpt_i2t_until_success(
            sources, user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success:
            meta_query.extra_info["request_error"] = output

        parsed_result, success = self._parse_result(output, parser_mode, eval_type="image", ref_path=image_ref, save_path=save_path)
        meta_query.extra_info["parsed_result"] = parsed_result

        return success
        

    def _evaluate_video_task(self, meta_query: MetaQuery):
        """execute video evaluation task."""
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)
        
        system_prompt = EVAL_PROMPTS["sub_video_system"]
        user_prompt_template = EVAL_PROMPTS["sub_video_user"]
        parser_mode = EVAL_PARSER["video"]
        
        design_prompt_excerpt = meta_query.extra_info["design_prompt_excerpt"]
        html_excerpt = meta_query.extra_info["html_excerpt"]
        content_frames = meta_query.extra_info["content_frames"]
        video_path = meta_query.extra_info["video_path"]
        video_ref = meta_query.extra_info["video_ref"]
        save_path = meta_query.save_path
        success = False

        user_prompt = user_prompt_template.format(
            design_prompt=design_prompt_excerpt,
            html_excerpt=html_excerpt,
            image_path=video_path
        )

        output, success = request_chatgpt_i2t_until_success(
            content_frames, user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success:
            meta_query.extra_info["request_error"] = output
        parsed_result, success = self._parse_result(output, parser_mode, eval_type="video", ref_path=video_ref, save_path=save_path)
        meta_query.extra_info["parsed_result"] = parsed_result

        return success


    def _evaluate_chart_task(self, meta_query: MetaQuery):
        """execute chart evaluation task."""
        url = self.api_keys["plain"]["url"]
        api_key = self.api_keys["plain"]["api_key"]
        model = self.api_keys["plain"]["model"]
        reasoning_effort = self.api_keys["plain"].get("reasoning_effort", None)

        chart_mode = (meta_query.extra_info or {}).get("chart_mode", "subpage")
        if chart_mode == "inline":
            system_prompt = EVAL_PROMPTS["sub_inline_chart_system"]
            user_prompt_template = EVAL_PROMPTS["sub_inline_chart_user"]
        else:
            system_prompt = EVAL_PROMPTS["sub_chart_system"]
            user_prompt_template = EVAL_PROMPTS["sub_chart_user"]
        parser_mode = EVAL_PARSER["chart"]     

        design_prompt = meta_query.extra_info["design_prompt"]
        html_excerpt = meta_query.extra_info["html_excerpt"]
        echart_html = meta_query.extra_info["echart_html"]
        iframe_height = meta_query.extra_info["iframe_height"]
        chart_ref = meta_query.extra_info["chart_ref"]
        iframe_height = meta_query.extra_info["iframe_height"]
        fullpage_screenshot = meta_query.extra_info["fullpage_screenshot"]
        embedded_screenshot = meta_query.extra_info["embedded_screenshot"]
        save_path = meta_query.save_path
        success = False

        if chart_mode == "inline":
            embed_info = meta_query.extra_info.get("embed_info", None)
            user_prompt = user_prompt_template.format(
                chart_ref=chart_ref,
                embed_info=(json.dumps(embed_info, ensure_ascii=False, indent=2)[:1800] if embed_info else "(none)"),
                html_excerpt=html_excerpt,
                design_prompt=design_prompt,
            )
        else:
            user_prompt = user_prompt_template.format(
                echart_path=chart_ref,
                generated_html=echart_html,
                webpage_html_excerpt=html_excerpt,
                design_prompt=design_prompt,
                iframe_height=iframe_height,
            )
        
        sources = [s for s in [fullpage_screenshot, embedded_screenshot] if s is not None]
        if len(sources) < 2:
            meta_query.extra_info["parsed_result"] = {
                "eval_type": "chart",
                "success": False,
                "score": 0.0,
                "raw_output": "missing screenshot(s) for chart evaluation",
                "parsed_info": {},
            }
            return False

        output, success = request_chatgpt_i2t_until_success(
            sources, user_prompt, system_prompt,
            url=url, api_key=api_key, debug=self.debug,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=TIMEOUT, max_retries=MAX_RETRIES, sleep_time=SLEEP_TIME,
            log_path=os.path.join(meta_query.project_dir, "error.log"),
        )
        if not success:
            meta_query.extra_info["request_error"] = output
        parsed_result, success = self._parse_result(output, parser_mode, eval_type="chart", ref_path=chart_ref, save_path=save_path)
        meta_query.extra_info["parsed_result"] = parsed_result

        return success
    
    def _evaluate_global_evaluation_task(self, meta_query: MetaQuery):
        """execute global evaluation task."""

        prompt = meta_query.extra_info["user_prompt"]
        input_image = meta_query.extra_info.get("input_image", None)
        input_html = meta_query.extra_info.get("input_html", None)
        eval_type = meta_query.extra_info.get("eval_type", "layout")
        parser_mode = EVAL_PARSER[eval_type]
        save_path = meta_query.save_path
        success = False

        if os.path.exists(input_html):
            html_data = read_html_file(input_html)

            system_prompt = EVAL_PROMPTS[f"{eval_type}_system"]
            user_prompt = EVAL_PROMPTS[f"{eval_type}_user"].format(
                design_prompt=prompt,
                generated_html=html_data
            )

            url = self.api_keys["plain"]["url"]
            api_key = self.api_keys["plain"]["api_key"]
            model = self.api_keys["plain"]["model"]

            output, success = request_chatgpt_i2t_until_success(
                path_url_or_pil=input_image,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                url=url,
                api_key=api_key,
                model=model,
                reasoning_effort=self.reasoning_effort,
                max_retries=MAX_RETRIES,
                timeout=TIMEOUT,
                sleep_time=SLEEP_TIME,
                debug=self.debug,
                log_path=os.path.join(meta_query.project_dir, "error.log"),
            )
            if not success:
                meta_query.extra_info["request_error"] = output

            parsed_result, success = self._parse_result(output, parser_mode, eval_type=eval_type, save_path=save_path)
            meta_query.extra_info["parsed_result"] = parsed_result

        return success

    def _parse_result(
        self,
        raw_output: str,
        parser_mode: str,
        eval_type: str="",
        ref_path: str="",
        save_path: str=None,
    ):
        success = False
        try:
            parsed_result, _ = parse_score(raw_output, parser_mode, debug=self.debug)
            success = True
        except:
            parsed_result = {}
            if self.debug:
                traceback.print_exc()

        # Always attach these fields so downstream code can be defensive and consistent
        # even when parsing fails.
        parsed_result["eval_type"] = eval_type
        parsed_result["raw_output"] = raw_output
        parsed_result.setdefault("parsed_info", {})
        # When parsing fails, keep a sane default score so reflection prep doesn't crash.
        parsed_result.setdefault("score", 0.0)
        parsed_result["success"] = success
        parsed_result["ref_path"] = ref_path

        if save_path is not None:
            with open(save_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(parsed_result, ensure_ascii=False) + "\n")

        return parsed_result, success


    def _take_screenshot_task(self, project_dir: str, *, inline_chart_targets=None):
        """execute take screenshot task."""
        
        html_path = os.path.join(project_dir, self.main_name)
        main_html_data, subfiles = parse_html_file(html_path)
        design_prompt = get_user_prompt(project_dir)

        screenshot_dir = os.path.join(project_dir, "screenshots")
        os.makedirs(screenshot_dir, exist_ok=True)

        fullpage_bytes, embedded_map, embedded_info_map = asyncio.run(
            screenshot_webpage_and_embedded_images(
                root=project_dir,
                webpage_fname=os.path.basename(html_path),
                image_refs=subfiles["image"] if self.do_image_eval else [],
                chart_refs=(subfiles["chart"] if (self.do_chart_eval and self.chart_mode in ("subpage", "auto")) else []),
                inline_chart_targets=inline_chart_targets if inline_chart_targets else None,
                fullpage_out_file=os.path.join(screenshot_dir, self.main_name.replace(".html", ".png")),
                embedded_out_dir=screenshot_dir,
            )
        )

        return fullpage_bytes, embedded_map, embedded_info_map

    def _clean_task_queue(self):
        """clean the task queue."""
        self.task_queue = defaultdict(list)

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
        else:
            print(f"Unknown task type: {task_type}")
            return False

    def __len__(self):
        return sum(len(v) for v in self.task_queue.values())
    

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
            self.reasoning_effort = "medium" if api_model in ["5.1", "5.2", "gpt-5.1", "gpt-5.2", "gpt5"] else None

            request_cfg = get_openai_request_config(api_model)
            self.api_keys[task_type] = {
                "url": request_cfg["url"],
                "api_key": request_cfg["headers"],
                "model": request_cfg["model"],
                "max_workers": max_workers,
            }
