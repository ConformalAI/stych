"""MongoDB source-side execution: scratch collections and aggregation pipelines.

The document counterpart to ``agents.sql_agent``. Working copies are collections
in a Stych-owned scratch database inside the customer's own cluster; the source
collection is never modified.
"""

from .pipeline_guard import PipelineGuardError, validate_pipeline
from .runner import MongoScratchRunner

__all__ = ["MongoScratchRunner", "PipelineGuardError", "validate_pipeline"]
