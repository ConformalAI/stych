"""The implementation contract for current and future data connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pyarrow import RecordBatch

from .capabilities import Capability, CapabilitySupport, ConnectorManifest
from .requests import (
    ConnectionTestResult,
    DiscoveryRequest,
    NativeQueryRequest,
    PreviewRequest,
    QueryEstimate,
    QueryHandle,
    SnapshotRequest,
    SnapshotResult,
)
from .sources import AssetRef, SourceRef


class UnsupportedCapability(RuntimeError):
    """A typed failure for optional methods a connector does not implement."""

    def __init__(self, capability: Capability, connector_id: str):
        self.capability = capability
        self.connector_id = connector_id
        super().__init__(f"{connector_id} does not support {capability.value}")


class Connector(ABC):
    """Stable connector boundary; methods may delegate to existing source modules."""

    @property
    @abstractmethod
    def manifest(self) -> ConnectorManifest:
        """Return the static, credential-free connector manifest."""

    @abstractmethod
    def test_connection(self, connection_ref: str) -> ConnectionTestResult:
        """Validate a credential reference without returning secret material."""

    @abstractmethod
    def discover(self, connection_ref: str, request: DiscoveryRequest) -> Sequence[AssetRef]:
        """Discover namespaces and assets visible through a connection."""

    @abstractmethod
    def describe(self, source: SourceRef) -> Sequence[Mapping[str, Any]]:
        """Return the source schema."""

    def capabilities(self, source: SourceRef) -> Mapping[Capability, CapabilitySupport]:
        """Override only when support depends on the selected runtime asset."""
        return self.manifest.capabilities

    @abstractmethod
    def preview(self, source: SourceRef, request: PreviewRequest) -> Sequence[RecordBatch]:
        """Return a bounded preview page."""

    def estimate(self, source: SourceRef, request: NativeQueryRequest) -> QueryEstimate:
        raise UnsupportedCapability(Capability.ESTIMATE_QUERY, self.manifest.id)

    def execute(self, source: SourceRef, request: NativeQueryRequest) -> QueryHandle:
        raise UnsupportedCapability(Capability.EXECUTE_NATIVE_QUERY, self.manifest.id)

    def stream(self, handle: QueryHandle) -> Iterator[RecordBatch]:
        raise UnsupportedCapability(Capability.STREAM_ARROW, self.manifest.id)

    def export_snapshot(self, source: SourceRef, request: SnapshotRequest) -> SnapshotResult:
        raise UnsupportedCapability(Capability.EXPORT_SNAPSHOT, self.manifest.id)

    def cancel(self, handle: QueryHandle) -> None:
        raise UnsupportedCapability(Capability.CANCEL_QUERY, self.manifest.id)

    def cleanup(self, source: SourceRef) -> None:
        """Release connector-owned temporary resources. Default is a no-op."""
