from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import List

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.mm_utils import parse_html_file
from planner.config import API_CONFIG


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _list_project_dirs(model_dir: Path) -> List[str]:
    project_dirs: List[str] = []
    for p in sorted(model_dir.iterdir()):
        if not p.is_dir():
            continue
        if (p / "main.html").exists() and (p / "planner_output.json").exists():
            project_dirs.append(str(p))
    return project_dirs


def _snapshot_html(project_dir: Path, *, tag: str, main_name: str = "main.html") -> None:
    src = project_dir / main_name
    if not src.exists():
        return
    dst = project_dir / f"{src.stem}_{tag}{src.suffix}"
    shutil.copyfile(src, dst)

def _checkpoint_dir(project_dir: Path, *, phase: str, round_id: str) -> Path:
    return project_dir / ".reflection_checkpoints" / phase / round_id


def _checkpoint_files(project_dir: Path, *, phase: str, round_id: str, relpaths: List[str]) -> None:
    base = _checkpoint_dir(project_dir, phase=phase, round_id=round_id)
    for rel in relpaths:
        if not rel:
            continue
        rel_path = Path(rel)
        src = project_dir / rel_path
        if not src.exists() or not src.is_file():
            continue
        dst = base / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _restore_checkpoint(project_dir: Path, *, phase: str, round_id: str) -> None:
    base = _checkpoint_dir(project_dir, phase=phase, round_id=round_id)
    if not base.exists():
        return
    for src in sorted(base.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(base)
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

def _restore_checkpoint_files(project_dir: Path, *, phase: str, round_id: str, relpaths: List[str]) -> None:
    base = _checkpoint_dir(project_dir, phase=phase, round_id=round_id)
    if not base.exists():
        return
    for rel in relpaths:
        if not rel:
            continue
        rel_path = Path(rel)
        src = base / rel_path
        if not src.exists() or not src.is_file():
            continue
        dst = project_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _collect_item_scores(rm, *, eval_type: str, ref_key: str) -> dict[str, dict[str, float]]:
    """
    Collect per-item scores from the *current* rm.task_queue after an evaluation run.

    Returns:
        dict[project_dir, dict[item_ref, score]]
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for tasks in (rm.task_queue or {}).values():
        for meta in tasks or []:
            parsed = (getattr(meta, "extra_info", None) or {}).get("parsed_result") or {}
            if parsed.get("eval_type") != eval_type:
                continue
            ref = (getattr(meta, "extra_info", None) or {}).get(ref_key)
            if not isinstance(ref, str) or not ref.strip():
                continue
            try:
                raw = float(parsed.get("score", 0.0))
            except Exception:
                raw = 0.0
            out[str(getattr(meta, "project_dir", ""))][ref] = float(rm.calculate_score(raw, eval_type))
    return dict(out)


def _pick_best_round_per_item(
    project_dirs: List[str],
    *,
    save_names_by_round: dict[str, str],
    item_scores_by_round: dict[str, dict[str, dict[str, float]]],
    item_refs_by_project: dict[str, List[str]],
) -> dict[str, dict[str, str]]:
    """
    Pick best round independently for each item (chart/image) within each project.

    Returns:
        dict[project_dir, dict[item_ref, round_id]]
    """
    out: dict[str, dict[str, str]] = {}
    round_ids = list(save_names_by_round.keys())
    for pd in project_dirs:
        refs = item_refs_by_project.get(pd, []) or []
        best_for_project: dict[str, str] = {}
        for ref in refs:
            best_round = None
            best_score = None
            for rid in round_ids:
                score = (item_scores_by_round.get(rid, {}).get(pd, {}) or {}).get(ref, None)
                if score is None:
                    continue
                if best_score is None or score > best_score:
                    best_score = score
                    best_round = rid
            best_for_project[ref] = best_round or round_ids[0]
        out[pd] = best_for_project
    return out


def _get_chart_relpaths(project_dir: Path, *, main_name: str = "main.html") -> List[str]:
    html_path = project_dir / main_name
    if not html_path.exists():
        return []
    _, subfiles = parse_html_file(str(html_path))
    chart_refs = subfiles.get("chart", []) or []
    relpaths = [main_name]
    for ref in chart_refs:
        if isinstance(ref, str) and not ref.startswith("http"):
            relpaths.append(ref)
    return sorted({p for p in relpaths if p})


def _get_image_relpaths(project_dir: Path, *, main_name: str = "main.html") -> List[str]:
    html_path = project_dir / main_name
    if not html_path.exists():
        return []
    _, subfiles = parse_html_file(str(html_path))
    image_refs = subfiles.get("image", []) or []
    relpaths = [main_name]
    for ref in image_refs:
        if not isinstance(ref, str) or not ref or ref.startswith("http"):
            continue
        # Only snapshot local paths within project_dir.
        rel = ref.lstrip("/").strip()
        p = project_dir / rel
        if p.exists() and p.is_file():
            relpaths.append(rel)
    return sorted({p for p in relpaths if p})


def _load_eval_json(project_dir: Path, *, save_name: str) -> dict:
    p = project_dir / save_name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _round_score(project_dirs: List[str], *, save_name: str, keys: List[str]) -> float:
    if not keys:
        return 0.0
    per_project = []
    for pd in project_dirs:
        data = _load_eval_json(Path(pd), save_name=save_name)
        final = (data or {}).get("final_result", {}) or {}
        vals = []
        for k in keys:
            try:
                vals.append(float(final.get(k, 0.0)))
            except Exception:
                vals.append(0.0)
        if vals:
            per_project.append(sum(vals) / len(vals))
    if not per_project:
        return 0.0
    return sum(per_project) / len(per_project)

def _round_score_one(project_dir: str, *, save_name: str, keys: List[str]) -> float:
    if not keys:
        return 0.0
    data = _load_eval_json(Path(project_dir), save_name=save_name)
    final = (data or {}).get("final_result", {}) or {}
    vals: list[float] = []
    for k in keys:
        try:
            vals.append(float(final.get(k, 0.0)))
        except Exception:
            vals.append(0.0)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _pick_best_round(project_dirs: List[str], *, save_names_by_round: dict[str, str], keys: List[str]) -> str:
    best_round = None
    best_score = None
    for round_id, save_name in save_names_by_round.items():
        score = _round_score(project_dirs, save_name=save_name, keys=keys)
        if best_score is None or score > best_score:
            best_score = score
            best_round = round_id
    return best_round or next(iter(save_names_by_round.keys()))

def _pick_best_round_per_project(
    project_dirs: List[str], *, save_names_by_round: dict[str, str], keys: List[str]
) -> dict[str, str]:
    """
    Pick best round independently for each project_dir.

    Returns:
        dict[project_dir, round_id]
    """
    out: dict[str, str] = {}
    if not project_dirs:
        return out
    for pd in project_dirs:
        best_round = None
        best_score = None
        for round_id, save_name in save_names_by_round.items():
            score = _round_score_one(pd, save_name=save_name, keys=keys)
            if best_score is None or score > best_score:
                best_score = score
                best_round = round_id
        out[pd] = best_round or next(iter(save_names_by_round.keys()))
    return out


def _write_eval_best(
    project_dirs: List[str],
    *,
    chart_best: dict[str, tuple[str, str]] | None,
    image_best: dict[str, tuple[str, str]] | None,
    global_best: dict[str, tuple[str, str]] | None,
) -> None:
    """
    Write an "assembled" evaluation JSON per project by stitching together:
    - chart score from the chart-best round eval
    - image score from the image-best round eval
    - global scores (layout/style/aes) from the global-best round eval

    Args:
        chart_best: dict[project_dir, (round_id, eval_json_filename)] or None
        image_best: dict[project_dir, (round_id, eval_json_filename)] or None
        global_best: dict[project_dir, (round_id, eval_json_filename)] or None
    """

    for pd in project_dirs:
        project_dir = Path(pd)
        baseline = _load_eval_json(project_dir, save_name="eval_result_0.json")
        out: dict = {
            "best_rounds": {},
            "sources": {},
            "final_result": {},
        }

        def _get_final(path_name: str) -> dict:
            d = _load_eval_json(project_dir, save_name=path_name)
            return (d or {}).get("final_result", {}) or {}

        # Start from baseline to ensure missing keys degrade gracefully.
        stitched = dict((baseline or {}).get("final_result", {}) or {})

        if chart_best is not None and pd in chart_best:
            rid, fname = chart_best[pd]
            out["best_rounds"]["chart"] = rid
            out["sources"]["chart"] = fname
            stitched["chart"] = _get_final(fname).get("chart", stitched.get("chart", 0.0))

        if image_best is not None and pd in image_best:
            rid, fname = image_best[pd]
            out["best_rounds"]["image"] = rid
            out["sources"]["image"] = fname
            stitched["image"] = _get_final(fname).get("image", stitched.get("image", 0.0))

        if global_best is not None and pd in global_best:
            rid, fname = global_best[pd]
            out["best_rounds"]["global"] = rid
            out["sources"]["global"] = fname
            gf = _get_final(fname)
            for k in ("layout", "style", "aes"):
                if k in gf:
                    stitched[k] = gf[k]

        out["final_result"] = stitched
        (project_dir / "eval_best.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

def _write_failed_tasks(project_dirs: List[str], failed_tasks: list, *, model_dir: Path) -> None:
    """
    Persist failures to disk so long-running experiments are debuggable.

    - Per project: <project_dir>/failed_tasks.json
    - Per model: <model_dir>/failed_tasks.json (merged summary)
    """
    tasks = failed_tasks or []
    by_project: dict[str, list] = defaultdict(list)
    for t in tasks:
        try:
            pd = str((t or {}).get("project_dir") or "")
        except Exception:
            pd = ""
        by_project[pd].append(t)

    for pd in project_dirs:
        p = Path(pd)
        out = {
            "project_dir": str(p),
            "count": len(by_project.get(str(p), [])),
            "failed_tasks": by_project.get(str(p), []),
        }
        (p / "failed_tasks.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    model_out = {
        "model_dir": str(model_dir),
        "count": len(tasks),
        "failed_tasks": tasks,
    }
    (model_dir / "failed_tasks.json").write_text(json.dumps(model_out, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_eval(
    rm,
    *,
    save_name: str,
    global_eval: bool,
    image_eval: bool,
    video_eval: bool,
    chart_eval: bool,
    check_missing: bool = True,
    do_mm_split: bool = True,
    eval_api_cfg: dict | None = None,
) -> None:
    rm.set_eval_status(
        global_eval=global_eval,
        image_eval=image_eval,
        video_eval=video_eval,
        chart_eval=chart_eval,
    )
    if eval_api_cfg is not None:
        rm.config_apis(eval_api_cfg)

    rm._clean_task_queue()
    rm.prepare_initial_evaluation_tasks(do_mm_split=do_mm_split)
    rm.run_all_tasks(do_clean=False)

    # Keep a two-stage evaluation flow:
    # after evaluating the assets present in HTML, run an extra pass to detect
    # prompt-described multimodal elements (image/video/chart) that were never
    # evaluated because they are missing from the output, and append 0-score
    # entries into eval_result.jsonl.
    enabled_meta_types = image_eval or video_eval or chart_eval

    if check_missing and enabled_meta_types and hasattr(rm, "prepare_final_evaluation_tasks"):
        eval_task_queue = rm.task_queue
        rm.prepare_final_evaluation_tasks()
        rm.task_queue["plain"] = [
            t
            for t in (rm.task_queue.get("plain", []) or [])
            if (getattr(t, "extra_info", None) or {}).get("eval_type")=="missing"
        ]
        if rm.task_queue.get("plain"):
            rm.run_all_tasks(do_clean=True)
        else:
            rm._clean_task_queue()
        rm.task_queue = eval_task_queue

    rm.parse_eval_results(save_name=save_name)


def _run_backfill_missing_eval(
    rm,
    *,
    save_name: str,
    image_eval: bool,
    video_eval: bool,
    chart_eval: bool,
    eval_api_cfg: dict | None = None,
) -> None:
    """
    Backfill the "missing multimodal elements" evaluation pass using an existing
    eval_result.jsonl produced by a previous evaluation run.

    This intentionally does NOT re-run:
      prepare_initial_evaluation_tasks() -> run_all_tasks() -> parse_eval_results()
    """
    enabled_meta_types = image_eval or video_eval or chart_eval
    if not enabled_meta_types:
        return
    if eval_api_cfg is not None:
        rm.config_apis(eval_api_cfg)

    rm._clean_task_queue()
    rm.prepare_final_evaluation_tasks()
    rm.task_queue["plain"] = [
        t
        for t in (rm.task_queue.get("plain", []) or [])
        if (getattr(t, "extra_info", None) or {}).get("eval_type") == "missing"
    ]
    if rm.task_queue.get("plain"):
        rm.run_all_tasks(do_clean=True)
    else:
        rm._clean_task_queue()

    rm.parse_eval_results(save_name=save_name)


def _maybe_snapshot_main(project_dirs: List[str], *, tag: str, enabled: bool) -> None:
    if not enabled:
        return
    for pd in project_dirs:
        _snapshot_html(Path(pd), tag=tag)

def _phase_done(project_dir: str, *, phase: str, rounds: int, enabled: bool) -> bool:
    """
    Phase completion heuristic based on the last expected eval json.
    This matches what the script writes at the end of each phase/round.
    """
    if not enabled:
        return True
    p = Path(project_dir)
    if phase == "chart":
        if rounds <= 0:
            return (p / "eval_result_chart_0.json").exists() or (p / "eval_result_0.json").exists()
        return (p / f"eval_result_after_chart_r{rounds}.json").exists()
    if phase == "image":
        if rounds <= 0:
            return (p / "eval_result_image_0.json").exists() or (p / "eval_result_0.json").exists()
        return (p / f"eval_result_after_image_r{rounds}.json").exists()
    if phase == "global":
        if rounds <= 0:
            return (p / "eval_result_global_0.json").exists()
        return (p / f"eval_result_after_global_r{rounds}.json").exists()
    if phase == "final":
        return (p / "eval_result_final.json").exists()
    return False

def load_api_config(path: str | None, default=API_CONFIG) -> dict:
    if path is not None:
        data = yaml.safe_load(open(path, "r"))
        api_config = data["api_config"]
        print("Loaded API config:", api_config)
    else:
        api_config = default
    return api_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp_dir", required=True, help="e.g. outputs/<exp_name>")
    ap.add_argument("--config", default="benchmark/configs/experiment.yaml")
    ap.add_argument("--eval_api_config", type=str, default=None, help="Path to API config YAML file.")
    ap.add_argument("--reflection_api_config", type=str, default=None, help="Path to API config YAML file.")
    ap.add_argument("--only_models", default="", help="Comma-separated model names to run")
    ap.add_argument(
        "--only_missing_final",
        action="store_true",
        help="Only evaluate projects missing eval_result_final.json (useful to resume partial runs).",
    )
    ap.add_argument(
        "--resume_by_phase",
        action="store_true",
        help=(
            "Resume unfinished projects by skipping already-completed phases per project "
            "(chart/image/global), based on existing eval_result_after_* files."
        ),
    )
    ap.add_argument(
        "--backfill_missing_meta",
        action="store_true",
        help=(
            "Only run the missing multimodal-element checker (prepare_final_evaluation_tasks) "
            "based on existing eval_result.jsonl, and rewrite the eval JSON (no re-evaluation)."
        ),
    )
    ap.add_argument(
        "--backfill_save_name",
        default="eval_result_final.json",
        help="Eval JSON filename to rewrite during --backfill_missing_meta (default: eval_result_final.json).",
    )
    ap.add_argument(
        "--chart_mode",
        choices=["auto", "subpage", "inline"],
        default="auto",
        help="Chart evaluation mode: auto=both iframe+inline, subpage=iframe only, inline=canvas/svg/div only.",
    )
    ap.add_argument(
        "--is_chart_subpage",
        action="store_true",
        help="Deprecated alias for --chart_mode=subpage.",
    )
    ap.add_argument(
        "--skip_snapshots",
        action="store_true",
        help="Skip saving HTML snapshots during reflection rounds (overrides config file setting).",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    exp_dir = Path(args.exp_dir)
    cfg = _load_yaml(Path(args.config))

    eval_api_cfg = load_api_config(args.eval_api_config, default=API_CONFIG)
    reflection_api_cfg = load_api_config(args.reflection_api_config, default=API_CONFIG)

    # Import directly to avoid `planner/__init__.py` side effects.
    from planner.reflection_manager import ReflectionManager

    eval_cfg = cfg.get("evaluation", {})
    refl_cfg = cfg.get("reflection", {})
    rt_cfg = cfg.get("runtime", {})

    only = {s.strip() for s in args.only_models.split(",") if s.strip()}

    for model_dir in sorted(exp_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        model_name = model_dir.name
        if only and model_name not in only:
            continue

        project_dirs = _list_project_dirs(model_dir)
        if not project_dirs:
            continue
        if args.only_missing_final:
            before = len(project_dirs)
            project_dirs = [pd for pd in project_dirs if not (Path(pd) / "eval_result_final.json").exists()]
            if not project_dirs:
                print(f"[eval] skip model={model_name} (all {before} projects already have eval_result_final.json)")
                continue
            print(f"[eval] resume model={model_name} missing={len(project_dirs)}/{before}")

        chart_mode = "subpage" if args.is_chart_subpage else args.chart_mode
        keep_snapshots = False if args.skip_snapshots else bool(rt_cfg.get("keep_html_snapshots", True))

        if args.backfill_missing_meta:
            have_jsonl = [pd for pd in project_dirs if (Path(pd) / "eval_result.jsonl").exists()]
            missing = len(project_dirs) - len(have_jsonl)
            if not have_jsonl:
                print(f"[eval] backfill skip model={model_name} (no eval_result.jsonl found)")
                continue
            if missing:
                print(f"[eval] backfill model={model_name} jsonl_missing={missing}/{len(project_dirs)}")

            rm = ReflectionManager(project_dirs=have_jsonl, debug=args.debug, chart_mode=chart_mode)
            _run_backfill_missing_eval(
                rm,
                save_name=args.backfill_save_name,
                image_eval=bool(eval_cfg.get("image_eval", False)),
                video_eval=bool(eval_cfg.get("video_eval", False)),
                chart_eval=bool(eval_cfg.get("chart_eval", False)),
                eval_api_cfg=eval_api_cfg,
            )
            _write_failed_tasks(have_jsonl, getattr(rm, "failed_tasks", []), model_dir=model_dir)
            print(f"[eval] backfill done model={model_name} projects={len(have_jsonl)}")
            continue

        enable_local_chart = bool(refl_cfg.get("enable_local_chart_reflection", False))
        enable_global_chart = bool(refl_cfg.get("enable_global_chart_reflection", False))
        chart_rounds = int(refl_cfg.get("chart_rounds", 1))
        chart_phase_enabled = chart_rounds > 0 and (enable_local_chart or enable_global_chart)

        enable_local_image = bool(refl_cfg.get("enable_local_image_reflection", False))
        enable_global_image = bool(refl_cfg.get("enable_global_image_reflection", False))
        image_rounds = int(refl_cfg.get("image_rounds", 1))
        image_phase_enabled = image_rounds > 0 and (enable_local_image or enable_global_image)

        global_phase_enabled = bool(refl_cfg.get("enable_global_reflection", True))
        global_rounds = int(refl_cfg.get("global_rounds", 1))

        enable_any_reflection = chart_phase_enabled or image_phase_enabled or global_phase_enabled

        if args.resume_by_phase:
            missing_baseline = [pd for pd in project_dirs if not (Path(pd) / "eval_result_0.json").exists()]
            if missing_baseline:
                rm0 = ReflectionManager(project_dirs=missing_baseline, debug=args.debug, chart_mode=chart_mode)
                _run_eval(
                    rm0,
                    save_name="eval_result_0.json",
                    global_eval=bool(eval_cfg.get("global_eval", True)),
                    image_eval=bool(eval_cfg.get("image_eval", False)),
                    video_eval=bool(eval_cfg.get("video_eval", False)),
                    chart_eval=bool(eval_cfg.get("chart_eval", False)),
                    eval_api_cfg=eval_api_cfg,
                )

            best_chart: dict[str, tuple[str, str]] | None = None
            best_image: dict[str, tuple[str, str]] | None = None
            best_global: dict[str, tuple[str, str]] | None = None

            need_chart = [
                pd for pd in project_dirs if not _phase_done(pd, phase="chart", rounds=chart_rounds, enabled=chart_phase_enabled)
            ]
            if chart_phase_enabled and need_chart:
                rm = ReflectionManager(project_dirs=need_chart, debug=args.debug, chart_mode=chart_mode)
                _run_eval(
                    rm,
                    save_name="eval_result_chart_0.json",
                    global_eval=False,
                    image_eval=False,
                    video_eval=False,
                    chart_eval=True,
                    eval_api_cfg=eval_api_cfg,
                )
                baseline_chart_scores = _collect_item_scores(rm, eval_type="chart", ref_key="chart_ref")
                for pd in need_chart:
                    _checkpoint_files(Path(pd), phase="chart", round_id="r0", relpaths=_get_chart_relpaths(Path(pd)))
                chart_save_names = {"r0": "eval_result_0.json"}
                chart_item_scores_by_round: dict[str, dict[str, dict[str, float]]] = {"r0": baseline_chart_scores}

                for r in range(1, chart_rounds + 1):
                    eval_metas = list(rm.task_queue.get("plain", []))
                    rm.config_apis(reflection_api_cfg)
                    if enable_local_chart:
                        _maybe_snapshot_main(need_chart, tag=f"pre_chart_r{r}_local", enabled=keep_snapshots)
                        rm.prepare_initial_reflection_tasks("local_chart")
                        rm.run_all_tasks(do_clean=True)
                    if enable_global_chart:
                        _maybe_snapshot_main(need_chart, tag=f"pre_chart_r{r}_global", enabled=keep_snapshots)
                        rm.task_queue = {"plain": eval_metas}
                        rm.prepare_initial_reflection_tasks("global_chart")
                        rm.run_all_tasks(do_clean=True)
                    _run_eval(
                        rm,
                        save_name=f"eval_result_after_chart_r{r}.json",
                        global_eval=False,
                        image_eval=False,
                        video_eval=False,
                        chart_eval=True,
                        eval_api_cfg=eval_api_cfg,
                    )
                    chart_item_scores_by_round[f"r{r}"] = _collect_item_scores(rm, eval_type="chart", ref_key="chart_ref")
                    for pd in need_chart:
                        _checkpoint_files(Path(pd), phase="chart", round_id=f"r{r}", relpaths=_get_chart_relpaths(Path(pd)))
                    chart_save_names[f"r{r}"] = f"eval_result_after_chart_r{r}.json"

                best_by_project = _pick_best_round_per_project(need_chart, save_names_by_round=chart_save_names, keys=["chart"])
                chart_refs_by_project: dict[str, List[str]] = {}
                for pd in need_chart:
                    rels = _get_chart_relpaths(Path(pd))
                    chart_refs_by_project[pd] = [
                        r for r in rels if r != "main.html" and isinstance(r, str) and r.lower().endswith(".html")
                    ]
                best_rounds_by_chart = _pick_best_round_per_item(
                    need_chart,
                    save_names_by_round=chart_save_names,
                    item_scores_by_round=chart_item_scores_by_round,
                    item_refs_by_project=chart_refs_by_project,
                )
                for pd, best_round in best_by_project.items():
                    _restore_checkpoint_files(Path(pd), phase="chart", round_id=best_round, relpaths=["main.html"])
                    per_chart = best_rounds_by_chart.get(pd, {}) or {}
                    for chart_ref, rid in per_chart.items():
                        _restore_checkpoint_files(Path(pd), phase="chart", round_id=rid, relpaths=[chart_ref])

            if chart_phase_enabled:
                chart_save_names_all = {"r0": "eval_result_0.json"}
                for r in range(1, chart_rounds + 1):
                    chart_save_names_all[f"r{r}"] = f"eval_result_after_chart_r{r}.json"
                best_by_project_all = _pick_best_round_per_project(project_dirs, save_names_by_round=chart_save_names_all, keys=["chart"])
                best_chart = {pd: (rid, chart_save_names_all[rid]) for pd, rid in best_by_project_all.items()}
            else:
                best_chart = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

            need_image = [
                pd for pd in project_dirs if not _phase_done(pd, phase="image", rounds=image_rounds, enabled=image_phase_enabled)
            ]
            if image_phase_enabled and need_image:
                rm = ReflectionManager(project_dirs=need_image, debug=args.debug, chart_mode=chart_mode)
                _run_eval(
                    rm,
                    save_name="eval_result_image_0.json",
                    global_eval=False,
                    image_eval=True,
                    video_eval=False,
                    chart_eval=False,
                    eval_api_cfg=eval_api_cfg,
                )
                baseline_image_scores = _collect_item_scores(rm, eval_type="image", ref_key="image_ref")
                for pd in need_image:
                    _checkpoint_files(Path(pd), phase="image", round_id="r0", relpaths=_get_image_relpaths(Path(pd)))
                image_save_names = {"r0": "eval_result_0.json"}
                image_item_scores_by_round: dict[str, dict[str, dict[str, float]]] = {"r0": baseline_image_scores}

                for r in range(1, image_rounds + 1):
                    rm.config_apis(reflection_api_cfg)
                    if enable_local_image:
                        _maybe_snapshot_main(need_image, tag=f"pre_image_r{r}_local", enabled=keep_snapshots)
                        rm.prepare_initial_reflection_tasks("local_image")
                        rm.run_all_tasks(do_clean=not enable_global_image)
                    if enable_global_image and rm.task_queue.get("imedit"):
                        _maybe_snapshot_main(need_image, tag=f"pre_image_r{r}_global", enabled=keep_snapshots)
                        rm.prepare_initial_reflection_tasks("global_image")
                        rm.run_all_tasks(do_clean=True)
                    if enable_local_image and enable_global_image is False:
                        rm._clean_task_queue()
                    _run_eval(
                        rm,
                        save_name=f"eval_result_after_image_r{r}.json",
                        global_eval=False,
                        image_eval=True,
                        video_eval=False,
                        chart_eval=False,
                        eval_api_cfg=eval_api_cfg,
                    )
                    image_item_scores_by_round[f"r{r}"] = _collect_item_scores(rm, eval_type="image", ref_key="image_ref")
                    for pd in need_image:
                        _checkpoint_files(Path(pd), phase="image", round_id=f"r{r}", relpaths=_get_image_relpaths(Path(pd)))
                    image_save_names[f"r{r}"] = f"eval_result_after_image_r{r}.json"

                best_by_project = _pick_best_round_per_project(need_image, save_names_by_round=image_save_names, keys=["image"])
                image_refs_by_project: dict[str, List[str]] = {}
                for pd in need_image:
                    rels = _get_image_relpaths(Path(pd))
                    image_refs_by_project[pd] = [r for r in rels if r != "main.html" and isinstance(r, str)]
                best_rounds_by_image = _pick_best_round_per_item(
                    need_image,
                    save_names_by_round=image_save_names,
                    item_scores_by_round=image_item_scores_by_round,
                    item_refs_by_project=image_refs_by_project,
                )
                for pd, best_round in best_by_project.items():
                    _restore_checkpoint_files(Path(pd), phase="image", round_id=best_round, relpaths=["main.html"])
                    per_image = best_rounds_by_image.get(pd, {}) or {}
                    for image_ref, rid in per_image.items():
                        _restore_checkpoint_files(Path(pd), phase="image", round_id=rid, relpaths=[image_ref])

            if image_phase_enabled:
                image_save_names_all = {"r0": "eval_result_0.json"}
                for r in range(1, image_rounds + 1):
                    image_save_names_all[f"r{r}"] = f"eval_result_after_image_r{r}.json"
                best_by_project_all = _pick_best_round_per_project(project_dirs, save_names_by_round=image_save_names_all, keys=["image"])
                best_image = {pd: (rid, image_save_names_all[rid]) for pd, rid in best_by_project_all.items()}
            else:
                best_image = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

            need_global = [
                pd for pd in project_dirs if not _phase_done(pd, phase="global", rounds=global_rounds, enabled=global_phase_enabled)
            ]
            if global_phase_enabled and need_global:
                rm = ReflectionManager(project_dirs=need_global, debug=args.debug, chart_mode=chart_mode)
                _run_eval(
                    rm,
                    save_name="eval_result_global_0.json",
                    global_eval=bool(eval_cfg.get("global_eval", True)),
                    image_eval=bool(eval_cfg.get("image_eval", False)),
                    video_eval=bool(eval_cfg.get("video_eval", False)),
                    chart_eval=bool(eval_cfg.get("chart_eval", False)),
                    eval_api_cfg=eval_api_cfg,
                )
                for pd in need_global:
                    _checkpoint_files(Path(pd), phase="global", round_id="r0", relpaths=["main.html"])
                global_save_names = {"r0": "eval_result_global_0.json"}

                for r in range(1, global_rounds + 1):
                    _run_eval(
                        rm,
                        save_name=f"eval_result_global_seed_r{r}.json",
                        global_eval=True,
                        image_eval=False,
                        video_eval=False,
                        chart_eval=False,
                        eval_api_cfg=eval_api_cfg,
                    )
                    _maybe_snapshot_main(need_global, tag=f"pre_global_r{r}", enabled=keep_snapshots)
                    rm.config_apis(reflection_api_cfg)
                    rm.prepare_initial_reflection_tasks("global")
                    rm.run_all_tasks(do_clean=True)
                    _run_eval(
                        rm,
                        save_name=f"eval_result_after_global_r{r}.json",
                        global_eval=bool(eval_cfg.get("global_eval", True)),
                        image_eval=bool(eval_cfg.get("image_eval", False)),
                        video_eval=bool(eval_cfg.get("video_eval", False)),
                        chart_eval=bool(eval_cfg.get("chart_eval", False)),
                        eval_api_cfg=eval_api_cfg,
                    )
                    for pd in need_global:
                        _checkpoint_files(Path(pd), phase="global", round_id=f"r{r}", relpaths=["main.html"])
                    global_save_names[f"r{r}"] = f"eval_result_after_global_r{r}.json"

                full_keys: List[str] = ["layout", "style", "aes"]
                best_by_project = _pick_best_round_per_project(need_global, save_names_by_round=global_save_names, keys=full_keys)
                for pd, best_round in best_by_project.items():
                    _restore_checkpoint(Path(pd), phase="global", round_id=best_round)

            if global_phase_enabled:
                global_save_names_all = {"r0": "eval_result_global_0.json"}
                for r in range(1, global_rounds + 1):
                    global_save_names_all[f"r{r}"] = f"eval_result_after_global_r{r}.json"
                full_keys = ["layout", "style", "aes"]
                best_by_project_all = _pick_best_round_per_project(project_dirs, save_names_by_round=global_save_names_all, keys=full_keys)
                best_global = {pd: (rid, global_save_names_all[rid]) for pd, rid in best_by_project_all.items()}
            else:
                best_global = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

            if enable_any_reflection:
                rm_final = ReflectionManager(project_dirs=project_dirs, debug=args.debug, chart_mode=chart_mode)
                _run_eval(
                    rm_final,
                    save_name="eval_result_final.json",
                    global_eval=bool(eval_cfg.get("global_eval", True)),
                    image_eval=bool(eval_cfg.get("image_eval", False)),
                    video_eval=bool(eval_cfg.get("video_eval", False)),
                    chart_eval=bool(eval_cfg.get("chart_eval", False)),
                    eval_api_cfg=eval_api_cfg,
                )
            _write_eval_best(project_dirs, chart_best=best_chart, image_best=best_image, global_best=best_global)
            _write_failed_tasks(project_dirs, getattr(rm_final, "failed_tasks", []), model_dir=model_dir)
            print(f"[eval] done(resume_by_phase) model={model_name} projects={len(project_dirs)}")
            continue

        rm = ReflectionManager(project_dirs=project_dirs, debug=args.debug, chart_mode=chart_mode)

        _run_eval(
            rm,
            save_name="eval_result_0.json",
            global_eval=bool(eval_cfg.get("global_eval", True)),
            image_eval=bool(eval_cfg.get("image_eval", False)),
            video_eval=bool(eval_cfg.get("video_eval", False)),
            chart_eval=bool(eval_cfg.get("chart_eval", False)),
            eval_api_cfg=eval_api_cfg,
        )
        baseline_chart_scores = (
            _collect_item_scores(rm, eval_type="chart", ref_key="chart_ref")
            if bool(eval_cfg.get("chart_eval", False))
            else {}
        )
        baseline_image_scores = (
            _collect_item_scores(rm, eval_type="image", ref_key="image_ref")
            if bool(eval_cfg.get("image_eval", False))
            else {}
        )

        best_chart: dict[str, tuple[str, str]] | None = None
        best_image: dict[str, tuple[str, str]] | None = None
        best_global: dict[str, tuple[str, str]] | None = None

        if chart_phase_enabled:
            _run_eval(
                rm,
                save_name="eval_result_chart_0.json",
                global_eval=False,
                image_eval=False,
                video_eval=False,
                chart_eval=True,
                eval_api_cfg=eval_api_cfg,
            )
            for pd in project_dirs:
                _checkpoint_files(
                    Path(pd),
                    phase="chart",
                    round_id="r0",
                    relpaths=_get_chart_relpaths(Path(pd)),
                )
            chart_save_names = {"r0": "eval_result_0.json"}
            chart_item_scores_by_round: dict[str, dict[str, dict[str, float]]] = {"r0": baseline_chart_scores}

            for r in range(1, chart_rounds + 1):
                eval_metas = list(rm.task_queue.get("plain", []))
                rm.config_apis(reflection_api_cfg)

                if enable_local_chart:
                    _maybe_snapshot_main(project_dirs, tag=f"pre_chart_r{r}_local", enabled=keep_snapshots)
                    rm.prepare_initial_reflection_tasks("local_chart")
                    rm.run_all_tasks(do_clean=True)

                if enable_global_chart:
                    _maybe_snapshot_main(project_dirs, tag=f"pre_chart_r{r}_global", enabled=keep_snapshots)
                    rm.task_queue = {"plain": eval_metas}
                    rm.prepare_initial_reflection_tasks("global_chart")
                    rm.run_all_tasks(do_clean=True)

                _run_eval(
                    rm,
                    save_name=f"eval_result_after_chart_r{r}.json",
                    global_eval=False,
                    image_eval=False,
                    video_eval=False,
                    chart_eval=True,
                    eval_api_cfg=eval_api_cfg,
                )
                chart_item_scores_by_round[f"r{r}"] = _collect_item_scores(rm, eval_type="chart", ref_key="chart_ref")
                for pd in project_dirs:
                    _checkpoint_files(
                        Path(pd),
                        phase="chart",
                        round_id=f"r{r}",
                        relpaths=_get_chart_relpaths(Path(pd)),
                    )
                chart_save_names[f"r{r}"] = f"eval_result_after_chart_r{r}.json"

            best_by_project = _pick_best_round_per_project(project_dirs, save_names_by_round=chart_save_names, keys=["chart"])
            chart_refs_by_project: dict[str, List[str]] = {}
            for pd in project_dirs:
                rels = _get_chart_relpaths(Path(pd))
                chart_refs_by_project[pd] = [
                    r for r in rels if r != "main.html" and isinstance(r, str) and r.lower().endswith(".html")
                ]
            best_rounds_by_chart = _pick_best_round_per_item(
                project_dirs,
                save_names_by_round=chart_save_names,
                item_scores_by_round=chart_item_scores_by_round,
                item_refs_by_project=chart_refs_by_project,
            )
            for pd, best_round in best_by_project.items():
                _restore_checkpoint_files(Path(pd), phase="chart", round_id=best_round, relpaths=["main.html"])
                per_chart = best_rounds_by_chart.get(pd, {}) or {}
                for chart_ref, rid in per_chart.items():
                    _restore_checkpoint_files(Path(pd), phase="chart", round_id=rid, relpaths=[chart_ref])
            best_chart = {pd: (rid, chart_save_names[rid]) for pd, rid in best_by_project.items()}
        else:
            best_chart = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

        if image_phase_enabled:
            _run_eval(
                rm,
                save_name="eval_result_image_0.json",
                global_eval=False,
                image_eval=True,
                video_eval=False,
                chart_eval=False,
                eval_api_cfg=eval_api_cfg,
            )
            for pd in project_dirs:
                _checkpoint_files(
                    Path(pd),
                    phase="image",
                    round_id="r0",
                    relpaths=_get_image_relpaths(Path(pd)),
                )
            image_save_names = {"r0": "eval_result_0.json"}
            image_item_scores_by_round: dict[str, dict[str, dict[str, float]]] = {"r0": baseline_image_scores}

            for r in range(1, image_rounds + 1):
                rm.config_apis(reflection_api_cfg)
                if enable_local_image:
                    _maybe_snapshot_main(project_dirs, tag=f"pre_image_r{r}_local", enabled=keep_snapshots)
                    rm.prepare_initial_reflection_tasks("local_image")
                    rm.run_all_tasks(do_clean=not enable_global_image)

                if enable_global_image and rm.task_queue.get("imedit"):
                    _maybe_snapshot_main(project_dirs, tag=f"pre_image_r{r}_global", enabled=keep_snapshots)
                    rm.prepare_initial_reflection_tasks("global_image")
                    rm.run_all_tasks(do_clean=True)

                if enable_local_image and enable_global_image is False:
                    rm._clean_task_queue()

                _run_eval(
                    rm,
                    save_name=f"eval_result_after_image_r{r}.json",
                    global_eval=False,
                    image_eval=True,
                    video_eval=False,
                    chart_eval=False,
                    eval_api_cfg=eval_api_cfg,
                )
                image_item_scores_by_round[f"r{r}"] = _collect_item_scores(rm, eval_type="image", ref_key="image_ref")
                for pd in project_dirs:
                    _checkpoint_files(
                        Path(pd),
                        phase="image",
                        round_id=f"r{r}",
                        relpaths=_get_image_relpaths(Path(pd)),
                    )
                image_save_names[f"r{r}"] = f"eval_result_after_image_r{r}.json"

            best_by_project = _pick_best_round_per_project(project_dirs, save_names_by_round=image_save_names, keys=["image"])
            image_refs_by_project: dict[str, List[str]] = {}
            for pd in project_dirs:
                rels = _get_image_relpaths(Path(pd))
                image_refs_by_project[pd] = [r for r in rels if r != "main.html" and isinstance(r, str)]
            best_rounds_by_image = _pick_best_round_per_item(
                project_dirs,
                save_names_by_round=image_save_names,
                item_scores_by_round=image_item_scores_by_round,
                item_refs_by_project=image_refs_by_project,
            )
            for pd, best_round in best_by_project.items():
                _restore_checkpoint_files(Path(pd), phase="image", round_id=best_round, relpaths=["main.html"])
                per_image = best_rounds_by_image.get(pd, {}) or {}
                for image_ref, rid in per_image.items():
                    _restore_checkpoint_files(Path(pd), phase="image", round_id=rid, relpaths=[image_ref])
            best_image = {pd: (rid, image_save_names[rid]) for pd, rid in best_by_project.items()}
        else:
            best_image = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

        if global_phase_enabled:
            _run_eval(
                rm,
                save_name="eval_result_global_0.json",
                global_eval=bool(eval_cfg.get("global_eval", True)),
                image_eval=bool(eval_cfg.get("image_eval", False)),
                video_eval=bool(eval_cfg.get("video_eval", False)),
                chart_eval=bool(eval_cfg.get("chart_eval", False)),
                eval_api_cfg=eval_api_cfg,
            )
            for pd in project_dirs:
                _checkpoint_files(Path(pd), phase="global", round_id="r0", relpaths=["main.html"])
            global_save_names = {"r0": "eval_result_global_0.json"}

            for r in range(1, global_rounds + 1):
                _run_eval(
                    rm,
                    save_name=f"eval_result_global_seed_r{r}.json",
                    global_eval=True,
                    image_eval=False,
                    video_eval=False,
                    chart_eval=False,
                    eval_api_cfg=eval_api_cfg,
                )
                _maybe_snapshot_main(project_dirs, tag=f"pre_global_r{r}", enabled=keep_snapshots)
                rm.config_apis(reflection_api_cfg)
                rm.prepare_initial_reflection_tasks("global")
                rm.run_all_tasks(do_clean=True)

                _run_eval(
                    rm,
                    save_name=f"eval_result_after_global_r{r}.json",
                    global_eval=bool(eval_cfg.get("global_eval", True)),
                    image_eval=bool(eval_cfg.get("image_eval", False)),
                    video_eval=bool(eval_cfg.get("video_eval", False)),
                    chart_eval=bool(eval_cfg.get("chart_eval", False)),
                    eval_api_cfg=eval_api_cfg,
                )
                for pd in project_dirs:
                    _checkpoint_files(Path(pd), phase="global", round_id=f"r{r}", relpaths=["main.html"])
                global_save_names[f"r{r}"] = f"eval_result_after_global_r{r}.json"

            # Global-best selection should be driven only by global metrics.
            full_keys: List[str] = ["layout", "style", "aes"]

            best_by_project = _pick_best_round_per_project(project_dirs, save_names_by_round=global_save_names, keys=full_keys)
            for pd, best_round in best_by_project.items():
                _restore_checkpoint(Path(pd), phase="global", round_id=best_round)
            best_global = {pd: (rid, global_save_names[rid]) for pd, rid in best_by_project.items()}
        else:
            best_global = {pd: ("baseline", "eval_result_0.json") for pd in project_dirs}

        if enable_any_reflection:
            _run_eval(
                rm,
                save_name="eval_result_final.json",
                global_eval=bool(eval_cfg.get("global_eval", True)),
                image_eval=bool(eval_cfg.get("image_eval", False)),
                video_eval=bool(eval_cfg.get("video_eval", False)),
                chart_eval=bool(eval_cfg.get("chart_eval", False)),
                eval_api_cfg=eval_api_cfg,
            )

        _write_eval_best(
            project_dirs,
            chart_best=best_chart,
            image_best=best_image,
            global_best=best_global,
        )
        _write_failed_tasks(project_dirs, getattr(rm, "failed_tasks", []), model_dir=model_dir)

        print(f"[eval] done model={model_name} projects={len(project_dirs)}")


if __name__ == "__main__":
    main()
