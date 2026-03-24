import ast
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None


DEFAULT_API_CONFIG_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "configs" / "api_config.yaml"


def _parse_simple_api_config_yaml(text: str) -> dict:
    data: dict[str, dict[str, list[object]]] = {}
    current_section: str | None = None
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not line.startswith(" "):
            if stripped != "api_config:":
                raise ValueError(f"Unsupported top-level key in API config: {stripped}")
            data["api_config"] = {}
            current_section = "api_config"
            current_key = None
            continue

        if current_section != "api_config":
            raise ValueError("Unsupported YAML structure in API config")

        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            current_key = stripped[:-1]
            data["api_config"][current_key] = []
            continue

        if line.startswith("    - "):
            if current_key is None:
                raise ValueError("Found list item before task type in API config")
            value_text = stripped[2:].strip()
            try:
                value = ast.literal_eval(value_text)
            except Exception:
                value = value_text.strip("\"'")
            data["api_config"][current_key].append(value)
            continue

        raise ValueError(f"Unsupported YAML line in API config: {raw_line}")

    return data


def load_api_config(path: str | Path | None = None) -> dict[str, tuple[str, int]]:
    config_path = Path(path).expanduser() if path is not None else DEFAULT_API_CONFIG_PATH
    config_text = config_path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(config_text) or {}
    else:
        data = _parse_simple_api_config_yaml(config_text)

    raw_api_config = data.get("api_config")
    if not isinstance(raw_api_config, dict):
        raise ValueError(f"Invalid API config file: missing `api_config` mapping in {config_path}")

    api_config: dict[str, tuple[str, int]] = {}
    for task_type, config_info in raw_api_config.items():
        if not isinstance(config_info, (list, tuple)) or len(config_info) != 2:
            raise ValueError(
                f"Invalid API config entry for `{task_type}` in {config_path}: expected [model, max_workers]"
            )
        api_model, max_workers = config_info
        api_config[str(task_type)] = (str(api_model), int(max_workers))

    return api_config

# agent configuration (api_model, max_workers)
API_CONFIG = load_api_config()

MAX_RETRIES = 5
TIMEOUT = 500
SLEEP_TIME = 30


@dataclass
class MetaQuery:
    type: str
    extra_info: dict = None
    save_path: str = None
    project_dir: str = None
    format_err: bool = False
    score: float = None
    file_id: str = None
