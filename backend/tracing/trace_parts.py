"""
Stable trace "parts" — the single source of truth for the live agent trace.

The reasoning trace is modelled as an ordered, append-only list of *parts*. Each
part has a **stable id** and an optional **parentId** that links a sub-agent's
steps to the orchestrator ``call_agent`` step that spawned them. This replaces the
old integer-``step``-keyed scheme (which collided across sequential sub-agents) and
the arrival-order nesting heuristic on the client.

IDs are deterministic and stable across the multiple emissions of one logical step
(``start`` → ``end``/``final``), so re-emitting a step upserts the same part rather
than creating a duplicate. Events arrive over a single ordered queue, so the client
preserves **insertion order** — no time-encoded ids or client-side sorting needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover
    from agents.progress import ProgressEmitter
    from react_engine.schema import ReactStepRecord

ORCH_PART_PREFIX = "orch"


def orch_part_id(step: int) -> str:
    """Stable id for an orchestrator step part (the parent of its sub-agent steps)."""
    return f"{ORCH_PART_PREFIX}-{step}"


def step_part(
    *,
    agent: str,
    step: int,
    phase: str,
    thought: str = "",
    action: str = "",
    tool_input: Any = "",
    observation: Any = "",
    part_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    call_index: int = 0,
) -> Dict[str, Any]:
    """
    Build a ``type:"step"`` trace part.

    ``part_id`` defaults to ``f"{parent_id}-{step}"`` (sub-agent steps, unique per
    parent so two sub-agents reusing local step numbers never collide) or
    ``f"{agent}-{step}"`` for a top-level part with no parent. A step can carry
    more than one tool call (native multi-tool-call turns, parallel-safe or
    not); ``call_index`` (position within the step, 0 for the first/only call)
    is appended so those calls get distinct ids instead of colliding on the
    same one and overwriting each other's start/end events.
    """
    base_id = part_id or (f"{parent_id}-{step}" if parent_id else f"{agent}-{step}")
    pid = base_id if call_index == 0 else f"{base_id}-{call_index}"
    return {
        "id": pid,
        "parentId": parent_id,
        "agent": agent,
        "type": "step",
        "step": step,
        "phase": phase,
        "thought": thought or "",
        "action": action or "",
        "input": tool_input,
        "observation": observation,
    }


async def emit_react_part(
    progress_emitter: Optional["ProgressEmitter"],
    agent: str,
    rec: "ReactStepRecord",
    *,
    parent_id: Optional[str] = None,
) -> None:
    """
    Emit a trace part from a ``ReactStepRecord`` — the shared emitter shape used by
    every sub-agent's loop. ``parent_id`` (read from ``_active_parent_part_id``) links
    the step to the orchestrator ``call_agent`` part that dispatched this sub-agent.
    """
    if progress_emitter is None:
        return
    await progress_emitter.emit_part(
        step_part(
            agent=agent,
            step=rec.step,
            phase=rec.phase,
            thought=rec.thought,
            action=rec.action,
            tool_input=rec.input_preview,
            observation=rec.observation_preview,
            parent_id=parent_id,
            call_index=rec.call_index,
        )
    )
