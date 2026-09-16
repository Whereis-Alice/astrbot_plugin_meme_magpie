"""Stable, deliberately small integration surfaces for other AstrBot plugins.

The integration package must not import AstrBook, imgbed_ferry, or any other
consumer.  Consumers use the duck-typed API documented in
``docs/integration.md`` so that the meme library remains the single owner of
selection and metadata.
"""

from .meme_asset import (
    MEME_ASSET_API_VERSION,
    MemeAssetError,
    MemeAssetExportError,
    MemeAssetExportService,
    MemeAssetHandle,
    MemeMagpieIntegrationAPI,
)
from .candidate_search import MemeCandidateSearchService

__all__ = [
    "MEME_ASSET_API_VERSION",
    "MemeAssetError",
    "MemeAssetExportError",
    "MemeAssetExportService",
    "MemeAssetHandle",
    "MemeMagpieIntegrationAPI",
    "MemeCandidateSearchService",
]
