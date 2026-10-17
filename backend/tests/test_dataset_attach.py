"""Duplicate connector-table attach detection."""

from api.dataset_attach import is_same_attached_relation


def test_same_table_same_connection_is_duplicate():
    existing = {
        "source": "databricks",
        "schema": "gold",
        "table": "orders",
        "connection_id": "c1",
        "database": "main",
    }
    incoming = dict(existing)
    assert is_same_attached_relation(existing, incoming) is True


def test_same_table_different_saved_connections_is_not_duplicate():
    existing = {
        "source": "databricks",
        "schema": "gold",
        "table": "orders",
        "connection_id": "c1",
        "database": "main",
    }
    incoming = {**existing, "connection_id": "c2"}
    assert is_same_attached_relation(existing, incoming) is False


def test_mongo_collection_uses_database_as_schema():
    existing = {"source": "mongodb", "database": "shop", "collection": "orders"}
    incoming = {"source": "mongodb", "database": "shop", "collection": "orders"}
    assert is_same_attached_relation(existing, incoming) is True
    other_db = {"source": "mongodb", "database": "archive", "collection": "orders"}
    assert is_same_attached_relation(existing, other_db) is False


def test_custom_query_is_its_own_relation():
    existing = {"source": "postgres", "schema": "public", "custom_query": "select 1"}
    incoming = {"source": "postgres", "schema": "public", "table": "t"}
    assert is_same_attached_relation(existing, incoming) is False
