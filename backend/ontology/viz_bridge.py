"""
Bridge — a VizSpec channel that names a metric becomes an ordinary VizSpec.

This is what makes "revenue" mean one thing in a chart and in chat. A channel
carrying ``metric: "revenue"`` is expanded, before planning, into constructs the
viz subsystem already has: a ``calculate`` transform holding the metric's
expression (with its own filter folded in) plus a plain field + aggregate on the
channel. Nothing downstream changes — ``build_plan``, both compilers, and the
frontend's ``compileSpec`` all see a spec they already understand.

The expression itself comes from ``resolve.metric_projection``, the same helper
the ontology's own planner uses, so the two cannot drift apart.

Charts that name no metric are untouched, and a session with no ontology is not
an error — the ontology is additive, never a precondition for drawing a chart.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from viz.spec import CalculateTransform, SpecError, VizSpec

from .resolve import metric_projection
from .spec import OntologySpec

logger = logging.getLogger(__name__)

# Channels that can carry a measure. Faceting channels are not planned yet.
_CHANNELS = ("x", "y", "color", "size")


def spec_uses_metrics(spec: VizSpec) -> bool:
    """Cheap check so the executor only pays for an ontology load when needed."""
    return any(
        getattr(getattr(spec.encoding, channel, None), "metric", None) for channel in _CHANNELS
    )


def _error(channel: str, code: str, message: str) -> SpecError:
    return SpecError(path=f"encoding.{channel}.metric", code=code, message=message)


def expand_metric_channels(
    spec: VizSpec,
    ontology: Optional[OntologySpec],
    dataset: str,
) -> Tuple[VizSpec, List[SpecError]]:
    """Rewrite metric-bearing channels into field + aggregate + a calculation.

    ``dataset`` is the chart's resolved ``DataContext.file_id``. A metric defined
    on an entity backed by a *different* dataset is refused rather than silently
    computed against the wrong rows — a chart binds to one dataset, and the
    ontology's cross-entity joins are not available to it.
    """
    errors: List[SpecError] = []
    updates: dict = {}
    calcs: List[CalculateTransform] = []

    for channel in _CHANNELS:
        definition = getattr(spec.encoding, channel, None)
        name = getattr(definition, "metric", None)
        if not definition or not name:
            continue

        if ontology is None:
            errors.append(
                _error(
                    channel,
                    "no_ontology",
                    f"This chart uses the metric '{name}', but this session has no ontology yet. "
                    "Define it in the Ontology tab, or set a field and aggregate directly.",
                )
            )
            continue

        metric = ontology.metric(name)
        if metric is None:
            known = ", ".join(ontology.metric_names()[:8]) or "none defined"
            errors.append(
                _error(channel, "unknown_metric", f"No metric named '{name}'. Defined: {known}.")
            )
            continue

        entity = ontology.entity(metric.entity)
        if entity is None:
            errors.append(
                _error(channel, "unknown_entity", f"Metric '{name}' has no entity."),
            )
            continue
        if entity.dataset != dataset:
            errors.append(
                _error(
                    channel,
                    "metric_other_dataset",
                    f"Metric '{name}' is defined on '{entity.name}', which is a different "
                    "dataset from this chart's. Charts read one dataset.",
                )
            )
            continue

        column, expression, aggregate = metric_projection(metric)
        if expression is None:
            # An unfiltered count needs no projection: COUNT(*) counts rows.
            updates[channel] = definition.model_copy(
                update={"field": None, "aggregate": aggregate, "type": "quantitative"}
            )
            continue

        calcs.append(CalculateTransform(expr=expression, **{"as": column}))
        updates[channel] = definition.model_copy(
            update={"field": column, "aggregate": aggregate, "type": "quantitative"}
        )

    if not updates:
        return spec, errors

    encoding = spec.encoding.model_copy(update=updates)
    # Metric projections go FIRST: they are plain row-level columns, and a
    # user's own calculations may legitimately reference them.
    return (
        spec.model_copy(update={"encoding": encoding, "transform": calcs + spec.transform}),
        errors,
    )


def load_session_ontology(state) -> Optional[OntologySpec]:
    """The session's ontology, or None when it has none.

    Never raises: a chart must still render when the ontology is unavailable for
    any reason, and a spec that actually needs one reports that per channel.
    """
    from database import ontology_store  # noqa: PLC0415 — avoids an import cycle
    from database.connection import SessionLocal  # noqa: PLC0415

    user_id = str((getattr(state, "metadata", None) or {}).get("user_id") or "")
    if not user_id:
        return None

    db = SessionLocal()
    try:
        row = ontology_store.get_for_user(db, user_id)
        if row is None:
            return None
        return OntologySpec.model_validate(json.loads(row.spec_json))
    except Exception as exc:  # noqa: BLE001 — a chart must not fail on this
        logger.warning("[ontology] could not load ontology for viz: %s", exc)
        return None
    finally:
        db.close()
