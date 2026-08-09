# AI 智能体框架使用说明

## 文档定位

本文是本仓库 AI 框架的维护者和 agent 开发手册。它描述的是当前仓库吸收上游 AI 重构后的实际接口，不是上游项目的原样文档。

适用范围：

- `zhenxun/` 核心服务和 `zhenxun/plugins/` 第一方插件；
- 需要调用聊天、视觉、结构化输出、Embedding、重排、生图或语音能力的代码；
- 需要工具调用、Agent、Team、Workflow、Memory、RAG、MCP 或运行上下文的代码。

明确边界：

- 第一方新代码统一使用 `zhenxun.services.ai`；
- `zhenxun.services.llm` 只是给尚未迁移的外部插件保留的兼容门面，不再增加业务能力；
- `plugins/` 是 vendor 目录，不要为了迁移 AI 修改其中的插件；
- 本轮保留本地 `scheduler` 和 `scheduler_admin`，AI 只通过 `ScheduleContext` 适配，不改调度器实现；
- Sandbox 和 MCP 默认不启用，不需要 Docker，也不要在测试或文档中填写真实 API key。

## 先理解整体变化

旧模式通常是插件自己拼厂商请求、自己转换消息、自己判断异常和自己处理模型名称。换一个 Gemini、DeepSeek 或 OpenAI 兼容网关，调用方就要跟着改。

现在的分层方式是：

```text
插件 / 定时任务 / Agent
          │
          ▼
zhenxun.services.ai（统一 API 与运行时）
  ├─ 消息与多模态 Part
  ├─ 配置、模型路由、健康检查、重试和 failover
  ├─ Provider Adapter（Gemini / OpenAI 兼容 / GLM / 豆包等）
  ├─ Tools / Capabilities / Agent / Team / Workflow
  ├─ Memory / RAG / MCP / Sandbox
  └─ RunContext（NoneBot 事件和本地 ScheduleContext）
          │
          ▼
     外部模型服务
```

调用方只表达“我要完成什么任务”，底层负责把同一个意图翻译成具体厂商协议。这样群聊总结、Quote OCR、RollPig、AI Creation 等第一方调用可以共用同一套错误处理和模型能力判断。

## 第一方公共 API

最常用的入口都从 `zhenxun.services.ai` 导入：

```python
from zhenxun.services.ai import (
    LLMMessage,
    chat,
    create_image,
    create_speech,
    embed,
    generate,
    generate_structured,
    rerank,
)
```

### 聊天与生成

`chat()` 适合一般单轮调用，接收字符串、NoneBot 消息或 `LLMMessage` 列表，返回 `ChatResponse`：

```python
response = await chat(
    "用一句话解释什么是 RAG",
    model="Gemini/gemini-3.5-flash",
    instruction="回答要简洁，面向初学者。",
)
text = response.text
```

`generate()` 是统一底层生成入口，适合调用方已经准备好标准消息列表的场景；`chat()` 会先替调用方做消息标准化，再转到它。

除非确实需要访问请求/响应的完整字段，不要在插件里直接构造 adapter 或 HTTP 请求。统一响应中还包含模型名、工具调用和 `UsageInfo` 等信息，具体字段以 `zhenxun/services/ai/core/messages/responses.py` 为准。

### 结构化输出

需要稳定 JSON 时，优先使用 `generate_structured()`，让 Pydantic 模型负责校验和解析，而不是手写 `json.loads()`：

```python
from pydantic import BaseModel
from zhenxun.services.ai import generate_structured


class Quote(BaseModel):
    author: str
    content: str


quote = await generate_structured(
    "从这段文字提取作者和正文：……",
    response_model=Quote,
)
```

框架会根据模型能力选择原生 JSON、工具调用或提示词策略，并在解析失败时按配置重试。需要额外规则时可传 `guardrails`，不建议在业务层重复实现同样的校验循环。

### 生图、语音、Embedding 和重排

```python
image = await create_image("一只戴着围巾的橘猫", model="Gemini/gemini-2.5-flash-image")
audio = await create_speech("你好，欢迎使用机器人")
vectors = await embed(["第一段文本", "第二段文本"], task="document")
ranked = await rerank("用户的问题", ["候选文档 A", "候选文档 B"], top_n=1)
```

这些接口统一处理批量输入、任务类型、厂商 endpoint 和响应对象。多模态 Embedding 可以显式传 `multimodal=True`，默认仍是纯文本，避免无意中增加请求成本。

## 消息与多模态 Part

`LLMMessage` 是统一的“消息信封”，`role` 表示 `user`、`assistant`、`system` 或 `tool`，`content` 是有顺序的 Part 列表。字符串会自动规范化为 `TextPart`，所以简单调用不需要手动包一层。

```python
from zhenxun.services.ai import LLMMessage
from zhenxun.services.ai.core.messages.parts import ImagePart

message = LLMMessage.user("请描述这张图片：") + ImagePart(url="https://example.com/cat.jpg")
response = await chat([message])
```

常用 Part 包括：

- `TextPart`：普通文本；
- `ImagePart`、`AudioPart`、`VideoPart`、`FilePart`：URL、本地路径或二进制媒体；
- `ThoughtPart`：模型思考片段；
- `ToolCallPart`、`ToolReturnPart`：工具调用请求和执行结果。

不要把图片 URL 拼到普通文本中，也不要假设所有 provider 都能处理同样的媒体类型。让 Part 和 adapter 做转换，Gemini 的多条 system 消息也会在适配层按原顺序合并。

## GenerationConfig 与 IntentBuilder

`GenerationConfig` 把配置分为通用参数、输出格式、工具策略、媒体参数和厂商扩展。需要表达“意图”时优先用 `IntentBuilder`，不要在业务代码里硬编码某家 API 的字段名：

```python
from zhenxun.services.ai.core.options import GenerationConfig
from zhenxun.services.ai.llm.builder import IntentBuilder

config = (
    IntentBuilder()
    .with_reasoning("none")
    .config_core(temperature=0.2, max_tokens=800)
    .with_json_output()
    .build()
)
response = await chat("输出一个 JSON 对象", config=config)
```

常用意图：

- `.with_reasoning("none" | "low" | "high" | "max")`：统一表达是否开启思考；
- `.with_json_output()`：要求通用 JSON；
- `.require_structured_output(Schema)`：根据 Pydantic 模型生成 Schema；
- `.with_local_cache(ttl=3600)`：开启相同输入的本地缓存，适合确定性抽取和 Embedding；
- `.with_image_generation_params(aspect_ratio="16:9", resolution="1K")`：统一生图参数；
- `.with_vision_optimization("low" | "medium" | "high")`：视觉输入质量意图；
- `.with_safety_level(...)`：使用统一安全等级；
- `.openai.enable_server_storage(...)`、`.gemini.set_safety_threshold(...)`：确有需要时再使用厂商专属设置。

`GenerationConfig.merge_with()` 可用于把全局默认和单次覆盖做深度合并。群聊总结现有的模型优先级、关闭 Gemini/DeepSeek 思考和 `<think>` 清理行为仍由调用方保留，迁移时不要删掉这些业务策略。

## Provider、模型路由与 failover

AI 配置仍位于 `AI` 配置组，但从单一 `default_model_name` 扩展成按任务分类的配置：

```yaml
AI:
  default_models:
    chat: Gemini/gemini-3.5-flash
    embedding: Gemini/gemini-embedding-2
    image: Gemini/gemini-2.5-flash-image
    tts: Gemini/gemini-3.1-flash-tts-preview
    rerank: siliconflow/BAAI/bge-reranker-v2-m3
  MODEL_GROUPS:
    cheap_models:
      - Gemini/gemini-3.5-flash
      - Doubao/doubao-seed-1-6-250615
  PROVIDERS: []
```

旧配置只读兼容：当没有 `default_models.chat` 时，`AI.default_model_name` 会作为 chat 默认值；框架不会自动迁移数据库或覆盖用户配置。新代码应写 `default_models.chat`。

每个 Provider 可配置多个 key、`proxy`、`extra_headers`、`api_base` 和协议类型；单个模型可以覆盖 `api_type` 与 `endpoint`。本地保留了 `zhipu → glm`、`ark → doubao`、`openai_image → openai` 等旧协议别名。

模型名通常写成 `Provider/Model`。也可以把 `MODEL_GROUPS` 的组名传给 `chat(model="cheap_models")`：组会展开、剔除不存在的模型，并按顺序尝试可用模型。健康管理器会记录 key 状态，失败时执行重试和 failover；模型管理器还提供 `list_available_models()`、`list_embedding_models()` 等发现入口。

## Tools 与 Tool Calling

工具是“模型可以请求、框架负责执行”的普通 Python 函数。使用 `@tool` 声明工具，使用 `AgentBuilder.with_tools()` 挂载：

```python
from zhenxun.services.ai import Agent
from zhenxun.services.ai.tools import tool


@tool(description="查询指定城市的天气")
async def get_weather(city: str) -> str:
    return f"{city}：晴"


agent = (
    Agent.builder("weather-agent")
    .with_instruction("需要天气信息时调用工具，不要猜测。")
    .with_tools(get_weather)
    .build()
)
result = await agent.run("北京今天的天气如何？")
```

工具调用过程会被记录为 `ToolCallPart` / `ToolReturnPart`，并统一处理参数校验、异常、重试、并行调用和权限规则。工具需要管理员、群聊、人工审批、缓存或沙箱依赖时，应使用 `zhenxun.services.ai.tools.Rules`、`ToolOptions` 或 `require_sandbox()` 声明，而不是在函数体里复制一套判断。

## Agent、Team 与 Workflow

### Agent

`Agent` 是带有系统指令、工具、记忆、护栏和能力插件的可循环执行单元。它最多执行 `AI.agent_settings.max_cycles` 次工具循环，并受全局循环上限保护；达到上限时可按配置生成兜底总结。

```python
from zhenxun.services.ai import Agent, IntentBuilder

agent = (
    Agent.builder("summarizer")
    .with_instruction("先收集必要信息，再给出简短结论。")
    .with_generation_config(IntentBuilder().with_reasoning("none"))
    .with_memory(True)
    .build()
)
result = await agent.run("总结这段文本：……")
text = result.output
```

### Team

`Team` 把多个 Agent、Workflow 或 Team 作为成员，再用 routing/task strategy 决定分工。适合“检索、分析、写作”之类需要多个专长的任务；每个成员仍可拥有自己的模型和工具。

### Workflow

`Workflow` 是确定性的节点流水线，适合需要固定顺序、状态快照或结构化黑板的任务。它可以嵌套 Agent/Team，也可以被上层 Agent 当作工具调用。两者都返回带状态和用量信息的运行结果，而不是裸字符串。

## RunContext 与调度适配

运行上下文按 Session → Run → Tool Call 分层，保存依赖、共享状态、消息历史、取消令牌和事件总线。NoneBot 事件可以使用 `NoneBotDeps.get_current()`；定时任务使用现有 scheduler 的 `ScheduleContext`：

```python
from zhenxun.services.ai.run import RunContext

context = RunContext.from_schedule(bot, schedule_context)
result = await agent.run("执行本次定时任务", context=context)
```

`ScheduledDeps.from_schedule_context()` 只读取本地上下文已有的 `group_id`、`user_id`、`schedule_id`、`platform_scope`，并结合 Bot 推断平台。不要为了 AI 修改 `zhenxun/services/scheduler/`；如果调度字段发生变化，应在这个适配函数内做最小兼容。

## Memory 与 RAG

### Memory

Memory 负责对话历史隔离和上下文压缩，不等于“永久记住一切”。默认关闭；可以用 `MemoryBuilder.auto()` 开启短期记忆和 LLM 总结：

```python
from zhenxun.services.ai.context.memory.builder import MemoryBuilder

memory = (
    MemoryBuilder.auto()
    .with_multimodal_window(5)
    .with_llm_summary(trigger_tokens=4000, keep_recent_turns=3)
    .build()
)
agent = Agent.builder("chat-agent").with_memory(memory).build()
```

记忆必须绑定明确的 scope，避免不同群组、用户或插件之间串线。压缩策略支持 LLM 总结、结构化总结和 unlimited；多模态视窗会把过旧的图片/视频降为文本占位符以控制 token。

### RAG

RAG 是“文档切块 → Embedding → 存储 → 检索 → 可选重排”的管线。`RAGBuilder` 必须提供存储后端，未指定 Embedder 时才使用 `default_models.embedding`：

```python
from zhenxun.services.ai.context.rag.builder import RAGBuilder

rag = (
    RAGBuilder(storage=my_storage)
    .with_scope("my-plugin")
    .enable_dedup()
    .enable_rerank("siliconflow/BAAI/bge-reranker-v2-m3", top_n=5)
    .build()
)
```

实际存储实现和 `BaseRetriever` 协议应优先复用 `zhenxun/services/ai/context/rag/` 中已有后端，不要在插件里另起向量数据库封装。scope 是隔离边界，写入和检索必须使用同一 scope。

## MCP 与 Sandbox

MCP provider 会把外部 MCP 服务发现为工具，并在退出时统一清理子进程；配置和生命周期由 AI 工具层管理。只有在明确需要外部 MCP 工具时才接入，并为其设置最小权限和闲置清理时间。

Sandbox 是可选的隔离执行环境，通常由工具声明所需依赖。当前默认 `AI.sandbox.enable_sandbox=False`：请求创建 session 会明确抛出 `SandboxFatalError`，但普通 Bot 启动不会加载 Docker 驱动。因此普通插件不应假设 Docker 存在，也不要为了“支持沙箱”在导入阶段连接 Docker。

## 旧 services.llm 兼容门面

`zhenxun.services.llm` 保留旧模块路径、`LLMException`、`LLMErrorCode`、旧生成配置构建器、工具调用符号和常用类型，并把它们转换到新框架。vendor `nonebot_plugin_tarot` 依赖这层，因此不能删除或改名。

迁移规则：

1. 新增或修改第一方代码时直接导入 `zhenxun.services.ai`；
2. 不要把新的业务 API 加到兼容门面；
3. 如果外部插件缺少旧符号，先在门面做窄兼容垫片，再单独安排迁移；
4. 修改门面后至少运行 legacy facade 和 vendor 所需导入测试。

## 新调用方迁移清单

- 把 `from zhenxun.services.llm ...` 换成 `from zhenxun.services.ai ...`；
- 把字符串消息逐步改为 `LLMMessage`，需要图片/文件时使用 Part；
- 把厂商参数改写为 `GenerationConfig` / `IntentBuilder` 意图；
- 把手写 JSON 解析改为 `generate_structured()`；
- 把手写工具循环改为 `Agent` 或 `@tool`；
- 给记忆和知识库指定 scope；
- 定时任务只通过 `RunContext.from_schedule()` 传递上下文；
- 不在 import 阶段读取真实密钥、启动 MCP 或连接 Sandbox。

## 常见问题

**没有配置模型时能启动吗？**

可以。Sandbox 默认关闭，AI 服务按需解析；没有可用 Provider 时，实际调用才会返回配置错误，不应阻断普通插件加载。

**为什么 `chat()` 返回的不是字符串？**

返回的是 `ChatResponse`，使用 `.text` 取纯文本；保留响应对象可以同时读取模型、工具调用和 token 用量。

**为什么结构化结果失败后又请求了几次？**

这是 `structured_retries` / `max_retries` 的 IVR 自校正流程。需要降低成本时在单次调用传更小的 `max_retries`，不要自行绕过校验。

**为什么新代码不能继续用旧门面？**

兼容门面只保证旧插件可运行，业务能力和类型契约以 `services.ai` 为准；继续依赖旧路径会让后续框架升级变得不可控。

## 验证命令

修改 AI 核心或调用方后，先做最小范围验证：

```bash
TMPDIR=/tmp/ai-framework-pytest uv run pytest -q -s tests/test_ai_framework_core.py tests/test_llm_legacy_facade.py
uv run pytest -q -s tests/test_gemini_adapter.py tests/test_summary_group_summary_generation.py
uv run pytest -q -s tests/test_quote_reply_resolution.py -k 'text_recognition or paddleocr_api'
uv run python -m compileall -q zhenxun/services/ai zhenxun/services/llm
git diff --check
```

没有 API key 的启动烟测只验证导入和生命周期，不验证外部模型行为。当前 `tests/test_ai_search_services.py` 依赖的插件在本仓库不存在，不能把它当作本轮失败依据。

## 当前能力边界

本轮已经把统一 LLM、多模态、Agent、Flow、Tools、Memory/RAG、MCP、Sandbox 和运行上下文接入核心层，但以下内容仍是显式边界：

- 不自动迁移数据库，也不覆盖已有 AI 配置；
- 不启用 Sandbox、MCP 或真实外部模型调用作为启动前提；
- 不吸收上游 scheduler 重构；
- 不修改 vendor 插件；
- 各 provider 的高级能力仍取决于其 adapter 和远端 API，框架统一的是调用契约，不是凭空增加厂商能力。
