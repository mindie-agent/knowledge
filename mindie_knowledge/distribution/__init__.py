"""OVPack distribution: build, release adaptation and local version sync.

Pipeline: fixed Git Markdown -> native dense OVPack (OpenViking 0.4.19,
``include_vectors=True``) -> release manifest with version/model/checksum
pins -> local release directory (network publishing disabled this round) ->
client download into isolated staging -> integrity + model contract checks ->
native ``import_ovpack(vector_mode="require")`` into an independent shared
version URI -> atomic ``current.json`` switch. Failures keep the old version.
"""

from __future__ import annotations

from mindie_knowledge.distribution.build import BUILD_ROOT_PREFIX, BuildResult, build_pack
from mindie_knowledge.distribution.client import (
    connect_client,
    embedding_info_from_health,
    provision_tenant_key,
)
from mindie_knowledge.distribution.errors import (
    BuildError,
    CorruptPack,
    DistributionError,
    ImportFailed,
    IncompatiblePack,
    ReleaseError,
    SourceUnavailable,
    SwitchInProgress,
)
from mindie_knowledge.distribution.manifest import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    OPENVIKING_SDK_VERSION,
    OPENVIKING_VERSION,
    RELEASE_SCHEMA,
    SHARED_PARENT_URI,
    ExpectedContract,
    ReleaseManifest,
    shared_root_uri,
    validate_release_manifest,
    version_id_from_sha,
)
from mindie_knowledge.distribution.pack import PackInfo, inspect_pack, verify_model_files, verify_pack
from mindie_knowledge.distribution.release import (
    LocalReleaseSource,
    ReleaseSnapshot,
    make_release,
    publish_release,
    source_from_location,
)
from mindie_knowledge.distribution.sync import (
    DistributionState,
    SyncResult,
    check_and_sync,
    current_shared,
)

__all__ = [
    "BUILD_ROOT_PREFIX",
    "BuildError",
    "BuildResult",
    "CorruptPack",
    "DistributionError",
    "DistributionState",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_MODEL",
    "EMBEDDING_PROVIDER",
    "ExpectedContract",
    "ImportFailed",
    "IncompatiblePack",
    "LocalReleaseSource",
    "OPENVIKING_SDK_VERSION",
    "OPENVIKING_VERSION",
    "PackInfo",
    "RELEASE_SCHEMA",
    "ReleaseError",
    "ReleaseManifest",
    "ReleaseSnapshot",
    "SHARED_PARENT_URI",
    "SourceUnavailable",
    "SwitchInProgress",
    "SyncResult",
    "build_pack",
    "check_and_sync",
    "connect_client",
    "current_shared",
    "embedding_info_from_health",
    "inspect_pack",
    "make_release",
    "provision_tenant_key",
    "publish_release",
    "shared_root_uri",
    "source_from_location",
    "validate_release_manifest",
    "verify_model_files",
    "verify_pack",
    "version_id_from_sha",
]
