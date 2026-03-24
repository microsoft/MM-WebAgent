import os
import sys
import json
import yaml
import argparse
from pathlib import Path

# Ensure repo root is first on sys.path so we import local `planner/` and `utils/`,
# not similarly-named installed packages.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from planner import GenerationManager
from planner.config import API_CONFIG

def _load_jsonl_inputs(path: str) -> list[str]:
    out: list[str] = []
    file_ids = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            out.append(str(obj.get("input", obj.get("instruction", ""))).strip())
            file_ids.append(str(obj.get("file_id", obj.get("id", ""))).strip())
    return out, file_ids


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="WebAgent official generation entrypoint.")
    ap.add_argument(
        "--data-path",
        default=str(repo_root / "datasets" / "evaluation_dataset.jsonl"),
        help="JSONL dataset file path; each row must contain an `input` field.",
    )
    ap.add_argument("--eval_ids", type=str, default=None, help="Path to eval IDs file.")
    ap.add_argument(
        "--save-dir",
        default="outputs/workflow_v3",
        help="Output directory. Projects are created as <save-dir>/001, <save-dir>/002, ...",
    )
    ap.add_argument("--main_name", default="main.html", help="Main file name for generated projects (default: main.html).")
    ap.add_argument("--api_config", type=str, default=None, help="Path to API config YAML file.")
    ap.add_argument("--start_idx", type=int, default=0, help="Start index of samples to generate.")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of samples to generate.")
    ap.add_argument("--planner-model", default="gpt-5.2", help="OpenAI model id for planning (default: gpt-5.2).")
    ap.add_argument("--planner-workers", type=int, default=8, help="Parallel planner calls (default: 8).")
    ap.add_argument("--planner-timeout", type=int, default=180, help="Planner request timeout seconds (default: 180).")
    ap.add_argument("--planner-sleep-time", type=int, default=5, help="Sleep seconds between planner retries (default: 5).")
    ap.add_argument("--planner-max-tokens", type=int, default=8192, help="Planner max tokens (default: 8192).")
    ap.add_argument("--enable-video", action="store_true", help="Enable real video generation (mp4) instead of png previews.")
    ap.add_argument("--agent-version", default="AGENTS_PROMPT_V5", help="Agent version for planning (default: AGENTS_PROMPT_V5).")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.api_config:
        with open(args.api_config, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        api_config = data["api_config"]
        print(f"[config] Loaded API config from {args.api_config}")
        args.planner_model = api_config["plain"][0]
    else:
        api_config = API_CONFIG

    data_path = str(Path(args.data_path).expanduser())
    user_prompts, loaded_filed_ids = _load_jsonl_inputs(data_path)
    if loaded_filed_ids and loaded_filed_ids[0]:
        print(f"[data] Loaded {len(loaded_filed_ids)} cases from {data_path}")
        file_ids = loaded_filed_ids
    else:
        file_ids = [str(i + 1).zfill(3) for i in range(len(user_prompts))]

    user_prompts = user_prompts[args.start_idx :]
    file_ids = file_ids[args.start_idx :]

    if args.eval_ids is not None:
        with open(args.eval_ids, "r") as f:
            keep_proj_names = [line.strip() for line in f.readlines()]
        new_user_prompts = []
        new_file_ids = []
        for prompt, fid in zip(user_prompts, file_ids):
            if fid in keep_proj_names:
                new_user_prompts.append(prompt)
                new_file_ids.append(fid)
        print(f"[data] Selected {len(new_user_prompts)} cases from {len(user_prompts)} via eval IDs")
        user_prompts = new_user_prompts
        file_ids = new_file_ids


    if args.limit is not None:
        user_prompts = user_prompts[: args.limit]
        file_ids = file_ids[: args.limit]
    if not user_prompts:
        raise SystemExit(f"No prompts loaded from {data_path}")

    print(
        f"[run] Starting generation for {len(user_prompts)} case(s) "
        f"-> save_dir={args.save_dir} planner_model={args.planner_model}"
    )
    
    manager = GenerationManager(
        save_dir=args.save_dir,
        user_prompts=user_prompts,
        file_ids=file_ids,
        api_config=api_config,
        main_name=args.main_name,
        planner_api_model=args.planner_model,
        planner_max_workers=args.planner_workers,
        planner_timeout=args.planner_timeout,
        planner_sleep_time=args.planner_sleep_time,
        planner_max_tokens=args.planner_max_tokens,
        enable_video=bool(args.enable_video),
        agent_version=args.agent_version,
        debug=bool(args.debug),
    )

    manager._summerize_task_queue()
    manager.run_all_tasks()
    print(f"[done] Generation finished. Outputs saved under {args.save_dir}")


if __name__ == "__main__":
    main()
