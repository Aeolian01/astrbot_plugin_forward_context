# astrbot_plugin_forward_context

把 QQ 复杂消息解析成可读文本的 AstrBot 插件。

本插件用于解决 AstrBot / NapCat / aiocqhttp 场景里，合并转发、引用合并转发、嵌套转发、图片消息、QQ JSON / ARK 分享卡片在进入 LLM 流程时经常变成空消息或占位符的问题。解析后的文本默认会写入 `event.extra`，供 enhance-mode、pokepro、私聊 LLM 请求和其他插件统一消费；如有兼容旧链路的需要，也可以显式开启注入 `event.message_str` 或 LLM request prompt。  

## 核心能力

- 解析用户直接发送的合并转发：`[CQ:forward,id=...]`
- 解析回复引用里的合并转发：`[CQ:reply,id=...]`
- 递归展开嵌套 `forward.data.content`
- 兼容 NapCat `multiForwardMsgElement.resId` / `fileName` 兜底
- 当 `get_forward_msg` 原始 ID 返回空时，自动尝试 raw 中的 `resId`
- 解析 `multiForwardMsgElement.xmlContent` 里的 XML 预览标题和摘要
- 处理文本、@、回复、图片、语音、视频、`Node`、`Nodes` 等常见消息段
- 支持 QQ JSON / ARK 分享卡片，例如小黑盒、新闻、应用分享等 `ComponentType.Json`
- 可把 JSON 分享卡片的 `url` 交给当前 LLM provider 读取并生成 `[UrlSummary]`
- 可选图片描述，支持内存缓存和 JSON 持久化缓存
- 可选视频描述，通过 `video_urls=[...]` 直传给支持视频输入的 provider，失败时回退 `[Video]`
- 可缓存其他插件最近输出，默认只写入 extra，由消费方决定是否拼入 prompt
- 暴露 `cache_plugin_output()`，供主动推送类插件手动写入最近输出缓存
- 暴露图片描述缓存读写接口，供 enhance-mode 等插件共享图片转述缓存

## 工作流程

```text
QQ 原始消息 / AstrBot 消息链
  -> forward_context 解析合并转发、JSON 卡片、图片等复杂段
  -> 写入 event.extra["_forward_context_text"]
  -> 写入 event.extra["_forward_context_parsed"] / image_count / video_count
  -> enhance-mode / 其他插件统一读取 extra 组装最终 prompt
  -> LLM 读取到可理解的上下文
```

如果开启插件输出缓存，还会额外维护同一 `unified_msg_origin` 下最近的普通插件输出：

```text
其他插件输出
  -> forward_context 在 on_decorating_result 阶段转成文本
  -> 写入 event.extra["_forward_context_recent_outputs"]
  -> 由消费方决定是否追加进最终 prompt
```

## 安装

把插件目录复制到 AstrBot 插件目录，例如：

```bash
a/root/astrbot-napcat/data/plugins/astrbot_plugin_forward_context
```

然后重启 AstrBot：

```bash
docker restart astrbot
```

如果使用发布 zip，解压后应得到一个显式顶层目录：

```text
astrbot_plugin_forward_context/
  metadata.yaml
  _conf_schema.json
  requirements.txt
  main.py
  parser.py
  image_caption.py
  cache.py
  config.py
  recent_context.py
  public_api.py
  __init__.py
  README.md
```

## 推荐配置

默认配置现在采用“extra-only”模式，适合和 enhance-mode 等统一组装 prompt 的插件联动：

```json
{
  "enable": true,
  "parse_group": true,
  "parse_private": true,
  "set_event_extra": true,
  "extra_key": "_forward_context_text",
  "inject_to_event_message_str": false,
  "inject_to_llm_request": false,
  "rewrite_when_prompt_empty_only": true,

  "capture_plugin_outputs": false,
  "plugin_output_extra_key": "_forward_context_recent_outputs",
  "plugin_output_ttl_sec": 600,
  "plugin_output_max_items": 5,
  "plugin_output_max_chars": 3000,
  "inject_plugin_outputs_to_llm_request": false,
  "include_llm_results_in_plugin_outputs": false,

  "max_forward_depth": 3,
  "max_forward_messages": 80,
  "max_output_chars": 8000,
  "parse_reply_forward": true,
  "parse_direct_forward": true,
  "parse_nested_forward": true,
  "xml_preview_fallback": true,

  "parse_json_url_content": true,
  "json_url_summary": true,
  "json_url_summary_provider_id": "",
  "json_url_summary_prompt": "请直接读取下面链接并用简体中文总结内容，限制在 100 字以内。优先说明主题、关键信息、时间/名称/结论；如果无法读取链接，请基于分享卡片信息简要说明。",
  "json_url_summary_max_chars": 100,
  "json_url_summary_gemini_url_context": true,

  "image_caption": false,
  "image_caption_provider_id": "",
  "image_caption_provider_ids": [],
  "image_caption_prompt": "请用简体中文简短描述这张图片，重点说明画面主体和可见文字。",
  "image_caption_timeout_sec": 30,
  "image_caption_cache_enable": true,
  "image_caption_cache_persist": true,
  "image_caption_cache_ttl_sec": 2592000,
  "image_caption_cache_max_items": 1000,

  "video_caption": false,
  "video_caption_provider_id": "",
  "video_caption_provider_ids": [],
  "video_caption_prompt": "请用简体中文简短描述这个视频，重点说明主要画面、动作、可见文字和关键信息。",
  "video_caption_timeout_sec": 60,
  "video_caption_cache_enable": true,
  "video_caption_cache_persist": true,
  "video_caption_cache_ttl_sec": 2592000,
  "video_caption_cache_max_items": 1000,

  "debug_log_raw_forward_result": false
}
```

常用配置说明：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `set_event_extra` | `true` | 把解析结果写入 `event.extra[extra_key]`，作为统一注入入口 |
| `inject_to_event_message_str` | `false` | 仅在需要兼容 AstrBot 默认链路时才开启，同步解析结果到 `event.message_str` |
| `inject_to_llm_request` | `false` | 仅在没有统一 prompt 组装器时才开启，直接改写 LLM request prompt |
| `rewrite_when_prompt_empty_only` | `true` | 开启 request prompt 改写后，仅当原 prompt 为空、`[转发消息]`、`[引用消息]`、`[ComponentType.Json]` 等占位内容时改写 |
| `capture_plugin_outputs` | `false` | 缓存其他插件最近输出，供消费方后续选择性读取 |
| `inject_plugin_outputs_to_llm_request` | `false` | 仅在启用 request prompt 改写并希望自动追加最近输出时开启 |
| `parse_json_url_content` | `true` | 解析 JSON 分享卡片时尝试处理卡片 URL |
| `json_url_summary` | `true` | 调用当前或指定 LLM provider 生成 `[UrlSummary]` |
| `json_url_summary_gemini_url_context` | `true` | 使用 Gemini / Google GenAI provider 总结链接时，临时启用 Gemini 原生 URL Context，适配 `gemini-2.5-flash` 等模型 |
| `image_caption` | `false` | 为图片消息生成文字描述，需要可用的视觉模型/provider |
| `image_caption_provider_ids` | `[]` | 图片转述 provider 列表，按顺序尝试；单个 provider 抛异常、超时或返回空文本时切换到下一个 |
| `image_caption_provider_id` | `""` | 兼容旧版单 provider 配置；`image_caption_provider_ids` 非空时优先使用列表 |
| `image_caption_timeout_sec` | `30` | 单个 provider 的图片转述最长等待秒数，超时后尝试下一个 provider，`0` 表示不限制 |
| `video_caption` | `false` | 为视频消息生成文字描述，调用 provider 时使用 `video_urls=[...]`，不做本地抽帧 |
| `video_caption_provider_ids` | `[]` | 视频转述 provider 列表，按顺序尝试；异常、超时或空文本会切换到下一个 |
| `video_caption_provider_id` | `""` | 兼容单 provider 配置；`video_caption_provider_ids` 非空时优先使用列表 |
| `video_caption_timeout_sec` | `60` | 单个 provider 的视频转述最长等待秒数，`0` 表示不限制 |
| `debug_log_raw_forward_result` | `false` | 打印 `get_forward_msg` / `get_msg` 返回结构，排查适配器字段差异 |

## JSON 分享卡片

当收到类似下面的 QQ JSON / ARK 分享卡片时：

```text
[ComponentType.Json]
```

插件会从 OneBot 段、`raw_message`、`arkElement.bytesData` 等位置提取 JSON，并输出类似：

```text
[JsonShare]
title: 《ARC Raiders》中国版号正式获批，国服定名《弧光猎人》
desc: 下载小黑盒查看更多精彩内容
tag: 小黑盒
url: https://api.xiaoheihe.cn/...
prompt: [分享]《ARC Raiders》中国版号正式获批...
[UrlSummary]
1. 链接内容要点...
2. 关键信息...
3. 结论或背景...
```

`parse_json_url_content` 和 `json_url_summary` 默认开启。插件本身不会抓取网页正文，而是把 URL 和分享卡片信息交给当前 LLM provider；能否直接读取 URL 取决于你配置的模型和 provider 能力。可以通过 `json_url_summary_provider_id` 指定专门用于链接总结的 provider。

当链接总结 provider 是 Gemini / Google GenAI 时，`json_url_summary_gemini_url_context` 默认会临时启用 Gemini 原生 URL Context 格式，适配 `gemini-2.5-flash` 等支持 URL Context 的模型。

为避免重复调用，URL 总结会在插件运行期间做短期内存缓存。

## 插件输出缓存

如果希望其他插件复用最近输出，可以开启：

```json
{
  "capture_plugin_outputs": true,
  "plugin_output_ttl_sec": 600,
  "plugin_output_max_items": 5,
  "plugin_output_max_chars": 3000
}
```

开启后，本插件会在 AstrBot 发送普通插件结果前，把 `Plain`、`Image`、`Node`、`Nodes` 等消息链解析成文本，按 `unified_msg_origin` 缓存到 `_forward_context_recent_outputs`。默认不会自动追加进 LLM prompt，而是由消费方自行决定是否读取和拼接。

默认不会缓存机器人自己的 LLM 回复，避免后续 prompt 被自身回复反复污染。只有确实需要时再开启 `include_llm_results_in_plugin_outputs`。

注意：自动捕获只覆盖通过 `yield event.chain_result(...)` / `yield event.plain_result(...)` 返回的插件结果。像 twitter 定时推送中直接 `context.send_message(...)` 的主动消息不经过当前事件结果，需要主动调用公共接口写入缓存。

## 公共接口

主动推送类插件可以在发送前调用本插件暴露的输出缓存接口：

```python
from astrbot_plugin_forward_context import cache_plugin_output

await cache_plugin_output(
    umo=umo,
    chain=message_chain.chain,
    source="astrbot_plugin_twitter.active_push",
)
```

也可以直接传文本：

```python
await cache_plugin_output(
    umo=umo,
    text="这里是其他插件刚生成的文本结果",
    source="astrbot_plugin_example",
)
```

返回值是当前会话渲染后的最近输出块；如果 `forward_context` 未加载或 `capture_plugin_outputs` 未开启，会返回空字符串。

图片转述可通过 `image_caption_provider_ids` 配置多个视觉模型 provider，插件会按列表顺序尝试。显式调用 `get_or_create_image_caption(..., provider_id="...")` 时只使用传入的 provider；未传入时优先使用列表，再回退到旧的 `image_caption_provider_id`，最后使用当前会话 provider。缓存仍按图片来源命中，不区分 provider。

图片描述缓存也可以由其他插件复用：

```python
from astrbot_plugin_forward_context import (
    build_image_caption_sources,
    get_cached_image_caption,
    get_cached_image_message,
    get_or_create_image_caption,
    set_cached_image_caption,
)

sources = build_image_caption_sources(
    image_url=image_url,
    cache_source=cache_source,
)
caption = await get_cached_image_caption(sources)
caption = caption or await get_or_create_image_caption(
    event,
    image_url,
    cache_source=cache_source,
)
image_entry = await get_cached_image_message(umo, message_id)
await set_cached_image_caption(sources, "图片描述")
```

`get_cached_image_caption` 和 `set_cached_image_caption` 兼容单个 source 字符串，也支持 source 列表；列表里任一别名命中后会回写同组别名。`get_cached_image_message` 返回持久化的 `message_id -> 图片记录`，不存在时返回空字典。如果 `forward_context` 未加载、缓存未注册或缓存关闭，读取返回空字符串/空字典，写入为 no-op。

平台历史消息也可以交给本插件按同一套合并转发、JSON 分享卡片、图片规则解析：

```python
from astrbot_plugin_forward_context import parse_history_message

text = await parse_history_message(event, adapter_history_message)
```

如果 `forward_context` 未加载或解析器未注册，返回空字符串。

## enhance-mode 集成

推荐集成方式：

```python
parsed = event.get_extra("_forward_context_text")
recent = event.get_extra("_forward_context_recent_outputs")
```

由 enhance-mode 统一决定：

- 当前消息是否优先使用 `parsed`
- 是否把 `recent` 拼入最终 prompt
- 何时、以什么格式给 provider 发请求

这种 extra-only 方式可以避免多个插件同时改写 `event.message_str` 或 `req.prompt` 导致重复注入。

## pokepro / twitter 集成

pokepro、twitter 等插件如果只需要共享解析结果或最近输出，一般不需要本插件自动改 prompt，只要读取 extra 或调用公共接口即可。主动推送类插件仍可在 `context.send_message(...)` 前调用 `cache_plugin_output()`，让后续统一组装器读取最近输出。

## 调试

启用 `debug_log_raw_forward_result` 后，插件会打印 `get_forward_msg` / `get_msg` 返回结构，方便排查不同 OneBot 适配器的字段差异。

常用日志过滤：

```bash
docker logs astrbot --since 5m | grep -E "forward-context|get_forward_msg|Forward|JsonShare|plugin output cached|prompt rewritten"
```

也可以直接观察写入的 extra：

```python
parsed = event.get_extra("_forward_context_text")
recent = event.get_extra("_forward_context_recent_outputs")
```

## 打包

发布 zip 应只包含插件运行文件，并保留显式顶层目录 `astrbot_plugin_forward_context/`。不要把 `CODEX_*.md`、`PACKAGING.md`、`integration/`、`dist/`、`.git/`、`__pycache__/` 或缓存文件放进发布包。

推荐产物路径：

```text
dist/astrbot_plugin_forward_context.zip
```

发布包内容应与安装章节中的目录结构一致。
