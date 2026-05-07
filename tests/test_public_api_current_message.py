from __future__ import annotations

import pytest

from astrbot_plugin_forward_context.public_api import (
    parse_current_message,
    register_current_message_parser,
    unregister_current_message_parser,
)


@pytest.mark.asyncio
async def test_parse_current_message_uses_registered_parser() -> None:
    calls: list[object] = []

    async def parser(event: object) -> str:
        calls.append(event)
        return "expanded forward text"

    event = object()
    register_current_message_parser(parser)
    try:
        result = await parse_current_message(event)
    finally:
        unregister_current_message_parser(parser)

    assert result == "expanded forward text"
    assert calls == [event]


@pytest.mark.asyncio
async def test_parse_current_message_returns_empty_without_parser() -> None:
    async def parser(_event: object) -> str:
        return "stale"

    register_current_message_parser(parser)
    unregister_current_message_parser(parser)

    assert await parse_current_message(object()) == ""
