"""Validate agent-authored join intent before any physical execution.

The LinkingAgent remains responsible for choosing datasets, keys, strategy,
threshold, order, and join type.  This module is the deterministic boundary:
it converts that proposal into the versioned connector-neutral contract and
rejects invented datasets, columns, strategies, or disconnected plans.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from datasources.contracts import (
    DataFreshness,
    DataScope,
    JoinInput,
    JoinRuntimeInput,
    JoinSpec,
    JoinStep,
    JoinStrategy,
    ResolvedJoinPlan,
    resolve_join_plan,
)
from datasources.source_identity import source_key

CURRENT_STRATEGIES = frozenset(
    {
        JoinStrategy.EXACT,
        JoinStrategy.LEVENSHTEIN,
        JoinStrategy.JARO_WINKLER,
        JoinStrategy.TOKEN_SORT,
        JoinStrategy.TOKEN_SET,
        JoinStrategy.PARTIAL_RATIO,
        JoinStrategy.COSINE_TFIDF,
        JoinStrategy.SOUNDEX,
        JoinStrategy.METAPHONE,
    }
)


class AgentJoinPlanError(ValueError):
    """The agent proposal is not a safe, executable join intent."""


def _context_name(ctx: Any) -> str:
    return str(getattr(ctx, "filename", "") or "").strip()


def _columns(ctx: Any) -> set[str]:
    frame = getattr(ctx, "working_dataframe", None)
    if frame is None:
        frame = getattr(ctx, "dataframe", None)
    return {str(column) for column in getattr(frame, "columns", ())}


def validate_agent_join_plan(
    contexts: Sequence[Any], raw_steps: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], JoinSpec]:
    """Return a normalized plan plus its typed intent contract."""
    names = [_context_name(ctx) for ctx in contexts]
    if len(names) < 2:
        raise AgentJoinPlanError("At least two datasets are required.")
    if any(not name for name in names) or len(set(names)) != len(names):
        raise AgentJoinPlanError("Dataset names must be present and unique before joining.")
    if not raw_steps:
        raise AgentJoinPlanError("The proposed join plan has no steps.")

    steps: list[JoinStep] = []
    for index, raw in enumerate(raw_steps):
        payload = dict(raw)
        payload["strategy"] = str(payload.get("strategy") or "exact").strip().lower()
        if payload["strategy"] == "full":
            payload["strategy"] = "exact"
        try:
            step = JoinStep.model_validate(payload)
        except Exception as exc:  # Pydantic keeps precise field context in str(exc)
            raise AgentJoinPlanError(f"Join step {index + 1} is invalid: {exc}") from exc
        if step.strategy not in CURRENT_STRATEGIES:
            raise AgentJoinPlanError(
                f"Join strategy '{step.strategy.value}' is not in the current parity scope."
            )
        if step.strategy != JoinStrategy.EXACT and len(step.key_pairs) != 1:
            raise AgentJoinPlanError("Lexical matching supports exactly one key pair per step.")
        steps.append(step)

    try:
        spec = JoinSpec(
            inputs=tuple(JoinInput(source_id=name, label=name) for name in names),
            steps=tuple(steps),
        )
    except Exception as exc:
        raise AgentJoinPlanError(f"The proposed join graph is invalid: {exc}") from exc

    def input_name(ref: str | int) -> str | None:
        if isinstance(ref, int):
            return names[ref] if 0 <= ref < len(names) else None
        return ref if ref in set(names) else None

    joined: set[str] = set()
    for index, step in enumerate(spec.steps):
        left_name, right_name = input_name(step.left), input_name(step.right)
        if index == 0:
            if left_name is None or right_name is None:
                raise AgentJoinPlanError("The first join step must connect two input datasets.")
            joined.update((left_name, right_name))
            continue
        left_joined = (
            left_name in joined if left_name is not None else step.left == f"_result_{index - 1}"
        )
        right_joined = (
            right_name in joined if right_name is not None else step.right == f"_result_{index - 1}"
        )
        left_new = left_name is not None and left_name not in joined
        right_new = right_name is not None and right_name not in joined
        if not ((left_joined and right_new) or (right_joined and left_new)):
            raise AgentJoinPlanError(
                f"Join step {index + 1} must add exactly one new dataset to the prior result."
            )
        joined.add(left_name if left_new else right_name)  # type: ignore[arg-type]

    by_name = {name: ctx for name, ctx in zip(names, contexts)}
    for index, step in enumerate(spec.steps):
        for side, ref in (("left", step.left), ("right", step.right)):
            if isinstance(ref, int):
                ctx = contexts[ref]
            elif ref in by_name:
                ctx = by_name[ref]
            else:
                # Intermediate result schemas are created by earlier steps and
                # are validated by the executor at that boundary.
                continue
            key_names = [pair.left if side == "left" else pair.right for pair in step.key_pairs]
            missing = [key for key in key_names if key not in _columns(ctx)]
            if missing:
                raise AgentJoinPlanError(
                    f"Join step {index + 1} references missing {side} key(s) "
                    f"{missing!r} in '{_context_name(ctx)}'."
                )

    normalized = [step.model_dump(mode="json", exclude_none=True) for step in spec.steps]
    for payload in normalized:
        if not payload.get("keys"):
            payload.pop("keys", None)
    return normalized, spec


def resolve_agent_join_plan(contexts: Sequence[Any], spec: JoinSpec) -> ResolvedJoinPlan:
    """Resolve intent from credential-free runtime facts for confirmation/UI."""
    runtime: list[JoinRuntimeInput] = []
    for ctx, item in zip(contexts, spec.inputs):
        meta = getattr(ctx, "metadata", None) or {}
        connector = str(meta.get("source") or "upload")
        connected = connector != "upload"
        identity = source_key(meta)
        # A connected input with unavailable credentials remains source-backed;
        # a unique unresolved key prevents it from masquerading as an upload or
        # accidentally matching another redacted connection.
        if connected and identity is None:
            identity = f"unresolved:{getattr(ctx, 'file_id', item.source_id)}"
        runtime.append(
            JoinRuntimeInput(
                source_id=item.source_id,
                source_backed=connected,
                source_key=identity,
                scope=DataScope.FULL,
                freshness=DataFreshness.LIVE if connected else DataFreshness.SNAPSHOT,
            )
        )
    return resolve_join_plan(spec, runtime)


__all__ = [
    "AgentJoinPlanError",
    "CURRENT_STRATEGIES",
    "resolve_agent_join_plan",
    "validate_agent_join_plan",
]
