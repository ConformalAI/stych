"""
Question & Answer Agent for conversational data queries.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from llm.infer import LLMRouter
from tools.data_tools import DataAnalysisTools

from ..base.agent import BaseStychAgent
from ..state import AgentState

logger = logging.getLogger(__name__)

_DEFAULT_DOMAIN_QA_PROMPT = """You are a data knowledge assistant specialising in data science,
data engineering, and analytics concepts. You help data professionals quickly look up
definitions, explanations, and best practices without leaving their workflow.

Answer questions about: statistics, machine learning, SQL, data engineering, data cleaning,
data visualization, probability, Python, and pandas.

If the question is clearly outside these domains (e.g. history, celebrities, general news,
cooking, sports), politely decline and suggest the user search elsewhere.

Keep answers concise but complete. Use examples where helpful."""


class QAAgent(BaseStychAgent):
    """Agent for answering questions about data."""

    def __init__(self, llm_router: LLMRouter, config: Optional[Dict[str, Any]] = None):
        super().__init__(
            name="QAAgent",
            description="Answers questions about datasets and data concepts",
            llm_router=llm_router,
        )
        self._config = config or {}
        self.system_prompt = """You are a helpful data assistant. Answer questions about the dataset clearly and accurately.
Use the provided context to give specific, data-driven answers."""

    async def answer(
        self,
        dataframe: Optional[pd.DataFrame],
        question: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Answer a question about the dataset or a general query."""
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
            ]

            if dataframe is not None:
                stats = DataAnalysisTools.describe_statistics(dataframe)
                if "error" not in stats:
                    messages.append({"role": "system", "content": f"Dataset information:\n{stats}"})
            else:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "No dataset is currently loaded. You can answer general questions "
                            "or suggest the user upload a CSV file for data analysis."
                        ),
                    }
                )

            if conversation_history:
                messages.extend(conversation_history[-5:])

            messages.append({"role": "user", "content": question})

            answer = await self.generate_response(messages)
            return answer

        except Exception as e:
            logger.error(f"Error in QA: {e}")
            return f"I encountered an error: {str(e)}"

    async def answer_knowledge(
        self,
        question: str,
        state: Optional[AgentState] = None,
    ) -> str:
        """
        Answer a domain-specific knowledge question (no dataset required).

        Uses a focused system prompt that restricts answers to data science,
        data engineering, SQL, statistics, ML, Python, and pandas concepts.
        Questions outside these domains receive a polite refusal.
        """
        routing_cfg = self._config.get("routing", {})
        system_prompt = routing_cfg.get("domain_qa_prompt", _DEFAULT_DOMAIN_QA_PROMPT)

        # Include recent conversation context so follow-up questions work naturally
        history_messages: List[Dict[str, str]] = []
        if state and hasattr(state, "get_recent_messages"):
            for msg in state.get_recent_messages(n=4):
                history_messages.append({"role": msg.role, "content": msg.content[:400]})

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": question},
        ]

        try:
            answer = await self.generate_response(messages, temperature=0.3)
            logger.info("[QAAgent] answer_knowledge completed for question: %s", question[:80])
            return answer
        except Exception as exc:
            logger.error("[QAAgent] answer_knowledge failed: %s", exc)
            return f"I encountered an error answering that: {exc}"
