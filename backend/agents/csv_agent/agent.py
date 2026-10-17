"""
CSV Agent: file-based (CSV, XLSX) cleaning and analysis.

Delegates to DataCleaningAgent (pandas path) and DataAnalysisAgent (sandbox path)
so all file-datasource logic stays in one place and can be called standalone or via orchestrator.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from ..progress import ProgressEmitter
from ..state import AgentState

logger = logging.getLogger(__name__)


class CSVAgent:
    """
    Handles CSV/XLSX (and other file) data: cleaning in-memory and analysis via sandbox.
    Used when session data comes from upload, not from a database.
    """

    def __init__(self, cleaning_agent: Any, analysis_agent: Any):
        self._cleaning_agent = cleaning_agent
        self._analysis_agent = analysis_agent

    async def clean_data(
        self,
        dataframe: pd.DataFrame,
        operations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Deprecated. Use chat cleaning flow instead."""
        return {"error": "CSV clean_data is deprecated. Use the chat cleaning flow instead."}

    async def run_analysis(
        self,
        state: AgentState,
        question: str,
        note_prefix: str = "",
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """Run sandbox-based analysis (code gen + execute + summarize). Same contract as DataAnalysisAgent._execute_analysis."""
        working_df = state.data_context.working_dataframe if state.data_context else None
        if working_df is None:
            return {
                "status": "completed",
                "response": "No data in session.",
                "result_data": None,
                "metadata": {"execution_mode": "csv", "original_question": question},
            }
        return await self._analysis_agent._execute_analysis(
            working_df=working_df,
            question=question,
            state=state,
            note_prefix=note_prefix,
            progress_emitter=progress_emitter,
        )

    def compute_quality_metrics(self, dataframe: pd.DataFrame) -> Dict[str, Any]:
        """Quality metrics for file data (duplicates, missing, outliers). Delegates to cleaning agent."""
        return self._cleaning_agent.compute_quality_metrics(dataframe)
