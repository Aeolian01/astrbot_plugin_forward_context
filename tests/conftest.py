from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


PLUGINS_ROOT = Path(__file__).resolve().parents[2]
plugins_root_str = str(PLUGINS_ROOT)
if plugins_root_str not in sys.path:
    sys.path.insert(0, plugins_root_str)


if importlib.util.find_spec("astrbot") is None:
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    core_mod = types.ModuleType("astrbot.core")
    config_pkg_mod = types.ModuleType("astrbot.core.config")
    astrbot_config_mod = types.ModuleType("astrbot.core.config.astrbot_config")

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class _Filter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def event_message_type(*args, **kwargs):
            def deco(func):
                return func

            return deco

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def deco(func):
                return func

            return deco

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class AstrBotConfig(dict):
        pass

    api_mod.logger = _Logger()
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter
    star_mod.Context = Context
    star_mod.Star = Star
    astrbot_config_mod.AstrBotConfig = AstrBotConfig
    astrbot_mod.api = api_mod
    api_mod.event = event_mod
    api_mod.star = star_mod
    core_mod.config = config_pkg_mod
    config_pkg_mod.astrbot_config = astrbot_config_mod

    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.core": core_mod,
            "astrbot.core.config": config_pkg_mod,
            "astrbot.core.config.astrbot_config": astrbot_config_mod,
        }
    )
