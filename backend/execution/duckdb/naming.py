"""Stable, injection-safe relation naming helpers."""

from __future__ import annotations

import hashlib
import re

from .types import DuckDBInvalidRelationName

_RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_relation_name(name: str) -> str:
    if not isinstance(name, str) or not _RELATION_RE.fullmatch(name):
        raise DuckDBInvalidRelationName(
            "Relation names must start with a letter or underscore, contain only "
            "letters, numbers, and underscores, and be at most 128 characters."
        )
    return name


def quote_identifier(name: str) -> str:
    """Quote a DuckDB identifier by doubling embedded double quotes.

    Generated relation names still go through ``validate_relation_name``.
    User column names (spaces, hyphens, leading digits, unicode) must be
    quotable too — the strict relation regex is the wrong tool here.
    """
    if not isinstance(name, str) or name == "":
        raise DuckDBInvalidRelationName("Identifier must be a non-empty string.")
    return '"' + name.replace('"', '""') + '"'


def source_relation_name(source_id: str, label: str | None = None) -> str:
    """Create a readable, collision-resistant SQL name for an opaque source id."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label or source_id).strip("_").lower()
    if not slug or not slug[0].isalpha():
        slug = f"source_{slug}" if slug else "source"
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:10]
    return validate_relation_name(f"src_{slug[:100]}_{digest}")
