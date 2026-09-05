"""外部表情包源集成。

该子包只依赖一层小而清楚的交换格式：它能读取表情包归档、GitHub
仓库以及保守的 HTTP JSON 目录，而不会去引用另一个插件的私有运行时对象。
"""

from .models import (
    ExternalSourceError,
    ExternalSourceSecurityError,
    PackInspection,
    SourceItem,
    SourceInspection,
)
from .github_source import GitHubSource
from .http_source import HTTPSource
from .pack_source import PackSource
from .source_service import SourceService

__all__ = [
    "ExternalSourceError",
    "ExternalSourceSecurityError",
    "PackInspection",
    "SourceItem",
    "SourceInspection",
    "GitHubSource",
    "HTTPSource",
    "PackSource",
    "SourceService",
]
