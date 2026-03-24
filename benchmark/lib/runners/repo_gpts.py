from __future__ import annotations

import time
from dataclasses import dataclass
import json
from typing import Any, Dict, Tuple

import importlib.util
from pathlib import Path


def _load_run_gpts():
    """
    Load `utils/run_gpts.py` without importing the `utils` package (which may import optional deps).
    """
    repo_root = Path(__file__).resolve().parents[3]
    mod_path = repo_root / "utils" / "run_gpts.py"
    spec = importlib.util.spec_from_file_location("_webagent_run_gpts", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load run_gpts module from {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


@dataclass(frozen=True)
class RepoGPTSConfig:
    """
    Uses the repo's existing OpenAI calling stack in `utils/run_gpts.py`.

    `model` is forwarded to `utils.get_openai_request_config(model)`, e.g. "gpt-5.2", "gpt-4.1".
    """

    model: str
    max_tokens: int = 8192
    temperature: float = 0.2
    timeout_s: int = 300
    max_retries: int = 3
    sleep_s: int = 3
    reasoning_effort: str | None = None
    log_path: str | None = None


class RepoGPTSRunner:
    def __init__(self, cfg: RepoGPTSConfig, *, debug: bool = False):
        self.cfg = cfg
        self.debug = debug
        self._run_gpts = _load_run_gpts()

    def chat(self, *, system: str, user: str) -> Tuple[str, Dict[str, Any]]:
        request_cfg = self._run_gpts.get_openai_request_config(self.cfg.model)
        url = request_cfg["url"]
        headers = request_cfg["headers"]
        model = request_cfg["model"]
        t0 = time.time()
        text, success = self._run_gpts.request_chatgpt_t2t_until_success(
            user,
            system,
            url,
            headers,
            model=model,
            max_tokens=self.cfg.max_tokens,
            debug=self.debug,
            timeout=self.cfg.timeout_s,
            max_retries=self.cfg.max_retries,
            sleep_time=self.cfg.sleep_s,
            reasoning_effort=self.cfg.reasoning_effort,
            log_path=self.cfg.log_path,
        )
        dt = time.time() - t0
        if not success or not isinstance(text, str):
            err_preview = ""
            if isinstance(text, dict):
                try:
                    err_preview = json.dumps(text, ensure_ascii=False)[:2000]
                except Exception:
                    err_preview = str(text)[:2000]
            else:
                err_preview = str(text)[:2000]
            raise RuntimeError(f"RepoGPTS call failed model={self.cfg.model} error={err_preview}")
        meta = {
            "backend": "repo_gpts",
            "model": self.cfg.model,
            "latency_s": round(dt, 3),
        }
        return text, meta
