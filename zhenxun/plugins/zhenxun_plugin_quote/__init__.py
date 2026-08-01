from nonebot.plugin import PluginMetadata
import asyncio

from zhenxun.utils.manager.priority_manager import PriorityLifecycle
from pathlib import Path
from zhenxun.configs.utils import PluginExtraData, RegisterConfig
from zhenxun.services.log import logger
from .command.manage_commands import quote_manage_cmd  # noqa: F401
from .command.query_commands import (  # noqa: F401
    quote_stats_cmd,
    record_pool,
)
from .command.upload_commands import (  # noqa: F401
    generate_quote_cmd,
    make_record_cmd,
    save_img_cmd,
)
from .config import ensure_quote_path
from zhenxun.services import renderer_service
from .services.quote_service import QuoteService

ensure_quote_path()

QUOTE_ASSETS_PATH = Path(__file__).parent / "templates"
_tag_migration_task: asyncio.Task[int] | None = None


def _log_tag_migration_result(task: asyncio.Task[int]) -> None:
    try:
        migrated = task.result()
        logger.info(f"统一 tag 后台迁移完成，共迁移 {migrated} 条。", "群聊语录")
    except asyncio.CancelledError:
        logger.info("统一 tag 后台迁移已取消。", "群聊语录")
    except Exception as e:
        logger.error(f"统一 tag 后台迁移失败: {e}", "群聊语录", e=e)


@PriorityLifecycle.on_startup(priority=9)
async def _init_quote_services():
    """
    初始化语录插件服务。
    必须在 RendererService (priority=10) 之前注册模板命名空间。
    """
    try:
        renderer_service.register_template_namespace("@quote", QUOTE_ASSETS_PATH)
        logger.info("语录插件模板命名空间 '@quote' 注册成功。", "群聊语录")
    except Exception as e:
        logger.error(f"注册语录插件模板命名空间失败: {e}", "群聊语录", e=e)

    global _tag_migration_task
    if _tag_migration_task is None or _tag_migration_task.done():
        _tag_migration_task = asyncio.create_task(QuoteService.migrate_legacy_tags())
        _tag_migration_task.add_done_callback(_log_tag_migration_result)

__plugin_meta__ = PluginMetadata(
    name="群聊语录",
    description="一款QQ群语录库——支持上传聊天截图为语录，随机投放语录，关键词搜索语录精准投放",
    usage=(
        "### 核心功能\n"
        "`语录 [关键词/@用户] [-n 数量]` - 随机发送语录，可按关键词或用户筛选，默认 1 张，最多 10 张\n"
        "`语录 [关键词/@用户] -序号` - 按入库倒序发送指定语录，"
        "例如 `语录 -1` 为最新一条\n"
        "示例：`语录`、`语录 南极`、`语录南极`、`语录 @南极`、"
        "`语录 -n 5 1969`、`语录 1969 五连`、`语录 x5`、"
        "`语录 *5`、`语录 xxx -3`\n\n"
        "`上传语录 [图片] [tag/@用户 ...]` - 上传图片作为语录，也可回复图片后发送 `上传语录`\n"
        "回复合并转发后发送 `上传语录`，会批量上传其中所有顶层图片\n"
        "示例：回复图片或合并转发后发送 `上传语录 南极`、`上传语录 [图片] 南极 @南极`\n\n"
        "`记录语录 [tag/@用户 ...]` - 回复文本消息后生成语录图片并保存，`记录` 为兼容别名\n"
        "示例：回复消息后发送 `记录语录 南极`、`记录语录`、`记录 @南极 经典`\n\n"
        "`入典 [tag/@用户 ...]` - 固定使用 classic 风格记录语录，需回复消息\n"
        "示例：回复消息后发送 `入典 南极`、`入典`、`入典 @南极 经典`\n\n"
        "`生成 [-s 主题ID] [-n 数量] [-o|--only]` - 预览语录图片但不入库\n"
        "`记录语录 [-s 主题ID] [-n 数量] [-o|--only]` - 记录时使用指定主题或连续消息，`记录` 为兼容别名\n"
        "`入典 [-n 数量] [-o|--only]` - classic 风格预设命令，参数行为与 `记录语录 -s classic` 一致\n"
        "`-o/--only` 与 `-n` 连用时，只查找并记录被回复用户的消息\n\n"
        "### 主题与预览\n"
        "`语录主题` - 查看所有可用主题，超级用户可在群聊或私聊使用\n"
        "`语录主题 主题名` - 切换全局默认主题，仅超级用户可用\n"
        "`quote theme` - `语录主题` 的英文写法\n\n"
        "### 统计功能\n"
        "`语录统计 [热门/高产上传/高产被录] [数量]` - 查看群内语录统计\n"
        "示例：`语录统计 热门`、`语录统计 高产上传 5`、`quote stats 热门 10`\n\n"
        "### 管理功能\n"
        "`删除` - 回复 Bot 发出的单图、多图或合并转发语录后删除，需为上传者或满足删除权限\n"
        "`删除语录` / `del` - 回复语录图片或合并转发后删除对应语录，需满足删除权限\n\n"
        "`tag` - 回复语录图片后查看统一 tag（多图按 1.、2.、3. 编号）\n"
        "`tag all` / `alltag` - `tag` 的兼容写法，查看相同的全部 tag\n"
        "`tag add` / `tag del` - 为回复中的全部语录图片添加或删除 tag\n"
        "`addtag` / `deltag` / `tagadd` / `tagdel` - `tag add/del` 的等价别名\n\n"
        "`taglink tag1 tag2 ...` - 创建或扩展当前群互通组，别名：`tag互通`、`标签互通`\n"
        "`tagunlink tag` - 将一个成员移出互通组，别名：`取消互通`\n"
        "`taggroup tag` - 查看成员所在互通组，别名：`tag组`、`tag别名`、`互通组`\n\n"
        "`语录管理 keyword 词1 ...` - 删除包含任一关键词的语录，仅超级用户可用\n"
        "`语录管理 clear --uploader @用户/QQ号` - 清空指定上传者的语录，仅超级用户可用\n"
        "`语录管理 clear --quoted @用户/QQ号` - 清空指定被记录用户的语录，仅超级用户可用\n"
        "`语录管理 cleanup` - 清理已退群用户的相关语录，仅超级用户可用\n"
        "`语录管理 检查` - 只读检查当前群的数据库记录和图片状态，仅超级用户可用\n"
        "`语录管理 检查 群号` - 只读检查指定群，仅超级用户可用\n"
        "`语录管理 检查 全部` - 只读检查全部记录和孤儿图片，仅超级用户可用\n\n"
        "### 说明\n"
        "`语录南极`、`记录语录aaa bbb`、`记录aaa bbb`、`入典aaa bbb`、`上传语录xxx` 支持命令头与正文不加空格\n"
        "`语录五连`、`语录 3连`、`语录 x5`、`语录 *5` "
        "会被当成数量请求；如果要查关键词“五连/三连”，请使用 `语录 -n 1 五连`\n"
        "`语录 -1`、`语录 xxx -3` 会按入库倒序取指定语录，不会随机补发\n"
        "`语录` 一次最多获取 10 张；1-5 张会合并成一条消息，6-10 张会使用合并转发\n"
        "`tag` 系列只在回复语录图或合并转发时生效，非回复场景会静默让路；反馈只发送文字，不重复原图\n"
        "`删除` 只在回复语录图或合并转发时有效，不会接管普通聊天里的“删除”\n"
        "OCR/AI、记录文本和用户添加的 tag 完全同权，可直接查询、添加和删除；"
        "使用 `语录 #tag` 可进行严格 tag 查询"
    ),
    type="application",
    homepage="https://github.com/webjoin111/zhenxun_plugin_quote",
    supported_adapters={"~onebot.v11"},
    extra=PluginExtraData(
        author="webjoin111",
        version="v1.4.1",
        admin_level=0,
        configs=[
            RegisterConfig(
                module="quote",
                key="TEXT_RECOGNITION_PRIORITY",
                value="llm",
                help="普通上传文字识别优先级，可选值: llm, paddleocr_api",
                default_value="llm",
            ),
            RegisterConfig(
                module="quote",
                key="BATCH_TEXT_RECOGNITION_PRIORITY",
                value="paddleocr_api",
                help=(
                    "合并转发批量上传文字识别优先级，可选值: "
                    "llm, paddleocr_api"
                ),
                default_value="paddleocr_api",
            ),
            RegisterConfig(
                module="quote",
                key="PADDLEOCR_API_TOKEN",
                value="",
                help="PaddleOCR 官方 API Token；留空时跳过 API。",
                default_value="",
            ),
            RegisterConfig(
                module="quote",
                key="PADDLEOCR_API_JOB_URL",
                value="https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
                help="PaddleOCR 官方异步任务 API 地址。",
                default_value="https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
            ),
            RegisterConfig(
                module="quote",
                key="PADDLEOCR_API_MODEL",
                value="PaddleOCR-VL-1.6",
                help="PaddleOCR API 使用的模型名称。",
                default_value="PaddleOCR-VL-1.6",
            ),
            RegisterConfig(
                module="quote",
                key="PADDLEOCR_API_TIMEOUT_SECONDS",
                value=180,
                help="单张图片等待 PaddleOCR API 完成的最长时间（秒）。",
                default_value=180,
            ),
            RegisterConfig(
                module="quote",
                key="AI_ENABLED",
                value=True,
                help="是否启用AI识别功能（启用后会先尝试使用AI识别，失败则降级使用OCR）",
                default_value=True,
            ),
            RegisterConfig(
                module="quote",
                key="OCR_AI_MODEL",
                value="Gemini/gemini-2.5-flash-lite-preview-06-17",
                help="用于OCR的、支持视觉功能的AI模型全名 (格式: Provider/ModelName)",
                default_value="Gemini/gemini-2.5-flash-lite-preview-06-17",
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_PATH",
                value="",
                help="语录图片保存路径（留空则使用默认路径：DATA_PATH/quote/images）",
                default_value="",
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_MAX_IMAGE_SIZE_MB",
                value=15,
                help="上传语录允许的最大图片大小（MB），小于等于 0 时关闭限制。",
                default_value=15,
            ),
            RegisterConfig(
                module="quote",
                key="THEME",
                value="qq-native",
                help="生成语录卡片时默认使用的主题/皮肤名称。",
                default_value="qq-native",
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_TEXT_ONLY_THEME",
                value="",
                help="仅用于纯文本（可包含@）的单条语录的主题。留空则默认使用 THEME。",
                default_value="",
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_ALLOW_SELF_RECORD",
                value=False,
                help="是否允许用户使用「记录」命令记录自己的消息。",
                default_value=False,
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_ALLOW_BOT_RECORD",
                value=False,
                help="是否允许记录Bot本身发送的消息。",
                default_value=False,
            ),
            RegisterConfig(
                module="quote",
                key="DELETE_ADMIN_LEVEL",
                value=5,
                help="设置使用「删除」命令所需的权限等级。默认值为5，允许群管理员使用。",
                default_value=5,
            ),
            RegisterConfig(
                module="quote",
                key="TAG_ALIAS_ADMIN_LEVEL",
                value=5,
                help="设置管理 tag 互通组所需的权限等级。默认值为5，允许群管理员使用。",
                default_value=5,
            ),
            RegisterConfig(
                module="quote",
                key="QUOTE_UPLOAD_PENDING_EMOJI_ID",
                value="10024",
                help="上传语录进入 OCR/AI 处理中时使用的贴表情 ID，留空则关闭该提示。",
                default_value="10024",
            ),
        ],
    ).dict(),
)
