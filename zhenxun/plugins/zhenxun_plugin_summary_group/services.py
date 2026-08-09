from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiofiles
from nonebot.adapters.onebot.v11 import Bot
from nonebot_plugin_alconna.uniseg import MsgTarget, UniMessage
from pydantic import BaseModel, Field

from zhenxun.configs.path_config import DATA_PATH
from zhenxun.services.ai.core.exceptions import LLMException
from zhenxun.services.log import logger

from . import base_config
from .utils.core import SummaryException
from .utils.message_processing import (
    MessageFetchResult,
    build_export_text,
    get_group_messages,
)
from .utils.scope import SummaryScope
from .utils.summary_generation import (
    SummaryGenerationResult,
    messages_summary,
    send_summary,
)


class SummaryParameters(BaseModel):
    """封装单次总结任务所需的所有参数"""

    bot: Bot
    target_group_id: int
    scope: SummaryScope
    style: str | None = None
    content_filter: str | None = None
    target_user_ids: set[str] = Field(default_factory=set)
    response_target: MsgTarget
    reply_to_message_id: str | None = None

    class Config:
        arbitrary_types_allowed = True


class ExportParameters(BaseModel):
    """封装聊天记录导出任务所需的所有参数"""

    bot: Bot
    target_group_id: int
    scope: SummaryScope
    response_target: MsgTarget

    class Config:
        arbitrary_types_allowed = True


class SummaryService:
    """封装群聊总结的核心业务逻辑"""

    def __init__(self, params: SummaryParameters):
        self.params = params
        self.logger = logger
        self.fetch_result: MessageFetchResult | None = None
        self.summary_result: SummaryGenerationResult | None = None

    async def _fetch_and_process_messages(self) -> None:
        self.logger.debug(
            f"Service: 开始获取群 {self.params.target_group_id} 的消息: "
            f"scope={self.params.scope.raw}",
            command="总结服务",
        )
        use_db = base_config.get("USE_DB_HISTORY", False)

        self.fetch_result = await get_group_messages(
            self.params.bot,
            self.params.target_group_id,
            self.params.scope,
            use_db=use_db,
            target_user_ids=self.params.target_user_ids,
        )

        if not self.fetch_result.messages:
            if self.params.target_user_ids:
                raise SummaryException(
                    f"在群聊 {self.params.target_group_id} "
                    "中未能获取到指定用户的有效聊天记录。"
                )
            if self.params.scope.is_time_based:
                raise SummaryException(
                    f"在群聊 {self.params.target_group_id} 中未能获取到"
                    f"“{self.params.scope.label}”范围内的聊天记录。"
                )
            raise SummaryException(
                f"未能获取到群聊 {self.params.target_group_id} 的聊天记录。"
            )

        self.logger.debug(
            "Service: 成功获取并处理消息，"
            f"得到 {len(self.fetch_result.messages)} 条记录",
            command="总结服务",
        )

    async def _generate_summary(self) -> SummaryGenerationResult:
        if not self.fetch_result:
            raise SummaryException("总结前缺少历史消息结果。")

        target_user_names = []
        if self.params.target_user_ids:
            target_user_names = [
                self.fetch_result.user_info_cache.get(uid, f"用户{uid[-4:]}")
                for uid in self.params.target_user_ids
            ]

        summary_content_target = MsgTarget(str(self.params.target_group_id))

        self.summary_result = await messages_summary(
            target=summary_content_target,
            messages=self.fetch_result.messages,
            content=self.params.content_filter,
            target_user_names=target_user_names or None,
            style=self.params.style,
        )
        self.logger.debug(
            f"Service: 群 {self.params.target_group_id} 总结生成成功，"
            f"长度: {len(self.summary_result.summary_text)} 字符",
            command="总结服务",
        )
        return self.summary_result

    async def _send_summary(self) -> bool:
        if not self.fetch_result or not self.summary_result:
            raise SummaryException("发送总结前缺少必要的中间结果。")

        if self.fetch_result.warning_message:
            await UniMessage.text(self.fetch_result.warning_message).send(
                self.params.response_target
            )

        return await send_summary(
            self.params.bot,
            self.params.response_target,
            self.summary_result.summary_text,
            self.fetch_result.user_info_cache,
            group_id=self.params.target_group_id,
            model_name=self.summary_result.resolved_model_name,
            reply_to_message_id=self.params.reply_to_message_id,
        )

    async def execute(self) -> bool:
        try:
            await self._fetch_and_process_messages()
            await self._generate_summary()
            return await self._send_summary()

        except SummaryException as e:
            self.logger.warning(
                f"总结服务执行失败 (业务异常): {e}",
                command="总结服务",
                e=e,
            )
            await UniMessage.text(e.user_friendly_message).send(
                self.params.response_target,
                self.params.bot,
                reply_to=self.params.reply_to_message_id or False,
            )
            return False
        except LLMException as e:
            self.logger.error(
                f"总结服务执行失败 (LLM异常): {e}",
                command="总结服务",
                e=e,
            )
            await UniMessage.text(e.user_friendly_message).send(
                self.params.response_target,
                self.params.bot,
                reply_to=self.params.reply_to_message_id or False,
            )
            return False
        except Exception as e:
            self.logger.error(
                f"总结服务执行时发生未知错误: {e}",
                command="总结服务",
                e=e,
            )
            await UniMessage.text(
                "处理总结时发生了一个未知的内部错误，请联系管理员。"
            ).send(
                self.params.response_target,
                self.params.bot,
                reply_to=self.params.reply_to_message_id or False,
            )
            return False


class ExportService:
    """导出聊天记录到本地文本文件。"""

    def __init__(self, params: ExportParameters):
        self.params = params
        self.logger = logger
        self.fetch_result: MessageFetchResult | None = None

    async def _fetch_and_process_messages(self) -> None:
        self.fetch_result = await get_group_messages(
            self.params.bot,
            self.params.target_group_id,
            self.params.scope,
            use_db=base_config.get("USE_DB_HISTORY", False),
        )
        if not self.fetch_result.messages:
            raise SummaryException(
                f"在群聊 {self.params.target_group_id} 中未能获取到可导出的聊天记录。"
            )

    async def _save_export_file(self) -> Path:
        if not self.fetch_result:
            raise SummaryException("导出前缺少历史消息结果。")

        save_dir = DATA_PATH / "summary_group"
        save_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"export_{self.params.target_group_id}_{timestamp}.txt"
        filepath = save_dir / filename
        export_text = build_export_text(
            messages=self.fetch_result.messages,
            group_id=self.params.target_group_id,
            scope=self.params.scope,
            source=self.fetch_result.source,
            warning_message=self.fetch_result.warning_message,
        )
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(export_text)
        return filepath

    async def execute(self) -> bool:
        try:
            await self._fetch_and_process_messages()
            filepath = await self._save_export_file()
            if self.fetch_result and self.fetch_result.warning_message:
                await UniMessage.text(self.fetch_result.warning_message).send(
                    self.params.response_target
                )
            await UniMessage.text(f"聊天记录已导出到：{filepath}").send(
                self.params.response_target
            )
            return True
        except SummaryException as e:
            self.logger.warning(
                f"导出记录执行失败 (业务异常): {e}",
                command="导出记录",
                e=e,
            )
            await UniMessage.text(e.user_friendly_message).send(
                self.params.response_target
            )
            return False
        except Exception as e:
            self.logger.error(
                f"导出记录执行时发生未知错误: {e}",
                command="导出记录",
                e=e,
            )
            await UniMessage.text(
                "导出聊天记录时发生了一个未知的内部错误，请联系管理员。"
            ).send(self.params.response_target)
            return False
