"""Export wire-contract schemas for frontend codegen.

Single direction of truth: Pydantic models → JSON Schema files consumed by
`npm run codegen` in the frontend (json-schema-to-typescript). Never hand-edit
anything under frontend/src/generated/ — regenerate instead:

    cd backend && uv run python scripts/export_contract_schemas.py
    cd ../frontend && npm run codegen
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

OUT_DIR = BACKEND_ROOT.parent / "frontend" / "src" / "generated" / "schema"

# Models whose wire shape the frontend depends on.
CONTRACT_MODELS = {
    "VizSpecSchema": ("viz.spec", "VizSpec"),
    "SpecError": ("viz.spec", "SpecError"),
    "FieldCatalog": ("viz.catalog", "FieldCatalog"),
}


def _empty_schemas_to_true(node: object) -> None:
    """Pydantic `Any` fields emit an empty schema ({title, default}) which
    json-schema-to-typescript mis-renders as an index-signature OBJECT — too
    narrow for scalar filter values. An empty schema means "match anything",
    so rewrite those nodes to the boolean schema `true` (= unknown)."""
    if isinstance(node, dict):
        if set(node.keys()) <= {"title", "default", "description"}:
            keep = {k: node[k] for k in ("description",) if k in node}
            node.clear()
            node.update(keep)
            return
        for value in node.values():
            _empty_schemas_to_true(value)
    elif isinstance(node, list):
        for item in node:
            _empty_schemas_to_true(item)


def main() -> int:
    from helpers.sse import SseEventType

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale_schema in OUT_DIR.glob("*.json"):
        stale_schema.unlink()

    # SSE event taxonomy — the enum is the source of truth for both sides.
    events = [e.value for e in SseEventType]
    (OUT_DIR / "sse_events.json").write_text(
        json.dumps({"name": "SseEventType", "values": events}, indent=2) + "\n"
    )
    print(f"wrote {OUT_DIR / 'sse_events.json'} ({len(events)} event types)")

    # API task_type union — derived from the agent registry.
    from helpers.agent_registry import api_task_types

    (OUT_DIR / "task_types.json").write_text(
        json.dumps({"name": "TaskType", "values": api_task_types()}, indent=2) + "\n"
    )
    print(f"wrote {OUT_DIR / 'task_types.json'} ({len(api_task_types())} task types)")

    # Scalar Literal types from viz.spec — emitted as named unions so the
    # frontend imports clean names instead of codegen property aliases.
    from typing import get_args

    import viz.spec as vspec

    literals = {
        name: list(get_args(getattr(vspec, name)))
        for name in (
            "FieldType",
            "Aggregate",
            "TimeUnit",
            "MarkType",
            "TexturePattern",
            "ColorMode",
            "ColorChannel",
            "SortDir",
            "Predicate",
        )
    }
    (OUT_DIR / "viz_literals.json").write_text(json.dumps(literals, indent=2) + "\n")
    print(f"wrote {OUT_DIR / 'viz_literals.json'} ({len(literals)} unions)")

    # Pydantic model schemas.
    import importlib

    modules: dict[str, object] = {}
    for out_name, (module_name, model_name) in CONTRACT_MODELS.items():
        module = modules.get(module_name)
        if module is None:
            module = importlib.import_module(module_name)
            modules[module_name] = module
        model = getattr(module, model_name)
        payload = model.model_json_schema(by_alias=True)
        _empty_schemas_to_true(payload)
        path = OUT_DIR / f"{out_name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
