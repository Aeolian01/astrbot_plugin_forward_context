from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from astrbot.api import logger

VOLATILE_IMAGE_QUERY_KEYS = {"rkey", "ukey", "token", "sig", "sign"}


def _source_values(source_or_sources: Any) -> list[str]:
    if isinstance(source_or_sources, str):
        values = [source_or_sources]
    elif isinstance(source_or_sources, (list, tuple, set)):
        values = [str(item or "") for item in source_or_sources]
    else:
        values = [str(source_or_sources or "")]
    return [value.strip() for value in values if value and value.strip()]


def _url_source_aliases(source: str) -> list[str]:
    if not source.startswith(("http://", "https://")):
        return []
    try:
        parsed = urlparse(source)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        aliases: list[str] = []
        fileid = str(qs.get("fileid", [""])[0] or "").strip()
        if fileid:
            aliases.append(f"fileid:{fileid}")
        for volatile_key in VOLATILE_IMAGE_QUERY_KEYS:
            qs.pop(volatile_key, None)
        normalized_query = urlencode(qs, doseq=True)
        normalized_url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, normalized_query, "")
        )
        if normalized_url and normalized_url != source:
            aliases.append(normalized_url)
        return aliases
    except Exception:
        return []


def build_image_caption_sources(
    image_url: str = "",
    cache_source: str = "",
    extra_sources: Any = None,
) -> list[str]:
    """Build all equivalent source keys for the shared image caption cache."""
    sources: list[str] = []

    def add(value: object) -> None:
        clean = str(value or "").strip()
        if clean and clean not in sources:
            sources.append(clean)
        for alias in _url_source_aliases(clean):
            if alias and alias not in sources:
                sources.append(alias)

    add(cache_source)
    add(image_url)
    for source in _source_values(extra_sources):
        add(source)
    return sources


class ImageCaptionCache:
    def __init__(
        self,
        path: Path,
        *,
        enable: bool = True,
        persist: bool = True,
        ttl_sec: int = 30 * 24 * 3600,
        max_items: int = 1000,
    ) -> None:
        self.path = path
        self.enable = enable
        self.persist = persist
        self.ttl_sec = ttl_sec
        self.max_items = max_items
        self._data: dict[str, dict[str, Any]] = {}
        if self.enable and self.persist:
            self._data = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if not self.path.exists():
                return {}
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug("forward-context | load image caption cache failed: %s", e)
            return {}

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = self._pruned(self._data)
            self._data = data
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug("forward-context | save image caption cache failed: %s", e)

    def _pruned(self, data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if self.max_items <= 0 or len(data) <= self.max_items:
            return data
        items = sorted(
            data.items(),
            key=lambda kv: float(kv[1].get("updated_at", 0))
            if isinstance(kv[1], dict)
            else 0,
            reverse=True,
        )
        return dict(items[: self.max_items])

    def _normalize_source(self, source: str) -> str:
        source = str(source or "").strip()
        if not source:
            return ""
        if source.startswith(("http://", "https://")):
            try:
                parsed = urlparse(source)
                qs = parse_qs(parsed.query, keep_blank_values=True)
                fileid = qs.get("fileid", [""])[0]
                if fileid:
                    return f"fileid:{fileid}"
                for volatile_key in VOLATILE_IMAGE_QUERY_KEYS:
                    qs.pop(volatile_key, None)
                query = urlencode(qs, doseq=True)
                return urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, "")
                )
            except Exception:
                return source
        return source

    def key(self, source: str) -> str:
        normalized = self._normalize_source(source)
        if not normalized:
            return ""
        return "imgcap:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _sources(self, source_or_sources: Any) -> list[str]:
        return build_image_caption_sources(extra_sources=source_or_sources)

    def get(self, source_or_sources: Any) -> str:
        if not self.enable:
            return ""
        sources = self._sources(source_or_sources)
        for source in sources:
            key = self.key(source)
            if not key:
                continue
            item = self._data.get(key)
            if not isinstance(item, dict):
                continue
            caption = str(item.get("caption") or "").strip()
            if not caption:
                continue
            updated_at = float(item.get("updated_at") or 0)
            if self.ttl_sec > 0 and time.time() - updated_at > self.ttl_sec:
                self._data.pop(key, None)
                continue
            if len(sources) > 1:
                self.set(sources, caption)
            return caption
        return ""

    def set(self, source_or_sources: Any, caption: str) -> None:
        if not self.enable:
            return
        sources = self._sources(source_or_sources)
        caption = str(caption or "").strip()
        if not sources or not caption:
            return
        updated_at = time.time()
        saved_sources: list[str] = []
        for source in sources:
            key = self.key(source)
            if not key:
                continue
            self._data[key] = {"caption": caption, "updated_at": updated_at}
            saved_sources.append(source)
        if not saved_sources:
            return
        self._save()
        logger.debug(
            "forward-context | image_caption cache saved | sources=%s",
            saved_sources,
        )


class ImageMessageRegistryStore:
    def __init__(
        self,
        path: Path,
        *,
        max_origins: int = 500,
        max_messages_per_origin: int = 900,
    ) -> None:
        self.path = path
        self.max_origins = max(1, int(max_origins))
        self.max_messages_per_origin = max(1, int(max_messages_per_origin))
        self._data: dict[str, dict[str, dict[str, Any]]] = self._load()

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            if not self.path.exists():
                return {}
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return self._normalize_registry(data)
        except Exception as e:
            logger.debug("forward-context | load image message registry failed: %s", e)
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = self._pruned(self._data)
            self._data = data
            tmp_path = self.path.with_name(f"{self.path.name}.tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)
        except Exception as e:
            logger.debug("forward-context | save image message registry failed: %s", e)

    def _normalize_registry(self, data: Any) -> dict[str, dict[str, dict[str, Any]]]:
        if not isinstance(data, dict):
            return {}
        registry: dict[str, dict[str, dict[str, Any]]] = {}
        for origin_raw, messages_raw in data.items():
            origin = "" if origin_raw is None else str(origin_raw).strip()
            if not origin or not isinstance(messages_raw, dict):
                continue
            messages: dict[str, dict[str, Any]] = {}
            for message_id_raw, entry_raw in messages_raw.items():
                message_id = "" if message_id_raw is None else str(message_id_raw).strip()
                entry = self._normalize_entry(entry_raw)
                if message_id and entry is not None:
                    messages[message_id] = entry
            if messages:
                registry[origin] = messages
        return registry

    @staticmethod
    def _normalize_entry(entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        urls_raw = entry.get("urls")
        if not isinstance(urls_raw, list):
            return None
        urls = [str(url or "").strip() for url in urls_raw]
        if not any(urls):
            return None
        cache_sources_raw = entry.get("cache_sources")
        cache_sources = (
            [str(source or "").strip() for source in cache_sources_raw]
            if isinstance(cache_sources_raw, list)
            else list(urls)
        )
        captions: dict[str, str] = {}
        captions_raw = entry.get("captions")
        if isinstance(captions_raw, dict):
            for idx_raw, caption_raw in captions_raw.items():
                idx = "" if idx_raw is None else str(idx_raw).strip()
                caption = str(caption_raw or "").strip()
                if idx and caption:
                    captions[idx] = caption
        try:
            updated_at = float(entry.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        if updated_at <= 0:
            updated_at = time.time()
        return {
            "urls": urls,
            "cache_sources": cache_sources,
            "captions": captions,
            "updated_at": updated_at,
        }

    @staticmethod
    def _entry_updated_at(entry: dict[str, Any]) -> float:
        try:
            return float(entry.get("updated_at") or 0)
        except (TypeError, ValueError):
            return 0

    def _pruned(
        self, data: dict[str, dict[str, dict[str, Any]]]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        pruned: dict[str, dict[str, dict[str, Any]]] = {}
        for origin, messages in data.items():
            sorted_messages = sorted(
                messages.items(),
                key=lambda item: self._entry_updated_at(item[1]),
                reverse=True,
            )
            kept = dict(sorted_messages[: self.max_messages_per_origin])
            if kept:
                pruned[origin] = kept
        sorted_origins = sorted(
            pruned.items(),
            key=lambda item: max(
                (self._entry_updated_at(entry) for entry in item[1].values()),
                default=0,
            ),
            reverse=True,
        )
        return dict(sorted_origins[: self.max_origins])

    def set_message(
        self,
        origin: str,
        message_id: str,
        image_urls: list[str],
        cache_sources: list[str] | None = None,
    ) -> None:
        origin = str(origin or "").strip()
        message_id = str(message_id or "").strip()
        urls = [str(url or "").strip() for url in image_urls]
        if not origin or not message_id or not any(urls):
            return
        cache_sources = cache_sources or []
        source_values = [
            str(cache_sources[idx] if idx < len(cache_sources) else "").strip()
            or str(url or "").strip()
            for idx, url in enumerate(urls)
        ]
        origin_messages = self._data.setdefault(origin, {})
        origin_messages[message_id] = {
            "urls": urls,
            "cache_sources": source_values,
            "captions": {},
            "updated_at": time.time(),
        }
        self._save()

    def get_message(self, origin: str, message_id: str) -> dict[str, Any]:
        origin = str(origin or "").strip()
        message_id = str(message_id or "").strip()
        if not origin or not message_id:
            return {}
        entry = self._data.get(origin, {}).get(message_id)
        if not isinstance(entry, dict):
            return {}
        urls = entry.get("urls")
        cache_sources = entry.get("cache_sources")
        captions = entry.get("captions")
        return {
            "urls": list(urls) if isinstance(urls, list) else [],
            "cache_sources": list(cache_sources) if isinstance(cache_sources, list) else [],
            "captions": dict(captions) if isinstance(captions, dict) else {},
            "updated_at": entry.get("updated_at", 0),
        }
