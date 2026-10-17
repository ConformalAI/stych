"""
Visualization subsystem — declarative chart specs, compilers, and execution.

The single source of truth is the :class:`~viz.spec.VizSpec` (Vega-Lite-inspired).
Both the Visualize-tab UI and the graph agent author/edit specs; this package
validates them (``viz.validate``), describes the dataset fields they may
reference (``viz.catalog``), and materializes their data deterministically via
pandas (``viz.compile_pandas``) or SQL pushdown (``viz.compile_sql``), chosen
by ``viz.executor``.
"""

from .catalog import FieldCatalog, FieldInfo, get_field_catalog
from .executor import SpecValidationError, VizData, run_spec
from .spec import VizSpec
from .validate import normalize_spec, validate_spec

__all__ = [
    "FieldCatalog",
    "FieldInfo",
    "SpecValidationError",
    "VizData",
    "VizSpec",
    "get_field_catalog",
    "normalize_spec",
    "run_spec",
    "validate_spec",
]
