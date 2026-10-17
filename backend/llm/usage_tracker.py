"""
LLM usage / credit accounting.

Consumes model+token usage telemetry (from LiteLLM/OpenRouter/Google paths) and
stores per-call usage events for billing / consumption dashboards.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Literal, Optional

from sqlalchemy import func

from database.connection import SessionLocal
from database.models import LLMUsageEvent, SessionHistory, UsageLedger, UserQuota

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_usd: float
    output_per_1k_usd: float


# Anthropic prompt-cache pricing relative to the normal input rate.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def compute_billed_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    """
    Cost-equivalent token count: cache reads count at 0.1x, cache writes at
    1.25x. This is what usage displays should show — a 20-step agent loop whose
    prompts are 90%+ cache reads costs a fraction of its raw token count.
    """
    cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
    creation = min(max(cache_creation_tokens, 0), max(prompt_tokens, 0) - cached)
    uncached = max(prompt_tokens, 0) - cached - creation
    return round(
        uncached
        + cached * CACHE_READ_FACTOR
        + creation * CACHE_WRITE_FACTOR
        + max(completion_tokens, 0)
    )


class UsageCostEstimator:
    """
    Lightweight price resolver for model token billing.

    Prices are rough defaults and can be overridden via env:
      CREDIT_USD_PER_CREDIT (default: 0.01 => 100 credits per $1)
    """

    _PRICING_HINTS: list[tuple[str, ModelPricing]] = [
        ("claude-sonnet-4.5", ModelPricing(0.003, 0.015)),
        ("claude-3.7-sonnet", ModelPricing(0.003, 0.015)),
        ("claude-3.5-sonnet", ModelPricing(0.003, 0.015)),
        ("claude-3-haiku", ModelPricing(0.00025, 0.00125)),
        ("gemini-2.5-pro", ModelPricing(0.0035, 0.0105)),
        ("gemini-1.5-pro", ModelPricing(0.0035, 0.0105)),
        ("gemini-1.5-flash", ModelPricing(0.00035, 0.00105)),
        ("gpt-4o", ModelPricing(0.005, 0.015)),
        ("gpt-4", ModelPricing(0.03, 0.06)),
        ("gpt-3.5", ModelPricing(0.0005, 0.0015)),
    ]

    def __init__(self) -> None:
        usd_per_credit_raw = os.getenv("CREDIT_USD_PER_CREDIT", "0.01").strip() or "0.01"
        try:
            self.usd_per_credit = max(float(usd_per_credit_raw), 1e-9)
        except ValueError:
            self.usd_per_credit = 0.01

    def _resolve_pricing(self, model: str) -> ModelPricing:
        model_l = (model or "").lower()
        for hint, pricing in self._PRICING_HINTS:
            if hint in model_l:
                return pricing
        return ModelPricing(0.001, 0.002)

    def estimate(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> tuple[float, float]:
        pricing = self._resolve_pricing(model)
        # Anthropic prompt-cache pricing: cache reads bill at 0.1x input price,
        # cache writes at 1.25x. Without the discount, cache-heavy agent loops
        # (95%+ of each step's prompt is a cache read) over-report cost ~4-10x.
        cached = min(max(cached_tokens, 0), prompt_tokens)
        creation = min(max(cache_creation_tokens, 0), prompt_tokens - cached)
        uncached = prompt_tokens - cached - creation
        input_cost = (
            uncached / 1000.0 * pricing.input_per_1k_usd
            + cached / 1000.0 * pricing.input_per_1k_usd * CACHE_READ_FACTOR
            + creation / 1000.0 * pricing.input_per_1k_usd * CACHE_WRITE_FACTOR
        )
        cost = input_cost + (completion_tokens / 1000.0) * pricing.output_per_1k_usd
        credits = cost / self.usd_per_credit
        return round(cost, 8), round(credits, 6)


def _billed_of(evt: LLMUsageEvent) -> int:
    """Billed tokens for an event; falls back to the raw total for rows recorded
    before billed accounting existed."""
    if evt.usage_metadata:
        try:
            billed = json.loads(evt.usage_metadata).get("billed_tokens")
            if billed is not None:
                return int(billed)
        except Exception:
            pass
    return int(evt.total_tokens or 0)


class UsageTracker:
    def __init__(self) -> None:
        self.estimator = UsageCostEstimator()

    def record_event(
        self,
        *,
        user_id: Optional[str],
        session_id: Optional[str],
        provider: str,
        model: str,
        agent_name: str,
        usage: Optional[Dict[str, int]],
        latency_ms: float,
    ) -> None:
        if not user_id:
            return
        if not usage:
            return

        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or (prompt_tokens + completion_tokens))

        estimated_cost_usd, credits_consumed = self.estimator.estimate(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=int(usage.get("cached_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_tokens", 0) or 0),
        )
        billed_tokens = compute_billed_tokens(
            prompt_tokens,
            completion_tokens,
            cached_tokens=int(usage.get("cached_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_tokens", 0) or 0),
        )

        metadata: Dict[str, Any] = {"latency_ms": latency_ms, "billed_tokens": billed_tokens}
        # Cache accounting + estimation flag, when the provider reported them —
        # this is how we verify prompt-cache hit rates from real traffic.
        for extra_key in ("cached_tokens", "cache_creation_tokens", "estimated"):
            if usage.get(extra_key) is not None:
                metadata[extra_key] = usage[extra_key]
        metadata_blob = json.dumps(metadata, separators=(",", ":"))

        db = SessionLocal()
        try:
            usage_evt = LLMUsageEvent(
                user_id=user_id,
                session_id=session_id,
                provider=provider,
                model=model,
                agent_name=agent_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=estimated_cost_usd,
                credits_consumed=credits_consumed,
                usage_metadata=metadata_blob,
            )
            db.add(usage_evt)
            db.flush()

            db.add(
                UsageLedger(
                    app_user_id=user_id,
                    session_history_id=session_id,
                    llm_usage_event_id=usage_evt.id,
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    credits_consumed=credits_consumed,
                    metadata_json=metadata_blob,
                )
            )

            if session_id:
                session_row = (
                    db.query(SessionHistory).filter(SessionHistory.id == session_id).first()
                )
                if session_row and session_row.app_user_id == user_id:
                    # Accumulate cost-equivalent (billed) tokens — the sidebar
                    # shows this figure, and raw counts overstate cache-heavy
                    # agent loops by 4-10x.
                    session_row.total_tokens = int((session_row.total_tokens or 0) + billed_tokens)
                    session_row.total_credits = float(
                        (session_row.total_credits or 0.0) + credits_consumed
                    )
                    session_row.last_activity_at = datetime.utcnow()
                    session_row.updated_at = datetime.utcnow()

            quota = db.query(UserQuota).filter(UserQuota.app_user_id == user_id).first()
            if quota:
                quota.tokens_used_monthly = int((quota.tokens_used_monthly or 0) + billed_tokens)
                quota.credits_used_monthly = float(
                    (quota.credits_used_monthly or 0.0) + credits_consumed
                )
                quota.updated_at = datetime.utcnow()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Failed to persist usage event: %s", exc)
        finally:
            db.close()

    def is_within_quota(self, *, user_id: str) -> tuple[bool, Optional[str]]:
        db = SessionLocal()
        try:
            quota = db.query(UserQuota).filter(UserQuota.app_user_id == user_id).first()
            if quota is None:
                return True, None

            token_limit = quota.token_limit_monthly
            if token_limit is not None and int(quota.tokens_used_monthly or 0) >= int(token_limit):
                return False, "Monthly token limit reached"

            credit_limit = quota.credit_limit_monthly
            if credit_limit is not None and float(quota.credits_used_monthly or 0.0) >= float(
                credit_limit
            ):
                return False, "Monthly credit limit reached"

            return True, None
        finally:
            db.close()

    def get_lifetime_spend_usd(self, *, user_id: str) -> float:
        """Total estimated USD spend for a user across all sessions and time."""
        db = SessionLocal()
        try:
            total = (
                db.query(func.coalesce(func.sum(LLMUsageEvent.estimated_cost_usd), 0.0))
                .filter(LLMUsageEvent.user_id == user_id)
                .scalar()
            )
            return float(total or 0.0)
        finally:
            db.close()

    def get_summary(self, *, user_id: str, days: int = 30) -> Dict[str, Any]:
        cutoff = datetime.utcnow() - timedelta(days=max(days, 1))
        db = SessionLocal()
        try:
            events = (
                db.query(LLMUsageEvent)
                .filter(
                    LLMUsageEvent.user_id == user_id,
                    LLMUsageEvent.created_at >= cutoff,
                )
                .order_by(LLMUsageEvent.created_at.desc())
                .all()
            )

            # Aggregate in Python: billed tokens live in the usage_metadata JSON
            # (kept out of the schema to avoid a migration), so SQL SUMs can't
            # produce them. Window row counts are small enough for this.
            by_model: Dict[str, Dict[str, Any]] = {}
            total_billed = total_raw = total_prompt = total_completion = 0
            total_cost = total_credits = 0.0
            for evt in events:
                billed = _billed_of(evt)
                total_billed += billed
                total_raw += int(evt.total_tokens or 0)
                total_prompt += int(evt.prompt_tokens or 0)
                total_completion += int(evt.completion_tokens or 0)
                total_cost += float(evt.estimated_cost_usd or 0.0)
                total_credits += float(evt.credits_consumed or 0.0)
                bucket = by_model.setdefault(
                    evt.model,
                    {
                        "model": evt.model,
                        "request_count": 0,
                        "total_tokens": 0,
                        "raw_total_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "credits_consumed": 0.0,
                    },
                )
                bucket["request_count"] += 1
                bucket["total_tokens"] += billed
                bucket["raw_total_tokens"] += int(evt.total_tokens or 0)
                bucket["estimated_cost_usd"] += float(evt.estimated_cost_usd or 0.0)
                bucket["credits_consumed"] += float(evt.credits_consumed or 0.0)

            return {
                "window_days": max(days, 1),
                "request_count": len(events),
                "total_tokens": total_billed,
                "raw_total_tokens": total_raw,
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "estimated_cost_usd": total_cost,
                "credits_consumed": total_credits,
                "by_model": sorted(
                    by_model.values(), key=lambda b: b["total_tokens"], reverse=True
                ),
                "recent_events": [self._serialize_event(evt) for evt in events[:25]],
            }
        finally:
            db.close()

    def _serialize_event(self, evt: LLMUsageEvent) -> Dict[str, Any]:
        latency_ms = None
        if evt.usage_metadata:
            try:
                payload = json.loads(evt.usage_metadata)
                latency_raw = payload.get("latency_ms")
                latency_ms = float(latency_raw) if latency_raw is not None else None
            except Exception:
                latency_ms = None
        return {
            "id": evt.id,
            "created_at": evt.created_at.isoformat() if evt.created_at else None,
            "provider": evt.provider,
            "model": evt.model,
            "agent_name": evt.agent_name,
            "session_id": evt.session_id,
            "latency_ms": latency_ms,
            "prompt_tokens": evt.prompt_tokens,
            "completion_tokens": evt.completion_tokens,
            # total_tokens is the cost-equivalent (billed) figure the UI shows;
            # the raw provider count stays available for diagnostics.
            "total_tokens": _billed_of(evt),
            "raw_total_tokens": evt.total_tokens,
            "estimated_cost_usd": evt.estimated_cost_usd,
            "credits_consumed": evt.credits_consumed,
        }

    def get_session_summaries(
        self,
        *,
        user_id: str,
        days: int = 30,
        limit: int = 20,
    ) -> Dict[str, Any]:
        cutoff = datetime.utcnow() - timedelta(days=max(days, 1))
        db = SessionLocal()
        try:
            # Only surface sessions whose SessionHistory row still exists — this
            # ensures deleted sessions are removed from the sidebar list immediately.
            existing_session_ids = db.query(SessionHistory.id).subquery()
            events = (
                db.query(LLMUsageEvent)
                .filter(
                    LLMUsageEvent.user_id == user_id,
                    LLMUsageEvent.created_at >= cutoff,
                    LLMUsageEvent.session_id.isnot(None),
                    LLMUsageEvent.session_id.in_(existing_session_ids),
                )
                .all()
            )

            # Group in Python so per-session token sums use billed tokens (kept
            # in usage_metadata JSON, out of reach of SQL SUM).
            groups: Dict[str, Dict[str, Any]] = {}
            for evt in events:
                g = groups.setdefault(
                    evt.session_id,
                    {
                        "session_id": evt.session_id,
                        "request_count": 0,
                        "total_tokens": 0,
                        "raw_total_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "credits_consumed": 0.0,
                        "first_event_at": evt.created_at,
                        "last_event_at": evt.created_at,
                    },
                )
                g["request_count"] += 1
                g["total_tokens"] += _billed_of(evt)
                g["raw_total_tokens"] += int(evt.total_tokens or 0)
                g["estimated_cost_usd"] += float(evt.estimated_cost_usd or 0.0)
                g["credits_consumed"] += float(evt.credits_consumed or 0.0)
                if evt.created_at:
                    if g["first_event_at"] is None or evt.created_at < g["first_event_at"]:
                        g["first_event_at"] = evt.created_at
                    if g["last_event_at"] is None or evt.created_at > g["last_event_at"]:
                        g["last_event_at"] = evt.created_at

            rows = sorted(groups.values(), key=lambda g: g["estimated_cost_usd"], reverse=True)[
                : max(1, min(limit, 200))
            ]

            # Fetch is_anonymous and title per session in a single resilient query.
            session_ids = [g["session_id"] for g in rows]
            is_anon_map: Dict[str, bool] = {}
            title_map: Dict[str, str] = {}
            if session_ids:
                try:
                    meta_rows = (
                        db.query(
                            SessionHistory.id,
                            SessionHistory.is_anonymous,
                            SessionHistory.title,
                        )
                        .filter(SessionHistory.id.in_(session_ids))
                        .all()
                    )
                    for r in meta_rows:
                        is_anon_map[r[0]] = bool(r[1])
                        if r[2]:
                            title_map[r[0]] = r[2]
                except Exception:
                    # Column may not exist yet (migration pending) — default to safe values
                    pass

            return {
                "window_days": max(days, 1),
                "sessions": [
                    {
                        "session_id": g["session_id"],
                        "request_count": g["request_count"],
                        "total_tokens": g["total_tokens"],
                        "raw_total_tokens": g["raw_total_tokens"],
                        "estimated_cost_usd": g["estimated_cost_usd"],
                        "credits_consumed": g["credits_consumed"],
                        "first_event_at": (
                            g["first_event_at"].isoformat() if g["first_event_at"] else None
                        ),
                        "last_event_at": (
                            g["last_event_at"].isoformat() if g["last_event_at"] else None
                        ),
                        "is_anonymous": is_anon_map.get(g["session_id"], False),
                        "title": title_map.get(g["session_id"]),
                    }
                    for g in rows
                ],
            }
        finally:
            db.close()

    def list_events(
        self,
        *,
        user_id: str,
        days: int = 30,
        page: int = 1,
        page_size: int = 20,
        sort_by: Literal[
            "created_at", "total_tokens", "credits_consumed", "estimated_cost_usd"
        ] = "created_at",
        sort_order: Literal["asc", "desc"] = "desc",
    ) -> Dict[str, Any]:
        cutoff = datetime.utcnow() - timedelta(days=max(days, 1))
        page = max(page, 1)
        page_size = max(1, min(page_size, 100))

        column_map = {
            "created_at": LLMUsageEvent.created_at,
            "total_tokens": LLMUsageEvent.total_tokens,
            "credits_consumed": LLMUsageEvent.credits_consumed,
            "estimated_cost_usd": LLMUsageEvent.estimated_cost_usd,
        }
        sort_col = column_map.get(sort_by, LLMUsageEvent.created_at)
        order_expr = sort_col.asc() if sort_order == "asc" else sort_col.desc()

        db = SessionLocal()
        try:
            base = db.query(LLMUsageEvent).filter(
                LLMUsageEvent.user_id == user_id,
                LLMUsageEvent.created_at >= cutoff,
            )
            total_count = base.count()
            items = (
                base.order_by(order_expr, LLMUsageEvent.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return {
                "window_days": max(days, 1),
                "page": page,
                "page_size": page_size,
                "total_count": int(total_count),
                "items": [self._serialize_event(evt) for evt in items],
            }
        finally:
            db.close()

    def get_event(self, *, user_id: str, event_id: str) -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            evt = (
                db.query(LLMUsageEvent)
                .filter(LLMUsageEvent.user_id == user_id, LLMUsageEvent.id == event_id)
                .first()
            )
            if evt is None:
                return None
            return self._serialize_event(evt)
        finally:
            db.close()


_usage_tracker: Optional[UsageTracker] = None


def get_usage_tracker() -> UsageTracker:
    global _usage_tracker
    if _usage_tracker is None:
        _usage_tracker = UsageTracker()
    return _usage_tracker
