from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .cache import ImageCaptionCache, build_image_caption_sources
from .config import ForwardContextConfig


class ImageCaptioner:
    """Image caption helper.

    初始版提供缓存和接口位置。不同 AstrBot 版本的视觉模型调用方式可能不同，
    所以 `_caption_via_provider` 里故意保守实现：默认返回空。

    给 Codex 的任务：在你的本地 AstrBot 环境里查 context.provider_manager / LLM provider API，
    将 `_caption_via_provider` 接到支持图片输入的 provider。
    """

    def __init__(self, context: Any, cfg: ForwardContextConfig, plugin_data_dir: Path) -> None:
        self.context = context
        self.cfg = cfg
        self.cache = ImageCaptionCache(
            plugin_data_dir / "image_caption_cache.json",
            enable=cfg.image_caption_cache_enable,
            persist=cfg.image_caption_cache_persist,
            ttl_sec=cfg.image_caption_cache_ttl_sec,
            max_items=cfg.image_caption_cache_max_items,
        )

    async def caption(self, event: Any, image_url: str, *, cache_source: str = "") -> str:
        if not self.cfg.image_caption:
            return ""
        return await self.get_or_create(event, image_url, cache_source=cache_source)

    async def get_or_create(
        self,
        event: Any,
        image_url: str,
        *,
        cache_source: str = "",
        extra_sources: Any = None,
        provider_id: str = "",
        prompt: str = "",
        timeout_sec: float | None = None,
    ) -> str:
        sources = build_image_caption_sources(
            image_url=image_url,
            cache_source=cache_source,
            extra_sources=extra_sources,
        )
        cached = self.cache.get(sources)
        if cached:
            logger.debug("forward-context | image_caption cache hit | sources=%s", sources)
            return cached
        if not image_url:
            return ""
        try:
            caption = await self._caption_with_timeout(
                event,
                image_url,
                provider_id=provider_id,
                prompt=prompt,
                timeout_sec=timeout_sec,
            )
            caption = str(caption or "").strip().replace("\n", " ")
            if len(caption) > 160:
                caption = caption[:160].rstrip() + "..."
            if caption:
                self.cache.set(sources, caption)
            return caption
        except asyncio.TimeoutError:
            logger.debug(
                "forward-context | image caption timeout | timeout_sec=%s image_url=%s",
                timeout_sec if timeout_sec is not None else self.cfg.image_caption_timeout_sec,
                image_url,
            )
            return ""
        except Exception as e:
            logger.debug("forward-context | image caption failed: %s", e)
            return ""

    async def _caption_with_timeout(
        self,
        event: Any,
        image_url: str,
        *,
        provider_id: str = "",
        prompt: str = "",
        timeout_sec: float | None = None,
    ) -> str:
        effective_timeout = (
            self.cfg.image_caption_timeout_sec if timeout_sec is None else timeout_sec
        )
        effective_timeout = max(0, float(effective_timeout))
        coro = self._caption_via_provider(
            event,
            image_url,
            provider_id=provider_id,
            prompt=prompt,
        )
        if effective_timeout <= 0:
            return await coro
        return await asyncio.wait_for(coro, timeout=effective_timeout)

    async def _caption_via_provider(
        self,
        event: Any,
        image_url: str,
        *,
        provider_id: str = "",
        prompt: str = "",
    ) -> str:
        prompt = (prompt or self.cfg.image_caption_prompt or "").strip()
        if not prompt:
            prompt = "请用简体中文简短描述这张图片，重点说明画面主体和可见文字。"

        provider_id = (provider_id or self.cfg.image_caption_provider_id or "").strip()
        if not provider_id:
            provider_id = await self._get_current_chat_provider_id(event)

        llm_generate = getattr(self.context, "llm_generate", None)
        if callable(llm_generate) and provider_id:
            try:
                resp = await llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[image_url],
                )
                text = self._response_text(resp)
                if text:
                    return text
            except Exception as e:
                logger.debug(
                    "forward-context | image caption llm_generate failed | provider_id=%s err=%s",
                    provider_id,
                    e,
                )

        provider = await self._get_provider(event, provider_id)
        text_chat = getattr(provider, "text_chat", None)
        if callable(text_chat):
            resp = await text_chat(prompt=prompt, image_urls=[image_url])
            return self._response_text(resp)

        logger.debug("forward-context | image caption provider unavailable | image_url=%s", image_url)
        return ""

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_current_chat_provider_id(self, event: Any) -> str:
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if not callable(getter):
            return ""
        umo = getattr(event, "unified_msg_origin", None)
        try:
            return str(await self._maybe_await(getter(umo=umo)) or "")
        except TypeError:
            return str(await self._maybe_await(getter(umo)) or "")
        except Exception as e:
            logger.debug("forward-context | get current provider id failed: %s", e)
            return ""

    async def _get_provider(self, event: Any, provider_id: str) -> Any:
        if provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = await self._maybe_await(getter(provider_id))
                    if provider is not None:
                        return provider
                except Exception as e:
                    logger.debug("forward-context | get provider by id failed: %s", e)

        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            umo = getattr(event, "unified_msg_origin", None)
            try:
                return await self._maybe_await(getter(umo=umo))
            except TypeError:
                return await self._maybe_await(getter(umo))
            except Exception as e:
                logger.debug("forward-context | get using provider failed: %s", e)
        return None

    def _response_text(self, resp: Any) -> str:
        if resp is None:
            return ""
        for attr in ("completion_text", "text", "content"):
            value = getattr(resp, attr, None)
            if value:
                return str(value).strip()
        if isinstance(resp, dict):
            for key in ("completion_text", "text", "content"):
                value = resp.get(key)
                if value:
                    return str(value).strip()
        chain = getattr(resp, "result_chain", None)
        if chain:
            for name in ("get_plain_text", "to_plain_text", "plain_text"):
                value = getattr(chain, name, None)
                if callable(value):
                    text = value()
                    if text:
                        return str(text).strip()
                elif value:
                    return str(value).strip()
        return str(resp).strip()
