"""
MongoDB data source connector.

Loads a collection (with optional filter query) into a pandas DataFrame.
Nested document fields are flattened via pd.json_normalize so the result
is always a flat, analysis-ready DataFrame.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd

logger = logging.getLogger(__name__)


class MongoLoadError(Exception):
    """Raised when loading from MongoDB fails."""

    pass


def coerce_bson(obj: Any) -> Any:
    """Recursively convert BSON / non-JSON-serialisable values to strings."""
    if isinstance(obj, dict):
        return {k: coerce_bson(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [coerce_bson(v) for v in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _is_nested(value: Any) -> bool:
    return isinstance(value, (dict, list, tuple, set))


def _nested_to_json(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if _is_nested(value):
        return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
    return value


def canonicalize_document_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode leftover nested values so parquet / Arrow / DuckDB stay tabular.

    ``json_normalize`` flattens objects into dotted columns but leaves arrays and
    any object nested inside one intact, and those cannot round-trip through a
    columnar format. Encoding them keeps the frame honest about being a *view*
    of documents rather than pretending the nesting was never there.
    """
    if frame.empty:
        return frame
    out = frame.copy(deep=False)
    mutated = False
    for column in out.columns:
        if out[column].dtype != "object":
            continue
        sample = out[column].dropna().head(32)
        if sample.empty or not sample.map(_is_nested).any():
            continue
        if not mutated:
            out = frame.copy()
            mutated = True
        out[column] = out[column].map(_nested_to_json)
    return out


def flatten_documents(docs: List[Any]) -> pd.DataFrame:
    """Flatten Mongo documents to a tabular frame for display and comparison.

    Nested objects become dotted columns via ``json_normalize``. Leftover
    arrays/objects are JSON-encoded so parquet and Arrow stay tabular. ``_id``
    and other BSON types are coerced to text.
    """
    if not docs:
        return pd.DataFrame()
    frame = pd.json_normalize([coerce_bson(doc) for doc in docs], sep=".")
    if "_id" in frame.columns:
        frame["_id"] = frame["_id"].map(lambda value: None if value is None else str(value))
    return canonicalize_document_frame(frame)


def build_mongodb_connection_url(
    host: str,
    port: int = 27017,
    database: str = "admin",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> str:
    """Build a MongoDB connection URI."""
    if user or password:
        u = quote_plus(user or "")
        p = quote_plus(password or "")
        auth = f"{u}:{p}@" if p else f"{u}@"
    else:
        auth = ""
    return f"mongodb://{auth}{host}:{port}/{database}"


def _get_client(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 27017,
    database: str = "admin",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> Any:
    """Create a pymongo MongoClient."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError  # noqa: F401
    except ImportError as e:
        raise MongoLoadError(
            "pymongo is required for MongoDB support. Install it with: pip install pymongo"
        ) from e

    if connection_url:
        return MongoClient(connection_url, serverSelectionTimeoutMS=10_000)

    url = build_mongodb_connection_url(
        host=host or "localhost",
        port=port,
        database=database,
        user=user or "",
        password=password or "",
    )
    return MongoClient(url, serverSelectionTimeoutMS=10_000)


def test_mongodb_connection(connection_url: str, database: str = "admin") -> Tuple[bool, str]:
    """
    Test connectivity to MongoDB with the given URL.

    Returns:
        (success, message)
    """
    if not connection_url or not connection_url.strip():
        return False, "No connection URL provided"
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    client = MongoClient(connection_url.strip(), serverSelectionTimeoutMS=5_000)
    try:
        client.server_info()
        return True, "Connection successful"
    except PyMongoError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    finally:
        client.close()


def list_mongodb_collections(
    database: str = "admin",
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 27017,
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> List[Dict[str, str]]:
    """
    List collections in the given database.

    Returns:
        List of {"collection": str}
    """
    client = _get_client(
        connection_url=connection_url,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        **kwargs,
    )
    try:
        from pymongo.errors import PyMongoError

        db = client[database]
        names = db.list_collection_names()
        return [{"collection": n} for n in sorted(names)]
    except PyMongoError as e:
        logger.error("Failed to list MongoDB collections: %s", e)
        raise MongoLoadError(str(e)) from e
    finally:
        client.close()


def list_mongodb_databases(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 27017,
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> List[str]:
    """List databases visible to the configured MongoDB principal."""
    client = _get_client(
        connection_url=connection_url,
        host=host,
        port=port,
        user=user,
        password=password,
        **kwargs,
    )
    try:
        from pymongo.errors import PyMongoError

        return sorted(str(name) for name in client.list_database_names())
    except PyMongoError as e:
        logger.error("Failed to list MongoDB databases: %s", e)
        raise MongoLoadError(str(e)) from e
    finally:
        client.close()


def load_from_mongodb(
    database: str = "admin",
    collection: Optional[str] = None,
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 27017,
    user: str = "",
    password: str = "",
    query: Optional[Dict[str, Any]] = None,
    max_rows: int = 0,
    preview_rows: int = 10_000,
    **kwargs: Any,
) -> Tuple[pd.DataFrame, str]:
    """
    Load documents from a MongoDB collection into a pandas DataFrame.

    Documents are flattened via pd.json_normalize so nested fields become
    dot-separated column names (e.g. address.city).  The _id ObjectId field
    is converted to a string to keep it serialisable.

    Returns:
        (DataFrame, source_label)  e.g.  (df, "mydb.users")
    """
    if not collection or not collection.strip():
        raise MongoLoadError("A collection name is required.")

    client = _get_client(
        connection_url=connection_url,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        **kwargs,
    )

    source_label = f"{database}.{collection}"

    try:
        from pymongo.errors import PyMongoError

        db = client[database]
        col = db[collection]

        # Validate user-supplied query to prevent NoSQL operator injection.
        # Reject any top-level key starting with '$' (MongoDB operators).
        filter_doc = query or {}
        if isinstance(filter_doc, dict):
            bad_keys = [k for k in filter_doc if isinstance(k, str) and k.startswith("$")]
            if bad_keys:
                raise MongoLoadError(
                    f"Query filter contains disallowed operators: {bad_keys}. "
                    "Only field equality filters are permitted."
                )
        cursor = col.find(filter_doc, limit=preview_rows)
        docs = list(cursor)

        if not docs:
            logger.warning("MongoDB collection %s is empty", source_label)
            return pd.DataFrame(), source_label

        df = flatten_documents(docs)

        logger.info(
            "Loaded preview of %s docs from MongoDB: %s",
            len(df),
            source_label,
        )
        return df, source_label
    except PyMongoError as e:
        logger.error("MongoDB load failed: %s", e)
        raise MongoLoadError(str(e)) from e
    finally:
        client.close()
