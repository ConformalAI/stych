"""Execution adapter for a process-owned agent run."""

from __future__ import annotations

import asyncio
import logging

from agents.progress import ProgressEmitter, _active_token_emitter
from agents.run_manager import AgentRun
from api.schemas import QueryRequest
from helpers.sse import SseEventType, safe_error_message, stream_timeout
from helpers.validators import sanitize_input
from llm.infer import _request_model_override
from llm.langfuse_client import get_langfuse_tracker
from llm.usage_limit import (
    over_limit_message_or_none,
    reset_usage_limit_context,
    set_usage_limit_context,
)

logger = logging.getLogger(__name__)


async def execute_agent_run(
    run: AgentRun,
    request: QueryRequest,
    *,
    orchestrator,
    session,
    config,
    user_id: str,
    user_email: str | None,
    effective_model: str | None,
) -> None:
    emitter = ProgressEmitter(run.events)
    emitter_token = _active_token_emitter.set(emitter)
    override_token = _request_model_override.set(effective_model) if effective_model else None
    usage_token = set_usage_limit_context(config, user_id, user_email)
    timeout_seconds = stream_timeout(config)
    message = sanitize_input(request.message)

    try:
        await run.set_status("running")
        tracker = get_langfuse_tracker()
        if tracker:
            tracker.set_session_id(request.session_id)
            tracker.set_user_id(user_id)

        limit_message = over_limit_message_or_none()
        if limit_message is not None:
            session.add_message(
                "assistant",
                limit_message,
                metadata={"status": "limit_reached", "run_id": run.id},
            )
            await run.events.put(
                {"type": SseEventType.LIMIT_REACHED.value, "message": limit_message}
            )
            await run.set_status("limit_reached")
            return

        result = await asyncio.wait_for(
            orchestrator.execute(
                task_type=request.task_type,
                message=message,
                state=session,
                session_id=request.session_id,
                progress_emitter=emitter,
                user_action=(
                    request.user_action.model_dump() if request.user_action is not None else None
                ),
                enabled_integrations=request.enabled_integrations,
                run_id=run.id,
                target_file_id=request.target_file_id,
            ),
            timeout=timeout_seconds,
        )
        if "error" in result:
            await run.events.put({"type": SseEventType.ERROR.value, "message": result["error"]})
            await run.set_status("failed")
            return

        await run.events.put(
            {
                "type": SseEventType.RESULT.value,
                "run_id": run.id,
                "response": result["response"],
                "task_type": result.get("task_type", request.task_type),
                "agents_used": result.get("agents_used"),
                "status": result.get("status", "completed"),
                "action_prompt": result.get("action_prompt"),
                "metadata": {
                    **(result.get("metadata", {}) or {}),
                    "run_id": run.id,
                },
            }
        )
        await run.set_status("completed")
    except asyncio.TimeoutError:
        timeout_message = (
            "The analysis ran for too long and was stopped. "
            "Try breaking the task into smaller steps."
        )
        session.add_message(
            "assistant",
            timeout_message,
            metadata={"status": "timeout", "run_id": run.id},
        )
        await run.events.put({"type": SseEventType.TIMEOUT.value, "message": timeout_message})
        await run.set_status("timed_out")
    except asyncio.CancelledError:
        # Python 3.11+ keeps the task cancelled until uncancel(); awaiting
        # without that leaves the run "running" forever (409 on the next query).
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        await run.events.put(
            {
                "type": SseEventType.CANCELLED.value,
                "message": "Generation stopped.",
                "run_id": run.id,
            }
        )
        await run.set_status("cancelled")
    except Exception as exc:
        logger.error(
            "Error in detached query run %s (session %s): %s",
            run.id,
            request.session_id,
            exc,
            exc_info=True,
        )
        safe_message = safe_error_message(exc)
        session.add_message(
            "assistant",
            safe_message,
            metadata={"status": "error", "run_id": run.id},
        )
        await run.events.put({"type": SseEventType.ERROR.value, "message": safe_message})
        await run.set_status("failed")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # Some AnyIO/MCP failures surface as BaseExceptionGroup and bypass
        # ``except Exception``. Detached runs must always reach a terminal state.
        logger.error(
            "Fatal error in detached query run %s (session %s): %s",
            run.id,
            request.session_id,
            exc,
            exc_info=True,
        )
        safe_message = safe_error_message(exc)
        session.add_message(
            "assistant",
            safe_message,
            metadata={"status": "error", "run_id": run.id},
        )
        await run.events.put({"type": SseEventType.ERROR.value, "message": safe_message})
        await run.set_status("failed")
    finally:
        await emitter.finish()
        if override_token is not None:
            _request_model_override.reset(override_token)
        _active_token_emitter.reset(emitter_token)
        reset_usage_limit_context(usage_token)
