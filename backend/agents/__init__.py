"""
Stych Agents Module - Distributed Architecture

Each agent has its own subfolder with independent endpoints.
Exports all agent classes for backward compatibility.
"""

from .analysis.agent import DataAnalysisAgent
from .base.agent import BaseStychAgent
from .cleaning.agent import DataCleaningAgent
from .csv_agent.agent import CSVAgent
from .formatter.agent import FormatterAgent
from .graph.agent import GraphAgent
from .linking.agent import DataLinkingAgent
from .orchestrator.agent import OrchestratorAgent
from .qa.agent import QAAgent
from .sql_agent.agent import SQLAgent

__all__ = [
    "BaseStychAgent",
    "DataAnalysisAgent",
    "DataCleaningAgent",
    "QAAgent",
    "OrchestratorAgent",
    "FormatterAgent",
    "GraphAgent",
    "SQLAgent",
    "CSVAgent",
    "DataLinkingAgent",
]
