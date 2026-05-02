from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Union

from .cache import build_image_caption_sources


CachePluginOutputHandler = Callable[..., Awaitable[str]]
ImageCaptionCacheReader = Callable[[Any], Union[str, Awaitable[str]]]
ImageCaptionCacheWriter = Callable[[Any, str], Union[Any, Awaitable[Any]]]
ImageCaptionCreator = Callable[..., Union[str, Awaitable[str]]]
ImageMessageReader = Callable[
    [str, str],
    Union[dict[str, Any], Awaitable[dict[str, Any]]],
]
HistoryMessageParser = Callable[[Any, Any], Union[str, Awaitable[str]]]

_cache_plugin_output_handler: CachePluginOutputHandler | None = None
_image_caption_cache_reader: ImageCaptionCacheReader | None = None
_image_caption_cache_writer: ImageCaptionCacheWriter | None = None
_image_caption_creator: ImageCaptionCreator | None = None
_image_message_reader: ImageMessageReader | None = None
_history_message_parser: HistoryMessageParser | None = None


def register_plugin_output_cache(handler: CachePluginOutputHandler) -> None:
    global _cache_plugin_output_handler
    _cache_plugin_output_handler = handler


def unregister_plugin_output_cache(handler: CachePluginOutputHandler) -> None:
    global _cache_plugin_output_handler
    if _cache_plugin_output_handler is handler:
        _cache_plugin_output_handler = None


def register_image_caption_cache(
    reader: ImageCaptionCacheReader, writer: ImageCaptionCacheWriter
) -> None:
    global _image_caption_cache_reader, _image_caption_cache_writer
    _image_caption_cache_reader = reader
    _image_caption_cache_writer = writer


def unregister_image_caption_cache(
    reader: ImageCaptionCacheReader, writer: ImageCaptionCacheWriter
) -> None:
    global _image_caption_cache_reader, _image_caption_cache_writer
    if _image_caption_cache_reader is reader and _image_caption_cache_writer is writer:
        _image_caption_cache_reader = None
        _image_caption_cache_writer = None


def register_image_caption_creator(handler: ImageCaptionCreator) -> None:
    global _image_caption_creator
    _image_caption_creator = handler


def unregister_image_caption_creator(handler: ImageCaptionCreator) -> None:
    global _image_caption_creator
    if _image_caption_creator is handler:
        _image_caption_creator = None


def register_image_message_reader(handler: ImageMessageReader) -> None:
    global _image_message_reader
    _image_message_reader = handler


def unregister_image_message_reader(handler: ImageMessageReader) -> None:
    global _image_message_reader
    if _image_message_reader is handler:
        _image_message_reader = None


def register_history_message_parser(parser: HistoryMessageParser) -> None:
    global _history_message_parser
    _history_message_parser = parser


def unregister_history_message_parser(parser: HistoryMessageParser) -> None:
    global _history_message_parser
    if _history_message_parser is parser:
        _history_message_parser = None


async def get_cached_image_caption(source: Any) -> str:
    """Read an image caption from forward-context's shared caption cache."""
    handler = _image_caption_cache_reader
    if handler is None:
        return ""
    result = handler(source)
    if inspect.isawaitable(result):
        result = await result
    return str(result or "").strip()


async def set_cached_image_caption(source: Any, caption: str) -> None:
    """Write an image caption to forward-context's shared caption cache."""
    handler = _image_caption_cache_writer
    if handler is None:
        return
    result = handler(source, caption)
    if inspect.isawaitable(result):
        await result


async def get_or_create_image_caption(
    event: Any,
    image_url: str,
    *,
    cache_source: str = "",
    extra_sources: Any = None,
    provider_id: str = "",
    prompt: str = "",
    timeout_sec: float | None = None,
) -> str:
    """Read a cached caption or ask forward-context to create and persist one."""
    handler = _image_caption_creator
    if handler is None:
        return ""
    result = handler(
        event,
        image_url,
        cache_source=cache_source,
        extra_sources=extra_sources,
        provider_id=provider_id,
        prompt=prompt,
        timeout_sec=timeout_sec,
    )
    if inspect.isawaitable(result):
        result = await result
    return str(result or "").strip()


async def get_cached_image_message(origin: str, message_id: str) -> dict[str, Any]:
    """Read a persisted image message entry from forward-context."""
    handler = _image_message_reader
    if handler is None:
        return {}
    result = handler(origin, message_id)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, dict) else {}


async def parse_history_message(event: Any, message: Any) -> str:
    """Parse one adapter history message through forward-context."""
    handler = _history_message_parser
    if handler is None:
        return ""
    result = handler(event, message)
    if inspect.isawaitable(result):
        result = await result
    return str(result or "").strip()


async def cache_plugin_output(
    *,
    umo: str,
    chain: Any = None,
    text: str = "",
    source: str = "",
    event: Any = None,
) -> str:
    """Cache proactive plugin output for later LLM prompt injection.

    Returns the rendered recent-output block, or an empty string when
    forward_context is not loaded or plugin-output caching is disabled.
    """
    handler = _cache_plugin_output_handler
    if handler is None:
        return ""
    return await handler(
        umo=umo,
        chain=chain,
        text=text,
        source=source,
        event=event,
    )
