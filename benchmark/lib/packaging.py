from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from .dataset import DatasetItem


def ensure_project_dir(
    *,
    project_dir: str | Path,
    item: DatasetItem,
    html: str,
    gen_meta: Dict[str, Any],
    main_name: str = "main.html",
) -> Path:
    """
    Create a project directory compatible with EvaluationManager/ReflectionManager.

    Required files:
    - planner_output.json: must contain `user_prompt` for utils/mm_utils.py:get_user_prompt
    - main.html: HTML to evaluate
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "planner_output.json").write_text(
        json.dumps({"user_prompt": item.prompt, "case_id": item.case_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / main_name).write_text(html, encoding="utf-8")
    (project_dir / "gen_meta.json").write_text(
        json.dumps({"case_id": item.case_id, **gen_meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "case.json").write_text(json.dumps(item.raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return project_dir
