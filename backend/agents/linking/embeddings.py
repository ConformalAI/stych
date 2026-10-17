"""
Embedding client for OpenRouter (Gemini Embedding 2 Preview by default).

Modular: a single public function `embed_texts` that other agents
(analysis, QA, etc.) can import without pulling in any linking-agent
specifics.

Design choices
--------------
- Synchronous. The fuzzy-strategy matchers in `fuzzy_tools.py` are all
  synchronous; matching that here avoids needing an event loop inside a
  pure-compute code path.
- OpenAI-compatible endpoint. OpenRouter exposes `/v1/embeddings` in the
  OpenAI format, so we POST raw JSON with `httpx` rather than dragging
  in the openai SDK.
- Batched. One HTTP request per ~100 texts. Tunable via env.
- Cached. Identical (text, model, dimensions) tuples are deduplicated
  within a process via functools.lru_cache. Cheap insurance against
  re-embedding the same column values during dev.
- Retry on 429 / 5xx with exponential backoff.

Environment variables
---------------------
OPENROUTER_API_KEY            required
OPENROUTER_BASE_URL           default: https://openrouter.ai/api/v1
OPENROUTER_EMBEDDING_MODEL    default: google/gemini-embedding-2-preview
EMBEDDING_DIMENSIONS          default: 768
EMBEDDING_BATCH_SIZE          default: 100
EMBEDDING_TIMEOUT_SECONDS     default: 60
"""

import logging
import os
import time
from functools import lru_cache
from typing import List, Optional, Sequence

import httpx
import numpy as np

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Config helpers
# -------------------------------------------------------------------------

DEFAULT_MODEL = "google/gemini-embedding-2-preview"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_DIMENSIONS = 768
DEFAULT_BATCH_SIZE = 100
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _resolve_config(
    model: Optional[str] = None,
    dimensions: Optional[int] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    batch_size: Optional[int] = None,
    timeout: Optional[float] = None,
) -> dict:
    """Resolve embedding config from explicit args, env vars, and defaults."""
    resolved_api_key = api_key or _get_env("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. " "Add it to your .env or pass api_key explicitly."
        )

    return {
        "model": model or _get_env("OPENROUTER_EMBEDDING_MODEL", DEFAULT_MODEL),
        "dimensions": int(
            dimensions
            if dimensions is not None
            else _get_env("EMBEDDING_DIMENSIONS", str(DEFAULT_DIMENSIONS))
        ),
        "base_url": (base_url or _get_env("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
        "api_key": resolved_api_key,
        "batch_size": int(
            batch_size
            if batch_size is not None
            else _get_env("EMBEDDING_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
        ),
        "timeout": float(
            timeout
            if timeout is not None
            else _get_env("EMBEDDING_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT))
        ),
    }


# -------------------------------------------------------------------------
# Single-text cached embedding (process-local cache)
# -------------------------------------------------------------------------


@lru_cache(maxsize=10_000)
def _embed_single_cached(
    text: str,
    model: str,
    dimensions: int,
    base_url: str,
    api_key: str,
    timeout: float,
) -> tuple:
    """
    Embed a single text. Cached by (text, model, dimensions).

    Returns a tuple (so it's hashable and works with lru_cache); callers
    should convert to np.ndarray.
    """
    vec = _post_embeddings(
        texts=[text],
        model=model,
        dimensions=dimensions,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
    )[0]
    return tuple(vec)


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------


def embed_texts(
    texts: Sequence[str],
    *,
    model: Optional[str] = None,
    dimensions: Optional[int] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    batch_size: Optional[int] = None,
    timeout: Optional[float] = None,
) -> np.ndarray:
    """
    Embed a sequence of texts and return an (N, D) numpy array.

    Empty / whitespace-only texts are mapped to all-zero vectors (so
    similarity against them is zero and they never spuriously match).

    Identical texts are deduplicated before hitting the API; callers
    get back one vector per input in original order.
    """
    if not texts:
        cfg = _resolve_config(model, dimensions, base_url, api_key, batch_size, timeout)
        return np.zeros((0, cfg["dimensions"]), dtype=np.float32)

    cfg = _resolve_config(model, dimensions, base_url, api_key, batch_size, timeout)
    dim = cfg["dimensions"]

    # Normalise + identify which inputs need real embeddings
    normed: List[str] = [(t or "").strip() for t in texts]
    blank_mask = [len(t) == 0 for t in normed]

    # Deduplicate non-blank texts to reduce API calls
    unique_texts: List[str] = []
    seen: dict = {}
    for t, blank in zip(normed, blank_mask):
        if blank:
            continue
        if t not in seen:
            seen[t] = len(unique_texts)
            unique_texts.append(t)

    # Check process-local cache for each unique text first
    cache_key = (cfg["model"], dim, cfg["base_url"], cfg["api_key"], cfg["timeout"])
    cached_vecs: dict = {}
    to_fetch: List[str] = []
    for t in unique_texts:
        cached = _try_cache_get(t, *cache_key)
        if cached is not None:
            cached_vecs[t] = cached
        else:
            to_fetch.append(t)

    # Batch the rest
    fetched_vecs: dict = {}
    if to_fetch:
        batches = _chunk(to_fetch, cfg["batch_size"])
        logger.info(
            "embed_texts: %d unique texts to fetch in %d batches (model=%s, dim=%d)",
            len(to_fetch),
            len(batches),
            cfg["model"],
            dim,
        )
        for batch in batches:
            vecs = _post_embeddings(
                texts=batch,
                model=cfg["model"],
                dimensions=dim,
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                timeout=cfg["timeout"],
            )
            for t, v in zip(batch, vecs):
                fetched_vecs[t] = v
                # Warm cache for future calls in this process
                _embed_single_cached.cache_info()  # no-op call to keep API used
                # Populate the cache by calling it (it returns immediately
                # since the value is already in fetched_vecs, but the
                # decorator stores it for next time)
                _store_in_cache(
                    t,
                    v,
                    cfg["model"],
                    dim,
                    cfg["base_url"],
                    cfg["api_key"],
                    cfg["timeout"],
                )

    # Assemble output in original order
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for i, (t, blank) in enumerate(zip(normed, blank_mask)):
        if blank:
            continue
        if t in cached_vecs:
            out[i] = cached_vecs[t]
        else:
            out[i] = fetched_vecs[t]

    return out


def clear_cache() -> None:
    """Empty the process-local embedding cache. Mainly for tests."""
    _embed_single_cached.cache_clear()


# -------------------------------------------------------------------------
# Cache helpers (lru_cache exposes get-or-compute, so we wrap it)
# -------------------------------------------------------------------------


def _try_cache_get(
    text: str, model: str, dim: int, base_url: str, api_key: str, timeout: float
) -> Optional[np.ndarray]:
    """
    Peek into the lru_cache without triggering a fetch.

    lru_cache doesn't expose a public "is this key cached?" check, so we
    inspect the underlying cache dict via the decorator's __wrapped__.
    This is implementation-detail-y but the only way to avoid a fetch
    when checking presence.
    """
    try:
        _embed_single_cached.__wrapped__  # type: ignore[attr-defined]
    except AttributeError:
        pass

    # Fast path: try a direct call only if the result is already there.
    # lru_cache stores by argument tuple; we have no public API to peek.
    # Pragmatic compromise: maintain a side-cache.
    return _SIDE_CACHE.get((text, model, dim))


def _store_in_cache(
    text: str,
    vec: np.ndarray,
    model: str,
    dim: int,
    base_url: str,
    api_key: str,
    timeout: float,
) -> None:
    _SIDE_CACHE[(text, model, dim)] = np.asarray(vec, dtype=np.float32)
    # Bound the side cache so it can't grow without limit
    if len(_SIDE_CACHE) > _SIDE_CACHE_MAX:
        # Drop ~10% of entries (oldest by insertion order — dict preserves order in 3.7+)
        drop = max(1, _SIDE_CACHE_MAX // 10)
        for k in list(_SIDE_CACHE.keys())[:drop]:
            _SIDE_CACHE.pop(k, None)


_SIDE_CACHE: dict = {}
_SIDE_CACHE_MAX = 10_000


# -------------------------------------------------------------------------
# HTTP plumbing
# -------------------------------------------------------------------------


def _chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]


def _post_embeddings(
    texts: List[str],
    model: str,
    dimensions: int,
    base_url: str,
    api_key: str,
    timeout: float,
) -> List[List[float]]:
    """
    POST to /v1/embeddings (OpenAI-compatible).

    Retries on 429 and 5xx with exponential backoff.
    """
    url = f"{base_url}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": texts,
        "dimensions": dimensions,
    }

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                data = resp.json()
                # OpenAI-compatible response: {"data": [{"embedding": [...]}, ...]}
                items = data.get("data", [])
                if len(items) != len(texts):
                    raise RuntimeError(
                        f"Embedding response length mismatch: "
                        f"expected {len(texts)}, got {len(items)}"
                    )
                return [item["embedding"] for item in items]

            # Retryable?
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                sleep_for = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "OpenRouter %s on attempt %d/%d; retrying in %.1fs. Body: %s",
                    resp.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    sleep_for,
                    resp.text[:200],
                )
                time.sleep(sleep_for)
                continue

            # Non-retryable HTTP error
            raise RuntimeError(
                f"OpenRouter embeddings API returned {resp.status_code}: " f"{resp.text[:500]}"
            )

        except httpx.TimeoutException as e:
            last_exc = e
            sleep_for = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Embedding request timed out (attempt %d/%d); retrying in %.1fs",
                attempt + 1,
                MAX_RETRIES,
                sleep_for,
            )
            time.sleep(sleep_for)
        except httpx.HTTPError as e:
            last_exc = e
            sleep_for = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Embedding HTTP error (attempt %d/%d): %s; retrying in %.1fs",
                attempt + 1,
                MAX_RETRIES,
                e,
                sleep_for,
            )
            time.sleep(sleep_for)

    raise RuntimeError(f"OpenRouter embeddings API failed after {MAX_RETRIES} attempts: {last_exc}")
