from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class DatasetItem:
    case_id: str
    prompt: str
    raw: dict


def iter_tests_jsonl(path: str | Path, *, limit: Optional[int] = None) -> Iterator[DatasetItem]:
    """
    Reads a benchmark JSONL file and yields items using the `input` field as prompt.

    case_id rule:
    - Sequential numbering starting from 0001 for each yielded (non-empty) prompt.
    - Intentionally ignores fields like `reference_image` to avoid duplicate/misaligned IDs.
    """
    path = Path(path)
    yielded = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            prompt = (raw.get("input",  raw.get("instruction", ""))).strip()
            if not prompt:
                continue
            yielded += 1
            case_id = f"{yielded:03d}"
            case_id = str(raw.get("file_id", raw.get("id", case_id))).strip()
            yield DatasetItem(case_id=case_id, prompt=prompt, raw=raw)
            if limit is not None and yielded >= limit:
                return
