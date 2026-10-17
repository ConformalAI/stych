"""
Formatter Agent - deterministic rendering of raw data into readable markdown.

Formatting is a pure presentation concern: the sub-agents already produce the
substance, so this agent renders dicts/lists/DataFrames with templates instead
of spending an LLM call (and a serialized data dump) per response.
"""

import json
import logging
from typing import Any, Optional

import pandas as pd

from llm.infer import LLMRouter

from ..base.agent import BaseStychAgent

logger = logging.getLogger(__name__)


class FormatterAgent(BaseStychAgent):
    """Agent that deterministically formats raw data into readable markdown."""

    def __init__(self, llm_router: LLMRouter):
        super().__init__(
            name="FormatterAgent",
            description="Formats raw data into beautiful, readable markdown",
            llm_router=llm_router,
        )

    async def format_data(
        self,
        raw_data: Any,
        question: str = "",
        context: Optional[str] = None,
    ) -> str:
        """
        Format raw data into markdown deterministically (no LLM call).

        Args:
            raw_data: Raw data from analysis (dict, list, DataFrame, str, etc.)
            question: Original user question, used as the heading
            context: Kept for API compatibility; unused

        Returns:
            Formatted markdown string
        """
        # Agent-authored text is already user-facing markdown — pass through.
        if isinstance(raw_data, str):
            return raw_data
        return self._basic_format(raw_data, question)

    def _basic_format(self, data: Any, question: str = "") -> str:
        """Fallback basic formatting without LLM."""
        try:
            if isinstance(data, pd.DataFrame):
                return self._format_dataframe_basic(data, question)
            elif isinstance(data, pd.Series):
                return self._format_series_basic(data, question)
            elif isinstance(data, dict):
                return self._format_dict_basic(data, question)
            elif isinstance(data, list):
                return self._format_list_basic(data, question)
            else:
                return f"**Result:** {data}"
        except Exception as e:
            logger.error(f"Error in basic formatting: {e}")
            return str(data)

    def _format_dataframe_basic(self, df: pd.DataFrame, question: str) -> str:
        """Basic DataFrame formatting."""
        parts = []
        if question:
            parts.append(f"**{question}**\n")
        parts.append(f"*{len(df)} rows*\n")

        # Limit to 20 rows
        display_df = df.head(20)
        parts.append(display_df.to_markdown(index=False))

        if len(df) > 20:
            parts.append(f"\n*Showing first 20 of {len(df)} rows*")

        return "\n".join(parts)

    def _format_series_basic(self, series: pd.Series, question: str) -> str:
        """Basic Series formatting."""
        df = pd.DataFrame(
            {
                series.index.name or "Item": series.index,
                series.name or "Value": series.values,
            }
        )
        return self._format_dataframe_basic(df, question)

    def _format_dict_basic(self, data: dict, question: str) -> str:
        """Basic dict formatting."""
        parts = []
        if question:
            parts.append(f"**{question}**\n")

        # Convert to DataFrame if possible
        try:
            df = pd.DataFrame(
                [{"Item": k, "Value": self._render_value(v)} for k, v in data.items()]
            )
            parts.append(df.to_markdown(index=False))
        except Exception:
            # Fallback to list
            for k, v in data.items():
                parts.append(f"- **{k}**: {self._render_value(v)}")

        return "\n".join(parts)

    @staticmethod
    def _render_value(value: Any) -> str:
        """Render a dict value for a markdown cell; compact JSON for nested data."""
        if isinstance(value, (dict, list)):
            blob = json.dumps(value, default=str)
            return blob if len(blob) <= 300 else blob[:300] + "…"
        return str(value)

    def _format_list_basic(self, data: list, question: str) -> str:
        """Basic list formatting."""
        parts = []
        if question:
            parts.append(f"**{question}**\n")

        # Try to convert to DataFrame if list of dicts
        if data and isinstance(data[0], dict):
            try:
                df = pd.DataFrame(data)
                return self._format_dataframe_basic(df, question)
            except Exception:
                pass

        # Fallback to numbered list
        for i, item in enumerate(data, 1):
            parts.append(f"{i}. {item}")

        return "\n".join(parts)
