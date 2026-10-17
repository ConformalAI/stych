"""Custom filters: persistent highlight-filter specs for the data preview.

A ``FilterSpec`` (``filters/spec.py``) has three stages — annotate (derived
columns keyed by row identity), select (declarative predicate tree compiled to
pandas and SQL), and present (highlight rules) — served through dual engines
(``filters/service.py`` pandas, ``filters/sql_service.py`` Postgres pushdown)
that emit the same page shape, mirroring the diff subsystem.
"""
