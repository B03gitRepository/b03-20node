"""场景读取与严格校验。"""

from .loader import load_scenario
from .validation import ScenarioValidationError

__all__ = ["load_scenario", "ScenarioValidationError"]
