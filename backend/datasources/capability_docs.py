"""Generate the developer connector-capability reference from code manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import (
    Capability,
    CapabilitySupport,
    FeatureDecision,
    ProductFeature,
    SupportMode,
)
from .feature_resolver import resolve_features
from .registry import manifests

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "connector-capabilities.md"


def _cell(support: CapabilitySupport) -> str:
    if support.mode == SupportMode.UNSUPPORTED:
        return f"— ({support.reason_code})"
    assert support.scope and support.freshness and support.executor
    return " · ".join(
        (support.mode.value, support.scope.value, support.freshness.value, support.executor.value)
    )


def _feature_cell(decision: FeatureDecision) -> str:
    if decision.execution_path:
        assert decision.scope and decision.freshness
        detail = (
            f"{decision.scope.value}/{decision.freshness.value}; {decision.execution_path.value}"
        )
        return f"{decision.availability.value} ({detail})"
    return decision.availability.value


def _join_cell(manifest) -> list[str]:
    support = manifest.capabilities[Capability.SAME_SOURCE_JOIN]
    if support.join is None:
        return ["unsupported", "—", "—", "—"]
    exact = next(item for item in support.join.strategies if item.strategy == "exact")
    lexical = [item for item in support.join.strategies if item.strategy != "exact"]
    lexical_names = ", ".join(item.strategy for item in lexical)
    lexical_detail = (
        f"{lexical_names}: {lexical[0].executor.value}, {lexical[0].scope.value}, "
        f"{lexical[0].egress} egress"
        if lexical
        else "—"
    )
    return [
        f"{exact.mode.value}, {exact.executor.value}, {exact.scope.value}, {exact.egress} egress",
        lexical_detail,
        ", ".join(support.join.join_types),
        " ".join(support.caveats),
    ]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    escaped = [[str(value).replace("|", "\\|") for value in row] for row in rows]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in escaped),
    ]


def render_markdown() -> str:
    connector_list = list(manifests())
    lines = [
        "# Connector capabilities",
        "",
        "> Generated from `backend/datasources/registry.py`. Do not edit by hand. Run",
        "> `cd backend && uv run python -m datasources.capability_docs --write` after manifest changes,",
        "> and use `--check` to detect drift. The backend contract test enforces the same check.",
        "",
        "This document describes what Stych implements today, not what the upstream data source",
        "could theoretically support. `native` executes in the source or local engine directly;",
        "`emulated` is implemented by Stych. Scope and freshness are explicit so a sampled snapshot",
        "cannot be mistaken for a live full-data path.",
        "",
        "## Connectors",
        "",
        *_table(
            ["Connector", "ID", "Maturity", "Verification", "User summary"],
            [
                [
                    item.display_name,
                    item.id,
                    item.maturity.value,
                    item.verification.value,
                    item.user_summary,
                ]
                for item in connector_list
            ],
        ),
        "",
        "Verification is separate from declared support: `contract_tested` validates the shared",
        "contract and local behavior; `integration_tested` also has real-engine parity tests. Cloud",
        "tests may still require environment-provided credentials.",
        "",
        "## Product features",
        "",
    ]
    feature_decisions = {item.id: resolve_features(item.id) for item in connector_list}
    lines.extend(
        _table(
            ["Feature", *(item.display_name for item in connector_list)],
            [
                [
                    feature.value,
                    *(
                        _feature_cell(feature_decisions[item.id][feature])
                        for item in connector_list
                    ),
                ]
                for feature in ProductFeature
            ],
        )
    )
    lines.extend(["", "## Same-source join matrix", ""])
    lines.extend(
        _table(
            ["Connector", "Exact", "Lexical strategies", "Join types", "Top-level caveat"],
            [[item.display_name, *_join_cell(item)] for item in connector_list],
        )
    )
    lines.extend(
        [
            "",
            "The lexical list is the current Stych set: levenshtein, jaro_winkler, token_sort, "
            "token_set, partial_ratio, cosine_tfidf, soundex, and metaphone. Full egress means "
            "the complete source inputs are streamed to Stych for matching; it never means a "
            "bounded preview is substituted. Exact source joins write only to Stych scratch.",
        ]
    )
    lines.extend(["", "## Primitive capabilities", ""])
    lines.extend(
        _table(
            ["Capability", *(item.display_name for item in connector_list)],
            [
                [
                    capability.value,
                    *(_cell(item.capabilities[capability]) for item in connector_list),
                ]
                for capability in Capability
            ],
        )
    )

    lines.extend(
        [
            "",
            "## Connector author contract",
            "",
            "1. Implement `datasources.contracts.Connector`; unsupported optional methods must raise",
            "   the typed `UnsupportedCapability` error.",
            "2. Register one exhaustive `ConnectorManifest` in `datasources.registry`. Every primitive",
            "   must be declared as `native`, `emulated`, or `unsupported`, including scope, freshness,",
            "   executor, limits, and caveats where applicable.",
            "3. Pass only an opaque `connection_ref` through `SourceRef`; credentials and connection URLs",
            "   are not part of the public source contract.",
            "4. Route product behavior through `datasources.feature_resolver` and add conformance, resolver,",
            "   API, and real-engine tests appropriate to the declared verification level.",
            "5. Regenerate this document and run `--check` before merging.",
            "",
            "A connector is not considered feature-complete merely because its upstream database supports",
            "an operation. The manifest records only paths that Stych implements and tests.",
        ]
    )

    caveat_rows = []
    for item in connector_list:
        for capability in Capability:
            for caveat in item.capabilities[capability].caveats:
                caveat_rows.append([item.display_name, capability.value, caveat])
    lines.extend(["", "## Declared caveats", ""])
    lines.extend(_table(["Connector", "Capability", "Caveat"], caveat_rows))
    return "\n".join(lines).rstrip() + "\n"


def render_json() -> str:
    return (
        json.dumps(
            [manifest.model_dump(mode="json") for manifest in manifests()], indent=2, sort_keys=True
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--write", action="store_true", help=f"write {DOC_PATH}")
    parser.add_argument("--check", action="store_true", help="fail if the generated doc is stale")
    args = parser.parse_args()
    if args.write and args.format != "markdown":
        parser.error("--write only supports markdown")

    output = render_markdown() if args.format == "markdown" else render_json()
    if args.check:
        if not DOC_PATH.exists() or DOC_PATH.read_text() != output:
            print(f"{DOC_PATH} is stale; run with --write")
            return 1
        print(f"{DOC_PATH} is current")
        return 0
    if args.write:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(output)
        print(f"Wrote {DOC_PATH}")
        return 0
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
