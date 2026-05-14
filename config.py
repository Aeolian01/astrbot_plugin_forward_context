from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Any = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


@dataclass(frozen=True)
class ForwardContextConfig:
    enable: bool = True
    parse_group: bool = True
    parse_private: bool = True
    set_event_extra: bool = True
    extra_key: str = "_forward_context_text"
    inject_to_event_message_str: bool = True
    inject_to_llm_request: bool = True
    rewrite_when_prompt_empty_only: bool = True

    capture_plugin_outputs: bool = False
    plugin_output_extra_key: str = "_forward_context_recent_outputs"
    plugin_output_ttl_sec: int = 600
    plugin_output_max_items: int = 5
    plugin_output_max_chars: int = 3000
    inject_plugin_outputs_to_llm_request: bool = True
    include_llm_results_in_plugin_outputs: bool = False

    max_forward_depth: int = 3
    max_forward_messages: int = 80
    max_output_chars: int = 8000

    parse_reply_forward: bool = True
    parse_direct_forward: bool = True
    parse_nested_forward: bool = True
    xml_preview_fallback: bool = True
    parse_json_url_content: bool = True
    json_url_summary: bool = True
    json_url_summary_provider_id: str = ""
    json_url_summary_prompt: str = (
        "请直接读取下面链接并用简体中文总结内容，限制在 100 字以内。"
        "优先说明主题、关键信息、时间/名称/结论；如果无法读取链接，请基于分享卡片信息简要说明。"
    )
    json_url_summary_max_chars: int = 100
    json_url_summary_gemini_url_context: bool = True

    image_caption: bool = False
    image_caption_provider_id: str = ""
    image_caption_provider_ids: list[str] = field(default_factory=list)
    image_caption_prompt: str = "请用简体中文简短描述这张图片，重点说明画面主体和可见文字。"
    image_caption_timeout_sec: int = 30
    image_caption_cache_enable: bool = True
    image_caption_cache_persist: bool = True
    image_caption_cache_ttl_sec: int = 30 * 24 * 3600
    image_caption_cache_max_items: int = 1000
    video_caption: bool = False
    video_caption_provider_id: str = ""
    video_caption_provider_ids: list[str] = field(default_factory=list)
    video_caption_prompt: str = "请用简体中文简短描述这个视频，重点说明主要画面、动作、可见文字和关键信息。"
    video_caption_timeout_sec: int = 60
    video_caption_cache_enable: bool = True
    video_caption_cache_persist: bool = True
    video_caption_cache_ttl_sec: int = 30 * 24 * 3600
    video_caption_cache_max_items: int = 1000

    debug_log_raw_forward_result: bool = False


def parse_config(raw: dict[str, Any] | None) -> ForwardContextConfig:
    raw = raw or {}
    return ForwardContextConfig(
        enable=_to_bool(raw.get("enable"), True),
        parse_group=_to_bool(raw.get("parse_group"), True),
        parse_private=_to_bool(raw.get("parse_private"), True),
        set_event_extra=_to_bool(raw.get("set_event_extra"), True),
        extra_key=str(raw.get("extra_key") or "_forward_context_text"),
        inject_to_event_message_str=_to_bool(raw.get("inject_to_event_message_str"), True),
        inject_to_llm_request=_to_bool(raw.get("inject_to_llm_request"), True),
        rewrite_when_prompt_empty_only=_to_bool(raw.get("rewrite_when_prompt_empty_only"), True),
        capture_plugin_outputs=_to_bool(raw.get("capture_plugin_outputs"), False),
        plugin_output_extra_key=str(raw.get("plugin_output_extra_key") or "_forward_context_recent_outputs"),
        plugin_output_ttl_sec=max(0, _to_int(raw.get("plugin_output_ttl_sec"), 600)),
        plugin_output_max_items=max(1, _to_int(raw.get("plugin_output_max_items"), 5)),
        plugin_output_max_chars=max(500, _to_int(raw.get("plugin_output_max_chars"), 3000)),
        inject_plugin_outputs_to_llm_request=_to_bool(raw.get("inject_plugin_outputs_to_llm_request"), True),
        include_llm_results_in_plugin_outputs=_to_bool(raw.get("include_llm_results_in_plugin_outputs"), False),
        max_forward_depth=max(0, _to_int(raw.get("max_forward_depth"), 3)),
        max_forward_messages=max(1, _to_int(raw.get("max_forward_messages"), 80)),
        max_output_chars=max(500, _to_int(raw.get("max_output_chars"), 8000)),
        parse_reply_forward=_to_bool(raw.get("parse_reply_forward"), True),
        parse_direct_forward=_to_bool(raw.get("parse_direct_forward"), True),
        parse_nested_forward=_to_bool(raw.get("parse_nested_forward"), True),
        xml_preview_fallback=_to_bool(raw.get("xml_preview_fallback"), True),
        parse_json_url_content=_to_bool(raw.get("parse_json_url_content"), True),
        json_url_summary=_to_bool(raw.get("json_url_summary"), True),
        json_url_summary_provider_id=str(raw.get("json_url_summary_provider_id") or ""),
        json_url_summary_prompt=str(
            raw.get("json_url_summary_prompt")
            or "请直接读取下面链接并用简体中文总结内容，限制在 100 字以内。"
            "优先说明主题、关键信息、时间/名称/结论；如果无法读取链接，请基于分享卡片信息简要说明。"
        ),
        json_url_summary_max_chars=max(100, _to_int(raw.get("json_url_summary_max_chars"), 100)),
        json_url_summary_gemini_url_context=_to_bool(
            raw.get("json_url_summary_gemini_url_context"), True
        ),
        image_caption=_to_bool(raw.get("image_caption"), False),
        image_caption_provider_id=str(raw.get("image_caption_provider_id") or ""),
        image_caption_prompt=str(
            raw.get("image_caption_prompt")
            or "请用简体中文简短描述这张图片，重点说明画面主体和可见文字。"
        ),
        image_caption_timeout_sec=max(0, _to_int(raw.get("image_caption_timeout_sec"), 30)),
        image_caption_provider_ids=_to_str_list(raw.get("image_caption_provider_ids")),
        image_caption_cache_enable=_to_bool(raw.get("image_caption_cache_enable"), True),
        image_caption_cache_persist=_to_bool(raw.get("image_caption_cache_persist"), True),
        image_caption_cache_ttl_sec=max(0, _to_int(raw.get("image_caption_cache_ttl_sec"), 30 * 24 * 3600)),
        image_caption_cache_max_items=max(0, _to_int(raw.get("image_caption_cache_max_items"), 1000)),
        video_caption=_to_bool(raw.get("video_caption"), False),
        video_caption_provider_id=str(raw.get("video_caption_provider_id") or ""),
        video_caption_prompt=str(
            raw.get("video_caption_prompt")
            or "请用简体中文简短描述这个视频，重点说明主要画面、动作、可见文字和关键信息。"
        ),
        video_caption_timeout_sec=max(0, _to_int(raw.get("video_caption_timeout_sec"), 60)),
        video_caption_provider_ids=_to_str_list(raw.get("video_caption_provider_ids")),
        video_caption_cache_enable=_to_bool(raw.get("video_caption_cache_enable"), True),
        video_caption_cache_persist=_to_bool(raw.get("video_caption_cache_persist"), True),
        video_caption_cache_ttl_sec=max(0, _to_int(raw.get("video_caption_cache_ttl_sec"), 30 * 24 * 3600)),
        video_caption_cache_max_items=max(0, _to_int(raw.get("video_caption_cache_max_items"), 1000)),
        debug_log_raw_forward_result=_to_bool(raw.get("debug_log_raw_forward_result"), False),
    )
