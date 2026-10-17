"""
Unified, transcript-driven agent loop (the single ReAct engine).

This replaces the four hand-rolled loops (orchestrator, analysis, cleaning,
integration) with one engine that drives an **append-only native-tool-call
transcript**:

    system → user → assistant(tool_calls) → tool(result) → assistant(tool_calls)
    → tool(result) → … → assistant(plain text = final answer)

Design choices (SOTA-aligned):

* **No `final_answer` pseudo-tool.** The terminal condition is "the model
  responded with no tool call" — the convention used by Claude Code / opencode /
  Codex and already by this repo's orchestrator and integration agents.
* **Native tool protocol.** Assistant turns carry real ``tool_calls`` (with ids);
  results come back as ``role:"tool"`` messages correlated by ``tool_call_id``.
  This is what models are trained on and what makes prompt caching engage.
* **Append-only + compaction.** The transcript only grows; a token-budget guard
  (``compaction.maybe_compact``) shrinks old message *content* when nearing the
  context window, never breaking the assistant/tool pairing.
* **Pause/resume invariant.** The *persisted* transcript is always API-valid: an
  in-flight assistant tool-call turn is stashed in a ``pending`` record (out of
  the transcript) until the user answers, then replayed on resume. This unifies
  the bespoke HITL machinery of every agent.

The engine is provider-agnostic: it calls ``llm_router.generate`` /
``generate_stream`` and works with any OpenAI-compatible provider. (Google's
text-only provider does not support tools and is unsupported for loops.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Collection, Dict, List, Optional

from llm.models import LLMRequest, Message
from llm.usage_limit import check_usage_limit
from memory.context_window import DEFAULT_OUTPUT_CAP, get_max_output_tokens

from .compaction import maybe_compact
from .schema import ReactStepRecord, preview
from .toolkit import Toolkit

logger = logging.getLogger(__name__)

# Executor returns an observation string for a single tool call.
ToolExecutorFn = Callable[[str, Any, str], Awaitable[str]]
# Optional emitter for SSE step events.
EmitterFn = Callable[[ReactStepRecord], Awaitable[None]]
# Optional finalizer: transform the terminal text + transcript into the result dict.
FinalizerFn = Callable[[str, List[Message]], Awaitable[Dict[str, Any]]]
# Optional summarizer when the step budget is exhausted.
BudgetSummarizerFn = Callable[[List[Message]], Awaitable[str]]

TRANSCRIPT_VERSION = "v2-native"

# Engine-level backstops on tool observations. Tools self-cap (3-6K chars), but
# the engine is the guarantee: every observation lands in the append-only
# transcript and is re-sent on every subsequent step of the loop.
MAX_OBSERVATION_CHARS = 6_000
# Aggregate cap across all parallel tool results committed in a single step —
# N parallel calls must not multiply the per-observation cap unbounded.
MAX_STEP_OBSERVATIONS_CHARS = 16_000


def _clip_observation(obs: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if len(obs) <= limit:
        return obs
    return obs[:limit] + f"\n… [truncated, {len(obs) - limit:,} chars omitted]"


def _enforce_step_budget(sibling_results: List[Dict[str, str]]) -> None:
    """Shrink the largest observations first until the step total fits the budget."""
    total = sum(len(s["content"]) for s in sibling_results)
    if total <= MAX_STEP_OBSERVATIONS_CHARS:
        return
    # Floor per observation so nothing is truncated into uselessness.
    floor = 1_000
    for sib in sorted(sibling_results, key=lambda s: len(s["content"]), reverse=True):
        if total <= MAX_STEP_OBSERVATIONS_CHARS:
            break
        excess = total - MAX_STEP_OBSERVATIONS_CHARS
        target = max(len(sib["content"]) - excess, floor)
        if target < len(sib["content"]):
            total -= len(sib["content"]) - target
            sib["content"] = _clip_observation(sib["content"], target)


def output_cap_from_config(config: Optional[Dict[str, Any]]) -> Optional[int]:
    """Read llm.react_output_cap from a loaded config dict.

    Returns ``None`` when unset so ``run_agent_loop`` falls back to its
    model-aware default (``DEFAULT_OUTPUT_CAP``).
    """
    if not config:
        return None
    val = (config.get("llm") or {}).get("react_output_cap")
    return int(val) if val else None


def transcript_budget_from_config(config: Optional[Dict[str, Any]]) -> Optional[int]:
    """Read llm.react_transcript_budget_tokens from a loaded config dict.

    Returns ``None`` when unset so compaction falls back to its default
    (``compaction.DEFAULT_MAX_TRANSCRIPT_TOKENS``).
    """
    if not config:
        return None
    val = (config.get("llm") or {}).get("react_transcript_budget_tokens")
    return int(val) if val else None


class AgentLoopResult(dict):
    """Result dict: status, response, metadata (and transcript under metadata)."""


class PauseSignal(Exception):
    """
    Raised by a tool executor to request human-in-the-loop input.

    The engine catches this, stashes the in-flight assistant turn, and returns a
    result with ``status="awaiting_user_action"`` and the supplied action_prompt.
    On the next turn the caller resumes the loop, supplying the user's answer as
    the paused tool's result.
    """

    def __init__(self, action_prompt: Dict[str, Any], *, response: str = "") -> None:
        super().__init__(action_prompt.get("question", "Awaiting user input"))
        self.action_prompt = action_prompt
        self.response = response or action_prompt.get("question", "")


# ---------------------------------------------------------------------------
# Transcript (de)serialization for cross-turn persistence in state.metadata
# ---------------------------------------------------------------------------


def serialize_transcript(transcript: List[Message]) -> List[Dict[str, Any]]:
    return [m.model_dump(exclude_none=True) for m in transcript]


def deserialize_transcript(raw: Any) -> List[Message]:
    if not isinstance(raw, list):
        return []
    out: List[Message] = []
    for d in raw:
        try:
            out.append(Message.model_validate(d))
        except Exception:
            logger.warning("[loop] dropping un-deserializable transcript message")
    return out


def _normalize_tool_calls(raw_tool_calls: List[Dict[str, Any]], step: int) -> List[Dict[str, Any]]:
    """
    Convert provider tool_calls ([{"id","name","arguments": dict}]) into the
    OpenAI request shape used on assistant messages, synthesizing an id when the
    provider omitted one (so the assistant call and its tool result correlate).
    """
    normalized: List[Dict[str, Any]] = []
    for idx, tc in enumerate(raw_tool_calls):
        call_id = tc.get("id") or f"call_{step}_{idx}_{uuid.uuid4().hex[:8]}"
        args = tc.get("arguments", {})
        args_str = args if isinstance(args, str) else json.dumps(args, default=str)
        normalized.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tc.get("name", ""), "arguments": args_str},
            }
        )
    return normalized


def _tool_args(tc: Dict[str, Any]) -> Any:
    """Parse a normalized assistant tool_call's arguments back to a Python object."""
    raw = tc.get("function", {}).get("arguments", "")
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return raw


def _truncated_args_error(
    truncated: bool,
    name: str,
    args: Any,
    required_by_tool: Dict[str, set],
) -> Optional[str]:
    """Return a recoverable observation if a tool call's args were truncated.

    A turn that ends with ``finish_reason == "length"`` whose tool-call arguments
    are missing a required field is almost certainly truncated mid-JSON (the
    provider coerced the unparseable arguments to ``{}``). Tell the model what
    happened and how to recover, rather than letting the tool fail on the missing
    field — which the model would otherwise retry identically and truncate again.
    """
    if not truncated:
        return None
    required = required_by_tool.get(name, set())
    present = set(args.keys()) if isinstance(args, dict) else set()
    missing = required - present
    if not missing:
        return None
    return (
        f"ERROR: Your `{name}` tool call was truncated at the output token limit, so its "
        f"arguments are incomplete (missing: {sorted(missing)}). Do not retry the same call. "
        "If you are writing a file, write it in smaller pieces (multiple write_file calls, or "
        "append), or generate it from inside run_script instead of passing the full content as "
        "a tool argument."
    )


# ---------------------------------------------------------------------------
# Repeat-failure guard
# ---------------------------------------------------------------------------


def _looks_like_error(obs: str) -> bool:
    """Heuristic over the two error conventions used by tool executors."""
    head = obs.lstrip()[:200]
    return head.startswith("ERROR") or '"error"' in head or '"validation_errors"' in head


class _RepeatFailureGuard:
    """
    Detect the model re-issuing an identical failing tool call.

    Without this, a stuck model repeats the same call until max_steps burns
    out, each retry appending another error to the append-only transcript. At
    ``WARN_AT`` identical failures the observation gains escalating guidance;
    at ``ABORT_AT`` the loop ends with status="partial".
    """

    WARN_AT = 3
    ABORT_AT = 5

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}

    @staticmethod
    def _fingerprint(name: str, args: Any) -> str:
        try:
            canon = json.dumps(args, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            canon = str(args)
        return f"{name}:{canon}"

    def note(self, name: str, args: Any, obs: str) -> tuple[str, bool]:
        """Return ``(observation, abort)``; may append guidance to *obs*."""
        fp = self._fingerprint(name, args)
        if not _looks_like_error(obs):
            self._counts.pop(fp, None)
            return obs, False
        n = self._counts.get(fp, 0) + 1
        self._counts[fp] = n
        if n >= self.ABORT_AT:
            return obs, True
        if n >= self.WARN_AT:
            obs += (
                f"\n\n[SYSTEM] This exact `{name}` call has now failed {n} times with the "
                "same error. Do NOT repeat it. Change the arguments or approach, or explain "
                "the blocker in a final answer."
            )
        return obs, False


# ---------------------------------------------------------------------------
# Provider call (shared by streaming and non-streaming paths)
# ---------------------------------------------------------------------------


async def _generate(
    llm_router: Any,
    request: LLMRequest,
    *,
    stream_final: bool,
    emit_token: Optional[Callable[[str], Awaitable[None]]],
) -> tuple[str, List[Dict[str, Any]], Optional[str]]:
    """
    Run one model turn. Returns ``(content, raw_tool_calls, finish_reason)``.

    ``finish_reason`` is surfaced so the caller can detect an output-token
    truncation (``"length"``), which silently corrupts a tool call's JSON
    arguments (they get coerced to ``{}`` in the provider layer).

    When ``stream_final`` and an ``emit_token`` callback is supplied, plain-text
    tokens are emitted live (so a final answer streams to the UI). As soon as a
    tool-call delta is seen, token emission stops — the already-streamed prose
    was the model's "thinking" before a tool call.
    """
    check_usage_limit()
    if not (stream_final and emit_token):
        resp = await llm_router.generate(request)
        # Strip inline <think>...</think> blocks that local models (e.g. Qwen3)
        # embed in the content field of non-streaming responses.
        raw_content = resp.content or ""
        clean_content = re.sub(
            r"<think>.*?</think>", "", raw_content, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        return (
            clean_content,
            (resp.metadata or {}).get("tool_calls") or [],
            getattr(resp, "finish_reason", None),
        )

    try:
        content = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason: Optional[str] = None
        is_tool_turn = False

        async for chunk in llm_router.generate_stream(request):
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            meta = chunk.metadata or {}
            if meta.get("tool_calls") is not None:
                tool_calls = meta["tool_calls"]
                break
            if meta.get("is_tool_call"):
                is_tool_turn = True
                continue
            # Skip thinking/reasoning chunks — Qwen3 and local models emit these
            # with is_thinking=True.  Including them would pollute the final-answer
            # text and make the model appear to have answered without calling a tool.
            if meta.get("is_thinking"):
                continue
            if chunk.content and not is_tool_turn:
                # Belt-and-suspenders: strip residual <think> tags that leaked
                # through as regular content chunks before the provider fix.
                safe = re.sub(
                    r"<think>.*?</think>", "", chunk.content, flags=re.DOTALL | re.IGNORECASE
                )
                safe = re.sub(r"<think>.*", "", safe, flags=re.DOTALL | re.IGNORECASE)
                if safe:
                    content += safe
                    await emit_token(safe)
        return content, tool_calls, finish_reason
    except Exception as exc:  # pragma: no cover - streaming fallback
        logger.warning("[loop] streaming failed, falling back to generate(): %s", exc)
        resp = await llm_router.generate(request)
        return (
            resp.content or "",
            (resp.metadata or {}).get("tool_calls") or [],
            getattr(resp, "finish_reason", None),
        )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


async def run_agent_loop(
    *,
    llm_router: Any,
    tools: Optional[List[Dict[str, Any]]] = None,
    toolkit: Optional[Toolkit] = None,
    transcript: List[Message],
    tool_executor: Optional[ToolExecutorFn] = None,
    parallel_safe_tools: Optional[Collection[str]] = None,
    max_steps: int,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
    output_token_cap: Optional[int] = None,
    emitter: Optional[EmitterFn] = None,
    finalizer: Optional[FinalizerFn] = None,
    budget_summarizer: Optional[BudgetSummarizerFn] = None,
    stream_final: bool = False,
    emit_token: Optional[Callable[[str], Awaitable[None]]] = None,
    stream_retract: Optional[Callable[[], Awaitable[None]]] = None,
    budget_fraction: float = 0.70,
    transcript_token_budget: Optional[int] = None,
    resume: Optional[Dict[str, Any]] = None,
    resume_observation: Optional[str] = None,
    start_step: int = 0,
) -> AgentLoopResult:
    """
    Drive the agent to completion (or a HITL pause / step-budget exhaustion).

    Parameters
    ----------
    transcript:
        Seed messages — ``[system, user]`` for a fresh run. Ignored when
        ``resume`` is provided (the persisted transcript is restored instead).
    toolkit:
        Preferred wiring: a ``Toolkit`` supplies the schemas, the executor, and
        the parallel-safe set in one object. ``tools``/``tool_executor`` are the
        legacy split and are ignored when ``toolkit`` is given.
    tool_executor:
        ``async (name, args, call_id) -> observation_str``. Raise ``PauseSignal``
        to request HITL.
    parallel_safe_tools:
        Tool names with no raceable side effects. A multi-call step whose calls
        are all parallel-safe executes concurrently.
    resume / resume_observation:
        When resuming a paused loop, ``resume`` is the previously returned
        ``pending`` record and ``resume_observation`` is the tool-result content
        derived from the user's answer (e.g. "User approved; cleaning done: …").
    stream_retract:
        Called when a ``stream_final`` turn streamed some content via
        ``emit_token`` but then turned out to carry tool calls — i.e. the
        streamed text was the model's pre-tool-call preamble, not a final
        answer. The caller should tell the UI to discard whatever it just
        rendered from that stream.

    Returns an ``AgentLoopResult`` with ``status`` in
    {"completed", "awaiting_user_action", "partial", "error"} and
    ``metadata["transcript"]`` carrying the serialized transcript.
    """
    step = start_step

    if toolkit is not None:
        tools = toolkit.schemas()
        tool_executor = toolkit.execute
        if parallel_safe_tools is None:
            parallel_safe_tools = toolkit.parallel_safe_names()
    if tool_executor is None:
        raise ValueError("run_agent_loop requires either toolkit or tool_executor")
    parallel_safe = frozenset(parallel_safe_tools or ())
    repeat_guard = _RepeatFailureGuard()

    # Resolve the per-step output budget once. An explicit max_tokens wins;
    # otherwise derive a model-aware cap so a tool call carrying a long file
    # `content` argument is not truncated mid-JSON (the silent {} failure).
    effective_max_tokens = max_tokens or get_max_output_tokens(
        model, output_token_cap or DEFAULT_OUTPUT_CAP
    )
    # required-args lookup per tool, for detecting truncation-corrupted calls.
    required_by_tool: Dict[str, set] = {}
    for t in tools or []:
        fn = t.get("function", t)
        params = fn.get("parameters", {}) or {}
        required_by_tool[fn.get("name", "")] = set(params.get("required", []) or [])

    # ---- Resume path: rebuild a valid transcript from the pending record ----
    if resume:
        transcript = deserialize_transcript(resume.get("transcript", []))
        pending = resume.get("pending", {})
        assistant_raw = pending.get("assistant")
        if assistant_raw:
            transcript.append(Message.model_validate(assistant_raw))
        # Sibling tool calls that ran before the pause keep their observations.
        for sib in pending.get("sibling_results", []):
            transcript.append(
                Message(role="tool", tool_call_id=sib["tool_call_id"], content=sib["content"])
            )
        # The paused tool's result becomes the user's decision.
        paused_id = pending.get("paused_call_id")
        if paused_id is not None:
            transcript.append(
                Message(
                    role="tool",
                    tool_call_id=paused_id,
                    content=resume_observation or "User responded.",
                )
            )
        # Any tool calls that were after the pause are re-run now.
        for rem in pending.get("remaining_calls", []):
            obs = await _safe_execute(tool_executor, rem["name"], rem["args"], rem["id"])
            transcript.append(
                Message(role="tool", tool_call_id=rem["id"], content=_clip_observation(obs))
            )
        step = int(pending.get("step", step))

    while step < max_steps:
        step_index = step + 1

        # Compaction preflight — shrink old content if over the transcript budget.
        transcript, _ = maybe_compact(
            transcript,
            model,
            budget_fraction=budget_fraction,
            max_transcript_tokens=transcript_token_budget,
        )

        request = LLMRequest(
            messages=transcript,
            model=model,
            temperature=temperature,
            max_tokens=effective_max_tokens,
            tools=tools,
            tool_choice="auto",
            metadata={"agent_name": agent_name} if agent_name else {},
        )
        content, raw_tool_calls, finish_reason = await _generate(
            llm_router,
            request,
            stream_final=stream_final,
            emit_token=emit_token,
        )
        truncated = finish_reason == "length"

        # A streamed turn that turns out to carry tool calls streamed its
        # pre-tool-call preamble to the UI as if it were the final answer —
        # tell the caller to retract it so a UI flag like "answer is
        # streaming" doesn't latch true for the rest of the run and hide
        # subsequent between-steps indicators.
        if stream_final and emit_token and raw_tool_calls and content.strip() and stream_retract:
            await stream_retract()

        # ---- Terminal: no tool call → the content is the final answer ----
        if not raw_tool_calls:
            transcript.append(Message(role="assistant", content=content))
            if emitter:
                await emitter(
                    ReactStepRecord(
                        step=step_index,
                        thought=content[:400],
                        action="final_answer",
                        phase="final",
                    )
                )
            if finalizer:
                result = await finalizer(content, transcript)
            else:
                result = {"status": "completed", "response": content, "metadata": {}}
            result.setdefault("metadata", {})["transcript"] = serialize_transcript(transcript)
            result["metadata"].setdefault("steps_taken", step_index)
            return AgentLoopResult(result)

        # ---- Tool turn: declare the assistant call, then execute ----
        norm_calls = _normalize_tool_calls(raw_tool_calls, step_index)
        assistant_msg = Message(role="assistant", content=content or None, tool_calls=norm_calls)

        sibling_results: List[Dict[str, str]] = []
        paused: Optional[PauseSignal] = None
        paused_index = -1
        abort_repeat: Optional[str] = None

        async def _emit_phase(
            name: str, args: Any, phase: str, obs: str = "", call_index: int = 0
        ) -> None:
            if emitter:
                await emitter(
                    ReactStepRecord(
                        step=step_index,
                        thought=content or "",
                        action=name,
                        input_preview=preview(args),
                        observation_preview=preview(obs, 1200) if obs else "",
                        phase=phase,
                        call_index=call_index,
                    )
                )

        def _note_repeat(name: str, args: Any, obs: str) -> str:
            nonlocal abort_repeat
            obs, abort = repeat_guard.note(name, args, obs)
            if abort:
                abort_repeat = name
            return obs

        # A multi-call step whose calls are all parallel-safe (and therefore
        # never HITL) executes concurrently; anything else keeps the strict
        # sequential order that PauseSignal resume semantics rely on.
        run_parallel = (
            len(norm_calls) > 1
            and bool(parallel_safe)
            and all(tc["function"]["name"] in parallel_safe for tc in norm_calls)
        )

        if run_parallel:

            async def _run_one(tc: Dict[str, Any]) -> str:
                name = tc["function"]["name"]
                args = _tool_args(tc)
                trunc_obs = _truncated_args_error(truncated, name, args, required_by_tool)
                if trunc_obs is not None:
                    return trunc_obs
                return _clip_observation(str(await tool_executor(name, args, tc["id"])))

            for idx, tc in enumerate(norm_calls):
                await _emit_phase(tc["function"]["name"], _tool_args(tc), "start", call_index=idx)
            gathered = await asyncio.gather(
                *(_run_one(tc) for tc in norm_calls), return_exceptions=True
            )
            for idx, (tc, res) in enumerate(zip(norm_calls, gathered)):
                name = tc["function"]["name"]
                args = _tool_args(tc)
                if isinstance(res, PauseSignal):
                    # parallel_safe tools should never pause; recover defensively.
                    res = (
                        "ERROR: tool requested a pause during a parallel batch; "
                        "call it again on its own."
                    )
                elif isinstance(res, BaseException):
                    res = f"ERROR calling {name}: {str(res)[:2000]}"
                res = _note_repeat(name, args, res)
                sibling_results.append({"tool_call_id": tc["id"], "content": res})
                await _emit_phase(name, args, "end", res, call_index=idx)
        else:
            for idx, tc in enumerate(norm_calls):
                name = tc["function"]["name"]
                args = _tool_args(tc)
                call_id = tc["id"]
                await _emit_phase(name, args, "start", call_index=idx)
                # Output-token truncation corrupts a tool call's JSON arguments — the
                # provider coerces them to {}. Detect it (finish_reason == "length" +
                # missing required fields) and feed back a recoverable instruction
                # instead of executing the broken call and dead-ending on a confusing
                # "missing path"-style error that the model then retries identically.
                trunc_obs = _truncated_args_error(truncated, name, args, required_by_tool)
                if trunc_obs is not None:
                    logger.warning(
                        "[loop] step %d: '%s' arguments truncated at output-token limit "
                        "(finish_reason=length); returning recoverable observation",
                        step_index,
                        name,
                    )
                    sibling_results.append({"tool_call_id": call_id, "content": trunc_obs})
                    await _emit_phase(name, args, "end", trunc_obs, call_index=idx)
                    continue
                try:
                    obs = await tool_executor(name, args, call_id)
                except PauseSignal as sig:
                    paused = sig
                    paused_index = idx
                    break
                obs = _note_repeat(name, args, _clip_observation(str(obs)))
                sibling_results.append({"tool_call_id": call_id, "content": obs})
                await _emit_phase(name, args, "end", obs, call_index=idx)

        # Aggregate cap: parallel tool calls must not multiply the per-call cap.
        _enforce_step_budget(sibling_results)

        # ---- HITL pause: stash the in-flight turn; persisted transcript stays valid ----
        if paused is not None:
            paused_call = norm_calls[paused_index]
            remaining = [
                {"id": c["id"], "name": c["function"]["name"], "args": _tool_args(c)}
                for c in norm_calls[paused_index + 1 :]
            ]
            pending = {
                "assistant": assistant_msg.model_dump(exclude_none=True),
                "sibling_results": sibling_results,
                "paused_call_id": paused_call["id"],
                "remaining_calls": remaining,
                "action_prompt": paused.action_prompt,
                "step": step_index,
            }
            if emitter:
                await emitter(
                    ReactStepRecord(
                        step=step_index,
                        thought=content or "",
                        action=paused_call["function"]["name"],
                        phase="pause",
                        call_index=paused_index,
                    )
                )
            return AgentLoopResult(
                {
                    "status": "awaiting_user_action",
                    "response": paused.response,
                    "action_prompt": paused.action_prompt,
                    "metadata": {
                        "transcript": serialize_transcript(transcript),
                        "pending": pending,
                        "steps_taken": step_index,
                    },
                }
            )

        # ---- No pause: commit the assistant turn + all tool results ----
        transcript.append(assistant_msg)
        for sib in sibling_results:
            transcript.append(
                Message(role="tool", tool_call_id=sib["tool_call_id"], content=sib["content"])
            )
        step += 1

        # ---- Repeat-failure abort: the model ignored the guidance and kept
        # re-issuing the same failing call; stop burning steps. ----
        if abort_repeat:
            logger.warning(
                "[loop] aborting: '%s' repeated an identical failing call %d times",
                abort_repeat,
                _RepeatFailureGuard.ABORT_AT,
            )
            return AgentLoopResult(
                {
                    "status": "partial",
                    "response": (
                        f"I stopped because the `{abort_repeat}` call kept failing the same "
                        "way despite retries. Please review the request or try a different "
                        "approach."
                    ),
                    "metadata": {
                        "transcript": serialize_transcript(transcript),
                        "repeat_failure_abort": abort_repeat,
                        "steps_taken": step,
                    },
                }
            )

    # ---- Step budget exhausted ----
    if budget_summarizer:
        summary = await budget_summarizer(transcript)
    else:
        summary = "I reached the step limit before finishing."
    return AgentLoopResult(
        {
            "status": "partial",
            "response": summary,
            "metadata": {
                "transcript": serialize_transcript(transcript),
                "budget_exhausted": True,
                "steps_taken": max_steps,
            },
        }
    )


async def _safe_execute(tool_executor: ToolExecutorFn, name: str, args: Any, call_id: str) -> str:
    try:
        return str(await tool_executor(name, args, call_id))
    except PauseSignal:
        # A second pause while replaying remaining calls is not supported; surface
        # a clear observation rather than nesting pauses.
        return "ERROR: tool requested another pause while resuming; skipped."
    except Exception as exc:  # noqa: BLE001
        # Cap exception text — driver errors can embed full statements/payloads,
        # and each retry re-appends its error to the append-only transcript.
        return f"ERROR calling {name}: {str(exc)[:2000]}"
