"""Flow registry utilities.

Provides a single source of truth for guided flow step lists so API handlers
can avoid hardcoding flow-specific imports and branches.
"""

from __future__ import annotations

from importlib import import_module
from typing import Dict, List, Optional, Tuple

# flow_name -> (module_path, class_name)
_STATIC_FLOW_CLASSES: Dict[str, Tuple[str, str]] = {
    "personal_accident": ("src.chatbot.flows.personal_accident", "PersonalAccidentFlow"),
    "travel_insurance": ("src.chatbot.flows.travel_insurance", "TravelInsuranceFlow"),
    "motor_private": ("src.chatbot.flows.motor_private", "MotorPrivateFlow"),
    "serenicare": ("src.chatbot.flows.serenicare", "SerenicareFlow"),
}


def get_flow_steps(flow_name: Optional[str]) -> Optional[List[str]]:
    """Return static step names for a guided flow when available.

    Returns:
    - list[str] for known static flows
    - None for dynamic/unknown flows
    """
    name = str(flow_name or "").strip().lower()
    if not name:
        return None

    if name == "journey":
        return None

    class_ref = _STATIC_FLOW_CLASSES.get(name)
    if not class_ref:
        return None

    module_path, class_name = class_ref
    module = import_module(module_path)
    flow_class = getattr(module, class_name)
    steps = getattr(flow_class, "STEPS", None)

    if not isinstance(steps, list):
        return None

    return list(steps)
