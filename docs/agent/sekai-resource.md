# SekaiResource Resource Service

## Scope
- `zhenxun/services/sekai_resource/` 是 Project Sekai 相关主数据与静态资源的共享服务层，当前主要被 `moesekai` 复用。
- 新功能需要歌曲、活动、卡牌、角色、称号、表情等 MasterData，或需要卡面、曲绘、短音频、活动图、表情图等资产时，优先接入本服务。
- 新代码直接从 `zhenxun.services.sekai_resource` 导入公共入口；`zhenxun.plugins.moesekai.providers.*` 只保留旧调用方兼容语义，不作为新接入路径。
- 本服务只负责资源获取、缓存、数据源选择与本地状态，不负责聊天命令、权限、渲染样式或业务规则。

## Public Entrypoints
推荐使用包级导出，避免绑定内部文件结构：

```python
from zhenxun.services.sekai_resource import (
    AssetFetchRequest,
    alias_provider,
    asset_provider,
    b30_constants_provider,
    master_data_provider,
    profile_static_provider,
    sync_music_aliases,
)
```

常用 MasterData 入口：

```python
events = await master_data_provider.get_events("jp")
cards = await master_data_provider.get_cards("jp")
musics = await master_data_provider.get_musics("jp")
music_difficulties = await master_data_provider.get_music_difficulties("jp")
current = await master_data_provider.get_current_event("jp", fallback="next_first")
```

常用静态资源入口：

```python
jacket = await asset_provider.get_music_jacket("jp", music["assetbundleName"])
jackets = await asset_provider.get_music_jackets(
    "jp",
    [music["assetbundleName"] for music in musics],
)
card = await asset_provider.get_card_image(
    "jp",
    card["assetbundleName"],
    after_training=True,
)
local_path = asset_provider.get_card_image_local_path(
    "jp",
    card["assetbundleName"],
    after_training=True,
)
```

需要刷新主数据时使用：

```python
result = await master_data_provider.update_region("jp", force=True)
results = await master_data_provider.update_all(force=True)
changed_results = await master_data_provider.probe_next_updates()
```

`RegionUpdateResult.to_message()` 可直接生成面向管理员的更新结果文本；业务代码需要更细粒度处理时读取 `updated`、`download_success`、`error`、`changed_datasets` 与 `added_records`。

## MasterData
支持区服固定为：

| 值 | 含义 |
| --- | --- |
| `cn` | 国服 |
| `jp` | 日服 |
| `tw` | 台服 |

内置数据集固定为：

| Dataset | 读取入口 |
| --- | --- |
| `events` | `get_events(server)` |
| `virtualLives` | `get_virtual_lives(server)` |
| `gachas` | `get_gachas(server)` |
| `cards` | `get_cards(server)` |
| `gameCharacters` | `get_game_characters(server)` |
| `honors` | `get_honors(server)` |
| `honorGroups` | `get_honor_groups(server)` |
| `stamps` | `get_stamps(server)` |
| `musics` | `get_musics(server)` |
| `musicDifficulties` | `get_music_difficulties(server)` |

读取行为：
- `get_dataset(server, dataset)` 会优先读内存缓存；本地文件不存在时会尝试自动更新对应区服。
- 下载失败、文件不存在或 JSON 损坏时，读取入口可能返回空列表；调用方必须按“资源暂不可用”处理。
- 主数据文件落在 `data/sekai_resource/master/<server>/<dataset>.json`，调用方不要直接拼这个路径读写，除非是在维护 `sekai_resource` 自身。
- 更新成功后会清理对应区服的内存缓存，后续读取会重新加载本地文件。

数据源选择：
- 默认按 `8823`、`haruki`、`sekai-viewer` 家族顺序选择可用源。
- 自动更新按数据源 version 判定是否有新版本；revision 仍会记录，用于排查与展示。
- 候选源版本低于本地已落地版本时不会自动回退。
- `force=True` 会包含 lazy 源，并在版本接口不可用时尝试可下载源兜底。

## Static Assets
资产读取以 `server + kind + assetbundle` 为核心。业务代码通常不需要直接传 `kind`，优先使用语义化方法：

| 资源 | 推荐入口 |
| --- | --- |
| 卡面 | `get_card_image(server, assetbundle, after_training=False, thumbnail=False)` |
| 卡面本地路径 | `get_card_image_local_path(server, assetbundle, after_training=False, thumbnail=False)` |
| 卡牌 cutout | `get_card_cutout_image(server, assetbundle, after_training=False)` |
| 卡牌 cutout 本地路径 | `get_card_cutout_image_local_path(server, assetbundle, after_training=False)` |
| 曲绘 | `get_music_jacket(server, assetbundle)` |
| 批量曲绘 | `get_music_jackets(server, assetbundles, timeout=20, concurrency=None)` |
| 通用批量 bytes | `fetch_many_contents(list[AssetFetchRequest], concurrency=None)` |
| 短音频 | `get_music_audio(server, assetbundle)` |
| 活动 banner | `get_event_banner(server, assetbundle)` |
| 虚拟 Live banner | `get_virtual_live_banner(server, assetbundle)` |
| 表情 | `get_stamp_image(server, assetbundle)` |
| 表情本地路径 | `get_stamp_image_local_path(server, assetbundle)` |
| 角色 cutout | `get_character_image(server, assetbundle)` |
| 个人档案静态素材 | `profile_static_provider.ensure_local_path([...])` |
| 批量个人档案静态素材 | `profile_static_provider.ensure_many_local_paths([...])` |
| 个人档案 credits | `profile_static_provider.get_credits()` |

资产来源按 `SEKAI_RESOURCE_ASSET_SOURCE_ORDER` 配置尝试，默认顺序为 `uni`、`haruki-main`、`haruki-jp-dedicated`、`legacy-viewer`。`haruki-jp-dedicated` 只会用于日服。

读取行为：
- 先查本地镜像缓存，命中时直接返回 bytes 或本地文件路径。
- 未命中时会按候选源顺序启动有限并发竞速，任一候选源成功后写入 `data/sekai_resource/assets/` 并更新缓存索引。
- 同一进程内同时请求同一资源时会复用同一个下载任务，避免批量场景重复打同一张图。
- 多张资源同时下载时优先使用批量入口；批量入口会按配置或调用方传入的 `concurrency` 做限流。
- 404 会进入负缓存，避免短时间内重复请求确定不存在的资源。
- 所有资源读取都可能返回 `None`；调用方不要假设图片或音频一定存在。
- 需要给渲染器传文件路径时优先用 `*_local_path`；没有本地路径时再考虑 bytes/base64 或占位图。
- 个人档案小人、服装 icon、honor SVG、credits.json 统一落在 `profile_static_provider`，不要再从 MoeSekai 插件内直接访问 GitHub raw 或维护私有下载循环。

卡牌特训辅助：
- `asset_provider.has_after_training(card)` 判断卡牌是否存在特训后资源。
- `asset_provider.only_has_after_training(card)` 判断卡牌是否只有特训后状态。
- 这两个方法依赖 MasterData 中的 rarity、特训消耗和初始特训状态字段，适合渲染前选择卡面版本。

## Concurrency Tuning
静态资源并发分为两层，含义不同：

| 配置 | 作用 |
| --- | --- |
| `SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY` | 同时获取多张不同资源时的总并发，例如 B30 曲绘或新卡提醒卡面 |
| `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY` | 单张资源未命中缓存时，同时尝试多少个候选源站 |
| `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL` | 是否让单张资源直接请求全部候选源 |

调优建议：
- B30、新卡提醒、十连卡面这类“同一业务流程要拿多张图”的场景，优先调高 `SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY`，或在调用 `fetch_many_contents()` / `get_music_jackets()` 时传入更小的局部 `concurrency`。
- 某个主源站经常卡住或超时时，可以适度调高 `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY`，让单张资源更快从备用源成功返回。
- `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL=true` 会让每个缓存未命中的资源同时打所有候选源，只有在明确接受额外源站请求量时再开启。
- 这些配置只影响缓存未命中的下载过程；本地缓存命中仍是直接读取，不会产生网络请求。

## Aliases
别名能力用于把用户输入的歌曲名、角色名、简称或 ID 解析成稳定的资源 ID。常用入口：

```python
from zhenxun.services.sekai_resource import alias_provider, sync_music_aliases


music = await alias_provider.resolve_music("消失", server="jp")
character = await alias_provider.resolve_character("miku", server="jp")
profile = await alias_provider.get_music_profile("74", server="jp")
await sync_music_aliases(force=False)
```

行为说明：
- `resolve_music()` / `resolve_character()` 会优先识别纯数字 ID，再匹配系统别名和 MasterData 官方名。
- 歌曲别名快照来自 MoeSekai-Hub，`sync_music_aliases()` 会按固定间隔复用本地快照，`force=True` 才强制刷新。
- 角色别名使用 MoeSekai 维护的本地 seed 文件；官方名仍来自 MasterData。
- 解析失败会返回 `None`；调用方需要给用户明确提示，不要把失败当异常处理。
- `AliasResolveResult.matched_source` 可用于区分 `id`、`system`、`official`，需要展示匹配来源时再读取。

## B30 Constants
B30 定数表是共享能力，插件不要自己再维护一份 CSV 拉取和解析逻辑。常用入口：

```python
from zhenxun.services.sekai_resource import b30_constants_provider


table = await b30_constants_provider.get_table()
constant = table.get(74, "master")
changed = await b30_constants_provider.refresh_if_needed(force=False)
```

行为说明：
- `get_table()` 会优先复用内存和本地 CSV 缓存；缓存过期或缺失时才访问远端。
- 远端刷新失败但本地已有缓存时，会继续使用旧缓存，避免 B30 查询被外部表格源拖垮。
- `ConstantsTable.get(music_id, difficulty)` 会规范化难度名，支持 `expert/ex`、`master/ma`、`append/apd` 等写法。
- `refresh_if_needed(force=True)` 适合管理员手动刷新或后台自动刷新；普通查询直接用 `get_table()`。

## Configuration And Storage
配置统一注册在 `sekai_resource` 模块名下：

| Key | 用途 |
| --- | --- |
| `SEKAI_RESOURCE_MASTER_SOURCE_ORDER` | MasterData 源家族优先级 |
| `SEKAI_RESOURCE_MASTER_SOURCES` | MasterData 数据源明细 |
| `SEKAI_RESOURCE_MASTER_CHECK_INTERVAL_SECONDS` | 自动检查间隔 |
| `SEKAI_RESOURCE_MASTER_CHECK_MODE` | 旧语义兼容项；当前统一按 version 判定 |
| `SEKAI_RESOURCE_GITHUB_TOKEN` | GitHub revision API token，留空匿名访问 |
| `SEKAI_RESOURCE_ASSET_SOURCE_ORDER` | 静态资源源站优先级 |
| `SEKAI_RESOURCE_ASSET_BATCH_FETCH_CONCURRENCY` | 批量静态资源下载并发数，默认 `12` |
| `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_CONCURRENCY` | 单个静态资源多源竞速并发数，默认 `4` |
| `SEKAI_RESOURCE_ASSET_SOURCE_FETCH_ALL` | 是否让单个静态资源同时请求全部候选源，默认 `false` |
| `SEKAI_RESOURCE_PROFILE_STATIC_ASSET_BASES` | 个人档案静态素材源站列表 |
| `SEKAI_RESOURCE_AUDIO_FORMAT_PRIORITY` | 短音频格式优先级，默认 `mp3`、`flac` |
| `SEKAI_RESOURCE_ASSET_MISS_CACHE_TTL_SECONDS` | 404 负缓存 TTL，0 表示不缓存缺失状态 |
| `SEKAI_RESOURCE_B30_CONSTANTS_URL` | B30 社区定数 CSV 地址 |
| `SEKAI_RESOURCE_B30_CONSTANTS_TIMEOUT_SECONDS` | B30 社区定数请求超时秒数，默认 `10.0` |
| `SEKAI_RESOURCE_B30_CONSTANTS_REFRESH_INTERVAL_SECONDS` | B30 社区定数自动检查间隔，默认 `86400` |

兼容规则：
- `get_settings()` 会先读 `SEKAI_RESOURCE_*` 新配置。
- 对仍保留旧键兼容的配置项，当新配置只是注册默认值、用户没有显式配置时，会回退读取旧 `MOESEKAI_*` 配置，避免升级后遮蔽历史配置。
- `SEKAI_RESOURCE_PROFILE_STATIC_ASSET_BASES` 会兼容读取旧 `MOESEKAI_PROFILE_STATIC_ASSET_BASES`，升级时不需要立刻改旧配置。
- 新增并发配置没有历史 `MOESEKAI_*` 键；需要调优时直接配置对应的 `SEKAI_RESOURCE_*` 键。
- 修改配置后如需在同一进程内立即生效，调用 `refresh_settings()` 清理 settings 缓存。

存储位置：

| 路径 | 内容 |
| --- | --- |
| `data/sekai_resource/master/` | 各区服 MasterData JSON |
| `data/sekai_resource/state/master_state.json` | 已落地主数据状态 |
| `data/sekai_resource/state/master_probe_state.json` | 数据源探测状态 |
| `data/sekai_resource/assets/` | 静态资源镜像缓存 |
| `data/sekai_resource/assets/profile_static/` | 个人档案小人、服装 icon、honor SVG、credits 等静态素材 |
| `data/sekai_resource/state/asset_cache_index.json` | 资产缓存索引 |
| `data/sekai_resource/state/asset_miss_cache.json` | 资源缺失负缓存 |
| `data/sekai_resource/state/music_alias_snapshot.json` | 歌曲别名快照 |
| `data/sekai_resource/state/alias_state.json` | 歌曲别名同步状态 |
| `data/sekai_resource/charts/b30_constants.csv` | B30 社区定数 CSV 缓存 |
| `data/sekai_resource/state/b30_constants_state.json` | B30 定数刷新状态 |

维护服务自身时注意：状态文件与主数据提交都使用临时文件或备份方式降低半写入风险，不要绕过现有 store 直接覆盖这些 JSON。

## Integration Rules
- 新插件接入时先确认需求属于共享资源能力；如果只是单插件私有业务规则，业务逻辑仍应留在插件层。
- 主数据和资产读取都是异步网络兜底入口，命令处理里要考虑超时、空数据和外部源不可用。
- 同一业务流程需要多张素材时，优先使用 `fetch_many_contents()` 或语义化批量入口；不要在插件里重复实现批量下载、限流和去重。
- 批量入口只负责“同时拿多张资源”；业务层仍负责决定要取哪些资源、缺图如何降级、以及是否需要更小的并发上限。
- 需要歌曲或角色搜索时优先复用 `alias_provider`；不要在插件内复制别名表或另写一套模糊匹配。
- 需要谱面定数时优先复用 `b30_constants_provider`；只有插件私有算法应留在插件层。
- 不要在插件里复制源站 URL、GitHub raw URL、缓存目录或数据集下载逻辑；这些都由 `sekai_resource` 维护。
- 不要把 `assetbundleName`、数据集字段名和区服映射写成不可配置的业务常量；优先从 MasterData 记录读取。
- 需要后台检查更新时复用 `master_data_provider.get_probe_interval_seconds()` 与 `master_data_provider.probe_next_updates()`，不要另写定时拉取循环。
- 管理命令手动刷新主数据时优先调用 `update_region(..., force=True)` 或 `update_all(force=True)`，并把 `to_message()` 返回给管理员。
- 测试中如需 monkeypatch 新代码，应 patch `zhenxun.services.sekai_resource` 的公共 provider；旧 `moesekai.providers` 路径只用于覆盖历史兼容行为。

## Minimal Examples
按歌曲 ID 获取曲绘：

```python
from zhenxun.services.sekai_resource import asset_provider, master_data_provider


async def get_music_jacket_by_id(server: str, music_id: int) -> bytes | None:
    musics = await master_data_provider.get_musics(server)
    music = next((item for item in musics if item.get("id") == music_id), None)
    if not music:
        return None
    assetbundle = str(music.get("assetbundleName") or "").strip()
    if not assetbundle:
        return None
    return await asset_provider.get_music_jacket(server, assetbundle)
```

按当前活动获取活动 banner：

```python
from zhenxun.services.sekai_resource import asset_provider, master_data_provider


async def get_current_event_banner(server: str) -> bytes | None:
    event = await master_data_provider.get_current_event(
        server,
        fallback="next_first",
    )
    if not event:
        return None
    assetbundle = str(event.get("assetbundleName") or "").strip()
    if not assetbundle:
        return None
    return await asset_provider.get_event_banner(server, assetbundle)
```

批量获取曲绘：

```python
from zhenxun.services.sekai_resource import asset_provider, master_data_provider


async def get_latest_music_jackets(server: str, limit: int = 10) -> list[bytes | None]:
    musics = await master_data_provider.get_musics(server)
    assetbundles = [
        str(music.get("assetbundleName") or "").strip()
        for music in musics[-limit:]
        if music.get("assetbundleName")
    ]
    return await asset_provider.get_music_jackets(
        server,
        assetbundles,
        timeout=8,
    )
```

批量获取混合资源：

```python
from zhenxun.services.sekai_resource import AssetFetchRequest, asset_provider


async def fetch_card_and_stamp(
    server: str,
    card_assetbundle: str,
    stamp_assetbundle: str,
) -> list[bytes | None]:
    return await asset_provider.fetch_many_contents(
        [
            AssetFetchRequest(server, "card_normal", card_assetbundle, timeout=8),
            AssetFetchRequest(server, "stamp", stamp_assetbundle, timeout=8),
        ],
        concurrency=2,
    )
```

管理员手动刷新并汇总结果：

```python
from zhenxun.services.sekai_resource import master_data_provider


async def refresh_all_master_data() -> str:
    results = await master_data_provider.update_all(force=True)
    return "\n\n".join(result.to_message() for result in results)
```

## Verification
- 文档或接入改动只涉及调用方式时，优先运行相关插件的定向测试。
- 涉及 `sekai_resource` 服务行为时，优先运行 `uv run pytest tests/test_moesekai_master_data.py tests/test_moesekai_providers.py`。
- 涉及 MoeSekai 业务链路时，再补充对应 `tests/test_moesekai_services.py` 或更窄的目标测试。
- 如果只是文档更新，可以不跑 pytest，但交付前必须复核文档中的导入路径、方法名、dataset 名、asset kind 名和配置键名仍与当前代码一致。
