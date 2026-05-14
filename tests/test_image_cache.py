from __future__ import annotations

import asyncio
import time

from astrbot_plugin_forward_context.cache import (
    ImageCaptionCache,
    ImageMessageRegistryStore,
    build_image_caption_sources,
)
from astrbot_plugin_forward_context.config import ForwardContextConfig
from astrbot_plugin_forward_context.main import ForwardContextPlugin


def test_build_image_caption_sources_adds_fileid_and_normalized_url() -> None:
    sources = build_image_caption_sources(
        image_url="https://example.com/get?fileid=abc&rkey=temp&token=secret&size=large",
        cache_source="image.png",
    )

    assert sources == [
        "image.png",
        "https://example.com/get?fileid=abc&rkey=temp&token=secret&size=large",
        "fileid:abc",
        "https://example.com/get?fileid=abc&size=large",
    ]


def test_caption_cache_accepts_lists_and_bridges_aliases(tmp_path) -> None:
    cache = ImageCaptionCache(tmp_path / "image_caption_cache.json")
    cache.set("alias-b", "caption text")

    assert cache.get(["alias-a", "alias-b"]) == "caption text"
    assert cache.get("alias-a") == "caption text"


def test_registry_bad_json_is_tolerated(tmp_path) -> None:
    path = tmp_path / "image_message_registry.json"
    path.write_text("{bad json", encoding="utf-8")

    store = ImageMessageRegistryStore(path)

    assert store.get_message("origin", "1") == {}


def test_registry_prunes_old_messages(tmp_path) -> None:
    path = tmp_path / "image_message_registry.json"
    store = ImageMessageRegistryStore(path, max_origins=5, max_messages_per_origin=2)
    store.set_message("origin", "1", ["https://example.com/1.png"])
    time.sleep(0.001)
    store.set_message("origin", "2", ["https://example.com/2.png"])
    time.sleep(0.001)
    store.set_message("origin", "3", ["https://example.com/3.png"])

    restored = ImageMessageRegistryStore(path, max_origins=5, max_messages_per_origin=2)

    assert restored.get_message("origin", "1") == {}
    assert restored.get_message("origin", "2")["urls"] == ["https://example.com/2.png"]
    assert restored.get_message("origin", "3")["urls"] == ["https://example.com/3.png"]


def test_registry_returns_copy(tmp_path) -> None:
    store = ImageMessageRegistryStore(tmp_path / "image_message_registry.json")
    store.set_message("origin", "1", ["https://example.com/1.png"], ["fileid:1"])

    entry = store.get_message("origin", "1")
    entry["urls"].append("mutated")
    entry["captions"]["0"] = "mutated"

    restored = store.get_message("origin", "1")
    assert restored["urls"] == ["https://example.com/1.png"]
    assert restored["captions"] == {}


class _DummyParser:
    def _get_message_segments(self, event):
        return event.segments

    def _segment_type(self, seg):
        return seg.get("type") if isinstance(seg, dict) else ""

    def _segment_data(self, seg):
        return seg.get("data", {}) if isinstance(seg, dict) else {}

    async def parse_event(self, _event):
        return _ParseResult()


class _ParseResult:
    text = "[Image]\n[Video]"
    found_forward = False
    used_forward_ids = []


class _MessageObj:
    message_id = "current-1"


class _Event:
    unified_msg_origin = "origin"
    message_obj = _MessageObj()
    segments = [
        {
            "type": "image",
            "data": {
                "url": "https://example.com/current.png?fileid=cur&rkey=temp",
                "fileid": "cur",
            },
        }
    ]

    def __init__(self) -> None:
        self.extras = {}

    def set_extra(self, key, value) -> None:
        self.extras[key] = value


def test_registers_current_and_history_image_messages(tmp_path) -> None:
    plugin = ForwardContextPlugin.__new__(ForwardContextPlugin)
    plugin.parser = _DummyParser()
    plugin.image_message_registry = ImageMessageRegistryStore(
        tmp_path / "image_message_registry.json"
    )

    plugin._register_event_image_message(_Event())
    plugin._register_history_image_message(
        _Event(),
        {
            "message_id": "history-1",
            "message": [
                {
                    "type": "image",
                    "data": {
                        "url": "https://example.com/history.png?fileid=hist&rkey=temp",
                        "file_id": "hist",
                    },
                }
            ],
        },
    )

    current = plugin.image_message_registry.get_message("origin", "current-1")
    history = plugin.image_message_registry.get_message("origin", "history-1")
    assert current["urls"] == ["https://example.com/current.png?fileid=cur&rkey=temp"]
    assert current["cache_sources"] == ["fileid:cur"]
    assert history["urls"] == ["https://example.com/history.png?fileid=hist&rkey=temp"]
    assert history["cache_sources"] == ["fileid:hist"]


def test_parse_and_attach_sets_media_extras_and_registers_images(tmp_path) -> None:
    class MediaEvent(_Event):
        segments = [
            *_Event.segments,
            {
                "type": "video",
                "data": {
                    "url": "https://example.com/video.mp4",
                    "fileid": "vid",
                },
            },
        ]

    plugin = ForwardContextPlugin.__new__(ForwardContextPlugin)
    plugin.cfg = ForwardContextConfig()
    plugin.parser = _DummyParser()
    plugin.image_message_registry = ImageMessageRegistryStore(
        tmp_path / "image_message_registry.json"
    )

    event = MediaEvent()
    text = asyncio.run(plugin._parse_and_attach(event))

    assert text == "[Image]\n[Video]"
    assert event.extras["_forward_context_text"] == "[Image]\n[Video]"
    assert event.extras["_forward_context_found"] is False
    assert event.extras["_forward_context_parsed"] is True
    assert event.extras["_forward_context_image_count"] == 1
    assert event.extras["_forward_context_video_count"] == 1
    current = plugin.image_message_registry.get_message("origin", "current-1")
    assert current["urls"] == ["https://example.com/current.png?fileid=cur&rkey=temp"]
