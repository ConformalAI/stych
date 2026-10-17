"""
Configurable file storage backend.

Supports:
  - local  : local filesystem (default, good for dev / single-node)
  - s3     : S3-compatible object storage (AWS S3, MinIO, etc.)

Backend is selected via config.yaml  storage.backend  (or env STORAGE_BACKEND).
Call init_storage(config) once at startup; everywhere else just use the
module-level  storage_manager  singleton (backward-compatible import).
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class StorageBackend(ABC):
    """Common interface every storage backend must implement."""

    @abstractmethod
    def save_upload(self, file_id: str, filename: str, content: bytes) -> str:
        """Persist *content* and return an opaque storage key."""
        ...

    @abstractmethod
    def get_upload_bytes(self, file_id: str, filename: str) -> Optional[bytes]:
        """Return raw bytes for the file, or None if it does not exist."""
        ...

    @abstractmethod
    def delete_upload(self, file_id: str) -> bool:
        """Delete every object belonging to *file_id*. Return True if anything was removed."""
        ...

    # ------------------------------------------------------------------
    # Session snapshot I/O (working DataFrame persistence)
    # Snapshots are stored under a separate namespace from uploads.
    # Each object is identified by (session_id, name) where name is
    # a filename-safe string like "{file_id}.parquet" or "__manifest__.json".
    # ------------------------------------------------------------------

    @abstractmethod
    def save_snapshot_object(self, session_id: str, name: str, content: bytes) -> None:
        """Persist a snapshot object (parquet / JSON) for *session_id*."""
        ...

    @abstractmethod
    def load_snapshot_object(self, session_id: str, name: str) -> Optional[bytes]:
        """Return snapshot object bytes or None if not found."""
        ...

    @abstractmethod
    def delete_session_snapshots(self, session_id: str) -> int:
        """Delete all snapshot objects for *session_id*. Return count deleted."""
        ...

    @abstractmethod
    def delete_snapshot_object(self, session_id: str, name: str) -> bool:
        """Delete a single snapshot object. Return True if it existed."""
        ...

    # ------------------------------------------------------------------
    # Cleaning workspace archive I/O
    # Each file is identified by (user_id, session_id, name).
    # S3 key pattern:  workspace-files/{user_id}/{session_id}/{name}
    # ------------------------------------------------------------------

    @abstractmethod
    def save_workspace_file(self, user_id: str, session_id: str, name: str, content: bytes) -> None:
        """Persist a cleaning workspace file for the given user/session."""
        ...

    @abstractmethod
    def list_workspace_files(self, user_id: str, session_id: str) -> list:
        """Return a list of filenames archived for this user/session."""
        ...

    @abstractmethod
    def load_workspace_file(self, user_id: str, session_id: str, name: str) -> Optional[bytes]:
        """Return file bytes or None if not found."""
        ...

    @abstractmethod
    def delete_workspace_archive(self, user_id: str, session_id: str) -> int:
        """Delete all archived workspace files for this user/session. Return count deleted."""
        ...

    @abstractmethod
    def delete_workspace_file(self, user_id: str, session_id: str, name: str) -> bool:
        """Delete a single archived workspace file. Return True if it existed."""
        ...

    @abstractmethod
    def get_stats(self) -> dict:
        """Return a backend-specific statistics / info dict."""
        ...


# ---------------------------------------------------------------------------
# Local filesystem backend
# ---------------------------------------------------------------------------


class LocalStorageBackend(StorageBackend):
    """Stores files on the local filesystem under upload_dir/{file_id}/{filename}."""

    def __init__(
        self,
        upload_dir: str = "./data/uploads",
        temp_dir: str = "./data/temp",
        snapshot_dir: str = "./data/snapshots",
        workspace_archive_dir: str = "./data/workspace-archives",
    ) -> None:
        self.upload_dir = Path(upload_dir)
        self.temp_dir = Path(temp_dir)
        self.snapshot_dir = Path(snapshot_dir)
        self.workspace_archive_dir = Path(workspace_archive_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "LocalStorageBackend ready: upload=%s  temp=%s  snapshots=%s  workspace_archive=%s",
            self.upload_dir,
            self.temp_dir,
            self.snapshot_dir,
            self.workspace_archive_dir,
        )

    # ------------------------------------------------------------------
    # StorageBackend interface
    # ------------------------------------------------------------------

    def save_upload(self, file_id: str, filename: str, content: bytes) -> str:
        file_dir = self.upload_dir / file_id
        file_dir.mkdir(exist_ok=True)
        file_path = file_dir / filename
        file_path.write_bytes(content)
        logger.info("LocalStorage: saved %s (%d bytes)", file_path, len(content))
        return str(file_path)

    def get_upload_bytes(self, file_id: str, filename: str) -> Optional[bytes]:
        file_path = self.upload_dir / file_id / filename
        if file_path.exists():
            return file_path.read_bytes()
        return None

    def delete_upload(self, file_id: str) -> bool:
        import shutil

        file_dir = self.upload_dir / file_id
        if file_dir.exists():
            shutil.rmtree(file_dir)
            logger.info("LocalStorage: deleted directory %s", file_dir)
            return True
        return False

    # ------------------------------------------------------------------
    # Snapshot I/O
    # ------------------------------------------------------------------

    def save_snapshot_object(self, session_id: str, name: str, content: bytes) -> None:
        session_snap_dir = self.snapshot_dir / session_id
        session_snap_dir.mkdir(parents=True, exist_ok=True)
        (session_snap_dir / name).write_bytes(content)
        logger.debug(
            "LocalStorage: snapshot saved %s/%s (%d bytes)",
            session_id,
            name,
            len(content),
        )

    def load_snapshot_object(self, session_id: str, name: str) -> Optional[bytes]:
        p = self.snapshot_dir / session_id / name
        return p.read_bytes() if p.exists() else None

    def delete_session_snapshots(self, session_id: str) -> int:
        import shutil

        session_snap_dir = self.snapshot_dir / session_id
        if not session_snap_dir.exists():
            return 0
        count = sum(1 for f in session_snap_dir.rglob("*") if f.is_file())
        shutil.rmtree(session_snap_dir)
        logger.info(
            "LocalStorage: deleted %d snapshot objects for session %s",
            count,
            session_id,
        )
        return count

    def delete_snapshot_object(self, session_id: str, name: str) -> bool:
        p = self.snapshot_dir / session_id / name
        if not p.exists():
            return False
        p.unlink()
        logger.debug("LocalStorage: deleted snapshot object %s/%s", session_id, name)
        return True

    # ------------------------------------------------------------------
    # Workspace archive I/O
    # ------------------------------------------------------------------

    def _workspace_archive_dir(self, user_id: str, session_id: str) -> Path:
        return self.workspace_archive_dir / user_id / session_id

    def save_workspace_file(self, user_id: str, session_id: str, name: str, content: bytes) -> None:
        d = self._workspace_archive_dir(user_id, session_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(content)
        logger.debug("LocalStorage: workspace file saved %s/%s/%s", user_id, session_id, name)

    def list_workspace_files(self, user_id: str, session_id: str) -> list:
        d = self._workspace_archive_dir(user_id, session_id)
        if not d.exists():
            return []
        return [f.name for f in sorted(d.iterdir()) if f.is_file()]

    def load_workspace_file(self, user_id: str, session_id: str, name: str) -> Optional[bytes]:
        p = self._workspace_archive_dir(user_id, session_id) / name
        return p.read_bytes() if p.exists() else None

    def delete_workspace_archive(self, user_id: str, session_id: str) -> int:
        import shutil

        d = self._workspace_archive_dir(user_id, session_id)
        if not d.exists():
            return 0
        count = sum(1 for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d)
        logger.info(
            "LocalStorage: deleted %d workspace archive files for %s/%s",
            count,
            user_id,
            session_id,
        )
        return count

    def delete_workspace_file(self, user_id: str, session_id: str, name: str) -> bool:
        p = self._workspace_archive_dir(user_id, session_id) / name
        if not p.exists():
            return False
        p.unlink()
        logger.debug("LocalStorage: deleted workspace file %s/%s/%s", user_id, session_id, name)
        return True

    def get_stats(self) -> dict:
        def _dir_size(path: Path) -> int:
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

        upload_bytes = _dir_size(self.upload_dir)
        temp_bytes = _dir_size(self.temp_dir)
        snap_bytes = _dir_size(self.snapshot_dir)
        archive_bytes = _dir_size(self.workspace_archive_dir)
        return {
            "backend": "local",
            "upload_dir": str(self.upload_dir),
            "temp_dir": str(self.temp_dir),
            "snapshot_dir": str(self.snapshot_dir),
            "workspace_archive_dir": str(self.workspace_archive_dir),
            "upload_size_mb": upload_bytes / (1024 * 1024),
            "temp_size_mb": temp_bytes / (1024 * 1024),
            "snapshot_size_mb": snap_bytes / (1024 * 1024),
            "workspace_archive_size_mb": archive_bytes / (1024 * 1024),
            "total_size_mb": (upload_bytes + temp_bytes + snap_bytes + archive_bytes)
            / (1024 * 1024),
        }

    # ------------------------------------------------------------------
    # Local-only helpers (optional callers)
    # ------------------------------------------------------------------

    def get_upload_path(self, file_id: str, filename: str) -> Optional[str]:
        """Return local filesystem path or None (local backend only)."""
        p = self.upload_dir / file_id / filename
        return str(p) if p.exists() else None

    def cleanup_old_temp_files(self, max_age_hours: int = 24) -> int:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        count = 0
        for f in self.temp_dir.iterdir():
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                count += 1
        if count:
            logger.info("LocalStorage: cleaned %d old temp files", count)
        return count


# ---------------------------------------------------------------------------
# S3-compatible backend (AWS S3 / MinIO)
# ---------------------------------------------------------------------------


class S3StorageBackend(StorageBackend):
    """
    Stores files in an S3-compatible object store (AWS S3 or MinIO).

    Object keys follow the pattern  uploads/{file_id}/{filename}  so the
    same logical path structure as the local backend is preserved.

    Requires:  pip install boto3
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "us-east-1",
        path_style: bool = True,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise ImportError("boto3 is required for S3 storage. Run: pip install boto3") from exc

        self.bucket = bucket
        boto_config = BotoConfig(s3={"addressing_style": "path" if path_style else "auto"})
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=boto_config,
        )
        self._ensure_bucket()
        logger.info(
            "S3StorageBackend ready: bucket=%s  endpoint=%s",
            bucket,
            endpoint_url or "AWS default",
        )

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
                logger.info("S3Storage: created bucket '%s'", self.bucket)
            except Exception as exc:
                logger.warning("S3Storage: could not create bucket '%s': %s", self.bucket, exc)

    @staticmethod
    def _object_key(file_id: str, filename: str) -> str:
        return f"uploads/{file_id}/{filename}"

    # ------------------------------------------------------------------
    # StorageBackend interface
    # ------------------------------------------------------------------

    def save_upload(self, file_id: str, filename: str, content: bytes) -> str:
        key = self._object_key(file_id, filename)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentLength=len(content),
        )
        logger.info("S3Storage: saved s3://%s/%s (%d bytes)", self.bucket, key, len(content))
        return key

    def get_upload_bytes(self, file_id: str, filename: str) -> Optional[bytes]:
        key = self._object_key(file_id, filename)
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:
            logger.debug("S3Storage: get_upload_bytes miss for %s: %s", key, exc)
            return None

    def _batch_delete(self, keys: list) -> None:
        """Delete S3 objects in batches of 1000 (AWS API limit)."""
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )

    def delete_upload(self, file_id: str) -> bool:
        prefix = f"uploads/{file_id}/"
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            keys = [
                obj["Key"]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
            if not keys:
                return False
            self._batch_delete(keys)
            logger.info("S3Storage: deleted %d objects under prefix %s", len(keys), prefix)
            return True
        except Exception as exc:
            logger.warning("S3Storage: delete failed for prefix %s: %s", prefix, exc)
            return False

    # ------------------------------------------------------------------
    # Snapshot I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_key(session_id: str, name: str) -> str:
        return f"session-snapshots/{session_id}/{name}"

    def save_snapshot_object(self, session_id: str, name: str, content: bytes) -> None:
        key = self._snapshot_key(session_id, name)
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=content, ContentLength=len(content)
        )
        logger.debug(
            "S3Storage: snapshot saved s3://%s/%s (%d bytes)",
            self.bucket,
            key,
            len(content),
        )

    def load_snapshot_object(self, session_id: str, name: str) -> Optional[bytes]:
        key = self._snapshot_key(session_id, name)
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:
            return None

    def delete_session_snapshots(self, session_id: str) -> int:
        prefix = f"session-snapshots/{session_id}/"
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            keys = [
                obj["Key"]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
            if not keys:
                return 0
            self._batch_delete(keys)
            logger.info(
                "S3Storage: deleted %d snapshot objects for session %s",
                len(keys),
                session_id,
            )
            return len(keys)
        except Exception as exc:
            logger.warning("S3Storage: delete_session_snapshots failed for %s: %s", session_id, exc)
            return 0

    def delete_snapshot_object(self, session_id: str, name: str) -> bool:
        key = self._snapshot_key(session_id, name)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            logger.warning("S3Storage: delete_snapshot_object failed for %s: %s", key, exc)
            return False

    # ------------------------------------------------------------------
    # Workspace archive I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _workspace_key(user_id: str, session_id: str, name: str) -> str:
        return f"workspace-files/{user_id}/{session_id}/{name}"

    @staticmethod
    def _workspace_prefix(user_id: str, session_id: str) -> str:
        return f"workspace-files/{user_id}/{session_id}/"

    def save_workspace_file(self, user_id: str, session_id: str, name: str, content: bytes) -> None:
        key = self._workspace_key(user_id, session_id, name)
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=content, ContentLength=len(content)
        )
        logger.debug("S3Storage: workspace file saved s3://%s/%s", self.bucket, key)

    def list_workspace_files(self, user_id: str, session_id: str) -> list:
        prefix = self._workspace_prefix(user_id, session_id)
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            return [
                obj["Key"][len(prefix) :]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
                if obj["Key"] != prefix
            ]
        except Exception as exc:
            logger.warning(
                "S3Storage: list_workspace_files failed for %s/%s: %s",
                user_id,
                session_id,
                exc,
            )
            return []

    def load_workspace_file(self, user_id: str, session_id: str, name: str) -> Optional[bytes]:
        key = self._workspace_key(user_id, session_id, name)
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:
            return None

    def delete_workspace_archive(self, user_id: str, session_id: str) -> int:
        prefix = self._workspace_prefix(user_id, session_id)
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            keys = [
                obj["Key"]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
            if not keys:
                return 0
            self._batch_delete(keys)
            logger.info(
                "S3Storage: deleted %d workspace archive files for %s/%s",
                len(keys),
                user_id,
                session_id,
            )
            return len(keys)
        except Exception as exc:
            logger.warning(
                "S3Storage: delete_workspace_archive failed for %s/%s: %s",
                user_id,
                session_id,
                exc,
            )
            return 0

    def delete_workspace_file(self, user_id: str, session_id: str, name: str) -> bool:
        key = self._workspace_key(user_id, session_id, name)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            logger.warning("S3Storage: delete_workspace_file failed for %s: %s", key, exc)
            return False

    def get_stats(self) -> dict:
        return {
            "backend": "s3",
            "bucket": self.bucket,
        }


# ---------------------------------------------------------------------------
# StorageManager — thin wrapper around the active backend
# ---------------------------------------------------------------------------


class StorageManager:
    """
    Public facade for file storage operations.

    The active backend is set at startup via init_storage(config).
    All callers should use the module-level  storage_manager  singleton.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return self._backend.get_stats().get("backend", "unknown")

    def save_upload(self, file_id: str, filename: str, content: bytes) -> str:
        """Save file bytes and return the storage key (path or S3 key)."""
        return self._backend.save_upload(file_id, filename, content)

    def get_upload_bytes(self, file_id: str, filename: str) -> Optional[bytes]:
        """Retrieve file bytes from storage, or None if not found."""
        return self._backend.get_upload_bytes(file_id, filename)

    def delete_upload(self, file_id: str) -> bool:
        """Delete all stored objects for this file_id."""
        return self._backend.delete_upload(file_id)

    # ------------------------------------------------------------------
    # Session snapshot passthrough
    # ------------------------------------------------------------------

    def save_snapshot_object(self, session_id: str, name: str, content: bytes) -> None:
        """Persist a snapshot object (e.g. parquet or JSON) for the given session."""
        self._backend.save_snapshot_object(session_id, name, content)

    def load_snapshot_object(self, session_id: str, name: str) -> Optional[bytes]:
        """Return snapshot object bytes or None if not found."""
        return self._backend.load_snapshot_object(session_id, name)

    def delete_session_snapshots(self, session_id: str) -> int:
        """Delete all snapshot objects for this session. Returns count deleted."""
        return self._backend.delete_session_snapshots(session_id)

    def delete_snapshot_object(self, session_id: str, name: str) -> bool:
        """Delete a single snapshot object. Returns True if it existed."""
        return self._backend.delete_snapshot_object(session_id, name)

    def get_upload_path(self, file_id: str, filename: str) -> Optional[str]:
        """Return local filesystem path for local backend, or None for S3/remote backends."""
        if hasattr(self._backend, "get_upload_path"):
            return self._backend.get_upload_path(file_id, filename)
        return None

    # ------------------------------------------------------------------
    # Cleaning workspace archive passthrough
    # ------------------------------------------------------------------

    def save_workspace_file(self, user_id: str, session_id: str, name: str, content: bytes) -> None:
        """Persist a cleaning workspace file to storage."""
        self._backend.save_workspace_file(user_id, session_id, name, content)

    def list_workspace_files(self, user_id: str, session_id: str) -> list:
        """List archived workspace filenames for this user/session."""
        return self._backend.list_workspace_files(user_id, session_id)

    def load_workspace_file(self, user_id: str, session_id: str, name: str) -> Optional[bytes]:
        """Return archived workspace file bytes, or None if not found."""
        return self._backend.load_workspace_file(user_id, session_id, name)

    def delete_workspace_archive(self, user_id: str, session_id: str) -> int:
        """Delete all archived workspace files for this user/session. Returns count deleted."""
        return self._backend.delete_workspace_archive(user_id, session_id)

    def delete_workspace_file(self, user_id: str, session_id: str, name: str) -> bool:
        """Delete a single archived workspace file. Returns True if it existed."""
        return self._backend.delete_workspace_file(user_id, session_id, name)

    def get_storage_stats(self) -> dict:
        return self._backend.get_stats()


# ---------------------------------------------------------------------------
# Module-level singleton — initialised at startup, importable everywhere
# ---------------------------------------------------------------------------

# Default to local so that any early import before init_storage() is safe.
storage_manager: StorageManager = StorageManager(LocalStorageBackend())


def init_storage(storage_config: dict) -> StorageManager:
    """
    Initialise the global storage_manager from the merged app config dict.

    Expected config shape (config.yaml -> storage section):
      backend: local | s3
      local:
        upload_dir: ./data/uploads
        temp_dir:   ./data/temp
      s3:
        endpoint_url: http://minio:9000
        access_key:   ...
        secret_key:   ...
        bucket:       stych-uploads
        region:       us-east-1
        path_style:   true

    Returns the global storage_manager after reinitialising its backend.
    """
    backend_name = str(storage_config.get("backend", "local")).lower()
    logger.info("Initialising storage backend: %s", backend_name)

    if backend_name == "s3":
        s3_cfg = storage_config.get("s3", {})
        new_backend: StorageBackend = S3StorageBackend(
            bucket=s3_cfg.get("bucket", "stych-uploads"),
            endpoint_url=s3_cfg.get("endpoint_url") or None,
            access_key=s3_cfg.get("access_key") or None,
            secret_key=s3_cfg.get("secret_key") or None,
            region=str(s3_cfg.get("region", "us-east-1")),
            path_style=str(s3_cfg.get("path_style", "true")).lower() not in ("false", "0"),
        )
    else:
        local_cfg = storage_config.get("local", {})
        new_backend = LocalStorageBackend(
            upload_dir=str(local_cfg.get("upload_dir", "./data/uploads")),
            temp_dir=str(local_cfg.get("temp_dir", "./data/temp")),
            snapshot_dir=str(local_cfg.get("snapshot_dir", "./data/snapshots")),
            workspace_archive_dir=str(
                local_cfg.get("workspace_archive_dir", "./data/workspace-archives")
            ),
        )

    # Mutate the backend in-place so that existing  `from helpers.storage import storage_manager`
    # references (which point to the same StorageManager object) pick up the new backend
    # without needing to be re-imported.
    storage_manager._backend = new_backend
    return storage_manager
