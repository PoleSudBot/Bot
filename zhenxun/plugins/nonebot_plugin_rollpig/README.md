# nonebot_plugin_rollpig

本目录是仓库内维护的一方版本，基于上游 `Felis2026/nonebot-plugin-rollpig` 做了本地账本、群内日报、猪王排行、PSB 面板和真寻配置层适配。

## 功能

- `今日小猪 / 今天是什么小猪 / jrxz`：每天抽取一次命运小猪，重复发送只查看当天结果；账本中的形态若暂时缺少对应资源会直接报错，不会随机替换。
- `我的猪圈 / 我的小猪 / 小猪图鉴 / 猪猪图鉴 / 完整图鉴`：生成全量展柜版猪圈图鉴，查看收集进度、等级与猪猪排名。
- `猪王争霸榜 / 猪猪总榜`：按图鉴数量排行，并用首次达到当前图鉴数的时间处理并列。
- `今日烤猪 / 烤群友 / 随机烤群友`：围绕当天小猪形态做烧烤、反噬、保护和日报事件记录；发起群友烧烤前必须先抽取今日小猪，随机选中无效目标时不消耗普通烧烤充能。
- `随机小猪 / 找猪`：从 PigHub 获取或搜索外部猪图。
- `同步小猪资源 / 刷新小猪资源`：superuser 手动刷新静态小猪图鉴资源。
- 猪圈日报在任务触发时固定统计日期，并逐群完成汇总与次日保护写入；单群异常不会阻断其他群，保护写入失败的群不会收到包含保护承诺的日报。

## 成长系统

- 每只猪都有累计抽到次数 `copies`，`1` 次为 `Lv.0`，之后每多一次提升一级，默认最高 `Lv.5`。
- 重复抽到已解锁小猪会增加 `duplicate_streak`，下一次未解锁小猪的抽取权重会随连续重复次数提高。
- 抽到新猪后 `duplicate_streak` 清零，并写入图鉴；同一天重复查看不会刷等级或连续重复次数。
- 旧账本迁移时只能确认“曾经拥有过”，因此历史图鉴统一回填为 `copies=1`，不会伪造历史重复次数。

## 配置

配置统一走真寻插件配置层，模块名为 `nonebot_plugin_rollpig`。

| Key | 默认值 | 说明 |
| --- | --- | --- |
| `AI_ENABLED` | `False` | 是否启用 AI 烤猪文案 |
| `LLM_MODEL_NAME` | `None` | 烤猪文案使用的 LLM 模型名 |
| `ROAST_COOLDOWN_HOURS` | `8.0` | 普通烤群友每次充能恢复时间 |
| `ROAST_CHARGE_MAX` | `2` | 普通烤群友最多可储存次数，设为 `1` 可恢复旧版单冷却 |
| `STORAGE_BACKEND` | `local` | 存储后端，支持 `local` / `cloud` |
| `CLOUD_API_URL` | `None` | cloud 存储服务地址 |
| `CLOUD_TOKEN` | `None` | cloud 存储鉴权 token |
| `CLOUD_TIMEOUT` | `3.0` | cloud 请求超时时间 |
| `CLOUD_STRICT_MODE` | `True` | cloud 是否启用严格模式 |
| `PROXY` | `None` | PigHub / cloud 外部请求代理 |
| `GROWTH_MAX_EXPERT_LEVEL` | `5` | EX 等级上限 |
| `GROWTH_PITY_WEIGHT_STEP` | `0.5` | 连续重复后未解锁小猪的单次权重加成 |
| `GROWTH_PITY_WEIGHT_CAP` | `4.0` | 连续重复后未解锁小猪的最大权重加成 |
| `RESOURCE_SYNC_ENABLED` | `True` | 是否启用静态小猪资源同步，默认下载上游公有资源包 |
| `RESOURCE_MANIFEST_URL` | `https://pig.felislab.cc/resources/rollpig/manifest.json` | 静态资源 manifest URL，设为空字符串可关闭公有同步 URL |
| `RESOURCE_SYNC_ON_STARTUP` | `True` | 启动后是否后台同步资源 |
| `RESOURCE_SYNC_INTERVAL_HOURS` | `24` | 定时资源同步间隔 |
| `RESOURCE_SYNC_TIMEOUT` | `10.0` | 资源同步请求超时时间 |
| `RESOURCE_MAX_FILE_SIZE` | `10485760` | 单个资源文件下载大小上限 |
| `PRIVATE_RESOURCE_MANIFEST_URL` | `https://pig.felislab.cc/resources/rollpig-pjsk/manifest.json` | 私有资源 overlay manifest URL，设为空字符串可关闭 |
| `PRIVATE_RESOURCE_TOKEN` | `None` | 私有资源 Bearer Token |
| `OFFICIAL_GIF_RESOURCE_ENABLED` | `True` | 是否同步并优先使用官方 GIF 动态小猪资源包 |
| `OFFICIAL_GIF_RESOURCE_MANIFEST_URL` | `https://pig.felislab.cc/resources/rollpig-gif/manifest.json` | 官方 GIF 资源 manifest URL |
| `PRIVATE_RESOURCE_MANIFESTS` | `[]` | 额外资源包列表；每项填写 `name`、`manifest_url` 与可选 `token`，后配置的包优先级更高 |
| `CATALOG_ENABLED` | `True` | 是否启用图片版小猪图鉴命令 |
| `CATALOG_CACHE_SECONDS` | `300` | 同一图鉴状态图片缓存秒数，设为 `0` 可关闭缓存 |
| `CATALOG_RENDER_TIMEOUT` | `8.0` | 单张小猪图鉴渲染超时时间 |
| `HTML_RENDER_CONCURRENCY` | `2` | rollpig HTML 图片渲染总并发预算 |
| `GIF_RENDER_CONCURRENCY` | `2` | Pillow GIF 卡片冷生成并发预算，运行时限制为 `1-2` |
| `CARD_DISK_CACHE_MAX_MB` | `64` | 固定小猪卡片磁盘 LRU 上限（MiB），运行时限制为 `0-64`，设为 `0` 可关闭 |

## 图片版图鉴

`我的猪圈` 与 `小猪图鉴` 系列别名会生成同一张全量展柜图鉴；所有小猪按当前运行资源顺序固定展示，已获得小猪亮相，未解锁小猪以锁定占位显示。

- 图片命令只支持本地账册模式，不新增云中心接口。
- 图鉴卡片会展示缩略图、名称、`Lv.`、累计次数，以及最近 7 个业务日获得的新猪贴纸。
- 等级使用低饱和颜色和字重区分：低等级偏灰，高等级偏紫/红紫；不再单独展示“最高等级”或分页信息。
- 顶部会展示收集进度、黑白进度条、当前群猪猪排名和本地总排名。
- 缩略图从当前 active/private/builtin 资源快照读取并缓存到 localstore cache；命令执行时不会临时远程拉图，缺图会显示占位。
- 渲染结果使用同状态 TTL 缓存和同 key 合流，避免多人同时触发时重复占用 Chromium。
- 普通小猪卡片使用真寻 `BuildImage` 与 Pillow 绘制；GIF 小猪逐帧合成并立即量化，异常时回退旧 HTML 静态卡片。
- GIF 最多输出 60 帧；长动画按完整周期均匀采样，并把被合并帧的规范化时长累计到代表帧，保持总周期语义。
- GIF 冷生成最多双并发，相同内容请求会合流；解码后的源帧使用最多 8 条、24 MiB 的进程内 LRU。
- 今天、昨日等固定卡片会按内容指纹缓存到 localstore 的 `card_results/`，默认总量不超过 64 MiB，命中可跨进程重启复用。
- 烧烤卡片包含动态文案，不缓存最终成品；GIF 源帧仍可复用。资源、字体、NEW 图标或渲染版本变化会使旧成品自然失效。

## 资源同步

资源同步只处理静态资源，不接入上游 rollpig 云端账本，也不会读写本地 `pig_data.json`。

- manifest 指向一组静态文件：`pig.json`、可选 `pig_rules.json`、以及小猪图片。
- 同步成功前写入 localstore 缓存目录，完整校验后再替换 active 快照；失败会继续使用旧缓存或内置元数据。
- 启动同步、定时同步和手动同步共用同一把同步锁；手动同步会等待当前任务，后台同步遇到已有任务时直接跳过。
- Bot 关闭时会取消并等待后台资源同步任务，避免退出过程中仍在替换缓存目录。
- `pig.json` 按 ID 合并：远端新增或更新的 ID 覆盖本地旧条目，远端缺失的旧 ID 会保留，避免历史本地账本引用失效。
- `pig_rules.json` 按规则类型取并集，用于继续识别人类、熟食、吃掉、卖掉等特殊形态。
- 仓库不再携带小猪图片，生产环境部署后会自动下载到 localstore 的 `resources/active/images/`。
- 小猪图片优先从私有 overlay 缓存读取，其次读取公有 active 缓存；首次同步完成前可能暂时无图。
- 私有 overlay 默认指向上游 PJSK 包；启用资源同步后会缓存到 localstore 的 `resources/private_active/`，优先级高于公有资源。
- 资源覆盖顺序为内置、公有、官方 GIF、旧私有包、`PRIVATE_RESOURCE_MANIFESTS`；同一包内优先使用 GIF，图片版图鉴固定读取 GIF 首帧。
- 额外资源包分别缓存到 `resources/private_overlays/<name>/`，单包同步失败不会阻断其它包更新。
- 私有 `pig.json` 默认只追加新 ID；如需覆盖公有小猪文案，必须通过 `pig_overrides.json` 显式声明。
- 本地特色的 `new.png` 贴纸位于 `resource/assets/new.png`，不属于小猪图片同步范围。

额外资源包配置示例：

```json
[
  {
    "name": "custom-pack",
    "manifest_url": "https://example.com/rollpig/manifest.json",
    "token": "optional-bearer-token"
  }
]
```

manifest 示例：

```json
{
  "resource_version": "2026-06-21",
  "pig_json": {"path": "pig.json", "sha256": "..."},
  "optional_files": {
    "pig_rules": {"path": "pig_rules.json", "sha256": "..."}
  },
  "images": [
    {"path": "images/pig.png", "filename": "pig.png", "sha256": "..."}
  ]
}
```

私有 overlay manifest 示例：

```json
{
  "resource_version": "private-2026-06-21",
  "overlay": true,
  "pig_json": {"path": "pig.json", "sha256": "..."},
  "optional_files": {
    "pig_rules": {"path": "pig_rules.json", "sha256": "..."},
    "pig_overrides": {"path": "pig_overrides.json", "sha256": "..."}
  },
  "images": [
    {"path": "images/mining-pig.png", "filename": "mining-pig.png", "sha256": "..."}
  ]
}
```

## 数据

本地模式使用 `nonebot_plugin_localstore` 下的 `pig_data.json`：

- `history`：每日抽猪结果，保留短期历史。
- `group_rolls`：群维度的今日显形记录，用于日报和随机烤群友候选。
- `collection`：永久图鉴。
- `collection_progress`：达到当前图鉴数量的时间，用于排行并列排序。
- `pig_progress`：每只猪的累计抽到次数和首次获得时间。
- `draw_state`：连续重复次数。
- `usage`：普通烤群友充能状态，兼容旧版单时间戳冷却记录。
- `daily_events`：烧烤事件，用于日报。
- `protected`：群维度保护名单。

图片版图鉴的 `recent_rolls`、连续打卡和近 7 天被烤次数都从这些本地字段只读聚合，不会补写历史、刷新 copies 或触发账册落盘。

账册落盘使用临时文件原子替换，并在每次成功写入前保留滚动备份：

- `pig_data.json.bak`：最近一次成功写入前的主文件。
- `pig_data.json.bak.1` / `pig_data.json.bak.2`：更早的备份。
- `pig_data.json.broken.<timestamp>.bak`：启动时发现主文件损坏后保留的现场文件。

如果主文件损坏，插件会先尝试从备份恢复；无可用备份时进入写保护模式，避免用空账册覆盖旧数据。此时需要人工修复 `pig_data.json` 或恢复备份后重启。

## PigHub

`随机小猪 / 找猪` 会优先使用 PigHub 新接口 `/api/images?sort=2`，失败时回退旧接口 `/api/all-images`。接口返回的 `data/images` 与 `image_url/thumbnail` 都会归一化，图片 URL 支持完整 URL、`/images/...`、`/data/...` 和裸文件名；外部请求继续使用 `PROXY` 配置。

## 验证

优先运行最小验证：

```bash
PYTHONPYCACHEPREFIX=/tmp/rollpig-pycache uv run --no-sync pytest -s tests/test_rollpig_ranking.py
PYTHONPYCACHEPREFIX=/tmp/rollpig-pycache uv run --no-sync python -m py_compile zhenxun/plugins/nonebot_plugin_rollpig/*.py zhenxun/plugins/nonebot_plugin_rollpig/store/*.py
git diff --check -- zhenxun/plugins/nonebot_plugin_rollpig tests/test_rollpig_ranking.py
```
