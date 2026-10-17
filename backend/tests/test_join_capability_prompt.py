from types import SimpleNamespace

import pandas as pd

from agents.orchestrator.orchestrator_react import _describe_workspace


def test_agent_workspace_lists_join_strategies_and_physical_paths():
    contexts = []
    for table in ("orders", "customers"):
        contexts.append(
            SimpleNamespace(
                filename=table,
                metadata={
                    "source": "postgres",
                    "connection_url": "postgresql://user:pw@localhost/analytics",
                    "schema": "public",
                    "table": table,
                },
                dataframe=pd.DataFrame({"id": [1]}),
            )
        )
    state = SimpleNamespace(
        session_id=None,
        metadata={},
        get_all_data_contexts=lambda: contexts,
    )

    description = _describe_workspace(state, None)

    assert "native in-source=['exact']" in description
    assert "levenshtein" in description
    assert "full-input stream" in description
    assert "FULL OUTER" in description
