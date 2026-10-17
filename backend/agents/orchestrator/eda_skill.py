"""Skill-style initial EDA.

EDA is not a bespoke pipeline. It is an objective-based prompt expanded
server-side and executed by the orchestrator's normal ReAct loop, similar to a
Claude Code or Codex skill. The orchestrator decides how to investigate; code
only scopes the selected dataset and describes a useful first-look outcome.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

EDA_TASK_TYPE = "eda"


def resolve_target_context(state: Any, target_file_id: Optional[str]) -> Any:
    """Find the DataContext the EDA run should analyze.

    Explicit file_id wins (works for additional datasets too); otherwise the
    session's active dataset. Raises ValueError with a user-safe message when
    nothing resolvable is found.
    """
    contexts = state.get_all_data_contexts() if hasattr(state, "get_all_data_contexts") else []
    if target_file_id:
        for dc in contexts:
            if dc.file_id == target_file_id:
                return dc
        raise ValueError("Dataset not found in session.")
    active = getattr(state, "data_context", None)
    if active is not None:
        return active
    raise ValueError("Please upload or connect a dataset first.")


def build_eda_skill_prompt(state: Any, target_file_id: Optional[str] = None) -> str:
    """Expand the adaptive initial-EDA skill for the shared agentic engine."""
    dc = resolve_target_context(state, target_file_id)
    source_label = dc.file_id
    filename = getattr(dc, "filename", "") or ""
    is_sql = bool(dc.is_sql_datasource())

    source_hint = (
        "This is a connected warehouse/database source: work via SQL-capable agents "
        "only; there are no workspace files for it."
        if is_sql
        else "Work against the workspace file for this dataset."
    )

    return (
        "Activate the Initial Exploratory Data Analysis skill for the selected dataset. "
        "Use your judgment and adapt the investigation to the data rather than following "
        "a fixed pipeline. Work through the normal agentic engine and delegate to the existing "
        "specialized agents when they materially improve the result.\n\n"
        f"Target dataset: `{source_label}`"
        + (f" ({filename})" if filename and filename != source_label else "")
        + f". {source_hint}\n\n"
        "Aim to give the user a useful first understanding of the dataset. Relevant lines of "
        "inquiry often include its shape and field types, missingness or duplicates, important "
        "distributions, categorical composition, relationships, correlations, and temporal "
        "patterns. These are heuristics, not a checklist: omit irrelevant work, go deeper on "
        "interesting signals, and use analysis, graph, or other appropriate agents as needed.\n\n"
        "Create persistent visualizations when they add explanatory value. Choose the number and "
        "form from the dataset rather than targeting a fixed chart count; do not create weak or "
        "redundant charts merely for coverage.\n\n"
        "Finish with a concise synthesis of what the data appears to represent, the most useful "
        "findings and data-quality concerns, any visualizations created, and a short prioritized "
        "set of sensible next investigations or improvements."
    )


def eda_target_metadata(state: Any, target_file_id: Optional[str] = None) -> Dict[str, Any]:
    """Metadata stamp for messages/results produced by an EDA run."""
    try:
        dc = resolve_target_context(state, target_file_id)
    except ValueError:
        return {"type": EDA_TASK_TYPE}
    return {
        "type": EDA_TASK_TYPE,
        "file_id": dc.file_id,
        "filename": getattr(dc, "filename", None),
    }
