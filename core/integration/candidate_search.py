"""Structured candidate search for cross-plugin meme integrations.

The regular ``magpie_search_meme`` tool keeps its conversational formatting in
``main.py``.  This service owns the separate machine-facing search contract
used by AstrBook and other cooperating plugins: it reuses the existing
``MemeSelector`` search implementation, keeps the internal path only in the
current event state, and returns metadata-only candidate records.
"""

from __future__ import annotations

import os
import time
from typing import Any

from astrbot.api import logger

from ..events.event_context import unwrap_event
from ..util.normalization import (
    canonicalize_path,
    normalize_label_list,
    normalize_scope_mode,
)


class MemeCandidateSearchService:
    """Build and store safe, structured candidates for integration callers."""

    DEFAULT_MAX_RESULTS = 5
    HARD_MAX_RESULTS = 50

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def search_meme_candidates(
        self,
        event: Any,
        query: str = "",
        *,
        limit: int = DEFAULT_MAX_RESULTS,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search existing meme candidates and retain their private paths.

        The returned dictionaries deliberately omit ``path``.  The matching
        internal records remain in the current event's turn state so a caller
        can pass the returned ``emoji_id`` to the asset-export service without
        ever guessing a local filename.
        """

        event = unwrap_event(event)
        query = str(query or "").strip()
        turn_state = None
        try:
            turn_state = self._emoji_turn_state(event)
            turn_state.set_candidates([])
        except Exception:
            pass

        if event is None:
            return []
        if not query and not filters:
            return []

        checker = getattr(self.plugin, "is_send_enabled_for_event", None)
        if callable(checker):
            try:
                if not checker(event):
                    return []
            except Exception:
                # Keep compatibility with light-weight test/adaptor objects;
                # the exporter performs the final permission check.
                pass

        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = self._max_results()
        if requested_limit <= 0:
            return []
        requested_limit = min(requested_limit, self.HARD_MAX_RESULTS)
        # Filtering happens after the selector returns its ranked results.  Ask
        # for a wider window so an early non-matching result does not produce a
        # misleading empty response.
        search_limit = min(
            self.HARD_MAX_RESULTS,
            requested_limit * (4 if filters else 1),
        )

        idx = await self._load_index()
        try:
            raw_results = await self._search_meme_candidates(
                event,
                query,
                limit=search_limit,
                idx=idx,
            )
        except Exception as exc:
            logger.warning("[Integration] 结构化搜索失败: %s", exc)
            return []

        created_at = time.time()
        candidates: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for raw_result in raw_results or []:
            candidate = self._make_integration_candidate(raw_result, idx, created_at)
            if candidate is None:
                continue
            path = str(candidate.get("path") or "")
            path_key = canonicalize_path(path)
            if not path_key or path_key in seen_paths:
                continue
            if not self._candidate_file_or_record_exists(
                path,
                self._search_index_meta(idx, path),
            ):
                continue
            if not self._candidate_matches_filters(candidate, filters):
                continue
            seen_paths.add(path_key)
            candidates.append(candidate)
            if len(candidates) >= requested_limit:
                break

        for index, candidate in enumerate(candidates, start=1):
            candidate_id = f"emoji_{index}"
            candidate["id"] = candidate_id
            candidate["emoji_id"] = candidate_id

        if turn_state is not None:
            try:
                turn_state.set_candidates(candidates)
            except Exception:
                pass
        return [self._public_integration_candidate(candidate) for candidate in candidates]

    async def _load_index(self) -> dict[str, Any]:
        """Load the existing read-only index without rebuilding it."""

        idx: dict[str, Any] = {}
        db = getattr(self.plugin, "db_service", None)
        try:
            count_total = getattr(db, "count_total", None)
            if callable(count_total) and count_total() > 0:
                getter = getattr(db, "get_index_cache_readonly", None)
                if callable(getter):
                    idx = getter() or {}
            if not idx:
                manager = getattr(self.plugin, "index_manager", None)
                loader = getattr(manager, "load_index", None)
                if callable(loader):
                    loaded = await loader()
                    if isinstance(loaded, dict):
                        idx = loaded
                if not idx:
                    getter = getattr(db, "get_index_cache_readonly", None)
                    if callable(getter):
                        idx = getter() or {}
        except Exception as exc:
            logger.debug("[Integration] 加载表情索引失败: %s", exc)
        return idx if isinstance(idx, dict) else {}

    def _max_results(self) -> int:
        try:
            value = int(getattr(self.plugin, "MAX_SEARCH_RESULTS", self.DEFAULT_MAX_RESULTS))
        except (TypeError, ValueError):
            value = self.DEFAULT_MAX_RESULTS
        return max(1, min(value, self.HARD_MAX_RESULTS))

    async def _search_meme_candidates(
        self,
        event: Any,
        query: str,
        *,
        limit: int,
        idx: dict[str, Any],
    ) -> Any:
        """Reuse the plugin's selector; no second classification pipeline."""

        delegated = getattr(self.plugin, "_search_meme_candidates", None)
        if callable(delegated):
            return await delegated(event, query, limit=limit, idx=idx)
        selector = getattr(self.plugin, "meme_selector", None)
        searcher = getattr(selector, "smart_search", None)
        if not callable(searcher):
            return []
        return await searcher(query, limit=limit, idx=idx, event=event)

    def _emoji_turn_state(self, event: Any) -> Any:
        getter = getattr(self.plugin, "_emoji_turn_state", None)
        if not callable(getter):
            return None
        return getter(event)

    def _search_index_meta(self, idx: dict[str, Any] | None, path: str) -> dict[str, Any]:
        """Find metadata by literal or canonical path, then fall back to DB."""

        if isinstance(idx, dict):
            direct = idx.get(path)
            if isinstance(direct, dict):
                return direct
            wanted = canonicalize_path(path)
            if wanted:
                for stored_path, value in idx.items():
                    if canonicalize_path(stored_path) == wanted and isinstance(value, dict):
                        return value

        db = getattr(self.plugin, "db_service", None)
        getter = getattr(db, "get_emoji", None)
        if callable(getter) and path:
            try:
                value = getter(path)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
        return {}

    def _candidate_file_or_record_exists(
        self,
        path: str,
        meta: dict[str, Any],
    ) -> bool:
        """Keep stale-path records visible so export can recover by hash."""

        if path and os.path.isfile(path):
            return True
        if not isinstance(meta, dict):
            return False
        db = getattr(self.plugin, "db_service", None)
        if db is None:
            return False
        getter = getattr(db, "get_emoji", None)
        if callable(getter) and path:
            try:
                if getter(path) is not None:
                    return True
            except Exception:
                pass
        hash_value = str(meta.get("hash") or "").strip()
        by_hash = getattr(db, "get_emoji_by_hash", None)
        if hash_value and callable(by_hash):
            try:
                return by_hash(hash_value) is not None
            except Exception:
                pass
        return False

    def _make_integration_candidate(
        self,
        raw_result: Any,
        idx: dict[str, Any] | None,
        created_at: float,
    ) -> dict[str, Any] | None:
        """Convert selector tuples to an internal integration candidate."""

        path = ""
        tuple_desc = ""
        tuple_category = ""
        tuple_tags: Any = []
        if isinstance(raw_result, dict):
            path = str(raw_result.get("path") or "").strip()
            tuple_desc = str(raw_result.get("desc") or "")
            tuple_category = str(
                raw_result.get("category") or raw_result.get("emotion") or ""
            )
            tuple_tags = raw_result.get("tags", [])
        elif isinstance(raw_result, (list, tuple)) and raw_result:
            path = str(raw_result[0] or "").strip()
            tuple_desc = str(raw_result[1] or "") if len(raw_result) > 1 else ""
            tuple_category = str(raw_result[2] or "") if len(raw_result) > 2 else ""
            tuple_tags = raw_result[3] if len(raw_result) > 3 else []
        if not path:
            return None

        meta = self._search_index_meta(idx, path)
        desc = str(meta.get("desc") or tuple_desc or "").strip()
        category = str(
            meta.get("category") or tuple_category or meta.get("emotion") or ""
        ).strip()
        raw_tags = meta.get("tags") or tuple_tags
        raw_scenes = meta.get("scenes") or meta.get("scene")
        tags = normalize_label_list(raw_tags)
        scenes = normalize_label_list(raw_scenes)
        character = str(meta.get("character") or "").strip()
        work = str(meta.get("work") or "").strip()
        overlay_text = str(meta.get("overlay_text") or "").strip()
        action = str(meta.get("action") or "").strip()
        source = str(meta.get("source") or "").strip()
        scope_mode = normalize_scope_mode(meta.get("scope_mode")) or "public"
        origin_target = str(meta.get("origin_target") or "").strip()
        hash_value = str(meta.get("hash") or "").strip().lower()
        try:
            use_count = int(meta.get("use_count", 0) or 0)
        except (TypeError, ValueError):
            use_count = 0
        favorite_value = meta.get("is_favorite", False)
        if isinstance(favorite_value, str):
            favorite = favorite_value.strip().casefold() in {
                "1",
                "true",
                "yes",
                "on",
                "是",
            }
        else:
            favorite = bool(favorite_value)

        return {
            "path": path,
            "desc": desc,
            "emotion": category,
            "category": category,
            "tags": tags,
            "scenes": scenes,
            "overlay_text": overlay_text,
            "character": character,
            "work": work,
            "action": action,
            "source": source,
            "scope_mode": scope_mode,
            "origin_target": origin_target,
            "hash": hash_value,
            "use_count": max(0, use_count),
            "is_favorite": favorite,
            "candidate_created_at": created_at,
        }

    @staticmethod
    def _filter_tokens(value: Any) -> list[str]:
        if isinstance(value, bool):
            return ["true" if value else "false"]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        return normalize_label_list(text) or [text]

    @classmethod
    def _candidate_matches_filters(
        cls,
        candidate: dict[str, Any],
        filters: dict[str, Any] | None,
    ) -> bool:
        """Apply optional metadata filters; unknown keys are ignored."""

        if not isinstance(filters, dict) or not filters:
            return True
        aliases = {
            "category": "category",
            "emotion": "emotion",
            "work": "work",
            "character": "character",
            "tag": "tags",
            "tags": "tags",
            "scene": "scenes",
            "scenes": "scenes",
            "scope": "scope_mode",
            "scope_mode": "scope_mode",
            "source": "source",
            "overlay": "overlay_text",
            "overlay_text": "overlay_text",
            "favorite": "is_favorite",
            "is_favorite": "is_favorite",
        }
        list_fields = {"tags", "scenes"}
        for raw_key, raw_value in filters.items():
            field_name = aliases.get(str(raw_key or "").strip().casefold())
            if field_name is None:
                continue
            requested = cls._filter_tokens(raw_value)
            if not requested:
                continue

            if field_name == "is_favorite":
                wanted = requested[0].casefold() in {"1", "true", "yes", "on", "是"}
                if bool(candidate.get(field_name, False)) != wanted:
                    return False
                continue

            if field_name == "scope_mode":
                actual = normalize_scope_mode(candidate.get(field_name)) or "public"
                if not any(
                    (normalize_scope_mode(item, default=None) or str(item).casefold())
                    == actual
                    for item in requested
                ):
                    return False
                continue

            if field_name in list_fields:
                actual_values = normalize_label_list(candidate.get(field_name))
                if any(item == "__none__" for item in requested):
                    if actual_values:
                        return False
                    requested = [item for item in requested if item != "__none__"]
                    if not requested:
                        continue
                actual_folded = [item.casefold() for item in actual_values]
                if not any(
                    item.casefold() == actual_item or item.casefold() in actual_item
                    for item in requested
                    for actual_item in actual_folded
                ):
                    return False
                continue

            actual = str(candidate.get(field_name) or "").strip()
            if any(item == "__none__" for item in requested):
                if actual:
                    return False
                requested = [item for item in requested if item != "__none__"]
                if not requested:
                    continue
            actual_folded = actual.casefold()
            if not any(
                item.casefold() == actual_folded or item.casefold() in actual_folded
                for item in requested
            ):
                return False
        return True

    @staticmethod
    def _public_integration_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        """Copy safe metadata fields without exposing a local path."""

        safe_keys = (
            "id",
            "emoji_id",
            "desc",
            "emotion",
            "category",
            "tags",
            "scenes",
            "overlay_text",
            "character",
            "work",
            "action",
            "source",
            "scope_mode",
            "origin_target",
            "hash",
            "use_count",
            "is_favorite",
            "candidate_created_at",
        )
        result = {key: candidate.get(key) for key in safe_keys if key in candidate}
        result["tags"] = list(normalize_label_list(candidate.get("tags")))
        result["scenes"] = list(normalize_label_list(candidate.get("scenes")))
        return result
