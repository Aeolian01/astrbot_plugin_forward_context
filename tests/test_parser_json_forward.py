from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot_plugin_forward_context.config import ForwardContextConfig
from astrbot_plugin_forward_context.parser import ForwardParser


def _json_seg(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}


def _forward_response(text: str) -> dict[str, Any]:
    return {
        "data": {
            "messages": [
                {
                    "sender": {"nickname": "Twitter"},
                    "message": [{"type": "text", "data": {"text": text}}],
                }
            ]
        }
    }


class _Api:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if action == "get_forward_msg":
            return self.responses.get(str(params.get("id")), {})
        return {}


class _Bot:
    def __init__(self, api: _Api) -> None:
        self.api = api


class _MessageObj:
    def __init__(
        self,
        *,
        message: list[Any] | None = None,
        raw: Any = None,
        raw_message: Any = None,
    ) -> None:
        self.message = message or []
        self.raw = raw
        self.raw_message = raw_message


class _Event:
    unified_msg_origin = "origin"

    def __init__(self, segments: list[Any], api: _Api, *, raw: Any = None) -> None:
        self.segments = segments
        self.bot = _Bot(api)
        self.message_obj = _MessageObj(message=segments, raw=raw)

    def get_messages(self) -> list[Any]:
        return self.segments


def _parser() -> ForwardParser:
    return ForwardParser(ForwardContextConfig(json_url_summary=False))


def test_json_chat_record_card_expands_from_outer_res_id() -> None:
    seg = _json_seg({"desc": "[聊天记录]", "tag": "群聊的聊天记录"})
    api = _Api({"res-1": _forward_response("tweet text")})
    event = _Event([seg], api, raw={"multiForwardMsgElement": {"resId": "res-1"}})

    result = asyncio.run(_parser().parse_event(event))

    assert result.text == "[Forward]\n  1. Twitter: tweet text"
    assert api.calls == [("get_forward_msg", {"id": "res-1"})]


def test_json_chat_record_without_res_id_falls_back_to_json_share() -> None:
    seg = _json_seg({"desc": "[聊天记录]", "tag": "群聊的聊天记录"})
    api = _Api()
    event = _Event([seg], api)

    result = asyncio.run(_parser().parse_event(event))

    assert result.text == "[JsonShare]\ndesc: [聊天记录]\ntag: 群聊的聊天记录"
    assert api.calls == []


def test_regular_json_share_does_not_call_get_forward_msg() -> None:
    seg = _json_seg({"title": "新闻标题", "desc": "普通分享", "tag": "小黑盒"})
    api = _Api()
    event = _Event([seg], api)

    result = asyncio.run(_parser().parse_event(event))

    assert result.text == "[JsonShare]\ntitle: 新闻标题\ndesc: 普通分享\ntag: 小黑盒"
    assert api.calls == []


def test_history_json_card_uses_outer_context_res_id() -> None:
    seg = _json_seg({"desc": "[聊天记录]", "tag": "群聊的聊天记录"})
    history = {
        "message": [seg],
        "multiForwardMsgElement": {"resId": "hist-res"},
    }
    api = _Api({"hist-res": _forward_response("history tweet")})
    event = _Event([], api)

    texts = asyncio.run(_parser().extract_json_texts_from_obj(history, event=event))

    assert texts == ["[Forward]\n  1. Twitter: history tweet"]
    assert api.calls == [("get_forward_msg", {"id": "hist-res"})]


def test_empty_forward_result_falls_back_to_single_json_share() -> None:
    seg = _json_seg({"desc": "[聊天记录]", "tag": "群聊的聊天记录"})
    api = _Api({"empty-res": {"data": {"messages": []}}})
    event = _Event([seg], api, raw={"multiForwardMsgElement": {"resId": "empty-res"}})

    result = asyncio.run(_parser().parse_event(event))

    assert result.text == "[JsonShare]\ndesc: [聊天记录]\ntag: 群聊的聊天记录"
    assert "[Forward" not in result.text
    assert api.calls == [("get_forward_msg", {"id": "empty-res"})]
