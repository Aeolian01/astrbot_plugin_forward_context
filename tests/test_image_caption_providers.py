from __future__ import annotations

import asyncio
import inspect
from typing import Any

from astrbot_plugin_forward_context.config import ForwardContextConfig, parse_config
from astrbot_plugin_forward_context.image_caption import ImageCaptioner
from astrbot_plugin_forward_context.video_caption import VideoCaptioner


class _Event:
    unified_msg_origin = "origin"


class _Context:
    def __init__(
        self, responses: dict[str, Any] | None = None, using_provider: Any = None
    ) -> None:
        self.responses = responses or {}
        self.using_provider = using_provider
        self.llm_generate_calls: list[str] = []
        self.video_llm_generate_calls: list[str] = []
        self.get_using_provider_calls = 0

    async def llm_generate(
        self,
        *,
        chat_provider_id: str,
        prompt: str,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
    ) -> Any:
        if video_urls is not None:
            self.video_llm_generate_calls.append(chat_provider_id)
        else:
            self.llm_generate_calls.append(chat_provider_id)
        response = self.responses.get(chat_provider_id, "")
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(
                prompt=prompt,
                image_urls=image_urls,
                video_urls=video_urls,
            )
        if inspect.isawaitable(response):
            response = await response
        if isinstance(response, dict):
            return response
        return {"text": response}

    async def get_using_provider(self, **_: Any) -> Any:
        self.get_using_provider_calls += 1
        return self.using_provider


class _Provider:
    async def text_chat(self, *, prompt: str, image_urls: list[str]) -> dict[str, str]:
        return {"text": "current provider caption"}


class _VideoProvider:
    async def text_chat(self, *, prompt: str, video_urls: list[str]) -> dict[str, str]:
        return {"text": "current provider video caption"}


def test_parse_config_accepts_ordered_image_caption_provider_ids() -> None:
    cfg = parse_config(
        {
            "image_caption_provider_ids": ["p1", "", "p2", "p1"],
            "image_caption_provider_id": "legacy",
        }
    )

    assert cfg.image_caption_provider_ids == ["p1", "p2"]
    assert cfg.image_caption_provider_id == "legacy"


def test_parse_config_accepts_ordered_video_caption_provider_ids() -> None:
    cfg = parse_config(
        {
            "video_caption_provider_ids": ["p1", "", "p2", "p1"],
            "video_caption_provider_id": "legacy",
        }
    )

    assert cfg.video_caption_provider_ids == ["p1", "p2"]
    assert cfg.video_caption_provider_id == "legacy"


def test_image_caption_falls_back_until_provider_returns_text(tmp_path) -> None:
    context = _Context(
        {
            "p1": "",
            "p2": RuntimeError("provider failed"),
            "p3": "caption from p3",
        }
    )
    cfg = ForwardContextConfig(image_caption_provider_ids=["p1", "p2", "p3"])
    captioner = ImageCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(_Event(), "https://example.com/image.png")
    )

    assert caption == "caption from p3"
    assert context.llm_generate_calls == ["p1", "p2", "p3"]


def test_image_caption_timeout_moves_to_next_provider(tmp_path) -> None:
    async def slow_response(**_: Any) -> dict[str, str]:
        await asyncio.sleep(0.05)
        return {"text": "too late"}

    context = _Context({"p1": slow_response, "p2": "caption from p2"})
    cfg = ForwardContextConfig(image_caption_provider_ids=["p1", "p2"])
    captioner = ImageCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(
            _Event(),
            "https://example.com/image.png",
            timeout_sec=0.01,
        )
    )

    assert caption == "caption from p2"
    assert context.llm_generate_calls == ["p1", "p2"]


def test_explicit_image_caption_provider_overrides_configured_list(tmp_path) -> None:
    context = _Context({"px": "caption from explicit provider", "p1": "wrong"})
    cfg = ForwardContextConfig(image_caption_provider_ids=["p1", "p2"])
    captioner = ImageCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(
            _Event(),
            "https://example.com/image.png",
            provider_id="px",
        )
    )

    assert caption == "caption from explicit provider"
    assert context.llm_generate_calls == ["px"]


def test_configured_image_caption_provider_does_not_fallback_to_current(
    tmp_path,
) -> None:
    context = _Context({"missing": ""}, using_provider=_Provider())
    cfg = ForwardContextConfig(image_caption_provider_ids=["missing"])
    captioner = ImageCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(_Event(), "https://example.com/image.png")
    )

    assert caption == ""
    assert context.llm_generate_calls == ["missing"]
    assert context.get_using_provider_calls == 0


def test_video_caption_falls_back_until_provider_returns_text(tmp_path) -> None:
    context = _Context(
        {
            "p1": "",
            "p2": RuntimeError("provider failed"),
            "p3": "video caption from p3",
        }
    )
    cfg = ForwardContextConfig(video_caption_provider_ids=["p1", "p2", "p3"])
    captioner = VideoCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(_Event(), "https://example.com/video.mp4")
    )

    assert caption == "video caption from p3"
    assert context.video_llm_generate_calls == ["p1", "p2", "p3"]


def test_video_caption_timeout_moves_to_next_provider(tmp_path) -> None:
    async def slow_response(**_: Any) -> dict[str, str]:
        await asyncio.sleep(0.05)
        return {"text": "too late"}

    context = _Context({"p1": slow_response, "p2": "video caption from p2"})
    cfg = ForwardContextConfig(video_caption_provider_ids=["p1", "p2"])
    captioner = VideoCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(
            _Event(),
            "https://example.com/video.mp4",
            timeout_sec=0.01,
        )
    )

    assert caption == "video caption from p2"
    assert context.video_llm_generate_calls == ["p1", "p2"]


def test_configured_video_caption_provider_does_not_fallback_to_current(
    tmp_path,
) -> None:
    context = _Context({"missing": ""}, using_provider=_VideoProvider())
    cfg = ForwardContextConfig(video_caption_provider_ids=["missing"])
    captioner = VideoCaptioner(context, cfg, tmp_path)

    caption = asyncio.run(
        captioner._caption_with_timeout(_Event(), "https://example.com/video.mp4")
    )

    assert caption == ""
    assert context.video_llm_generate_calls == ["missing"]
    assert context.get_using_provider_calls == 0
