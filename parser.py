from __future__ import annotations

import html
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

from .config import ForwardContextConfig
from .image_caption import ImageCaptioner


@dataclass
class ForwardParseResult:
    text: str = ""
    found_forward: bool = False
    used_forward_ids: list[str] | None = None


class ForwardParser:
    def __init__(self, cfg: ForwardContextConfig, image_captioner: ImageCaptioner | None = None) -> None:
        self.cfg = cfg
        self.image_captioner = image_captioner
        self._url_content_cache: dict[str, str] = {}
        self._member_name_cache: dict[tuple[str, str], str] = {}
        self._user_name_cache: dict[str, str] = {}

    async def parse_event(self, event: Any) -> ForwardParseResult:
        parts: list[str] = []
        found = False
        used_ids: list[str] = []

        direct = await self._parse_direct_message(event)
        if direct.text:
            parts.append(direct.text)
            found = found or direct.found_forward
            used_ids.extend(direct.used_forward_ids or [])

        reply = await self._parse_reply_forward(event)
        if reply.text:
            parts.append(reply.text)
            found = True
            used_ids.extend(reply.used_forward_ids or [])

        text = "\n".join(p for p in parts if p).strip()
        if len(text) > self.cfg.max_output_chars:
            text = text[: self.cfg.max_output_chars].rstrip() + "\n...[truncated]"
        return ForwardParseResult(text=text, found_forward=found, used_forward_ids=used_ids)

    async def message_chain_to_text(self, event: Any, chain: Any) -> str:
        if not isinstance(chain, list):
            return ""
        parts: list[str] = []
        for seg in chain:
            text = await self.segment_to_text(event, seg, depth=0)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    async def _parse_direct_message(self, event: Any) -> ForwardParseResult:
        messages = self._get_message_segments(event)
        lines: list[str] = []
        found = False
        used_ids: list[str] = []

        for seg in messages:
            seg_type = self._segment_type(seg)
            if seg_type == "forward" and self.cfg.parse_direct_forward:
                forward_id = self._segment_data(seg).get("id") or self._segment_data(seg).get("resId")
                content = self._segment_data(seg).get("content")
                found = True
                if forward_id:
                    used_ids.append(str(forward_id))
                    text = await self.fetch_forward_text(event, str(forward_id), embedded_content=content, depth=0)
                    if text:
                        lines.append(text)
                    else:
                        lines.append(f"[Forward: {forward_id}]")
                elif content:
                    text = await self.forward_messages_to_text(event, content, depth=1)
                    lines.append(text or "[Forward]")
            else:
                text = await self.segment_to_text(event, seg, depth=0)
                if text:
                    lines.append(text)
                    if seg_type == "json":
                        found = True

        if not found:
            # Direct raw may contain multiForwardMsgElement even when message segments do not.
            ids = self.extract_forward_res_ids_from_event(event)
            if ids and self.cfg.parse_direct_forward:
                found = True
                for fid in ids[:1]:
                    used_ids.append(fid)
                    text = await self.fetch_forward_text(event, fid, depth=0)
                    if text:
                        lines.append(text)

        for text in await self.extract_json_texts_from_event(event):
            if text and text not in lines:
                lines.append(text)
                found = True

        return ForwardParseResult("\n".join(lines).strip(), found, used_ids)

    async def _parse_reply_forward(self, event: Any) -> ForwardParseResult:
        if not self.cfg.parse_reply_forward:
            return ForwardParseResult()

        messages = self._get_message_segments(event)
        reply_ids: list[str] = []
        for seg in messages:
            if self._segment_type(seg) == "reply":
                rid = self._segment_data(seg).get("id")
                if rid:
                    reply_ids.append(str(rid))

        if not reply_ids:
            return ForwardParseResult()

        lines: list[str] = []
        used_ids: list[str] = []

        # In NapCat reply event, quoted forward often exists directly in raw.records.
        res_ids = self.extract_forward_res_ids_from_event(event)
        if res_ids:
            for fid in res_ids[:1]:
                used_ids.append(fid)
                text = await self.fetch_forward_text(event, fid, depth=0)
                if text:
                    lines.append(f"[QuotedForward #msg{reply_ids[0]}]\n{text}")
                    return ForwardParseResult("\n".join(lines).strip(), True, used_ids)

        # Fallback: get_msg(reply_id), then extract forward id / resId.
        for rid in reply_ids[:1]:
            msg_data = await self.call_onebot_action(event, "get_msg", message_id=int(rid) if str(rid).isdigit() else rid)
            if self.cfg.debug_log_raw_forward_result:
                logger.debug("forward-context | get_msg raw result | reply_id=%s result=%s", rid, msg_data)
            ids = self.extract_forward_res_ids_from_obj(msg_data)
            for fid in ids[:1]:
                used_ids.append(fid)
                text = await self.fetch_forward_text(event, fid, depth=0)
                if text:
                    lines.append(f"[QuotedForward #msg{rid}]\n{text}")
                    return ForwardParseResult("\n".join(lines).strip(), True, used_ids)

        return ForwardParseResult("\n".join(lines).strip(), bool(lines), used_ids)

    async def fetch_forward_text(
        self,
        event: Any,
        forward_id: str,
        *,
        embedded_content: Any = None,
        depth: int = 0,
        extra_res_ids: list[str] | None = None,
        include_event_res_ids: bool = True,
    ) -> str:
        if depth > self.cfg.max_forward_depth:
            return f"[Forward: {forward_id}, depth limit]"

        tried_ids = []
        ids = [str(forward_id)] if forward_id else []
        for rid in extra_res_ids or []:
            if rid not in ids:
                ids.append(rid)
        if include_event_res_ids:
            for rid in self.extract_forward_res_ids_from_event(event):
                if rid not in ids:
                    ids.append(rid)

        for fid in ids:
            tried_ids.append(fid)
            data = await self.call_onebot_action(event, "get_forward_msg", id=fid)
            if self.cfg.debug_log_raw_forward_result:
                logger.debug("forward-context | get_forward_msg raw result | forward_id=%s result=%s", fid, data)
            messages = self.extract_forward_messages(data)
            if messages:
                preview_entries = (
                    self.extract_xml_preview_entries_from_obj(data)
                    or self.extract_xml_preview_entries_from_obj(embedded_content)
                    or (self.extract_xml_preview_entries_from_event(event) if include_event_res_ids else [])
                )
                text = await self.forward_messages_to_text(
                    event,
                    messages,
                    depth=depth + 1,
                    preview_entries=preview_entries,
                )
                if text:
                    return text

        if embedded_content and self.cfg.parse_nested_forward:
            text = await self.forward_messages_to_text(
                event,
                embedded_content,
                depth=depth + 1,
                preview_entries=self.extract_xml_preview_entries_from_obj(embedded_content),
            )
            if text:
                return f"[Forward: {forward_id}]\n{text}"

        if self.cfg.xml_preview_fallback:
            preview = (
                self.extract_xml_preview_from_event(event)
                if include_event_res_ids
                else ""
            ) or self.extract_xml_preview_from_obj(embedded_content)
            if preview:
                return preview

        return f"[Forward: {forward_id}]"

    async def forward_messages_to_text(
        self,
        event: Any,
        messages: Any,
        *,
        depth: int = 0,
        sender_hints: list[str] | None = None,
        preview_entries: list[tuple[str, str]] | None = None,
    ) -> str:
        if not isinstance(messages, list):
            return ""
        lines: list[str] = ["[Forward]"]
        preview_entries = preview_entries or []
        sender_hints = sender_hints or [name for name, _ in preview_entries]
        sender_hint_by_user_id: dict[str, str] = {}
        preview_idx = 0
        shown = 0
        idx = 1
        node_idx = 0
        while node_idx < len(messages):
            if shown >= self.cfg.max_forward_messages:
                lines.append("...[truncated]")
                break
            node = messages[node_idx]
            preview_entry = preview_entries[preview_idx] if preview_idx < len(preview_entries) else ("", "")
            text = await self.forward_node_to_text(event, node, depth=depth)
            user_id = self.sender_user_id(node)
            if (
                text
                and self._is_forward_preview_placeholder(preview_entry[1])
                and not self._is_forward_text(text)
            ):
                sender_hint = preview_entry[0] or sender_hint_by_user_id.get(user_id, "")
                sender = await self.resolve_sender_name(event, node, sender_hint=sender_hint)
                prefix = "  " * max(0, depth)
                lines.append(f"{prefix}{idx}. {sender}: [ForwardPreview: {preview_entry[1]}]")
                if user_id and self._valid_display_name(sender, user_id):
                    sender_hint_by_user_id.setdefault(user_id, sender)
                idx += 1
                shown += 1
                preview_idx += 1
                continue
            if not text:
                if self._is_forward_preview_placeholder(preview_entry[1]):
                    sender_hint = preview_entry[0] or sender_hint_by_user_id.get(user_id, "")
                    sender = await self.resolve_sender_name(event, node, sender_hint=sender_hint)
                    prefix = "  " * max(0, depth)
                    lines.append(f"{prefix}{idx}. {sender}: [ForwardPreview: {preview_entry[1]}]")
                    if user_id and self._valid_display_name(sender, user_id):
                        sender_hint_by_user_id.setdefault(user_id, sender)
                    idx += 1
                    shown += 1
                preview_idx += 1
                node_idx += 1
                continue
            sender_hint = sender_hints[preview_idx] if preview_idx < len(sender_hints) else ""
            if not sender_hint and user_id:
                sender_hint = sender_hint_by_user_id.get(user_id, "")
            sender = await self.resolve_sender_name(event, node, sender_hint=sender_hint)
            if user_id and self._valid_display_name(sender, user_id):
                sender_hint_by_user_id.setdefault(user_id, sender)
            prefix = "  " * max(0, depth)
            # Multi-line nested forward.
            indented = text.replace("\n", "\n" + prefix + "  ")
            lines.append(f"{prefix}{idx}. {sender}: {indented}")
            idx += 1
            shown += 1
            preview_idx += 1
            node_idx += 1
        return "\n".join(lines).strip()

    async def forward_node_to_text(self, event: Any, node: Any, *, depth: int = 0) -> str:
        if not isinstance(node, dict):
            segments = getattr(node, "message", None) or getattr(node, "content", None) or []
            if isinstance(segments, str):
                return html.unescape(segments).strip()
            if isinstance(segments, list):
                parts: list[str] = []
                for seg in segments:
                    text = await self.segment_to_text(event, seg, depth=depth)
                    if text:
                        parts.append(text)
                return " ".join(parts).strip()
            raw = str(getattr(node, "raw_message", "") or "").strip()
            return html.unescape(raw) if raw else str(node).strip()
        segments = node.get("message") or node.get("content") or []
        if isinstance(segments, str):
            return html.unescape(segments).strip()
        if not isinstance(segments, list):
            forward_text = await self.raw_forward_to_text(event, node, depth=depth)
            if forward_text:
                return forward_text
            raw = str(node.get("raw_message") or "").strip()
            return html.unescape(raw)

        parts: list[str] = []
        for seg in segments:
            text = await self.segment_to_text(event, seg, depth=depth)
            if text:
                parts.append(text)
        if parts:
            return " ".join(parts).strip()
        forward_text = await self.raw_forward_to_text(event, node, depth=depth)
        if forward_text:
            return forward_text
        raw = str(node.get("raw_message") or "").strip()
        return html.unescape(raw)

    async def segment_to_text(self, event: Any, seg: Any, *, depth: int = 0) -> str:
        seg_type = self._segment_type(seg)
        data = self._segment_data(seg)
        if not seg_type:
            forward_text = await self.raw_forward_to_text(event, seg, depth=depth)
            if forward_text:
                return forward_text

        if seg_type == "text":
            return str(data.get("text") or "").strip()
        if seg_type == "at":
            return f"[At: {data.get('qq') or data.get('uid') or ''}]"
        if seg_type == "reply":
            return f"[Reply #msg{data.get('id') or ''}]"
        if seg_type == "image":
            fallback = data.get("summary") or "[Image]"
            url = data.get("url") or data.get("file") or ""
            cache_source = data.get("file") or url
            if self.image_captioner and self.cfg.image_caption:
                caption = await self.image_captioner.caption(event, url, cache_source=cache_source)
                if caption:
                    return f"[Image: {caption}]"
            return str(fallback or "[Image]")
        if seg_type == "video":
            return "[Video]"
        if seg_type == "record":
            return "[Record]"
        if seg_type == "json":
            return await self.json_segment_to_text(data, event=event)
        if seg_type == "node":
            return await self.forward_node_to_text(event, seg, depth=depth)
        if seg_type == "nodes":
            nodes = data.get("nodes") or data.get("messages") or data.get("content") or []
            return await self.forward_messages_to_text(event, nodes, depth=depth)
        if seg_type == "forward":
            nested_ids = self.extract_forward_res_ids_from_obj(seg)
            forward_id = data.get("id") or data.get("resId") or data.get("resid") or (nested_ids[0] if nested_ids else "")
            content = data.get("content")
            if self.cfg.parse_nested_forward and depth < self.cfg.max_forward_depth:
                text = ""
                if content:
                    text = await self.forward_messages_to_text(event, content, depth=depth + 1)
                if not text and forward_id:
                    text = await self.fetch_forward_text(
                        event,
                        str(forward_id),
                        embedded_content=content,
                        depth=depth + 1,
                        extra_res_ids=nested_ids,
                        include_event_res_ids=not nested_ids,
                    )
                if text:
                    return text
            return f"[Forward: {forward_id}, depth limit]" if forward_id else "[Forward: depth limit]"

        forward_text = await self.raw_forward_to_text(event, seg, depth=depth)
        if forward_text:
            return forward_text
        if isinstance(seg, str):
            return seg.strip()
        return ""

    async def raw_forward_to_text(self, event: Any, obj: Any, *, depth: int = 0) -> str:
        if not self.cfg.parse_nested_forward or depth >= self.cfg.max_forward_depth:
            return ""
        ids = self.extract_forward_res_ids_from_obj(obj)
        for fid in ids[:1]:
            text = await self.fetch_forward_text(
                event,
                fid,
                embedded_content=obj,
                depth=depth + 1,
                extra_res_ids=ids,
                include_event_res_ids=False,
            )
            if text:
                return text
        return self.extract_xml_preview_from_obj(obj)

    async def extract_json_texts_from_event(self, event: Any) -> list[str]:
        objs: list[Any] = []
        msg_obj = getattr(event, "message_obj", None)
        for attr in ("raw", "raw_message", "message"):
            value = getattr(msg_obj, attr, None) if msg_obj is not None else None
            if value:
                objs.append(value)
        return await self.extract_json_texts_from_obj(objs, event=event)

    async def extract_json_texts_from_obj(self, obj: Any, *, event: Any = None) -> list[str]:
        payloads: list[Any] = []
        seen_payloads: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if self._normalize_segment_type(value.get("type")) == "json":
                    self._append_json_payload(payloads, seen_payloads, self._segment_data(value) or value)
                raw = value.get("raw_message")
                if isinstance(raw, str) and "[CQ:json" in raw:
                    self._append_json_payload(payloads, seen_payloads, raw)
                ark = value.get("arkElement")
                if isinstance(ark, dict) and ark.get("bytesData"):
                    self._append_json_payload(payloads, seen_payloads, ark.get("bytesData"))
                bytes_data = value.get("bytesData")
                if isinstance(bytes_data, str):
                    self._append_json_payload(payloads, seen_payloads, bytes_data)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(obj)

        texts: list[str] = []
        seen_texts: set[str] = set()
        for payload in payloads:
            text = await self.json_segment_to_text(payload, event=event)
            if text and text not in seen_texts:
                seen_texts.add(text)
                texts.append(text)
        return texts

    def _append_json_payload(self, payloads: list[Any], seen: set[str], value: Any) -> None:
        marker = repr(value)
        if marker in seen:
            return
        seen.add(marker)
        payloads.append(value)

    async def json_segment_to_text(self, data: Any, *, event: Any = None) -> str:
        obj = self._json_payload_to_obj(data)
        if not isinstance(obj, dict):
            return ""

        lines: list[str] = ["[JsonShare]"]
        seen_values: set[str] = set()
        url = self._clean_json_text(
            self._first_json_scalar(
                obj,
                ("jumpurl", "jump_url", "jumpurl1", "link", "href", "targeturl", "target_url", "url"),
            )
        )

        def add(label: str, value: Any) -> None:
            text = self._clean_json_text(value)
            if not text or text in seen_values:
                return
            seen_values.add(text)
            lines.append(f"{label}: {text}")

        add("title", self._first_json_scalar(obj, ("title", "name")))
        add("desc", self._first_json_scalar(obj, ("desc", "description", "summary")))
        add("tag", self._first_json_scalar(obj, ("tag", "source", "appname", "app_name")))
        add("url", url)
        add("prompt", obj.get("prompt"))
        url_content = await self.url_content_to_text(url, event=event, card_context="\n".join(lines))
        if url_content:
            lines.append(url_content)

        return "\n".join(lines).strip() if len(lines) > 1 else ""

    def _json_payload_to_obj(self, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("data", "json", "raw", "bytesData"):
                nested = value.get(key)
                parsed = self._json_payload_to_obj(nested)
                if isinstance(parsed, dict):
                    return parsed
            if "meta" in value or "prompt" in value or "app" in value:
                return value
            return None

        if not isinstance(value, str):
            return None

        text = value.strip()
        cq_match = re.search(r"\[CQ:json,data=(.*?)]", text, flags=re.S)
        if cq_match:
            text = html.unescape(cq_match.group(1)).strip()
        else:
            text = html.unescape(text).strip()

        if not text.startswith("{"):
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _first_json_scalar(self, obj: Any, keys: tuple[str, ...]) -> Any:
        wanted = {key.lower() for key in keys}
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in wanted and self._clean_json_text(value):
                    return value
            for value in obj.values():
                found = self._first_json_scalar(value, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._first_json_scalar(item, keys)
                if found is not None:
                    return found
        return None

    def _clean_json_text(self, value: Any) -> str:
        if value is None or isinstance(value, (dict, list)):
            return ""
        text = html.unescape(str(value)).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    async def url_content_to_text(self, url: str, *, event: Any = None, card_context: str = "") -> str:
        if not self.cfg.parse_json_url_content or not self.cfg.json_url_summary:
            return ""
        url = self._clean_json_text(url)
        if not self._is_supported_url(url):
            return ""
        if url in self._url_content_cache:
            return self._url_content_cache[url]
        try:
            summary = await self.summarize_url_content(event, url, card_context)
        except Exception as e:
            logger.debug("forward-context | json url summary failed | url=%s err=%s", url, e)
            return ""
        text = f"[UrlSummary]\n{summary}" if summary else ""
        if text:
            if len(self._url_content_cache) >= 128:
                self._url_content_cache.pop(next(iter(self._url_content_cache)), None)
            self._url_content_cache[url] = text
        return text

    async def summarize_url_content(self, event: Any, url: str, card_context: str) -> str:
        if self.image_captioner is None:
            return ""
        prompt = (self.cfg.json_url_summary_prompt or "").strip()
        if not prompt:
            return ""
        prompt = f"{prompt}\n\n链接：{url}"
        card_context = str(card_context or "").strip()
        if card_context:
            prompt = f"{prompt}\n\nQQ 分享卡片信息：\n{card_context}"

        provider_id = (self.cfg.json_url_summary_provider_id or "").strip()
        if not provider_id:
            provider_id = await self.image_captioner._get_current_chat_provider_id(event)

        provider = None
        try:
            provider = await self.image_captioner._get_provider(event, provider_id)
        except Exception as e:
            logger.debug("forward-context | json url summary get provider failed | err=%s", e)
        if self._should_use_gemini_url_context(provider_id, provider):
            text = await self._text_chat_with_gemini_url_context(provider, prompt)
            if text:
                return self._limit_url_text(text)

        context = getattr(self.image_captioner, "context", None)
        llm_generate = getattr(context, "llm_generate", None)
        if callable(llm_generate) and provider_id:
            try:
                resp = await llm_generate(chat_provider_id=provider_id, prompt=prompt)
                text = self.image_captioner._response_text(resp)
                if text:
                    return self._limit_url_text(text)
            except Exception as e:
                logger.debug(
                    "forward-context | json url summary llm_generate failed | provider_id=%s err=%s",
                    provider_id,
                    e,
                )

        text_chat = getattr(provider, "text_chat", None)
        if callable(text_chat):
            try:
                resp = await text_chat(prompt=prompt)
                text = self.image_captioner._response_text(resp)
                if text:
                    return self._limit_url_text(text)
            except Exception as e:
                logger.debug("forward-context | json url summary text_chat failed | err=%s", e)

        return ""

    def _should_use_gemini_url_context(self, provider_id: str, provider: Any) -> bool:
        if not self.cfg.json_url_summary_gemini_url_context or provider is None:
            return False
        tokens = [provider_id, provider.__class__.__name__]
        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            for key in ("type", "provider", "model", "model_name", "api_base"):
                tokens.append(str(provider_config.get(key) or ""))
        for attr in ("type", "provider_type", "model", "model_name", "api_base"):
            tokens.append(str(getattr(provider, attr, "") or ""))
        get_model = getattr(provider, "get_model", None)
        if callable(get_model):
            try:
                tokens.append(str(get_model() or ""))
            except Exception:
                pass
        text = " ".join(tokens).lower()
        return any(
            marker in text
            for marker in (
                "gemini-2.5",
                "gemini-3",
                "googlegenai",
                "google-genai",
                "providergooglegenai",
                "generativelanguage.googleapis.com",
            )
        )

    async def _text_chat_with_gemini_url_context(self, provider: Any, prompt: str) -> str:
        text_chat = getattr(provider, "text_chat", None)
        if not callable(text_chat):
            return ""

        provider_config = getattr(provider, "provider_config", None)
        if not isinstance(provider_config, dict):
            try:
                resp = await text_chat(prompt=prompt)
                return self.image_captioner._response_text(resp)
            except Exception as e:
                logger.debug("forward-context | gemini url context summary failed | err=%s", e)
                return ""

        had_url_context = "gm_url_context" in provider_config
        old_url_context = provider_config.get("gm_url_context")
        had_coderunner = "gm_native_coderunner" in provider_config
        old_coderunner = provider_config.get("gm_native_coderunner")
        try:
            # AstrBot's Google GenAI provider reads native URL Context from
            # provider_config while building GenerateContentConfig.
            provider_config["gm_url_context"] = True
            provider_config["gm_native_coderunner"] = False
            resp = await text_chat(prompt=prompt)
            return self.image_captioner._response_text(resp)
        except Exception as e:
            logger.debug("forward-context | gemini url context summary failed | err=%s", e)
            return ""
        finally:
            if had_url_context:
                provider_config["gm_url_context"] = old_url_context
            else:
                provider_config.pop("gm_url_context", None)
            if had_coderunner:
                provider_config["gm_native_coderunner"] = old_coderunner
            else:
                provider_config.pop("gm_native_coderunner", None)

    def _limit_url_text(self, text: str) -> str:
        max_chars = max(100, int(self.cfg.json_url_summary_max_chars))
        if len(text) > max_chars:
            return text[:max_chars].rstrip() + "...[truncated]"
        return text

    def _is_supported_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").strip()
        if not host:
            return False
        if host.lower() in {"localhost", "localhost.localdomain"}:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast)

    def extract_forward_messages(self, data: Any) -> list[Any]:
        if not isinstance(data, dict):
            return []
        candidates = [data]
        if isinstance(data.get("data"), dict):
            candidates.append(data["data"])
        for obj in candidates:
            for key in ("messages", "message", "content"):
                value = obj.get(key)
                if isinstance(value, list):
                    return value
        return []

    def extract_forward_res_ids_from_event(self, event: Any) -> list[str]:
        objs: list[Any] = []
        msg_obj = getattr(event, "message_obj", None)
        for attr in ("raw", "raw_message"):
            value = getattr(msg_obj, attr, None) if msg_obj is not None else None
            if value:
                objs.append(value)
        return self.extract_forward_res_ids_from_obj(objs)

    def extract_forward_res_ids_from_obj(self, obj: Any) -> list[str]:
        ids: list[str] = []

        def add(value: Any) -> None:
            if value:
                text = str(value)
                if text not in ids:
                    ids.append(text)

        for mfe in self.iter_multi_forward_elements(obj):
            for key in ("resId", "resid", "m_resid", "id", "fileName"):
                add(mfe.get(key))
            xml = str(mfe.get("xmlContent") or "")
            m = re.search(r'm_resid="([^"]+)"', xml)
            if m:
                add(m.group(1))
        for text in self.iter_strings(obj):
            for m in re.finditer(r"\[CQ:forward,[^\]]*\bid=([^,\]]+)", text, flags=re.I):
                add(html.unescape(m.group(1)).strip())
            for m in re.finditer(r'm_resid="([^"]+)"', text):
                add(html.unescape(m.group(1)).strip())
        return ids

    def iter_multi_forward_elements(self, obj: Any):
        if isinstance(obj, dict):
            mfe = obj.get("multiForwardMsgElement")
            if isinstance(mfe, dict):
                yield mfe
            # OneBot segment forward data can carry id/content but not multiForwardMsgElement.
            for value in obj.values():
                yield from self.iter_multi_forward_elements(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self.iter_multi_forward_elements(item)

    def iter_strings(self, obj: Any):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for value in obj.values():
                yield from self.iter_strings(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self.iter_strings(item)

    def extract_xml_preview_from_event(self, event: Any) -> str:
        objs: list[Any] = []
        msg_obj = getattr(event, "message_obj", None)
        for attr in ("raw", "raw_message"):
            value = getattr(msg_obj, attr, None) if msg_obj is not None else None
            if value:
                objs.append(value)
        return self.extract_xml_preview_from_obj(objs)

    def extract_xml_preview_from_obj(self, obj: Any) -> str:
        lines: list[str] = []
        for mfe in self.iter_multi_forward_elements(obj):
            xml = str(mfe.get("xmlContent") or "")
            if not xml:
                continue
            titles = re.findall(r"<title[^>]*>(.*?)</title>", xml, flags=re.S)
            summaries = re.findall(r"<summary[^>]*>(.*?)</summary>", xml, flags=re.S)
            for item in titles + summaries:
                text = html.unescape(re.sub(r"<[^>]+>", "", item)).strip()
                if text and text not in lines:
                    lines.append(text)
        if not lines:
            return ""
        return "[ForwardPreview]\n" + "\n".join(lines)

    def extract_xml_sender_hints_from_event(self, event: Any) -> list[str]:
        return [name for name, _ in self.extract_xml_preview_entries_from_event(event)]

    def extract_xml_sender_hints_from_obj(self, obj: Any) -> list[str]:
        return [name for name, _ in self.extract_xml_preview_entries_from_obj(obj)]

    def extract_xml_preview_entries_from_event(self, event: Any) -> list[tuple[str, str]]:
        objs: list[Any] = []
        msg_obj = getattr(event, "message_obj", None)
        for attr in ("raw", "raw_message"):
            value = getattr(msg_obj, attr, None) if msg_obj is not None else None
            if value:
                objs.append(value)
        return self.extract_xml_preview_entries_from_obj(objs)

    def extract_xml_preview_entries_from_obj(self, obj: Any) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for mfe in self.iter_multi_forward_elements(obj):
            xml = str(mfe.get("xmlContent") or "")
            if not xml:
                continue
            for item in re.findall(r"<title[^>]*>(.*?)</title>", xml, flags=re.S):
                text = html.unescape(re.sub(r"<[^>]+>", "", item)).strip()
                match = re.match(r"^([^:\uFF1A]{1,64})(?::|\uFF1A)\s*(.+)$", text)
                if match:
                    name = match.group(1).strip()
                    body = match.group(2).strip()
                    if name and body:
                        entries.append((name, body))
        return entries

    async def call_onebot_action(self, event: Any, action: str, **params: Any) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        # Most aiocqhttp adapters expose bot.api.call_action(action, **params)
        api = getattr(bot, "api", None)
        for target in (api, bot):
            if target is None:
                continue
            call_action = getattr(target, "call_action", None)
            if callable(call_action):
                try:
                    return await call_action(action, **params)
                except TypeError:
                    try:
                        return await call_action(action, params)
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug("forward-context | call_action failed | action=%s params=%s err=%s", action, params, e)
            direct = getattr(target, action, None)
            if callable(direct):
                try:
                    return await direct(**params)
                except Exception as e:
                    logger.debug("forward-context | direct api failed | action=%s params=%s err=%s", action, params, e)
        return None

    async def resolve_sender_name(self, event: Any, node: Any, *, sender_hint: str = "") -> str:
        fallback = self.sender_name(node)
        user_id = self.sender_user_id(node)
        sender_hint = self._valid_display_name(sender_hint, user_id)
        if not user_id:
            return sender_hint or self._valid_display_name(fallback) or fallback
        valid_fallback = self._valid_display_name(fallback, user_id)
        if valid_fallback:
            return valid_fallback

        group_id = self.event_group_id(event)
        if not group_id:
            name = await self.user_profile_name(event, user_id)
            return (
                self._valid_display_name(name, user_id)
                or sender_hint
                or user_id
            )

        name = await self.group_member_name(event, group_id, user_id)
        name = self._valid_display_name(name, user_id) or self._valid_display_name(
            await self.user_profile_name(event, user_id),
            user_id,
        )
        return name or sender_hint or user_id

    async def group_member_name(self, event: Any, group_id: str, user_id: str) -> str:
        cache_key = (group_id, user_id)
        if cache_key in self._member_name_cache:
            return self._member_name_cache[cache_key]

        result = await self.call_onebot_action(
            event,
            "get_group_member_info",
            group_id=self._numeric_if_digits(group_id),
            user_id=self._numeric_if_digits(user_id),
            no_cache=False,
        )
        data = self._onebot_data(result)
        name = ""
        if isinstance(data, dict):
            name = self._first_nonempty_text(
                data.get("card"),
                data.get("nickname"),
                data.get("name"),
            )
        self._member_name_cache[cache_key] = name
        return name

    async def user_profile_name(self, event: Any, user_id: str) -> str:
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]

        result = await self.call_onebot_action(
            event,
            "get_stranger_info",
            user_id=self._numeric_if_digits(user_id),
            no_cache=False,
        )
        data = self._onebot_data(result)
        name = ""
        if isinstance(data, dict):
            name = self._first_nonempty_text(
                data.get("card"),
                data.get("nickname"),
                data.get("name"),
                data.get("remark"),
            )
        self._user_name_cache[user_id] = name
        return name

    def sender_name(self, node: Any) -> str:
        if isinstance(node, dict):
            sender = node.get("sender") or {}
            if isinstance(sender, dict):
                return self._first_nonempty_text(
                    sender.get("card"),
                    sender.get("nickname"),
                    sender.get("name"),
                    sender.get("user_id"),
                    "Unknown",
                )
            return self._first_nonempty_text(node.get("user_id"), node.get("uin"), "Unknown")
        name = getattr(node, "name", None)
        nickname = getattr(node, "nickname", None)
        user_id = getattr(node, "user_id", None)
        uin = getattr(node, "uin", None)
        if name or nickname or user_id or uin:
            return self._first_nonempty_text(name, nickname, user_id, uin)
        return "Unknown"

    def sender_user_id(self, node: Any) -> str:
        if isinstance(node, dict):
            sender = node.get("sender") or {}
            if isinstance(sender, dict):
                value = self._first_nonempty_text(
                    sender.get("user_id"),
                    sender.get("uin"),
                    sender.get("uid"),
                    sender.get("qq"),
                )
                if value:
                    return value
            return self._first_nonempty_text(
                node.get("user_id"),
                node.get("uin"),
                node.get("uid"),
                node.get("qq"),
            )
        return self._first_nonempty_text(
            getattr(node, "user_id", None),
            getattr(node, "uin", None),
            getattr(node, "uid", None),
            getattr(node, "qq", None),
        )

    def event_group_id(self, event: Any) -> str:
        for name in ("get_group_id", "get_groupid"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    value = fn()
                    text = self._first_nonempty_text(value)
                    if text:
                        return text
                except Exception:
                    pass

        for obj in (event, getattr(event, "message_obj", None)):
            value = self._nested_first(obj, ("group_id", "groupId"))
            text = self._first_nonempty_text(value)
            if text:
                return text

        origin = str(getattr(event, "unified_msg_origin", "") or "")
        match = re.search(r":GroupMessage:(\d+)(?:_|$)", origin, flags=re.I)
        return match.group(1) if match else ""

    def _nested_first(self, obj: Any, keys: tuple[str, ...]) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            for key in keys:
                if key in obj and obj.get(key) not in (None, ""):
                    return obj.get(key)
            for key in ("data", "raw", "raw_message", "message"):
                value = obj.get(key)
                found = self._nested_first(value, keys)
                if found not in (None, ""):
                    return found
            return None
        if isinstance(obj, list):
            for item in obj:
                found = self._nested_first(item, keys)
                if found not in (None, ""):
                    return found
            return None
        for key in keys:
            if hasattr(obj, key):
                value = getattr(obj, key)
                if value not in (None, ""):
                    return value
        for key in ("data", "raw", "raw_message", "message"):
            if hasattr(obj, key):
                found = self._nested_first(getattr(obj, key), keys)
                if found not in (None, ""):
                    return found
        return None

    def _onebot_data(self, result: Any) -> Any:
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return result["data"]
        return result

    def _numeric_if_digits(self, value: str) -> int | str:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else text

    def _valid_display_name(self, value: Any, user_id: str = "") -> str:
        text = self._first_nonempty_text(value)
        placeholder_names = {
            "Unknown",
            "QQ用户",
            "QQ用戶",
            "QQ User",
            "QQUser",
        }
        if not text or text in placeholder_names or (user_id and text == str(user_id)):
            return ""
        return text

    def _is_forward_preview_placeholder(self, value: Any) -> bool:
        text = self._first_nonempty_text(value)
        return text in {"[聊天记录]", "[聊天記錄]", "[Forward]", "[转发消息]", "[轉發消息]"}

    def _is_forward_text(self, value: Any) -> bool:
        text = self._first_nonempty_text(value)
        return text.startswith(("[Forward]", "[Forward:", "[ForwardPreview]"))

    def _first_nonempty_text(self, *values: Any) -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _get_message_segments(self, event: Any) -> list[Any]:
        try:
            segments = event.get_messages()
            if isinstance(segments, list):
                return segments
        except Exception:
            pass
        msg_obj = getattr(event, "message_obj", None)
        segments = getattr(msg_obj, "message", None) if msg_obj is not None else None
        if isinstance(segments, list):
            return segments
        return []

    def _segment_type(self, seg: Any) -> str:
        if isinstance(seg, dict):
            return self._normalize_segment_type(seg.get("type"))
        for name in ("type", "component_type"):
            if hasattr(seg, name):
                seg_type = self._normalize_segment_type(getattr(seg, name))
                if seg_type:
                    return seg_type
        cname = type(seg).__name__.lower()
        # AstrBot components often have class names: Plain, Image, At, Reply, Forward.
        if cname == "plain":
            return "text"
        if cname == "image":
            return "image"
        if cname == "at":
            return "at"
        if cname == "reply":
            return "reply"
        if cname == "forward":
            return "forward"
        if cname == "json":
            return "json"
        return cname

    def _normalize_segment_type(self, value: Any) -> str:
        if value is None:
            return ""
        name = getattr(value, "name", None)
        text = str(name or value).strip().strip("[]").lower()
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        if text == "plain":
            return "text"
        return text

    def _segment_data(self, seg: Any) -> dict[str, Any]:
        if isinstance(seg, dict):
            data = seg.get("data")
            if isinstance(data, dict):
                return data
            if data is not None:
                return {"data": data}
            return {}
        data: dict[str, Any] = {}
        for name in (
            "text",
            "url",
            "file",
            "summary",
            "qq",
            "id",
            "content",
            "data",
            "json",
            "raw",
            "raw_message",
            "bytesData",
            "prompt",
            "title",
            "desc",
            "nodes",
            "messages",
            "name",
            "uin",
            "sender",
            "user_id",
            "nickname",
        ):
            if hasattr(seg, name):
                data[name] = getattr(seg, name)
        return data
