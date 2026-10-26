# MiniMax 国内 Token Plan Plus 接入

核对日期：2026年10月26日。本说明基于 MiniMax 官方文档；兼容适配通过显式模拟契约验证，国内订阅账号也已完成下述自制视频的真实模型流程。模型探测、内容识别与完整视频流程分别记录；整体状态与其他待接入条件见[验收报告](验收报告.md)。

## 先确认订阅与密钥类型

Token Plan Plus 是旧版订阅档位。官方目前已推出 M Plan，旧用户可以保留原套餐；不需要为使用本工作台自动升级。旧 Plus 的具体历史权益以本人的订阅页面和官方确认为准，不能仅凭套餐名称保证 ASR 已包含。

订阅专属 Key 和普通按量 API Key 不能互换。当前官方规则中，订阅 Key 使用套餐额度与适用的积分包；普通按量 Key 消耗开放平台账户余额。ASR 有单独按量价，不能把语音合成或“音频生成”权益当成已经证实的语音转写额度。请先核对 ASR 权益，再决定是否发起真实转写测试。本应用不会升级套餐、代购资源或替换你的密钥。

## 本次真实验收及已知限制

[脱敏真实验收证据](../artifacts/verification/media-model-live.json)记录了一段 **15.3 秒自制视频**的实际完整任务：国内 MiniMax 原生 `asr-1.0` 转写、`MiniMax-M3.1-Flash` 理解 4 张抽样关键帧，再由自配 DeepSeek 文字模型融合分析。任务状态为 `succeeded`，分析依据为 `transcript_and_frames`；4 张画面的颜色、OCR 文字和对应秒数已人工核对。

本次所用国内订阅账号的现有权益页已核对，且该账号的上述 ASR 与图片调用均真实通过。这一结果仅适用于本次账号和配置，不能由“旧 Token Plan Plus”名称推断每个旧 Plus 用户都拥有相同 ASR 权益。本文及公开证据不包含账户余额、会员时间、密钥尾号或订阅页面截图。

ASR 将品牌名“利明智耀”误识别为“利明制药”。原始转写回答保留，未为了通过验收而改写。该夹具的去标点参考文本共 59 字，有 2 处替换，字符错误率为 **2 ÷ 59 ≈ 3.39%**；这只是这一个短夹具的误差，不能推导模型总体准确率。图片结论也仅限实际抽取的 4 帧，不代表逐帧查看了整段视频。涉及品牌、专有名词或精确引用时仍需人工核对。

## 在设置页分别配置

国内推荐 Base URL：`https://api.minimax.cn/v1`。不要把 `/chat/completions` 或 `/speech_to_text` 放进 Base URL。

| 工作台能力 | 官方模型 ID 提示 | 实际接口 | 接入边界 |
|---|---|---|---|
| 文字分析 | `MiniMax-M3.1-Flash`；其他模型按当前权限选择 | `POST /v1/chat/completions` | 文本配置成功不能代替图片或转写验收 |
| 图片理解 | `MiniMax-M3.1-Flash`、`MiniMax-M3` | 同一 Chat Completions 接口，使用 `image_url` | 当前官方支持原生图片输入，无需强制接入 MCP |
| 语音转写 | `asr-1.0` | `POST /v1/speech_to_text` | 旧 Plus 是否覆盖 ASR 须先核对；`speech-2.8-*` 等 TTS 模型不是转写模型 |

文字、图片与转写分别填写模型和密钥，并分别测试。本应用不会把文字密钥复制给其他能力，也不会根据地址提示自动改变模型。设置页的模型 ID 只是提示，不代表账户已经有对应权限。点击能力测试会发起真实请求，可能消耗套餐、积分或按量费用。

## 转写适配与限制

- 只有精确主机名 `api.minimax.cn` 和 `api.minimax.io` 自动选择 `speech_to_text`；普通兼容供应商仍请求 `audio/transcriptions`，不会因地址包含“minimax”就改变路径。国内与国际密钥须匹配对应服务地区，国际适配当前仅通过契约测试。
- 原生请求是 `multipart/form-data`，包含 `model`、`file`、`response_format=json`。成功响应读取顶层 `text`，保留供应商返回的 `duration`、`trace_id`；不假设 `data` 包装，也不编造用量。
- 官方 ASR 单次不超过 500 秒、50 MB，支持带容器的 WAV、AIFF、FLAC、M4A、MP3、AAC、Opus、Ogg，不支持裸 PCM。工作台先用 FFmpeg 转为单声道 WAV，按 120 秒分段。官方主机的适配在请求前检查文件大小，并检查 WAV 时长；大小按 50,000,000 字节作保守限制。其他格式的时长还受媒体预处理与服务端校验。
- HTTP `401` 归为认证错误，`402` 为配额/余额不足，`413` 为文件过大，`422` 为音频被拒绝，`429` 为限流，`5xx` 为临时服务故障。不会把错误响应原文写入用户提示或日志。
- 当前原生 ASR 使用 HTTP 错误与 OpenAI 风格错误对象。若意外收到 HTTP 200 的错误对象、非零 `base_resp` 包装或缺少有效 `text`，适配会拒绝将其标记成功；没有猜测旧式业务错误码。
- 连通探测发送 1 秒静音 WAV。即使通过，也只证明接口接受请求并返回文本字段，不能证明语音识别准确率、视频分析闭环或旧 Plus 的所有权益。

## 可复现契约验证

```bash
.venv/bin/python -m pytest tests/test_minimax.py tests/test_core.py -q
npm run build --prefix frontend
```

测试使用 `httpx.MockTransport`、自行生成的 WAV 与隔离临时数据，不访问真实模型或用户凭证。覆盖官方/普通/仿冒主机路径选择、探测与实际转写使用同一路径、独立能力密钥、原生 HTTP 错误与脱敏、异常 200 响应，以及文件大小和时长超限时不发请求。

## 官方依据

- [Token Plan 老用户安排](https://platform.minimax.cn/docs/m-plan/token-plan-notice)：原套餐与新 M Plan 的区别，历史权益保留与升级条件。
- [订阅用量说明](https://platform.minimax.cn/docs/m-plan/usage-rules)：共享额度、积分和按量 API Key 的计费边界。
- [OpenAI 兼容接口与多模态输入](https://platform.minimax.cn/docs/api-reference/text-openai-api)：模型 ID、Base URL、原生图片/视频内容块。
- [语音识别指南](https://platform.minimax.cn/docs/guides/speech-to-text)：`asr-1.0`、`speech_to_text`、响应、文件限制和 HTTP 错误。
- [官方定价](https://platform.minimax.cn/docs/pricing/overview)：ASR 与 TTS 分开列价；实际费用应在使用前重新核对。
