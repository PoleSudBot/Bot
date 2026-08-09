from nonebot.adapters.onebot.v11 import Bot
from pydantic import BaseModel, Field

from zhenxun.services.ai.core.exceptions import LLMException
from zhenxun.services.log import logger
from zhenxun.services.scheduler import ScheduleContext, scheduler_manager

from .. import base_config
from .core import SummaryException
from .scope import build_count_scope, build_preset_scope
from .summary_generation import messages_summary, send_summary


class SummaryTaskParams(BaseModel):
    """定时总结任务的参数模型"""

    least_message_count: int = Field(
        default_factory=lambda: base_config.get("SUMMARY_MAX_LENGTH", 1000),
        description="总结所需的最少消息数",
    )
    style: str | None = Field(default=None, description="总结的风格")
    model: str | None = Field(default=None, description="使用的AI模型")
    time_range_type: str | None = Field(
        default=None,
        description="时间范围类型: today 或 yesterday",
    )


@scheduler_manager.register(
    plugin_name="summary_group",
    params_model=SummaryTaskParams,
)
async def scheduled_summary_task(
    bot: Bot,
    context: ScheduleContext,
    params: SummaryTaskParams,
) -> None:
    """
    这是由 scheduler_manager 调度的、支持依赖注入的任务函数。
    它处理单个群组的总结任务。
    """
    group_id = context.group_id
    if not group_id:
        logger.warning(
            f"定时总结任务 (ID: {context.schedule_id}) 缺少 group_id，跳过执行。"
        )
        return

    task_id = f"summary_task_{group_id}"
    logger.info(f"开始执行定时总结任务 [{task_id}]", group_id=group_id)

    try:
        least_message_count = params.least_message_count
        style = params.style
        model = params.model
        time_range_type = params.time_range_type

        from .message_processing import get_group_messages

        min_len_required = base_config.get("SUMMARY_MIN_LENGTH", 50)

        # 时间范围模式不检查最少消息数，因为消息量由时间决定
        if not time_range_type and least_message_count < min_len_required:
            logger.warning(
                f"[{task_id}] 群 {group_id} 定时任务的"
                f"最少消息数 ({least_message_count}) "
                f"低于系统要求 ({min_len_required})"
                f"，跳过本次执行。"
            )
            return

        scope = (
            build_preset_scope(time_range_type)
            if time_range_type
            else build_count_scope(least_message_count)
        )

        result = await get_group_messages(
            bot,
            int(group_id),
            scope,
            use_db=base_config.get("USE_DB_HISTORY", False),
        )
        processed_messages = result.messages
        user_info_cache = result.user_info_cache
        warning_msg = result.warning_message

        if not processed_messages:
            logger.info(
                f"[{task_id}] 群 {group_id} 没有获取到消息"
                f"{'(时间范围: ' + time_range_type + ')' if time_range_type else ''}"
                f"，不生成总结。"
            )
            return

        if not time_range_type and len(processed_messages) < min_len_required:
            logger.info(
                f"[{task_id}] 群 {group_id} 消息数量不足 "
                f"({len(processed_messages)}/{min_len_required})"
                f"，不生成总结。"
            )
            return

        from nonebot_plugin_alconna.uniseg import Target

        msg_target = Target.group(group_id=int(group_id))

        # 如果有超出限制警告，先发送
        if warning_msg:
            from nonebot_plugin_alconna.uniseg import UniMessage

            await UniMessage.text(warning_msg).send(msg_target, bot)

        summary_result = await messages_summary(
            target=msg_target,
            messages=processed_messages,
            style=style,
            model_name=model,
        )

        await send_summary(
            bot,
            msg_target,
            summary_result.summary_text,
            user_info_cache,
            group_id=int(group_id),
            model_name=summary_result.resolved_model_name,
        )

    except (SummaryException, LLMException) as e:
        logger.error(
            f"[{task_id}] 执行定时总结失败: {e}",
            group_id=group_id,
            e=e,
        )
    except Exception as e:
        logger.error(
            f"[{task_id}] 执行定时总结时发生未知错误: {e}",
            group_id=group_id,
            e=e,
        )
