from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .cache import ImageMessageRegistryStore
from .config import ForwardContextConfig, parse_config
from .image_caption import ImageCaptioner
from .parser import ForwardParser
from .public_api import (
    register_image_caption_cache,
    register_image_caption_creator,
    register_image_message_reader,
    register_current_message_parser,
    register_history_message_parser,
    register_plugin_output_cache,
    unregister_current_message_parser,
    unregister_image_caption_cache,
    unregister_image_caption_creator,
    unregister_image_message_reader,
    unregister_history_message_parser,
    unregister_plugin_output_cache,
)
from .recent_context import RecentContextStore
from .video_caption import VideoCaptioner


class ForwardContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        raw_cfg: dict[str, Any] = {}
        if config is not None:
            try:
                raw_cfg = dict(config)
            except Exception:
                raw_cfg = getattr(config, "data", {}) or {}

        self.cfg: ForwardContextConfig = parse_config(raw_cfg)

        # AstrBot data path moved between versions; prefer the current official API.
        base_data = Path("/AstrBot/data")
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore

            base_data = Path(get_astrbot_data_path())
        except Exception:
            try:
                from astrbot.api.star import get_astrbot_data_path  # type: ignore

                base_data = Path(get_astrbot_data_path())
            except Exception:
                pass

        self.plugin_data_dir = base_data / "plugin_data" / "astrbot_plugin_forward_context"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)

        self.image_captioner = ImageCaptioner(context, self.cfg, self.plugin_data_dir)
        self.video_captioner = VideoCaptioner(context, self.cfg, self.plugin_data_dir)
        self.image_message_registry = ImageMessageRegistryStore(
            self.plugin_data_dir / "image_message_registry.json",
            max_messages_per_origin=max(
                100,
                self.cfg.image_caption_cache_max_items,
                self.cfg.video_caption_cache_max_items,
            ),
            max_origins=500,
        )
        self.parser = ForwardParser(self.cfg, self.image_captioner, self.video_captioner)
        self.recent_outputs = RecentContextStore(
            ttl_sec=self.cfg.plugin_output_ttl_sec,
            max_items=self.cfg.plugin_output_max_items,
            max_chars_per_item=self.cfg.plugin_output_max_chars,
        )
        self._cache_handler = self.cache_external_output
        register_plugin_output_cache(self._cache_handler)
        self._image_caption_cache_reader = self.image_captioner.cache.get
        self._image_caption_cache_writer = self.image_captioner.cache.set
        register_image_caption_cache(
            self._image_caption_cache_reader,
            self._image_caption_cache_writer,
        )
        self._image_caption_creator = self.image_captioner.get_or_create
        register_image_caption_creator(self._image_caption_creator)
        self._image_message_reader = self.image_message_registry.get_message
        register_image_message_reader(self._image_message_reader)
        self._history_message_parser = self.parse_history_message
        register_history_message_parser(self._history_message_parser)
        self._current_message_parser = self._parse_and_attach
        register_current_message_parser(self._current_message_parser)

    async def terminate(self):
        unregister_plugin_output_cache(self._cache_handler)
        unregister_image_caption_cache(
            self._image_caption_cache_reader,
            self._image_caption_cache_writer,
        )
        unregister_image_caption_creator(self._image_caption_creator)
        unregister_image_message_reader(self._image_message_reader)
        unregister_history_message_parser(self._history_message_parser)
        unregister_current_message_parser(self._current_message_parser)

    def _message_type_name(self, event: AstrMessageEvent) -> str:
        try:
            mt = event.get_message_type()
            return str(getattr(mt, "name", mt)).lower()
        except Exception:
            return ""

    def _should_parse_event(self, event: AstrMessageEvent) -> bool:
        if not self.cfg.enable:
            return False
        name = self._message_type_name(event)
        if "group" in name:
            return self.cfg.parse_group
        if "private" in name or "friend" in name:
            return self.cfg.parse_private
        # Unknown message type: allow parsing, because forward parser is harmless.
        return True

    @staticmethod
    def _normalize_message_id(raw: Any) -> str:
        return str(raw or "").strip()

    @staticmethod
    def _pick_first(data: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = data.get(key)
            clean = str(value or "").strip()
            if clean:
                return clean
        return ""

    def _media_source_from_data(
        self, data: dict[str, Any], *, media_type: str
    ) -> dict[str, str] | None:
        media_url_keys = (
            ("url", "image_url", "src", "download_url", "origin_url", "file", "path")
            if media_type == "image"
            else (
                "url",
                "video_url",
                "src",
                "download_url",
                "origin_url",
                "file",
                "path",
            )
        )
        media_url = self._pick_first(
            data,
            media_url_keys,
        )
        fileid = self._pick_first(data, ("fileid", "file_id"))
        if fileid and not fileid.startswith("fileid:"):
            fileid = f"fileid:{fileid}"
        cache_source = fileid or self._pick_first(
            data,
            ("file", "url", "image_url", "video_url", "src", "path", "download_url", "origin_url"),
        )
        if not media_url and not cache_source:
            return None
        return {
            "url": media_url or cache_source,
            "cache_source": cache_source or media_url,
        }

    def _image_source_from_data(self, data: dict[str, Any]) -> dict[str, str] | None:
        return self._media_source_from_data(data, media_type="image")

    def _media_sources_from_segments(
        self, segments: Any, *, media_type: str
    ) -> list[dict[str, str]]:
        if not isinstance(segments, list):
            return []
        sources: list[dict[str, str]] = []
        for seg in segments:
            if self.parser._segment_type(seg) != media_type:
                continue
            source = self._media_source_from_data(
                self.parser._segment_data(seg),
                media_type=media_type,
            )
            if source is not None:
                sources.append(source)
        return sources

    def _image_sources_from_segments(self, segments: Any) -> list[dict[str, str]]:
        return self._media_sources_from_segments(segments, media_type="image")

    def _video_sources_from_segments(self, segments: Any) -> list[dict[str, str]]:
        return self._media_sources_from_segments(segments, media_type="video")

    def _event_media_counts(self, event: Any) -> tuple[int, int]:
        segments = self.parser._get_message_segments(event)
        image_count = len(self._image_sources_from_segments(segments))
        video_count = len(self._video_sources_from_segments(segments))
        return image_count, video_count

    def _event_message_id(self, event: Any) -> str:
        msg_obj = getattr(event, "message_obj", None)
        return self._normalize_message_id(
            getattr(msg_obj, "message_id", "")
            or getattr(event, "message_id", "")
            or getattr(event, "id", "")
        )

    def _register_event_image_message(self, event: Any) -> None:
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        message_id = self._event_message_id(event)
        if not origin or not message_id:
            return
        sources = self._image_sources_from_segments(self.parser._get_message_segments(event))
        if not sources:
            return
        self.image_message_registry.set_message(
            origin,
            message_id,
            [source["url"] for source in sources],
            [source["cache_source"] for source in sources],
        )

    def _register_history_image_message(self, event: Any, message: Any) -> None:
        if not isinstance(message, dict):
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        message_id = self._normalize_message_id(
            message.get("message_id") or message.get("id") or message.get("real_id") or ""
        )
        if not origin or not message_id:
            return
        raw_segments = message.get("message")
        if raw_segments is None:
            raw_segments = message.get("raw_message")
        sources = self._image_sources_from_segments(raw_segments)
        if not sources:
            return
        self.image_message_registry.set_message(
            origin,
            message_id,
            [source["url"] for source in sources],
            [source["cache_source"] for source in sources],
        )

    def _looks_empty_or_forward_prompt(self, prompt: str) -> bool:
        prompt = str(prompt or "").strip()
        if not prompt:
            return True
        normalized = prompt.replace(" ", "")
        normalized_lower = normalized.lower()
        return normalized in {
            "[转发消息]",
            "[引用消息]",
            "[引用消息][At]",
            "[Empty]",
            "[Forward]",
            "[ComponentType.Json]",
            "[Json]",
        } or normalized_lower in {
            "[componenttype.json]",
            "[json]",
        } or "[CQ:forward" in prompt or "[CQ:json" in prompt or "[转发消息]" in prompt

    def _get_req_prompt(self, req: Any) -> str:
        if req is None:
            return ""
        if isinstance(req, dict):
            return str(req.get("prompt") or "")
        return str(getattr(req, "prompt", "") or "")

    def _set_req_prompt(self, req: Any, prompt: str) -> bool:
        if req is None:
            return False
        if isinstance(req, dict):
            req["prompt"] = prompt
            return True
        try:
            setattr(req, "prompt", prompt)
            return True
        except Exception:
            return False

    def _get_recent_output_context(self, event: AstrMessageEvent) -> str:
        if not self.cfg.capture_plugin_outputs:
            return ""
        try:
            existing = event.get_extra(self.cfg.plugin_output_extra_key) or ""
            if existing:
                return str(existing)
        except Exception:
            pass
        text = self.recent_outputs.render(
            getattr(event, "unified_msg_origin", ""),
            max_items=self.cfg.plugin_output_max_items,
            max_chars=self.cfg.plugin_output_max_chars,
        )
        if text:
            try:
                event.set_extra(self.cfg.plugin_output_extra_key, text)
            except Exception:
                pass
        return text

    def _activated_source_name(self, event: AstrMessageEvent) -> str:
        try:
            handlers = event.get_extra("activated_handlers") or []
        except Exception:
            handlers = []
        names: list[str] = []
        for handler in handlers:
            module = str(getattr(handler, "handler_module_path", "") or "")
            handler_name = str(getattr(handler, "handler_name", "") or "")
            plugin = ""
            for part in module.split("."):
                if part.startswith("astrbot_plugin_"):
                    plugin = part
                    break
            label = plugin or module.rsplit(".", 1)[0] or module
            if handler_name:
                label = f"{label}.{handler_name}" if label else handler_name
            if label and label not in names:
                names.append(label)
        return ", ".join(names[:3])

    def _result_is_model_output(self, result: Any) -> bool:
        for name in ("is_model_result", "is_llm_result"):
            fn = getattr(result, name, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:
                    continue
        return False

    async def cache_external_output(
        self,
        *,
        umo: str,
        chain: Any = None,
        text: str = "",
        source: str = "",
        event: Any = None,
    ) -> str:
        """Public cache bridge for proactive sends from other plugins."""
        if not self.cfg.enable or not self.cfg.capture_plugin_outputs:
            return ""

        origin = str(umo or "").strip()
        if not origin:
            return ""

        body = str(text or "").strip()
        if not body and chain is not None:
            chain_value = getattr(chain, "chain", chain)
            try:
                body = await self.parser.message_chain_to_text(event, chain_value)
            except Exception as e:
                logger.debug("forward-context | parse external output failed: %s", e)
                body = ""
        body = body.strip()
        if not body:
            return ""

        self.recent_outputs.add(origin, body, source=source)
        rendered = self.recent_outputs.render(
            origin,
            max_items=self.cfg.plugin_output_max_items,
            max_chars=self.cfg.plugin_output_max_chars,
        )
        logger.debug(
            "forward-context | external plugin output cached | origin=%s source=%s text=%s",
            origin,
            source,
            body[:800],
        )
        return rendered

    async def parse_history_message(self, event: Any, message: Any) -> str:
        """Public bridge for parsing one message from adapter history."""
        if not self.cfg.enable:
            return ""
        self._register_history_image_message(event, message)
        try:
            node_text = await self.parser.forward_node_to_text(event, message, depth=0)
            json_texts = await self.parser.extract_json_texts_from_obj(
                message,
                event=event,
            )
        except Exception as e:
            logger.debug("forward-context | parse history message failed: %s", e)
            return ""

        parts: list[str] = []
        raw_node_text = str(node_text or "").strip()
        has_json_text = bool(json_texts)
        lowered_node_text = raw_node_text.lower()
        if raw_node_text and not (
            has_json_text
            and (
                "[cq:json" in lowered_node_text
                or raw_node_text in {"[Json]", "[ComponentType.Json]"}
            )
        ):
            parts.append(raw_node_text)
        for json_text in json_texts:
            clean = str(json_text or "").strip()
            if clean and clean not in parts and clean not in raw_node_text:
                parts.append(clean)

        text = "\n".join(parts).strip()
        if len(text) > self.cfg.max_output_chars:
            text = text[: self.cfg.max_output_chars].rstrip() + "\n...[truncated]"
        return text

    def _attach_to_event_message_str(self, event: AstrMessageEvent, text: str) -> bool:
        """Make parsed forward content visible to AstrBot's default LLM flow."""
        updated = False
        try:
            setattr(event, "message_str", text)
            updated = True
        except Exception as e:
            logger.debug("forward-context | set event.message_str failed: %s", e)

        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            try:
                setattr(msg_obj, "message_str", text)
                updated = True
            except Exception as e:
                logger.debug("forward-context | set message_obj.message_str failed: %s", e)
        return updated

    async def _parse_and_attach(self, event: AstrMessageEvent) -> str:
        self._register_event_image_message(event)
        image_count, video_count = self._event_media_counts(event)
        result = await self.parser.parse_event(event)
        text = result.text.strip()
        if not text:
            return ""
        if self.cfg.set_event_extra:
            try:
                event.set_extra(self.cfg.extra_key, text)
                event.set_extra("_forward_context_found", result.found_forward)
                event.set_extra("_forward_context_ids", result.used_forward_ids or [])
                event.set_extra("_forward_context_parsed", True)
                event.set_extra("_forward_context_image_count", image_count)
                event.set_extra("_forward_context_video_count", video_count)
            except Exception as e:
                logger.debug("forward-context | set event extra failed: %s", e)
        if self.cfg.inject_to_event_message_str and result.found_forward:
            if self._attach_to_event_message_str(event, text):
                logger.debug(
                    "forward-context | event message_str rewritten | origin=%s",
                    getattr(event, "unified_msg_origin", ""),
                )
        logger.debug(
            "forward-context | parsed | origin=%s found=%s images=%s videos=%s text=%s",
            getattr(event, "unified_msg_origin", ""),
            result.found_forward,
            image_count,
            video_count,
            text[:800],
        )
        return text

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """Parse early and store text in event.extra.

        注意：如果 enhance-mode 的 on_group_message 先于本插件执行，则 model_choice 仍然看不到 extra。
        解决：调整插件加载顺序，或在 enhance-mode 中直接调用本插件 parser / 读取 extra。
        """
        if not self._should_parse_event(event):
            return
        try:
            await self._parse_and_attach(event)
            self._get_recent_output_context(event)
        except Exception as e:
            logger.warning("forward-context | on_message parse failed: %s", e, exc_info=True)

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: Any = None):
        """Rewrite LLM prompt when current message is forward-like.

        This hook is useful for private chat and @-bot requests. For enhance-mode active_reply
        model_choice, the parse must happen before enhance-mode's on_group_message, or enhance-mode
        must consume event.extra explicitly.
        """
        if not self.cfg.enable:
            return
        inject_recent_outputs = (
            self.cfg.capture_plugin_outputs
            and self.cfg.inject_plugin_outputs_to_llm_request
        )
        if not self.cfg.inject_to_llm_request and not inject_recent_outputs:
            return

        parsed = ""
        if self.cfg.inject_to_llm_request:
            try:
                parsed = event.get_extra(self.cfg.extra_key) or ""
            except Exception:
                parsed = ""

            if not parsed and self._should_parse_event(event):
                try:
                    parsed = await self._parse_and_attach(event)
                except Exception as e:
                    logger.debug("forward-context | on_llm_request parse failed: %s", e)
                    parsed = ""

        recent_outputs = self._get_recent_output_context(event) if inject_recent_outputs else ""

        if not parsed and not recent_outputs:
            return

        # Current AstrBot calls hooks as (event, req); older builds may only expose extra.
        if req is None:
            try:
                req = event.get_extra("provider_request")
            except Exception:
                req = None

        if req is None:
            logger.debug("forward-context | provider_request not found; prompt not rewritten")
            return

        current_prompt = self._get_req_prompt(req)
        next_prompt = current_prompt

        if parsed:
            if not (
                self.cfg.rewrite_when_prompt_empty_only
                and not self._looks_empty_or_forward_prompt(current_prompt)
            ):
                next_prompt = parsed

        if recent_outputs:
            output_block = (
                "以下是最近其他插件输出，可作为上下文参考：\n"
                f"{recent_outputs}"
            )
            if recent_outputs not in next_prompt:
                next_prompt = f"{next_prompt.rstrip()}\n\n{output_block}".strip()

        if next_prompt == current_prompt:
            return

        if self._set_req_prompt(req, next_prompt):
            logger.debug(
                "forward-context | llm prompt rewritten | origin=%s text=%s",
                getattr(event, "unified_msg_origin", ""),
                next_prompt[:800],
            )
        else:
            logger.debug("forward-context | rewrite prompt failed: unsupported request object")

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """Cache ordinary plugin results before AstrBot sends them."""
        if not self.cfg.enable or not self.cfg.capture_plugin_outputs:
            return

        try:
            result = event.get_result()
        except Exception:
            result = None
        if result is None:
            return
        if (
            not self.cfg.include_llm_results_in_plugin_outputs
            and self._result_is_model_output(result)
        ):
            return

        chain = getattr(result, "chain", None)
        if not chain:
            return

        try:
            text = await self.parser.message_chain_to_text(event, chain)
        except Exception as e:
            logger.debug("forward-context | parse plugin output failed: %s", e)
            return
        text = text.strip()
        if not text:
            return

        origin = getattr(event, "unified_msg_origin", "")
        source = self._activated_source_name(event)
        self.recent_outputs.add(origin, text, source=source)
        rendered = self.recent_outputs.render(
            origin,
            max_items=self.cfg.plugin_output_max_items,
            max_chars=self.cfg.plugin_output_max_chars,
        )
        if rendered:
            try:
                event.set_extra(self.cfg.plugin_output_extra_key, rendered)
            except Exception:
                pass
        logger.debug(
            "forward-context | plugin output cached | origin=%s source=%s text=%s",
            origin,
            source,
            text[:800],
        )
