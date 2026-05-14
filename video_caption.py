from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .cache import ImageCaptionCache, build_video_caption_sources
from .config import ForwardContextConfig


class VideoCaptioner:
    def __init__(self, context: Any, cfg: ForwardContextConfig, plugin_data_dir: Path) -> None:
        self.context = context
        self.cfg = cfg
        self.cache = ImageCaptionCache(
            plugin_data_dir / "video_caption_cache.json",
            enable=cfg.video_caption_cache_enable,
            persist=cfg.video_caption_cache_persist,
            ttl_sec=cfg.video_caption_cache_ttl_sec,
            max_items=cfg.video_caption_cache_max_items,
            key_prefix="vidcap",
            log_label="video_caption",
        )

    async def caption(self, event: Any, video_url: str, *, cache_source: str = "") -> str:
        if not self.cfg.video_caption:
            return ""
        return await self.get_or_create(event, video_url, cache_source=cache_source)

    async def get_or_create(
        self,
        event: Any,
        video_url: str,
        *,
        cache_source: str = "",
        extra_sources: Any = None,
        provider_id: str = "",
        prompt: str = "",
        timeout_sec: float | None = None,
    ) -> str:
        sources = build_video_caption_sources(
            video_url=video_url,
            cache_source=cache_source,
            extra_sources=extra_sources,
        )
        cached = self.cache.get(sources)
        if cached:
            logger.debug("forward-context | video_caption cache hit | sources=%s", sources)
            return cached
        if not video_url:
            return ""
        try:
            caption = await self._caption_with_timeout(
                event,
                video_url,
                provider_id=provider_id,
                prompt=prompt,
                timeout_sec=timeout_sec,
            )
            caption = str(caption or "").strip().replace("\n", " ")
            if len(caption) > 240:
                caption = caption[:240].rstrip() + "..."
            if caption:
                self.cache.set(sources, caption)
            return caption
        except asyncio.TimeoutError:
            logger.debug(
                "forward-context | video caption timeout | timeout_sec=%s video_url=%s",
                timeout_sec if timeout_sec is not None else self.cfg.video_caption_timeout_sec,
                video_url,
            )
            return ""
        except Exception as e:
            logger.debug("forward-context | video caption failed: %s", e)
            return ""

    async def _caption_with_timeout(
        self,
        event: Any,
        video_url: str,
        *,
        provider_id: str = "",
        prompt: str = "",
        timeout_sec: float | None = None,
    ) -> str:
        effective_timeout = (
            self.cfg.video_caption_timeout_sec if timeout_sec is None else timeout_sec
        )
        effective_timeout = max(0, float(effective_timeout))
        candidates = await self._caption_provider_candidates(event, provider_id)
        for candidate_id, fallback_to_using_provider in candidates:
            provider_label = candidate_id or "<using_provider>"
            try:
                coro = self._caption_via_provider(
                    event,
                    video_url,
                    provider_id=candidate_id,
                    prompt=prompt,
                    fallback_to_using_provider=fallback_to_using_provider,
                )
                if effective_timeout <= 0:
                    text = await coro
                else:
                    text = await asyncio.wait_for(coro, timeout=effective_timeout)
            except asyncio.TimeoutError:
                logger.debug(
                    "forward-context | video caption provider timeout | provider_id=%s timeout_sec=%s video_url=%s",
                    provider_label,
                    effective_timeout,
                    video_url,
                )
                continue
            except Exception as e:
                logger.debug(
                    "forward-context | video caption provider failed | provider_id=%s err=%s",
                    provider_label,
                    e,
                )
                continue

            text = str(text or "").strip()
            if text:
                return text
            logger.debug(
                "forward-context | video caption provider returned empty | provider_id=%s video_url=%s",
                provider_label,
                video_url,
            )
        return ""

    async def _caption_provider_candidates(
        self, event: Any, provider_id: str = ""
    ) -> list[tuple[str, bool]]:
        explicit_provider_id = str(provider_id or "").strip()
        if explicit_provider_id:
            return [(explicit_provider_id, False)]

        configured_provider_ids: list[str] = []
        seen_provider_ids: set[str] = set()
        for item in getattr(self.cfg, "video_caption_provider_ids", []):
            text = str(item or "").strip()
            if not text or text in seen_provider_ids:
                continue
            configured_provider_ids.append(text)
            seen_provider_ids.add(text)
        if configured_provider_ids:
            return [(item, False) for item in configured_provider_ids]

        legacy_provider_id = str(self.cfg.video_caption_provider_id or "").strip()
        if legacy_provider_id:
            return [(legacy_provider_id, False)]

        current_provider_id = await self._get_current_chat_provider_id(event)
        if current_provider_id:
            return [(current_provider_id, True)]
        return [("", True)]

    async def _caption_via_provider(
        self,
        event: Any,
        video_url: str,
        *,
        provider_id: str = "",
        prompt: str = "",
        fallback_to_using_provider: bool = True,
    ) -> str:
        prompt = (prompt or self.cfg.video_caption_prompt or "").strip()
        if not prompt:
            prompt = (
                "Please briefly describe this video, including the main scene, "
                "actions, visible text, and key information."
            )

        provider_id = str(provider_id or "").strip()

        llm_generate = getattr(self.context, "llm_generate", None)
        if callable(llm_generate) and provider_id:
            try:
                resp = await llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    video_urls=[video_url],
                )
                text = self._response_text(resp)
                if text:
                    return text
            except Exception as e:
                logger.debug(
                    "forward-context | video caption llm_generate failed | provider_id=%s err=%s",
                    provider_id,
                    e,
                )

        provider = await self._get_provider(
            event,
            provider_id,
            fallback_to_using_provider=fallback_to_using_provider,
        )
        text_chat = getattr(provider, "text_chat", None)
        if callable(text_chat):
            resp = await text_chat(prompt=prompt, video_urls=[video_url])
            return self._response_text(resp)

        logger.debug("forward-context | video caption provider unavailable | video_url=%s", video_url)
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

    async def _get_provider(
        self,
        event: Any,
        provider_id: str,
        *,
        fallback_to_using_provider: bool = True,
    ) -> Any:
        if provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = await self._maybe_await(getter(provider_id))
                    if provider is not None:
                        return provider
                except Exception as e:
                    logger.debug("forward-context | get provider by id failed: %s", e)

        if not fallback_to_using_provider:
            return None

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
        saw_text_field = False
        for attr in ("completion_text", "text", "content"):
            if hasattr(resp, attr):
                saw_text_field = True
            value = getattr(resp, attr, None)
            if value:
                return str(value).strip()
        if isinstance(resp, dict):
            for key in ("completion_text", "text", "content"):
                if key in resp:
                    saw_text_field = True
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
        if saw_text_field:
            return ""
        return str(resp).strip()
