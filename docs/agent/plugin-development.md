# zhenxun/plugins Plugin Development

## Scope
- 本文仅适用于 `zhenxun/plugins/` 下的一方插件与真寻适配插件。
- `plugins/` 下第三方nonebot2插件默认不套用本文规范；第三方插件优先遵循原项目结构和用户意图。
- 如果改动本质上是共享框架能力、公共模型、配置系统或运行时基础设施，应优先放在 `zhenxun/`，而不是强行塞进单个插件。

## Placement Decision
- 放到 `zhenxun/`：多插件共享服务、公共模型、配置 / 权限 / 加载器 / 渲染 / LLM / 基础设施调整。
- 放到 `zhenxun/plugins/`：与真寻运行时强耦合的一方功能，或需要深度复用本仓库服务层、配置层、任务体系的插件。
- 放到 `plugins/`：明确是在维护 `plugins.txt` / `[tool.uv.sources]` 接入的第三方 NoneBot 插件。
- 拿不准时，优先在第一方代码中做兼容、封装或桥接，不要直接改 vendor。

## Plugin Skeleton
- 先跟随相邻插件的真实结构，不强制统一模板。
- 简单插件可以集中在 `__init__.py`；复杂插件可拆分为 `commands.py`、`config.py`、`model.py`、`services/`、`utils/`、`templates/` 等。
- 入口模块通常使用 `__plugin_meta__ = PluginMetadata(...)`，`extra` 使用 `PluginExtraData(...).dict()` 或同目录已有的当前写法。
- 新增插件必须写明初始 `PluginExtraData.version`；维护已有一方插件并准备提交时，必须同步递增该插件版本号，版本格式延续插件现有写法。
- 新插件必须在自身目录提供 `README.md`；已有插件新增明显用户功能、部署配置或外部依赖时，同步补充或更新该 README。
- 需要启动 / 关闭钩子、模板命名空间、后台任务时，优先参考 `zhenxun_plugin_quote`、`parse_bilibili` 等现有插件。
- 插件名、菜单分类、配置键名、帮助文本尽量延续仓库现有命名风格。

## Commands and User Help
- 面向用户的普通命令优先使用 `nonebot_plugin_alconna` 提供的 `Alconna`、`on_alconna` 与 `UniMessage`，以复用统一解析、跨平台消息和现有帮助生态；可参考 `mc_server`、`parse_bilibili` 或 `zhenxun_plugin_summary_group`。
- 被动消息监听、外部 bot/黑箱命令转发、协议事件或必须先于普通 matcher 拦截的兼容入口，才使用 `on_message`、自定义 `Rule` 等较底层机制，并在代码中说明原因。
- `PluginMetadata.usage` 是给最终用户阅读的“使用帮助”，会被内建 help 插件按 Markdown 内容渲染，也可能进入智能帮助上下文。内容应写可直接发送的命令、参数含义、权限限制和必要提示，不写开发实现说明。
- 指令少的插件应使用简短的命令列表和示例；只有命令、别名或模式确实较多时，才使用分节或 Markdown 表格组织内容。复杂帮助文案可参考 `pjsk` 的 `usage` 表达方式，但 `pjsk` 的网关 matcher 结构不作为普通 Alconna 插件模板。
- `README.md` 面向部署者与维护者，覆盖功能说明、配置、依赖、运维注意点与验证方式；`usage` 面向聊天中的普通用户。两者都应维护，不能只写其中一个替代另一个。

## Preferred Project Abstractions
- 配置与元数据：`PluginMetadata`、`PluginExtraData`、`RegisterConfig`、`Task` 以及现有 group config model。
- 日志：`from zhenxun.services.log import logger`
- 消息构建：`from zhenxun.utils.message import MessageUtils`
- 平台操作：`from zhenxun.utils.platform import PlatformUtils`
- 网络请求：`from zhenxun.utils.http_utils import AsyncHttpx`
- 数据模型：`from zhenxun.services.db_context import Model`
- 图片 / Markdown / 模板渲染：优先使用 `from zhenxun import ui` 的 `render`、`render_template`、`render_markdown` 等入口，复用 `zhenxun.services.renderer` 的主题、模板和截图能力。
- 调度与大模型：分别优先使用 `zhenxun.services.scheduler` 与 `zhenxun.services.ai` 的现有接口；AI 的详细用法见 [`ai-framework.md`](ai-framework.md)。
- 生命周期与优先级：`PriorityLifecycle`、driver hooks、现有初始化模式。
- 规则、依赖注入、渲染、缓存、LLM、调度等能力优先复用仓库已有封装，而不是重新下沉到底层库。

## UI and Theme Guidance
- 插件需要输出图片、Markdown 图片或 HTML/Jinja2 页面时，默认贴合当前使用的 PSB 主题，不另起一套强对比色、深色大屏或营销式视觉风格。
- 视觉语言可参考 `resources/themes/psb/pages/builtin/my_info/style.css` 与 `main.html`：粉白页面底、深粉主色、浅粉卡片/边框、柔和阴影、紧凑信息卡、图标 tile、进度条、统计块和浅色图表容器。
- 只参考色调、材质、信息层级和元素处理，不照抄 `my_info` 的双栏 dashboard 布局；不同插件应按自身内容选择列表、表格、卡片、图表或摘要结构。
- 页面宽度、间距、圆角和阴影应克制稳定，避免大面积渐变、装饰性背景、过度发光或与 PSB 粉白色调冲突的高饱和配色。
- 动态文本必须预留换行、截断或 `overflow-wrap` 处理；昵称、标题、数值、说明文字和表格列不能因为内容变长而互相覆盖。
- 图标优先使用现有主题风格的线性图标或同类 icon tile 表达，不为普通状态随手画复杂插图；图表色彩优先复用主粉、深粉和柔和辅助色。

## Tests and Verification
- 新增一方插件的插件内行为测试，默认放在 `zhenxun/plugins/<plugin>/tests/`，使实现、README 与专属回归入口保持在同一目录。
- 涉及 `zhenxun/` 共享服务、内建 hook、跨插件协作、外部桥接或全局运行时行为的测试，放在根级 `tests/`。
- 已存在于根级 `tests/` 的历史测试不因本规范主动迁移；在扩展同一历史链路时，可继续补在原套件附近，避免一次行为变更拆散回归覆盖。
- 优先执行最小定向验证，例如 `uv run pytest zhenxun/plugins/<plugin>/tests/` 或关联的根级测试文件；涉及渲染时同时检查模板渲染结果或既有渲染测试。
- 不牵涉具体功能语义的低风险调整不强制运行测试，例如数值微调、别名增删、帮助文案、README 或 UI 样式微调；交付时说明未运行测试及原因即可。

## Red Lines
- 不要硬编码可变配置；应优先通过 `RegisterConfig` 或现有配置层暴露。
- 新增网络逻辑优先使用 `AsyncHttpx` 或现有服务封装，不要随手新建长期使用的底层 HTTP client。
- 不要绕过 `MessageUtils` 或项目消息封装直接大面积构造底层消息，除非现有能力确实不支持。
- 不要使用标准 `logging` 替代 `zhenxun.services.log.logger`。
- 不要把第三方插件的实现风格原样照搬进 `zhenxun/plugins/`；优先贴合本仓库现有一方插件模式。
- 不要因为单个插件需求把通用逻辑复制到多个插件里；可复用逻辑应抽到 `zhenxun/`。

## New Plugin Checklist
- 先确认落点：这是核心能力、一方插件，还是第三方插件问题。
- 明确 `__plugin_meta__` 的名称、菜单分类、权限等级、配置项，以及面向用户可读的 `usage` 使用帮助。
- 更新已有一方插件时，提交前检查并递增 `PluginExtraData.version`，不要把功能改动和旧版本号一起提交。
- 为插件提供 `README.md`，写明功能、配置、依赖、操作说明与验证方式。
- 普通用户命令优先使用 Alconna；若需要底层 matcher，说明它解决的监听或兼容需求。
- 明确是否需要模型、缓存、定时任务、启动初始化、模板命名空间或外部服务凭据。
- 明确应复用的项目抽象：日志、消息、HTTP、数据库、规则、渲染、LLM、任务。
- 明确最小验证方式与测试落点：插件内行为默认使用插件目录下的测试，核心或跨模块链路使用根级 `tests/`。
