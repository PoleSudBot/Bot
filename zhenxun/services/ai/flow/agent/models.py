from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from zhenxun.services.ai.capabilities import CapabilitySource, CombinedCapability
from zhenxun.services.ai.context.memory.builder import MemoryBuilder
from zhenxun.services.ai.context.memory.engine import SessionMemoryContext
from zhenxun.services.ai.context.memory.models import MemoryConfig
from zhenxun.services.ai.context.memory.types import SessionMetadata
from zhenxun.services.ai.core.messages import (
    AgentMessage,
    ChatResponse,
    LLMMessage,
    ToolCallPart,
    UsageInfo,
)
from zhenxun.services.ai.core.models import ModelCapabilities
from zhenxun.services.ai.core.options import GenerationConfig
from zhenxun.services.ai.flow.core.models import BaseRuntimeConfig
from zhenxun.services.ai.run import RunContext
from zhenxun.services.ai.run.models import AgentRunResult, AgentTask
from zhenxun.services.ai.tools.core.toolkit import BaseToolkit
from zhenxun.services.ai.tools.engine.registry import ToolCollection
from zhenxun.utils.pydantic_compat import model_copy


class Persona(BaseModel):
    """智能体人设与上下文背景"""

    role: str = Field(...)
    """扮演的角色身份"""

    goal: str = Field(...)
    """角色的核心目标"""

    backstory: str | None = Field(default=None)
    """角色背景故事或性格设定"""

    model_config = ConfigDict(extra="ignore")  # type: ignore


class AgentConfig(BaseRuntimeConfig):
    """统一的智能体全局与单次运行配置"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_cycles: int = Field(default=10)
    """工具调用最大循环次数"""
    global_max_cycles: int | None = Field(default=None)
    """整个会话生命周期内的绝对最大循环次数上限（覆盖全局配置）。"""
    enable_parallel_calls: bool = Field(default=True)
    """允许并行工具调用"""
    reflexion_retries: int = Field(default=1)
    """反思重试次数"""
    enable_fallback_summary: bool = Field(default=True)
    """达到最大循环次数时，是否触发大模型兜底总结（而不是直接报错）"""
    enable_hitl: bool | None = Field(default=None)
    """是否允许智能体主动挂起任务，向用户求助 (Human-in-the-Loop)。
    若为 None 则跟随全局设置。
    """

    message_history: Sequence[AgentMessage] | None = Field(default=None)
    """初始化的底层对话历史记录。"""

    memory: MemoryConfig | MemoryBuilder | bool | None = Field(default=None)
    """单次运行级别的记忆门面覆盖"""
    generation_config: GenerationConfig | None = Field(default=None)
    """单次运行覆盖的大模型生成配置。"""
    capabilities: list[CapabilitySource] | None = Field(default=None)
    """仅针对本次运行动态注入的临时拦截器/能力组件列表。"""
    skills: Sequence[str | Path | Any] | None = Field(default=None)
    """仅针对本次运行动态注入的临时技能集合。"""
    executor: Any | None = Field(default=None)
    """单次运行覆盖的核心执行引擎策略 (BaseAgentExecutor)。"""

    verbose_ui: bool = Field(default=False)
    """是否在 UI 前端展示细粒度的工具执行中间过程。
    在不支持流式更新的平台(如QQ)建议保持 False。"""

    def merge_with(self, other: "AgentConfig | dict | None") -> "AgentConfig":
        """深度合并另一份配置，生成一个新的覆盖实例"""

        if not other:
            return model_copy(self, deep=True)

        update_dict = {}
        if isinstance(other, dict):
            other_dict = {k: v for k, v in other.items() if v is not None}
        else:
            fields_set = getattr(
                other, "model_fields_set", getattr(other, "__fields_set__", set())
            )
            other_dict = {}
            for k in fields_set:
                val = getattr(other, k)
                if val is not None:
                    other_dict[k] = val

        for k, v in other_dict.items():
            if k in ("capabilities", "skills") and isinstance(v, list):
                base_list = getattr(self, k) or []
                update_dict[k] = base_list + v
            else:
                update_dict[k] = v

        return model_copy(self, update=update_dict, deep=True)


class AgentState(BaseModel):
    """大模型思考循环的有限状态机 (FSM) 流转状态"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    static_system_prompt: str | list[str] = ""
    """绝对不变的系统提示词（用于前缀缓存）"""
    dynamic_system_messages: list[LLMMessage] = Field(default_factory=list)
    """包含变量与实时状态的动态独立提示消息列表（绝对头部注入）"""
    tools: ToolCollection | None = None
    """当前轮次生效的、已完成鉴权和过滤的工具集合"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """供第三方开发者或生命周期钩子使用的自定义插槽，
    在单个 Agent FSM 循环中存取临时变量"""

    _origin_msg_len: int = PrivateAttr(default=0)
    """内部变量：初始进入循环时的消息历史长度 (用于增量保存记忆，防止被外部业务篡改)"""

    current_cycle: int = 0
    """当前思考循环的轮次索引"""
    current_request_messages: list[AgentMessage] = Field(default_factory=list)
    """当前即将发往大模型的实际请求消息"""
    current_request_extra: dict[str, Any] = Field(default_factory=dict)
    """当前请求附加的Extra控制参数"""
    current_response: ChatResponse | None = None
    """大模型最新返回的响应实体"""
    current_tool_calls: list[ToolCallPart] = Field(default_factory=list)
    """当前轮次被提取出准备执行的客户端工具调用"""
    current_tool_results: list[Any] = Field(default_factory=list)
    """当前轮次工具执行的结果或异常收集"""

    should_reset_cycle: bool = False
    """标记是否需要重置当前思考循环（例如由于外部干预插入了新消息）"""
    is_finished: bool = False
    """标记大模型循环是否彻底结束"""
    pending_result: AgentRunResult[Any] | None = None
    """单一数据源：等待返回的最终运行结果 (SSOT)"""
    usage: UsageInfo = Field(default_factory=UsageInfo)
    """累计的 Token 消耗"""
    token_drift: int = 0
    """动态 Token 校准偏移量 (真实 - 预估)"""


class AgentRunResources(BaseModel):
    """大模型执行过程中的全局静态资源与配置载体"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_context: RunContext
    """保留依赖注入(DI)与黑板引用的全局运行时上下文"""
    session_meta: SessionMetadata | None = None
    """隔离会话的元信息(Session ID, 命名空间, 权限等)"""
    memory_context: SessionMemoryContext | None = None
    """统一处理对话历史读写、压缩与清洗的会话记忆门面"""
    run_scoped_cap: CombinedCapability | None = None
    """聚合了 Agent/AgentTask/全局 的复合能力拦截器 (CombinedCapability)"""
    task_obj: AgentTask | None = None
    """(如有) 解析后的结构化数据任务契约"""
    toolkits: list[BaseToolkit] = Field(default_factory=list)
    """当前轮次生效的工具箱列表 (需要执行生命周期挂载)"""
    config: AgentConfig = Field(default_factory=AgentConfig)
    """Agent 全局与运行时的统一策略配置"""
    generation_config: GenerationConfig | None = None
    """大模型生成配置"""
    model_capabilities: ModelCapabilities | None = None
    """当前底层大模型的能力配置（合并了用户自定义覆盖）"""
