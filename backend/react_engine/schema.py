"""Shared types for the ReAct engine: step trace records and previews.

The JSON/XML decision-parsing fallbacks that used to live here (parse_decision,
ReactDecision, <call_agent> XML recovery) were removed once every loop user
moved to the native tool-call protocol — the engine's terminal condition is
"no tool call", and providers deliver structured tool_calls directly.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field


class ReactStepRecord(BaseModel):
    step: int
    thought: str = ""
    action: str
    input_preview: str = ""
    observation_preview: str = ""
    phase: str = "end"
    timestamp: float = Field(default_factory=time.time)
    # Position of this tool call within its step (0 for the common single-call
    # case). Steps with 2+ tool calls need this to keep their trace-part ids
    # distinct — see tracing.trace_parts.step_part.
    call_index: int = 0


def preview(value: Any, limit: int = 400) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text
