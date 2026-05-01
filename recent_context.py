from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class RecentContextItem:
    text: str
    source: str
    created_at: float


class RecentContextStore:
    def __init__(
        self,
        *,
        ttl_sec: int = 600,
        max_items: int = 5,
        max_chars_per_item: int = 3000,
    ) -> None:
        self.ttl_sec = ttl_sec
        self.max_items = max(1, max_items)
        self.max_chars_per_item = max(200, max_chars_per_item)
        self._items: dict[str, deque[RecentContextItem]] = {}

    def add(self, origin: str, text: str, *, source: str = "") -> None:
        origin = str(origin or "").strip()
        text = str(text or "").strip()
        if not origin or not text:
            return

        if len(text) > self.max_chars_per_item:
            text = text[: self.max_chars_per_item].rstrip() + "\n...[truncated]"

        now = time.time()
        queue = self._items.setdefault(origin, deque())
        if queue and queue[-1].text == text:
            queue[-1] = RecentContextItem(text=text, source=source, created_at=now)
        else:
            queue.append(RecentContextItem(text=text, source=source, created_at=now))
        self._prune(origin)

    def render(self, origin: str, *, max_items: int | None = None, max_chars: int | None = None) -> str:
        origin = str(origin or "").strip()
        if not origin:
            return ""
        self._prune(origin)
        items = list(self._items.get(origin, ()))
        if not items:
            return ""
        if max_items is not None and max_items > 0:
            items = items[-max_items:]

        lines = ["[RecentPluginOutput]"]
        for idx, item in enumerate(items, 1):
            source = f" ({item.source})" if item.source else ""
            body = item.text.replace("\n", "\n  ")
            lines.append(f"{idx}.{source}\n  {body}")

        text = "\n".join(lines).strip()
        if max_chars is not None and max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...[truncated]"
        return text

    def _prune(self, origin: str) -> None:
        queue = self._items.get(origin)
        if not queue:
            return

        now = time.time()
        if self.ttl_sec > 0:
            while queue and now - queue[0].created_at > self.ttl_sec:
                queue.popleft()
        while len(queue) > self.max_items:
            queue.popleft()
        if not queue:
            self._items.pop(origin, None)
