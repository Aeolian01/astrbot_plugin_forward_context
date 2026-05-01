from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .config import ForwardContextConfig, parse_config
from .image_caption import ImageCaptioner
from .parser import ForwardParser
from .public_api import (
    register_image_caption_cache,
    register_history_message_parser,
    register_plugin_output_cache,
    unregister_image_caption_cache,
    unregister_history_message_parser,
    unregister_plugin_output_cache,
)
from .recent_context import RecentContextStore


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
        self.parser = ForwardParser(self.cfg, self.image_captioner)
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
        self._history_message_parser = self.parse_history_message
        register_history_message_parser(self._history_message_parser)

    async def terminate(self):
        unregister_plugin_output_cache(self._cache_handler)
        unregister_image_caption_cache(
            self._image_caption_cache_reader,
            self._image_caption_cache_writer,
        )
        unregister_history_message_parser(self._history_message_parser)

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

    async def _parse_and_attach(self, event: AstrMessageEvent) -> str:
        result = await self.parser.parse_event(event)
        text = result.text.strip()
        if not text:
            return ""
        if self.cfg.set_event_extra:
            try:
                event.set_extra(self.cfg.extra_key, text)
                event.set_extra("_forward_context_found", result.found_forward)
                event.set_extra("_forward_context_ids", result.used_forward_ids or [])
            except Exception as e:
                logger.debug("forward-context | set event extra failed: %s", e)
        logger.debug(
            "forward-context | parsed | origin=%s found=%s text=%s",
            getattr(event, "unified_msg_origin", ""),
            result.found_forward,
            text[:800],
        )
        return text

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """Parse early and store text in event.extra."""
        if not self._should_parse_event(event):
            return
        try:
            await self._parse_and_attach(event)
            self._get_recent_output_context(event)
        except Exception as e:
            logger.warning("forward-context | on_message parse failed: %s", e, exc_info=True)

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
