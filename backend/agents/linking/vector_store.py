"""
pgvector wrapper for semantic similarity search.

Reuses the existing psycopg2 connection helper from `tools.py` so there
is only one Postgres driver in the codebase.

Usage pattern (intended to be called from a fuzzy strategy matcher):

    conn = get_connection(db_config)
    try:
        VectorStore.ensure_extension(conn)
        VectorStore.create_temp_table(conn, "vec_right", dim=768)
        VectorStore.bulk_insert(conn, "vec_right", values, embeddings)
        VectorStore.maybe_create_hnsw(conn, "vec_right")
        for left_idx, qvec in enumerate(left_embeddings):
            hits = VectorStore.knn(conn, "vec_right", qvec, k=5, threshold=0.8)
            ...
    finally:
        conn.close()

Distance / similarity convention
--------------------------------
We use the cosine-distance operator `<=>` from pgvector. Cosine
similarity is then `1 - cosine_distance`, in [0, 1] for non-negative
similarity. Thresholds throughout the linking agent are expressed as
*similarities* (higher = better), to match the existing fuzzy
strategies' convention.
"""

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np
import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------

# Below this row count on the indexed side, exact search is fast enough
# and gives perfect recall. Above it we build an HNSW index.
DEFAULT_HNSW_MIN_ROWS = 10_000

# pgvector HNSW defaults that match the upstream documentation
DEFAULT_HNSW_M = 16
DEFAULT_HNSW_EF_CONSTRUCTION = 64


# -------------------------------------------------------------------------
# VectorStore
# -------------------------------------------------------------------------


class VectorStore:
    """Static helpers for pgvector-backed similarity search."""

    @staticmethod
    def ensure_extension(conn) -> None:
        """
        Idempotently install the pgvector extension in the current DB.
        Must be called once per connection before any vector operation.
        """
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            raise RuntimeError(
                "Failed to enable pgvector extension. Make sure the Postgres "
                "instance is built with pgvector (image: pgvector/pgvector:pgXX). "
                f"Underlying error: {e}"
            ) from e

    # ---- table lifecycle ------------------------------------------------

    @staticmethod
    def create_temp_table(conn, table_name: str, dim: int) -> None:
        """
        Create a session-scoped temp table for embeddings.

        Schema:
            idx        INT        — original row index in the source DataFrame
            value      TEXT       — the source text (kept for debugging / joins)
            embedding  vector(D)  — the embedding itself
        """
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{table_name}";')
                cur.execute(
                    f'CREATE TEMP TABLE "{table_name}" ('
                    f"  idx INT NOT NULL,"
                    f"  value TEXT,"
                    f"  embedding vector({int(dim)}) NOT NULL"
                    f");"
                )
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            raise RuntimeError(f"create_temp_table failed: {e}") from e

    # ---- ingestion ------------------------------------------------------

    @staticmethod
    def bulk_insert(
        conn,
        table_name: str,
        values: Sequence[str],
        embeddings: np.ndarray,
        indices: Optional[Sequence[int]] = None,
    ) -> int:
        """
        Bulk-load (idx, value, embedding) rows. Returns the row count.

        `embeddings` is an (N, D) numpy array. We format each row as the
        pgvector textual literal `[x,y,z]` — psycopg2 then sends it as
        plain text and the server parses it into a vector. This avoids
        the need for a separate vector-type registration step.
        """
        if len(values) != len(embeddings):
            raise ValueError(
                f"bulk_insert: len(values)={len(values)} != " f"len(embeddings)={len(embeddings)}"
            )
        if len(values) == 0:
            return 0

        if indices is None:
            indices = list(range(len(values)))
        elif len(indices) != len(values):
            raise ValueError("bulk_insert: indices length mismatch")

        rows: List[Tuple[int, Optional[str], str]] = []
        for idx, val, vec in zip(indices, values, embeddings):
            literal = _vec_to_literal(vec)
            rows.append((int(idx), None if val is None else str(val), literal))

        try:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    f'INSERT INTO "{table_name}" (idx, value, embedding) VALUES %s',
                    rows,
                    template="(%s, %s, %s::vector)",
                    page_size=1000,
                )
            conn.commit()
        except psycopg2.Error as e:
            conn.rollback()
            raise RuntimeError(f"bulk_insert failed: {e}") from e

        return len(rows)

    # ---- index management ----------------------------------------------

    @staticmethod
    def maybe_create_hnsw(
        conn,
        table_name: str,
        min_rows: int = DEFAULT_HNSW_MIN_ROWS,
        m: int = DEFAULT_HNSW_M,
        ef_construction: int = DEFAULT_HNSW_EF_CONSTRUCTION,
    ) -> bool:
        """
        Build an HNSW index over `embedding` using cosine distance, but
        only if the table has at least `min_rows` rows. Returns True if
        the index was built.

        Called *after* bulk_insert — building HNSW on an empty table and
        inserting afterward is slower and lower quality (per the
        pgvector docs).
        """
        try:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{table_name}";')
                row_count = cur.fetchone()[0]

            if row_count < min_rows:
                logger.debug(
                    "Skipping HNSW index on %s (rows=%d < min=%d); " "exact search will be used.",
                    table_name,
                    row_count,
                    min_rows,
                )
                return False

            index_name = f"{table_name}_hnsw"
            with conn.cursor() as cur:
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                    f'ON "{table_name}" USING hnsw (embedding vector_cosine_ops) '
                    f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)});"
                )
            conn.commit()
            logger.info(
                "Built HNSW index on %s (rows=%d, m=%d, ef_construction=%d)",
                table_name,
                row_count,
                m,
                ef_construction,
            )
            return True
        except psycopg2.Error as e:
            conn.rollback()
            raise RuntimeError(f"maybe_create_hnsw failed: {e}") from e

    # ---- search ---------------------------------------------------------

    @staticmethod
    def knn(
        conn,
        table_name: str,
        query_vec: np.ndarray,
        k: int = 1,
        threshold: float = 0.0,
    ) -> List[Tuple[int, float]]:
        """
        Return up to k nearest neighbours for `query_vec`, filtered by
        cosine similarity >= threshold.

        Returns a list of (idx, similarity) tuples ordered best first.
        `idx` is the original DataFrame row index stored at ingest time.
        """
        if k <= 0:
            return []

        literal = _vec_to_literal(query_vec)
        # cosine distance is `<=>`, similarity = 1 - distance
        # We compute similarity inline so the threshold filter is simple.
        sql = (
            f"SELECT idx, 1 - (embedding <=> %s::vector) AS similarity "
            f'FROM "{table_name}" '
            f"WHERE 1 - (embedding <=> %s::vector) >= %s "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s;"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (literal, literal, float(threshold), literal, int(k)))
                rows = cur.fetchall()
        except psycopg2.Error as e:
            conn.rollback()
            raise RuntimeError(f"knn query failed: {e}") from e

        return [(int(r[0]), float(r[1])) for r in rows]

    @staticmethod
    def knn_batch(
        conn,
        table_name: str,
        query_vecs: np.ndarray,
        k: int = 1,
        threshold: float = 0.0,
    ) -> List[List[Tuple[int, float]]]:
        """
        Convenience wrapper: call `knn` once per query vector.

        For small-to-medium left sides (up to a few thousand rows) this
        is fine. For much larger left sides you'd want a single
        cross-join query, but we leave that as a future optimisation —
        per-row kNN is what makes the HNSW index actually pay off.
        """
        return [VectorStore.knn(conn, table_name, q, k=k, threshold=threshold) for q in query_vecs]


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _vec_to_literal(vec) -> str:
    """
    Format a numpy array (or list) as a pgvector textual literal: '[x,y,z]'.

    Using the textual format means we don't need to register the vector
    type with psycopg2 — the server-side `::vector` cast handles it.
    """
    arr = np.asarray(vec, dtype=np.float32).ravel()
    # Compact, no spaces — matches pgvector's canonical text format
    return "[" + ",".join(f"{float(x):.7g}" for x in arr) + "]"
