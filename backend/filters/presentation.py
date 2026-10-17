"""Turn evaluated rule flags into the grid's highlight metadata.

The *present* stage of a FilterSpec is engine-independent: once each row's match
flag, rule flags, and group value are known, deciding which cells get which
severity is pure logic over those values. Every engine that evaluates the rules
— pandas, SQL, MongoDB — lands here, so the highlight semantics are defined once
instead of drifting between three copies.

Wire shape (kept terse because it is emitted per cell over a whole page):
``cell_meta`` entries are ``{"k": severity, "l": label, "t": tooltip}`` and
``row_meta`` entries are ``{"m": matched, "s": severity, "l", "t", "g": group}``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from filters.spec import SEVERITY_ORDER, PresentRule

_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITY_ORDER)}


class PresentationBuilder:
    """Accumulates highlight metadata across the rows of one page.

    Group ordinals are assigned in the order groups are first seen, so banding
    alternates correctly for whatever slice of rows this page holds.
    """

    def __init__(self, display_names: Sequence[str], rules: Sequence[PresentRule]):
        self._names = list(display_names)
        self._position = {name: index for index, name in enumerate(self._names)}
        self._rules = list(rules)
        self._group_ordinal: Dict[Any, int] = {}
        self.cell_meta: List[List[Optional[Dict[str, Any]]]] = []
        self.row_meta: List[Optional[Dict[str, Any]]] = []

    def add_row(
        self,
        values: Sequence[Any],
        *,
        matched: bool,
        fired_rule_ids: Sequence[str],
        group_value: Any = None,
    ) -> None:
        """Record one row's metadata. *values* is already JSON-safe."""
        # Deferred: filters.service reaches workspace.manager, which imports
        # back into this package.
        from filters.service import _render_tooltip  # noqa: PLC0415

        row_values = dict(zip(self._names, values))
        fired = set(fired_rule_ids)
        cells: List[Optional[Dict[str, Any]]] = [None] * len(self._names)
        entry: Dict[str, Any] = {"m": matched}
        row_rank = float("inf")

        for rule in self._rules:
            if rule.id not in fired:
                continue
            rank = _SEVERITY_RANK[rule.severity]
            if rule.target == "row":
                if rank < row_rank:
                    row_rank = rank
                    entry["s"] = rule.severity
                    if rule.label:
                        entry["l"] = rule.label
                    if rule.tooltip:
                        entry["t"] = _render_tooltip(rule.tooltip, row_values)
                continue
            for column in rule.columns or []:
                index = self._position.get(column)
                if index is None:
                    continue
                existing = cells[index]
                # The most severe rule wins a cell; ties keep the first.
                if existing is not None and _SEVERITY_RANK[existing["k"]] <= rank:
                    continue
                cell: Dict[str, Any] = {"k": rule.severity}
                if rule.label:
                    cell["l"] = rule.label
                if rule.tooltip:
                    cell["t"] = _render_tooltip(rule.tooltip, row_values)
                cells[index] = cell

        self.cell_meta.append(cells)

        has_group = group_value is not None
        if has_group and group_value not in self._group_ordinal:
            self._group_ordinal[group_value] = len(self._group_ordinal)
        if has_group:
            entry["g"] = self._group_ordinal[group_value]
        # A row with nothing to say carries no metadata at all.
        self.row_meta.append(entry if (has_group or "s" in entry or matched) else None)
