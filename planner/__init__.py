from .reflection import validate_parsed_plan

# Keep package imports lightweight: some modules require optional heavy deps
# (e.g. playwright sync, decord, openai).
try:
    from .task_manager import GenerationManager, GenerationManagerV2  # type: ignore
except Exception:
    GenerationManager = None  # type: ignore
    GenerationManagerV2 = None  # type: ignore

try:
    from .evaluation_manager import EvaluationManager  # type: ignore
except Exception:
    EvaluationManager = None  # type: ignore

try:
    from .reflection_manager import ReflectionManager  # type: ignore
except Exception:
    ReflectionManager = None  # type: ignore
