from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from astrbot.api import logger


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
                for volatile_key in ("rkey", "ukey", "token", "sig", "sign"):
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

    def get(self, source: str) -> str:
        if not self.enable:
            return ""
        key = self.key(source)
        if not key:
            return ""
        item = self._data.get(key)
        if not isinstance(item, dict):
            return ""
        caption = str(item.get("caption") or "").strip()
        if not caption:
            return ""
        updated_at = float(item.get("updated_at") or 0)
        if self.ttl_sec > 0 and time.time() - updated_at > self.ttl_sec:
            self._data.pop(key, None)
            return ""
        return caption

    def set(self, source: str, caption: str) -> None:
        if not self.enable:
            return
        key = self.key(source)
        caption = str(caption or "").strip()
        if not key or not caption:
            return
        self._data[key] = {"caption": caption, "updated_at": time.time()}
        self._save()
        logger.debug("forward-context | image_caption cache saved | source=%s", source)
