import json
import asyncio
import os
from pathlib import Path
import random
import base64
import uuid
import unicodedata
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, ClassVar

from cachetools import TTLCache
from nonebot.adapters.onebot.v11 import Bot
from nonebot_plugin_alconna import At, Text
from tortoise import timezone
from tortoise.expressions import F, Q
from tortoise.functions import Count
from tortoise.transactions import in_transaction

from zhenxun import ui
from zhenxun.models.group_member_info import GroupInfoUser
from zhenxun.services import avatar_service, logger
from zhenxun.utils.echart_utils import ChartUtils
from zhenxun.utils.echart_utils.models import Barh
from zhenxun.utils.platform import PlatformUtils
import aiofiles

from ..config import DATA_PATH, get_quote_path, resolve_quote_image_path
from ..model import (
    HotQuoteItemData,
    HotQuotesPageData,
    Quote,
    QuoteCardData,
    QuoteTag,
    QuoteTagLink,
)

try:
    import spacy_pkuseg as pkuseg

    seg = pkuseg.pkuseg(model_name="web")
except ImportError:
    logger.warning(
        "未安装 'spacy_pkuseg'，分词功能将受限。请运行 `pip install zhenxun[pkuseg]`",
        "群聊语录",
    )

    class DummySeg:
        def cut(self, text):
            return [text] if text else []

    seg = DummySeg()


@dataclass(frozen=True)
class TagMutationResult:
    """统一 tag 修改结果。"""

    success: bool
    tags: list[str]
    changed_tags: list[str]


@dataclass(frozen=True)
class TagAliasMutationResult:
    """群内 tag 互通修改结果。"""

    success: bool
    members: list[str]
    existing_group_count: int = 0
    requires_confirmation: bool = False


@dataclass(frozen=True)
class QuoteAuditIssue:
    """语录存储审计发现的单条异常。"""

    id: int
    group_id: str
    image_path: str
    reason: str


@dataclass(frozen=True)
class QuoteAuditSummary:
    """语录数据库与图片目录的只读审计汇总。"""

    total: int
    valid: int
    missing: int
    out_of_bounds: int
    orphan_files: int
    issues: list[QuoteAuditIssue]


@dataclass(frozen=True)
class QuotePurgeSummary:
    """到期回收站清理结果。"""

    purged: int
    failed: int


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class _KeyedAsyncLockPool:
    """仅串行化同一 key 的异步操作，并在无人使用时释放锁条目。"""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._guard = Lock()

    @asynccontextmanager
    async def hold(self, key: str):
        with self._guard:
            entry = self._entries.setdefault(key, _LockEntry(asyncio.Lock()))
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)


class QuoteService:
    """语录服务类"""

    _recent_quotes: ClassVar[TTLCache] = TTLCache(maxsize=1000, ttl=600)
    _max_history_per_key: ClassVar[int] = 30
    _user_tag_prefix: ClassVar[str] = "user:"
    _tag_schema_version: ClassVar[int] = 1
    _tag_migration_batch_size: ClassVar[int] = 200
    _slow_query_seconds: ClassVar[float] = 0.5
    _recycle_retention: ClassVar[timedelta] = timedelta(days=7)
    _purge_batch_size: ClassVar[int] = 100
    _operation_locks: ClassVar[_KeyedAsyncLockPool] = _KeyedAsyncLockPool()
    _purge_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @staticmethod
    def active_quotes(*args, **filters):
        """返回未进入回收站的语录查询。"""
        return Quote.filter(*args, deleted_at__isnull=True, **filters)

    @staticmethod
    def recycled_quotes(*args, **filters):
        """返回回收站中的语录查询。"""
        return Quote.filter(*args, deleted_at__not_isnull=True, **filters)

    @classmethod
    @asynccontextmanager
    async def image_write_lock(cls, image_path: str | Path):
        """串行化同一路径的文件创建、数据库写入与失败清理。"""
        lock_key = f"image:{Path(image_path).resolve()}"
        async with cls._operation_locks.hold(lock_key):
            yield

    @classmethod
    @asynccontextmanager
    async def quote_save_lock(
        cls,
        group_id: str,
        image_hash: str | None,
        image_path: str | Path,
    ):
        """按数据库去重键串行保存，并同步保护最终图片路径。"""
        dedup_key = f"save:{group_id}:{image_hash or Path(image_path).resolve()}"
        async with cls._operation_locks.hold(dedup_key):
            async with cls.image_write_lock(image_path):
                yield

    @staticmethod
    def serialize_image_path(image_path: str | Path) -> str:
        """将实际图片路径转换为数据库使用的 DATA_PATH 相对路径。"""
        return Path(os.path.relpath(image_path, DATA_PATH)).as_posix()

    @classmethod
    async def audit_storage(cls, group_id: str | None = None) -> QuoteAuditSummary:
        """只读检查数据库记录、图片路径和全局孤儿 PNG 文件。"""
        filters = {"group_id": group_id} if group_id else {}
        quotes = list(await Quote.filter(**filters))
        valid = 0
        missing = 0
        out_of_bounds = 0
        issues: list[QuoteAuditIssue] = []
        referenced_paths: set[Path] = set()

        # 每条记录只解析和检查文件状态，异常仅进入报告，不修改任何持久化数据。
        for quote in quotes:
            try:
                absolute_path = resolve_quote_image_path(quote.image_path)
            except ValueError:
                out_of_bounds += 1
                if len(issues) < 20:
                    issues.append(
                        QuoteAuditIssue(
                            id=quote.id,
                            group_id=str(quote.group_id),
                            image_path=str(quote.image_path),
                            reason="out_of_bounds",
                        )
                    )
                continue

            referenced_paths.add(absolute_path.resolve())
            if absolute_path.is_file():
                valid += 1
                continue

            missing += 1
            if len(issues) < 20:
                issues.append(
                    QuoteAuditIssue(
                        id=quote.id,
                        group_id=str(quote.group_id),
                        image_path=str(quote.image_path),
                        reason="missing",
                    )
                )

        orphan_files = 0
        if group_id is None:
            quote_path = get_quote_path()
            if quote_path.is_dir():
                # 临时上传文件不属于语录资产，避免把处理中间态误报为孤儿。
                orphan_files = sum(
                    1
                    for image_path in quote_path.rglob("*.png")
                    if not image_path.name.startswith("temp_")
                    and ".deleting" not in image_path.name
                    and image_path.resolve() not in referenced_paths
                )

        return QuoteAuditSummary(
            total=len(quotes),
            valid=valid,
            missing=missing,
            out_of_bounds=out_of_bounds,
            orphan_files=orphan_files,
            issues=issues,
        )

    @classmethod
    async def delete_image_if_unreferenced(cls, image_path: str | Path) -> bool:
        """仅删除未被任何语录记录引用的图片文件。"""
        stored_path = cls.serialize_image_path(image_path)
        if await Quote.filter(image_path=stored_path).exists():
            return False

        absolute_path = resolve_quote_image_path(image_path)
        if not absolute_path.exists():
            return False
        absolute_path.unlink()
        return True

    @classmethod
    def serialize_user_tag(cls, user_id: str) -> str:
        return f"{cls._user_tag_prefix}{str(user_id).strip()}"

    @classmethod
    def parse_tag_text(cls, text: str | None) -> list[str]:
        if not text:
            return []
        return [part.strip() for part in text.split() if part.strip()]

    @staticmethod
    def normalize_tag_value(tag: str | None) -> str:
        """生成用于群内唯一约束与精确查询的稳定 tag 值。"""
        if tag is None:
            return ""
        normalized = unicodedata.normalize("NFKC", str(tag))
        return " ".join(normalized.split()).casefold()

    @classmethod
    def normalize_tags(cls, tags: Iterable[str | None]) -> list[str]:
        normalized_tags: list[str] = []
        seen: set[str] = set()

        for tag in tags:
            if tag is None:
                continue
            tag_value = " ".join(unicodedata.normalize("NFKC", str(tag)).split())
            normalized_value = cls.normalize_tag_value(tag_value)
            if not normalized_value or normalized_value in seen:
                continue
            seen.add(normalized_value)
            normalized_tags.append(tag_value)

        return normalized_tags

    @classmethod
    def parse_tag_segments(cls, parts: Iterable[Any]) -> list[str]:
        raw_tags: list[str] = []

        for part in parts:
            if (
                isinstance(part, At) or part.__class__.__name__ == "At"
            ) and getattr(part, "target", None):
                raw_tags.append(cls.serialize_user_tag(str(part.target)))
            elif isinstance(part, Text) or part.__class__.__name__ == "Text":
                raw_tags.extend(cls.parse_tag_text(part.text))
            elif isinstance(part, str):
                raw_tags.extend(cls.parse_tag_text(part))

        return cls.normalize_tags(raw_tags)

    @classmethod
    def _deserialize_storage_tags(cls, raw_tags: Any) -> list[str]:
        if isinstance(raw_tags, list):
            return cls.normalize_tags(str(tag) for tag in raw_tags)

        if isinstance(raw_tags, tuple):
            return cls.normalize_tags(str(tag) for tag in raw_tags)

        if isinstance(raw_tags, str):
            stripped = raw_tags.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return cls.normalize_tags(cls.parse_tag_text(stripped))
            return cls._deserialize_storage_tags(parsed)

        return []

    @classmethod
    def get_auto_tags(cls, quote: Quote) -> list[str]:
        return cls._deserialize_storage_tags(getattr(quote, "tags", []))

    @classmethod
    def get_manual_tags(cls, quote: Quote) -> list[str]:
        return cls._deserialize_storage_tags(getattr(quote, "manual_tags", []))

    @classmethod
    def get_all_tags(cls, quote: Quote) -> list[str]:
        return cls.normalize_tags(cls.get_manual_tags(quote) + cls.get_auto_tags(quote))

    @classmethod
    def _uses_relation_tags(cls, quote: Quote) -> bool:
        return getattr(quote, "tag_schema_version", None) == cls._tag_schema_version

    @classmethod
    async def _get_or_create_tag(
        cls,
        group_id: str,
        value: str,
        *,
        connection=None,
    ) -> QuoteTag:
        normalized_value = cls.normalize_tag_value(value)
        tag, _ = await QuoteTag.get_or_create(
            group_id=str(group_id),
            normalized_value=normalized_value,
            defaults={"value": value},
            using_db=connection,
        )
        return tag

    @classmethod
    async def _replace_quote_tag_links(
        cls,
        quote: Quote,
        tags: Iterable[str],
        *,
        connection=None,
    ) -> list[str]:
        normalized_tags = cls.normalize_tags(tags)
        link_query = QuoteTagLink.filter(quote_id=quote.id)
        if connection is not None:
            link_query = link_query.using_db(connection)
        await link_query.delete()
        for value in normalized_tags:
            tag = await cls._get_or_create_tag(
                str(quote.group_id), value, connection=connection
            )
            await QuoteTagLink.get_or_create(
                quote_id=quote.id,
                tag_id=tag.id,
                using_db=connection,
            )
        return normalized_tags

    @classmethod
    async def _migrate_locked_quote(cls, quote: Quote, connection=None) -> list[str]:
        """将一条旧 JSON 语录幂等迁移到统一 tag 关联表。"""
        if cls._uses_relation_tags(quote):
            link_query = QuoteTagLink.filter(quote_id=quote.id)
            if connection is not None:
                link_query = link_query.using_db(connection)
            return list(
                await link_query.order_by("id").values_list("tag__value", flat=True)
            )

        unified_tags = cls.get_all_tags(quote)
        await cls._replace_quote_tag_links(
            quote,
            unified_tags,
            connection=connection,
        )
        quote.tags = []
        quote.manual_tags = unified_tags
        quote.tag_schema_version = cls._tag_schema_version
        await quote.save(
            using_db=connection,
            update_fields=["tags", "manual_tags", "tag_schema_version"],
        )
        return unified_tags

    @classmethod
    async def migrate_legacy_tags(cls, batch_size: int | None = None) -> int:
        """分批迁移旧 tag；可重复执行，也可在任意批次后安全中断。"""
        limit = batch_size or cls._tag_migration_batch_size
        migrated = 0
        while True:
            quote_ids = list(
                await Quote.filter(tag_schema_version=0)
                .order_by("id")
                .limit(limit)
                .values_list("id", flat=True)
            )
            if not quote_ids:
                break

            for quote_id in quote_ids:
                async with in_transaction() as connection:
                    quote = (
                        await Quote.filter(id=quote_id)
                        .select_for_update()
                        .using_db(connection)
                        .get()
                    )
                    if not cls._uses_relation_tags(quote):
                        await cls._migrate_locked_quote(quote, connection)
                        migrated += 1
            logger.info(
                f"统一 tag 迁移进度：本批 {len(quote_ids)} 条，累计 {migrated} 条",
                "群聊语录",
            )
        return migrated

    @classmethod
    async def get_quote_tags(cls, quote: Quote) -> list[str]:
        """读取语录实际保存的统一 tag，旧记录在迁移前回退 JSON。"""
        if not cls._uses_relation_tags(quote):
            return cls.get_all_tags(quote)
        return list(
            await QuoteTagLink.filter(quote_id=quote.id)
            .order_by("id")
            .values_list("tag__value", flat=True)
        )

    @classmethod
    async def get_quote_tag_groups(cls, quote: Quote) -> list[list[str]]:
        """按互通组折叠展示语录 tag，并将语录实际保存的 tag 置于组首。"""
        if not cls._uses_relation_tags(quote):
            return [[tag] for tag in cls.get_all_tags(quote)]

        linked_tags = list(
            await QuoteTag.filter(quote_links__quote_id=quote.id).order_by("id")
        )
        display_groups: list[list[str]] = []
        shown_alias_keys: set[str] = set()
        for tag in linked_tags:
            if not tag.alias_group_key:
                display_groups.append([tag.value])
                continue
            if tag.alias_group_key in shown_alias_keys:
                continue
            shown_alias_keys.add(tag.alias_group_key)
            members = list(
                await QuoteTag.filter(
                    group_id=str(quote.group_id),
                    alias_group_key=tag.alias_group_key,
                )
                .order_by("id")
                .values_list("value", flat=True)
            )
            aliases = [member for member in members if member != tag.value]
            display_groups.append([tag.value, *aliases])
        return display_groups

    @classmethod
    async def get_tag_alias_group(cls, group_id: str, value: str) -> list[str]:
        tag = await QuoteTag.filter(
            group_id=str(group_id),
            normalized_value=cls.normalize_tag_value(value),
        ).first()
        if not tag:
            return []
        if not tag.alias_group_key:
            return [tag.value]
        return list(
            await QuoteTag.filter(
                group_id=str(group_id),
                alias_group_key=tag.alias_group_key,
            )
            .order_by("id")
            .values_list("value", flat=True)
        )

    @classmethod
    async def preview_tag_alias_link(
        cls, group_id: str, values: Iterable[str]
    ) -> TagAliasMutationResult:
        normalized_values = cls.normalize_tags(values)
        if len(normalized_values) < 2:
            return TagAliasMutationResult(False, normalized_values)
        normalized_keys = [cls.normalize_tag_value(value) for value in normalized_values]
        existing_tags = list(
            await QuoteTag.filter(
                group_id=str(group_id), normalized_value__in=normalized_keys
            )
        )
        existing_group_keys = {
            tag.alias_group_key for tag in existing_tags if tag.alias_group_key
        }
        members = list(normalized_values)
        if existing_group_keys:
            group_members = await QuoteTag.filter(
                group_id=str(group_id),
                alias_group_key__in=list(existing_group_keys),
            ).values_list("value", flat=True)
            members = cls.normalize_tags([*members, *group_members])
        return TagAliasMutationResult(
            True,
            members,
            existing_group_count=len(existing_group_keys),
            requires_confirmation=len(existing_group_keys) > 1,
        )

    @classmethod
    async def link_tag_aliases(
        cls,
        group_id: str,
        values: Iterable[str],
        *,
        confirm_merge: bool = False,
    ) -> TagAliasMutationResult:
        preview = await cls.preview_tag_alias_link(group_id, values)
        if not preview.success or (
            preview.requires_confirmation and not confirm_merge
        ):
            return preview

        async with in_transaction() as connection:
            tags = [
                await cls._get_or_create_tag(
                    str(group_id), value, connection=connection
                )
                for value in cls.normalize_tags(values)
            ]
            existing_keys = sorted(
                {
                    tag.alias_group_key
                    for tag in tags
                    if tag.alias_group_key is not None
                }
            )
            target_key = existing_keys[0] if existing_keys else uuid.uuid4().hex
            affected_query = QuoteTag.filter(
                Q(id__in=[tag.id for tag in tags])
                | Q(alias_group_key__in=existing_keys),
                group_id=str(group_id),
            ).using_db(connection)
            await affected_query.update(alias_group_key=target_key)
            members = list(
                await QuoteTag.filter(
                    group_id=str(group_id), alias_group_key=target_key
                )
                .using_db(connection)
                .order_by("id")
                .values_list("value", flat=True)
            )
        return TagAliasMutationResult(
            True,
            members,
            existing_group_count=len(existing_keys),
        )

    @classmethod
    async def unlink_tag_alias(
        cls, group_id: str, value: str
    ) -> TagAliasMutationResult:
        async with in_transaction() as connection:
            tag = await QuoteTag.filter(
                group_id=str(group_id),
                normalized_value=cls.normalize_tag_value(value),
            ).using_db(connection).first()
            if not tag:
                return TagAliasMutationResult(False, [])
            if not tag.alias_group_key:
                return TagAliasMutationResult(True, [tag.value])

            alias_group_key = tag.alias_group_key
            await QuoteTag.filter(id=tag.id).using_db(connection).update(
                alias_group_key=None
            )
            remaining = list(
                await QuoteTag.filter(
                    group_id=str(group_id), alias_group_key=alias_group_key
                )
                .using_db(connection)
                .order_by("id")
            )
            if len(remaining) < 2:
                await QuoteTag.filter(
                    group_id=str(group_id), alias_group_key=alias_group_key
                ).using_db(connection).update(alias_group_key=None)
                return TagAliasMutationResult(True, [tag.value])
            return TagAliasMutationResult(
                True,
                [tag.value, *(item.value for item in remaining)],
            )

    @classmethod
    async def _resolve_exact_tag_ids(cls, group_id: str, value: str) -> list[int]:
        tag = await QuoteTag.filter(
            group_id=str(group_id),
            normalized_value=cls.normalize_tag_value(value),
        ).first()
        if not tag:
            return []
        if tag.alias_group_key:
            return list(
                await QuoteTag.filter(
                    group_id=str(group_id), alias_group_key=tag.alias_group_key
                ).values_list("id", flat=True)
            )
        return [tag.id]

    @classmethod
    async def _resolve_exact_tag_normalized_values(
        cls, group_id: str, value: str
    ) -> set[str]:
        tag = await QuoteTag.filter(
            group_id=str(group_id),
            normalized_value=cls.normalize_tag_value(value),
        ).first()
        if not tag:
            return {cls.normalize_tag_value(value)}
        if not tag.alias_group_key:
            return {tag.normalized_value}
        return set(
            await QuoteTag.filter(
                group_id=str(group_id), alias_group_key=tag.alias_group_key
            ).values_list("normalized_value", flat=True)
        )

    @classmethod
    async def _quote_ids_for_exact_tag(cls, group_id: str, value: str) -> set[int]:
        tag_ids = await cls._resolve_exact_tag_ids(group_id, value)
        if not tag_ids:
            return set()
        return set(
            await QuoteTagLink.filter(tag_id__in=tag_ids).values_list(
                "quote_id", flat=True
            )
        )

    @classmethod
    async def _resolve_fuzzy_tag_candidates(
        cls, group_id: str, value: str
    ) -> tuple[set[int], set[str]]:
        normalized_value = cls.normalize_tag_value(value)
        if not normalized_value:
            return set(), set()

        matched_tags = list(
            await QuoteTag.filter(
                group_id=str(group_id),
                normalized_value__contains=normalized_value,
            )
        )
        if not matched_tags:
            return set(), set()

        alias_group_keys = {
            tag.alias_group_key for tag in matched_tags if tag.alias_group_key
        }
        expanded_tags = matched_tags
        if alias_group_keys:
            expanded_tags = list(
                await QuoteTag.filter(
                    Q(id__in=[tag.id for tag in matched_tags])
                    | Q(alias_group_key__in=alias_group_keys),
                    group_id=str(group_id),
                )
            )

        tag_ids = [tag.id for tag in expanded_tags]
        quote_ids = set(
            await QuoteTagLink.filter(tag_id__in=tag_ids).values_list(
                "quote_id", flat=True
            )
        )
        normalized_values = {tag.normalized_value for tag in expanded_tags}
        return quote_ids, normalized_values

    @classmethod
    async def get_quote_ids_for_user(cls, group_id: str, user_id: str) -> list[int]:
        quoted_ids = set(
            await cls.active_quotes(
                group_id=str(group_id), quoted_user_id=str(user_id)
            ).values_list("id", flat=True)
        )
        tagged_ids = await cls._quote_ids_for_exact_tag(
            str(group_id), cls.serialize_user_tag(str(user_id))
        )
        alias_values = await cls._resolve_exact_tag_normalized_values(
            str(group_id), cls.serialize_user_tag(str(user_id))
        )
        legacy_tagged_ids = {
            quote.id
            for quote in await cls.active_quotes(
                group_id=str(group_id), tag_schema_version=0
            )
            if str(getattr(quote, "quoted_user_id", "") or "") == str(user_id)
            or any(
                cls.normalize_tag_value(tag) in alias_values
                for tag in cls.get_all_tags(quote)
            )
        }
        return list(quoted_ids | tagged_ids | legacy_tagged_ids)

    @classmethod
    def parse_user_tag(cls, tag: str) -> str | None:
        if not tag.startswith(cls._user_tag_prefix):
            return None
        user_id = tag.removeprefix(cls._user_tag_prefix).strip()
        return user_id or None

    @classmethod
    def format_tags_for_message(cls, tags: Iterable[str]) -> list[Any]:
        normalized_tags = cls.normalize_tags(tags)
        if not normalized_tags:
            return ["无"]

        message_parts: list[Any] = []
        for index, tag in enumerate(normalized_tags):
            if index:
                message_parts.append(" ")

            if user_id := cls.parse_user_tag(tag):
                message_parts.append(At(target=user_id, flag="user"))
            else:
                message_parts.append(tag)

        return message_parts

    @classmethod
    def _match_user_filter(cls, quote: Quote, user_id_filter: str | None) -> bool:
        if not user_id_filter:
            return True

        user_id = str(user_id_filter)
        if str(getattr(quote, "quoted_user_id", "") or "") == user_id:
            return True

        user_tag = cls.serialize_user_tag(user_id)
        return user_tag in cls.get_all_tags(quote)

    @classmethod
    async def add_quote(
        cls,
        group_id: str,
        image_path: str,
        ocr_content: str | None,
        recorded_text: str | None,
        quoted_user_id: str | None = None,
        image_hash: str | None = None,
        uploader_user_id: str | None = None,
        manual_tags: list[str] | None = None,
    ) -> tuple[Quote | None, bool]:
        """
        向数据库添加语录，并在内部处理所有重复性检查。
        """
        try:
            logger.info(
                f"开始添加语录 - 群组: {group_id}, 图片路径: {image_path}, 被记录用户: {quoted_user_id}, 上传者: {uploader_user_id}",
                "群聊语录",
            )
            if image_hash:
                existing_quote = await Quote.filter(
                    group_id=group_id, image_hash=image_hash
                ).first()
                if existing_quote:
                    logger.warning(
                        f"发现重复语录 (基于图片哈希值) - 群组: {group_id}, 已存在ID: {existing_quote.id}",
                        "群聊语录",
                    )
                    return existing_quote, False

            tags_source = ocr_content if ocr_content else recorded_text
            recognized_tags = cls.cut_sentence(tags_source) if tags_source else []
            unified_tags = cls.normalize_tags(recognized_tags + (manual_tags or []))

            relative_image_path = cls.serialize_image_path(image_path)

            async with in_transaction() as connection:
                quote = await Quote.create(
                    using_db=connection,
                    group_id=group_id,
                    image_path=relative_image_path,
                    image_hash=image_hash,
                    ocr_text=ocr_content,
                    recorded_text=recorded_text,
                    tags=[],
                    manual_tags=unified_tags,
                    tag_schema_version=cls._tag_schema_version,
                    quoted_user_id=quoted_user_id,
                    uploader_user_id=uploader_user_id,
                    view_count=0,
                )
                await cls._replace_quote_tag_links(
                    quote,
                    unified_tags,
                    connection=connection,
                )

            logger.info(f"语录添加成功 - ID: {quote.id}, 群组: {group_id}", "群聊语录")
            return quote, True

        except Exception as e:
            logger.error(f"添加语录失败 - 群组: {group_id}, 错误: {e}", "群聊语录", e=e)
            return None, False

    @staticmethod
    async def delete_quote(
        group_id: str,
        image_basename: str,
        deleted_by_user_id: str | None = None,
    ) -> bool:
        """将指定语录移入回收站。"""
        try:
            logger.info(
                f"尝试删除语录 - 群组: {group_id}, 图片: {image_basename}", "群聊语录"
            )
            quote = await QuoteService.find_quote_by_basename(group_id, image_basename)
            if quote:
                return await QuoteService.delete_quote_instance(
                    quote,
                    deleted_by_user_id=deleted_by_user_id,
                )
            else:
                logger.warning(
                    f"要删除的语录不存在 - 群组: {group_id}, 图片: {image_basename}",
                    "群聊语录",
                )
                return False
        except Exception as e:
            logger.error(f"删除语录失败 - 群组: {group_id}, 错误: {e}", "群聊语录", e=e)
            return False

    @staticmethod
    async def delete_quote_instance(
        quote: Quote,
        deleted_by_user_id: str | None = None,
    ) -> bool:
        """将语录原位标记为已删除，保留图片与全部追责数据。"""
        async with QuoteService._operation_locks.hold(f"quote:{quote.id}"):
            deleted_at = timezone.now()
            try:
                updated_count = await QuoteService.active_quotes(id=quote.id).update(
                    deleted_at=deleted_at,
                    deleted_by_user_id=(
                        str(deleted_by_user_id) if deleted_by_user_id else None
                    ),
                )
                if not updated_count:
                    logger.warning(
                        f"语录不存在或已在回收站 - ID: {quote.id}", "群聊语录"
                    )
                    return False

                quote.deleted_at = deleted_at
                quote.deleted_by_user_id = (
                    str(deleted_by_user_id) if deleted_by_user_id else None
                )
                logger.info(
                    f"语录已移入回收站 - ID: {quote.id}, 群组: {quote.group_id}, "
                    f"删除者: {quote.deleted_by_user_id or '未知'}",
                    "群聊语录",
                )
                return True
            except Exception as e:
                logger.error(
                    f"语录移入回收站失败 - ID: {quote.id}, "
                    f"群组: {quote.group_id}, 错误: {e}",
                    "群聊语录",
                    e=e,
                )
                return False

    @staticmethod
    async def restore_quote_instance(quote: Quote) -> bool:
        """恢复回收站语录；缺图或越界记录保持原状。"""
        async with QuoteService._operation_locks.hold(f"quote:{quote.id}"):
            try:
                absolute_image_path = resolve_quote_image_path(quote.image_path)
            except ValueError as path_error:
                logger.warning(
                    f"拒绝恢复越界语录图片 - ID: {quote.id}, 路径: {quote.image_path}",
                    "群聊语录",
                    e=path_error,
                )
                return False

            async with QuoteService.image_write_lock(absolute_image_path):
                if not absolute_image_path.is_file():
                    logger.warning(
                        f"拒绝恢复缺图语录 - ID: {quote.id}, "
                        f"路径: {absolute_image_path}",
                        "群聊语录",
                    )
                    return False

                try:
                    updated_count = await QuoteService.recycled_quotes(
                        id=quote.id
                    ).update(deleted_at=None, deleted_by_user_id=None)
                    if not updated_count:
                        logger.warning(
                            f"语录不存在或已恢复 - ID: {quote.id}", "群聊语录"
                        )
                        return False

                    quote.deleted_at = None
                    quote.deleted_by_user_id = None
                    logger.info(
                        f"语录恢复成功 - ID: {quote.id}, 群组: {quote.group_id}",
                        "群聊语录",
                    )
                    return True
                except Exception as e:
                    logger.error(
                        f"语录恢复失败 - ID: {quote.id}, "
                        f"群组: {quote.group_id}, 错误: {e}",
                        "群聊语录",
                        e=e,
                    )
                    return False

    @classmethod
    async def purge_expired_quotes(
        cls,
        *,
        now: datetime | None = None,
    ) -> QuotePurgeSummary:
        """分批彻底清理已超过七天的回收站语录。"""
        cutoff = (now or timezone.now()) - cls._recycle_retention
        purged = 0
        failed = 0
        last_id = 0

        async with cls._purge_lock:
            while True:
                quote_ids = list(
                    await cls.recycled_quotes(
                        deleted_at__lte=cutoff,
                        id__gt=last_id,
                    )
                    .order_by("id")
                    .limit(cls._purge_batch_size)
                    .values_list("id", flat=True)
                )
                if not quote_ids:
                    break

                last_id = quote_ids[-1]
                for quote_id in quote_ids:
                    quote = await cls.recycled_quotes(id=quote_id).first()
                    if quote and await cls.purge_quote_instance(quote):
                        purged += 1
                    else:
                        failed += 1

        return QuotePurgeSummary(purged=purged, failed=failed)

    @staticmethod
    async def purge_quote_instance(quote: Quote) -> bool:
        """隔离图片后物理删除到期记录，数据库失败时恢复原文件。"""
        async with QuoteService._operation_locks.hold(f"quote:{quote.id}"):
            try:
                absolute_image_path = resolve_quote_image_path(quote.image_path)
            except ValueError as path_error:
                logger.warning(
                    f"拒绝清理越界语录图片 - ID: {quote.id}, 路径: {quote.image_path}",
                    "群聊语录",
                    e=path_error,
                )
                return False

            async with QuoteService.image_write_lock(absolute_image_path):
                return await QuoteService._purge_quote_instance_unlocked(
                    quote,
                    absolute_image_path,
                )

    @staticmethod
    async def _purge_quote_instance_unlocked(
        quote: Quote,
        absolute_image_path: Path,
    ) -> bool:
        """在同一语录锁内执行文件隔离和数据库删除补偿。"""
        isolated_image_path: Path | None = None
        try:
            if absolute_image_path.exists():
                isolated_image_path = absolute_image_path.with_name(
                    f".{absolute_image_path.name}.{uuid.uuid4().hex}.deleting"
                )
                # 同盘 rename 是原子的，先隔离可避免数据库失败后留下缺图记录。
                os.replace(absolute_image_path, isolated_image_path)
            else:
                logger.warning(f"图片文件不存在: {absolute_image_path}", "群聊语录")

            deleted_count = await QuoteService.recycled_quotes(id=quote.id).delete()
            if not deleted_count:
                raise RuntimeError("语录记录不存在或已被删除")

            if isolated_image_path:
                try:
                    isolated_image_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.warning(
                        "清理已隔离图片失败: "
                        f"{isolated_image_path}, 错误: {cleanup_error}",
                        "群聊语录",
                        e=cleanup_error,
                    )
            logger.info(
                f"回收站语录彻底删除成功 - ID: {quote.id}, 群组: {quote.group_id}",
                "群聊语录",
            )
            return True
        except Exception as e:
            if (
                isolated_image_path
                and isolated_image_path.exists()
                and not absolute_image_path.exists()
            ):
                try:
                    os.replace(isolated_image_path, absolute_image_path)
                except OSError as restore_error:
                    logger.error(
                        "恢复隔离图片失败: "
                        f"{isolated_image_path} -> {absolute_image_path}",
                        "群聊语录",
                        e=restore_error,
                    )
            logger.error(
                f"回收站语录彻底删除失败 - ID: {quote.id}, "
                f"群组: {quote.group_id}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return False

    @classmethod
    async def get_random_quote(
        cls, group_id: str, user_id_filter: str | None = None
    ) -> Quote | None:
        """随机获取一条语录，可根据用户筛选，并避免短时间内重复展示"""
        try:
            logger.info(
                f"尝试随机获取语录 - 群组: {group_id}, 用户筛选: {user_id_filter}",
                "群聊语录",
            )
            if user_id_filter:
                candidate_ids = await cls.get_quote_ids_for_user(
                    group_id, user_id_filter
                )
                quotes = await cls.get_quotes_by_ids_in_order(candidate_ids)
            else:
                quotes = list(await cls.active_quotes(group_id=group_id))

            count = len(quotes)
            if count == 0:
                logger.info(
                    f"群组 {group_id} 中 (用户: {user_id_filter or '任意'}) 没有语录",
                    "群聊语录",
                )
                return None

            memory_key = f"{group_id}_{user_id_filter or 'all'}"

            recent_ids = cls._recent_quotes.get(memory_key) or []
            if recent_ids and count > cls._max_history_per_key:
                unseen_quotes = [q for q in quotes if q.id not in recent_ids]
                if unseen_quotes:
                    quotes = unseen_quotes
                else:
                    logger.warning(
                        f"所有语录 ({memory_key}) 都已展示过，将重置记忆",
                        "群聊语录",
                    )
                    cls._recent_quotes[memory_key] = []

            quote = cls._select_and_record_quote(memory_key, quotes)

            if quote:
                logger.info(
                    f"随机获取到语录 ID: {quote.id} (路径: {quote.image_path}) 来自群组 {group_id} (用户: {user_id_filter or '任意'})",
                    "群聊语录",
                )
                return quote
            else:
                logger.warning(
                    f"随机获取语录失败，即使 count > 0 (count={count}) - {memory_key}",
                    "群聊语录",
                )
                return None
        except Exception as e:
            logger.error(
                f"随机获取语录时发生错误 - 群组: {group_id}, 用户筛选: {user_id_filter}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return None

    @classmethod
    async def _search_quotes_by_text_and_filter_by_tags(
        cls,
        group_id: str,
        keyword: str,
        user_id_filter: str | None = None,
        *,
        tag_only: bool = False,
    ) -> list[Quote]:
        """用数据库候选 ID 完成文本/tag 混合搜索，并兼容未迁移 JSON。"""
        started_at = time.perf_counter()
        keyword = keyword.strip()
        if not keyword:
            return []

        exact_alias_values = await cls._resolve_exact_tag_normalized_values(
            group_id, keyword
        )
        fuzzy_tag_ids: set[int] = set()
        fuzzy_alias_values: set[str] = set()
        if not tag_only:
            fuzzy_tag_ids, fuzzy_alias_values = (
                await cls._resolve_fuzzy_tag_candidates(group_id, keyword)
            )

        def legacy_exact_match(quote: Quote) -> bool:
            alias_match = any(
                cls.normalize_tag_value(tag) in exact_alias_values
                for tag in cls.get_all_tags(quote)
            )
            return alias_match if tag_only else (
                alias_match
                or any(
                    cls.normalize_tag_value(tag) in fuzzy_alias_values
                    for tag in cls.get_all_tags(quote)
                )
                or cls._check_exact_keyword_in_quote(keyword, quote)
            )

        legacy_quotes = [
            quote
            for quote in await cls.active_quotes(
                group_id=group_id, tag_schema_version=0
            )
            if not cls._uses_relation_tags(quote)
        ]
        if legacy_quotes and not hasattr(legacy_quotes[0], "tag_schema_version"):
            candidate_quotes = legacy_quotes
            if user_id_filter:
                candidate_quotes = [
                    quote
                    for quote in candidate_quotes
                    if cls._match_user_filter(quote, user_id_filter)
                ]
            exact_matches = [
                quote
                for quote in candidate_quotes
                if legacy_exact_match(quote)
            ]
            if exact_matches:
                return exact_matches
            if tag_only:
                return []
            tokens = cls.normalize_tags([*keyword.split(), *cls.cut_sentence(keyword)])
            return [
                quote
                for quote in candidate_quotes
                if all(cls._check_single_keyword_in_quote(token, quote) for token in tokens)
            ]

        allowed_user_ids: set[int] | None = None
        if user_id_filter:
            allowed_user_ids = set(
                await cls.get_quote_ids_for_user(group_id, user_id_filter)
            )

        exact_tag_ids = await cls._quote_ids_for_exact_tag(group_id, keyword)
        tag_candidate_ids = exact_tag_ids | fuzzy_tag_ids
        text_candidate_ids: set[int] = set()
        if not tag_only:
            text_candidate_ids = set(
                await cls.active_quotes(
                    Q(ocr_text__icontains=keyword)
                    | Q(recorded_text__icontains=keyword),
                    group_id=group_id,
                )
                .values_list("id", flat=True)
            )

        exact_candidate_ids = tag_candidate_ids | text_candidate_ids
        if allowed_user_ids is not None:
            exact_candidate_ids &= allowed_user_ids

        legacy_matches = [
            quote
            for quote in legacy_quotes
            if legacy_exact_match(quote)
            and (
                allowed_user_ids is None
                or quote.id in allowed_user_ids
                or cls._match_user_filter(quote, user_id_filter)
            )
        ]

        if exact_candidate_ids or legacy_matches or tag_only:
            quotes = await cls.get_quotes_by_ids_in_order(
                sorted(exact_candidate_ids, reverse=True)
            )
            result_by_id = {quote.id: quote for quote in [*quotes, *legacy_matches]}
            result = list(result_by_id.values())
            elapsed = time.perf_counter() - started_at
            log_method = logger.warning if elapsed >= cls._slow_query_seconds else logger.debug
            log_method(
                "语录查询候选："
                f"tag={len(tag_candidate_ids)} text={len(text_candidate_ids)} "
                f"result={len(result)} elapsed={elapsed:.3f}s",
                "群聊语录-搜索",
            )
            return result

        tokens = cls.normalize_tags([*keyword.split(), *cls.cut_sentence(keyword)])
        if not tokens:
            return []
        token_alias_values: list[set[str]] = []
        token_candidate_sets: list[set[int]] = []
        for token in tokens:
            exact_token_tag_ids = await cls._quote_ids_for_exact_tag(group_id, token)
            exact_token_alias_values = await cls._resolve_exact_tag_normalized_values(
                group_id, token
            )
            token_tag_ids, fuzzy_token_alias_values = (
                await cls._resolve_fuzzy_tag_candidates(group_id, token)
            )
            token_alias_values.append(
                exact_token_alias_values | fuzzy_token_alias_values
            )
            token_text_ids = set(
                await cls.active_quotes(
                    Q(ocr_text__icontains=token)
                    | Q(recorded_text__icontains=token),
                    group_id=group_id,
                )
                .values_list("id", flat=True)
            )
            token_candidate_sets.append(
                exact_token_tag_ids | token_tag_ids | token_text_ids
            )

        candidate_ids = set.intersection(*token_candidate_sets) if token_candidate_sets else set()
        if allowed_user_ids is not None:
            candidate_ids &= allowed_user_ids
        new_matches = await cls.get_quotes_by_ids_in_order(
            sorted(candidate_ids, reverse=True)
        )
        legacy_fuzzy_matches = [
            quote
            for quote in legacy_quotes
            if all(
                cls._check_single_keyword_in_quote(token, quote)
                or any(
                    cls.normalize_tag_value(tag) in alias_values
                    for tag in cls.get_all_tags(quote)
                )
                for token, alias_values in zip(
                    tokens, token_alias_values, strict=True
                )
            )
            and (
                allowed_user_ids is None
                or quote.id in allowed_user_ids
                or cls._match_user_filter(quote, user_id_filter)
            )
        ]
        result_by_id = {
            quote.id: quote for quote in [*new_matches, *legacy_fuzzy_matches]
        }
        elapsed = time.perf_counter() - started_at
        log_method = logger.warning if elapsed >= cls._slow_query_seconds else logger.debug
        log_method(
            f"语录查询候选：tokens={len(tokens)} result={len(result_by_id)} "
            f"elapsed={elapsed:.3f}s",
            "群聊语录-搜索",
        )
        return list(result_by_id.values())

    @classmethod
    async def search_quotes(
        cls,
        group_id: str,
        keyword: str,
        user_id_filter: str | None = None,
        *,
        tag_only: bool = False,
    ) -> list[Quote]:
        """根据关键词搜索语录并返回完整命中集。"""
        logger.info(
            f"开始搜索语录 - 群组: {group_id}, 关键词: {keyword}, 用户筛选: {user_id_filter}",
            "群聊语录",
        )

        try:
            all_matches = await cls._search_quotes_by_text_and_filter_by_tags(
                group_id, keyword, user_id_filter, tag_only=tag_only
            )
            if all_matches:
                logger.info(f"总共找到匹配的语录 {len(all_matches)} 条", "群聊语录")
            else:
                logger.info(
                    f"群组 {group_id} (用户: {user_id_filter or '任意'}) 中未找到与 '{keyword}' 相关的语录。",
                    "群聊语录",
                )
            return all_matches
        except Exception as e:
            logger.error(
                f"搜索语录时发生错误 - 群组: {group_id}, 关键词: {keyword}, "
                f"用户筛选: {user_id_filter}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return []

    @classmethod
    def _check_exact_keyword_in_quote(cls, keyword: str, quote: Quote) -> bool:
        """
        检查完整关键词是否直接命中语录文本或任一 tag。

        精确阶段如果只看 ocr/recorded_text，会导致文本命中先返回，
        让手动 tag 命中的语录完全失去参与抽取的机会。
        """
        kw_lower = keyword.lower()

        if quote.ocr_text and kw_lower in quote.ocr_text.lower():
            return True
        if quote.recorded_text and kw_lower in quote.recorded_text.lower():
            return True

        return any(kw_lower in str(tag).lower() for tag in cls.get_all_tags(quote))

    @classmethod
    def _check_single_keyword_in_quote(cls, keyword: str, quote: Quote) -> bool:
        """
        检查单个关键词（及其分词）是否存在于语录的文本或标签中。
        这是一个在Python层面执行的辅助函数。
        """
        kw_lower = keyword.lower()
        tokens = cls.cut_sentence(keyword)

        if quote.ocr_text and kw_lower in quote.ocr_text.lower():
            return True
        if quote.recorded_text and kw_lower in quote.recorded_text.lower():
            return True
        for token in tokens:
            if quote.ocr_text and token.lower() in quote.ocr_text.lower():
                return True
            if quote.recorded_text and token.lower() in quote.recorded_text.lower():
                return True

        tags_to_check = cls.get_all_tags(quote)
        for tag in tags_to_check:
            tag_lower = str(tag).lower()
            if kw_lower in tag_lower:
                return True
            for token in tokens:
                if token.lower() in tag_lower:
                    return True

        return False

    @classmethod
    async def search_quote(
        cls, group_id: str, keyword: str, user_id_filter: str | None = None
    ) -> Quote | None:
        """根据关键词搜索单条语录，可根据用户筛选。"""
        memory_key = f"{group_id}_{user_id_filter or 'all'}_{keyword}"

        try:
            all_matches = await cls.search_quotes(group_id, keyword, user_id_filter)

            if all_matches:
                random_quote = cls._select_and_record_quote(memory_key, all_matches)
                logger.info(
                    f"搜索到语录 ID: {random_quote.id} (路径: {random_quote.image_path})",
                    "群聊语录",
                )
                return random_quote

            return None
        except Exception as e:
            logger.error(
                f"搜索语录时发生错误 - 群组: {group_id}, 关键词: {keyword}, "
                f"用户筛选: {user_id_filter}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return None

    @staticmethod
    async def get_random_quote_ids(group_id: str) -> list[int]:
        """仅拉取语录 ID，避免随机多张时把整群 ORM 实体化到内存。"""
        try:
            return list(
                await QuoteService.active_quotes(group_id=group_id)
                .values_list("id", flat=True)
            )
        except Exception as e:
            logger.error(
                f"获取随机语录 ID 列表失败 - 群组: {group_id}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return []

    @staticmethod
    async def get_quotes_by_ids_in_order(ids: list[int]) -> list[Quote]:
        """按输入 ID 顺序返回语录，避免 id__in 查询结果顺序不稳定。"""
        if not ids:
            return []

        try:
            quotes = await QuoteService.active_quotes(id__in=ids)
            quote_by_id = {quote.id: quote for quote in quotes}
            # SQL 的 IN 查询不保证结果顺序，这里按输入顺序重排，
            # 否则多张发送时会把前面挑好的去重/随机顺序打乱。
            return [quote_by_id[quote_id] for quote_id in ids if quote_id in quote_by_id]
        except Exception as e:
            logger.error(
                f"根据 ID 列表获取语录失败 - IDs: {ids}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return []

    @classmethod
    async def find_quote_by_basename(
        cls,
        group_id: str,
        image_basename: str,
        *,
        recycled_only: bool = False,
    ) -> Quote | None:
        """根据图片文件名查找语录"""
        try:
            logger.info(
                f"根据文件名查找语录 - 群组: {group_id}, 文件名: {image_basename}",
                "群聊语录",
            )

            quote_query = cls.recycled_quotes if recycled_only else cls.active_quotes
            quotes = await quote_query(
                group_id=group_id, image_path__iendswith=image_basename
            )
            quote = quotes[0] if quotes else None

            if quote:
                logger.info(
                    f"找到语录 ID: {quote.id} (路径: {quote.image_path})",
                    "群聊语录",
                )
                return quote

            logger.info(f"未找到包含文件名 {image_basename} 的语录", "群聊语录")
            return None
        except Exception as e:
            logger.error(
                f"根据文件名查找语录时发生错误 - 群组: {group_id}, 文件名: {image_basename}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return None

    @staticmethod
    def _is_quote_image_stem_match(quote: Quote, image_md5: str) -> bool:
        """只比较最终文件名主干，避免把路径中的其他十六进制串误判为语录标识。"""
        return Path(str(quote.image_path)).stem.lower() == image_md5.lower()

    @classmethod
    def _pick_quote_by_image_md5(
        cls, quotes: Iterable[Quote], image_md5: str
    ) -> Quote | None:
        for quote in quotes:
            if cls._is_quote_image_stem_match(quote, image_md5):
                logger.debug(
                    f"回复图片 md5 最终校验通过 - quote_id={quote.id}, image_path={quote.image_path}",
                    "群聊语录",
                )
                return quote

            logger.debug(
                f"回复图片 md5 最终校验未通过 - quote_id={quote.id}, image_path={quote.image_path}, image_md5={image_md5}",
                "群聊语录",
            )
        return None

    @classmethod
    async def find_quote_by_reply_image(
        cls,
        group_id: str,
        reply_image_md5: str | None = None,
        reply_image_basename: str | None = None,
        *,
        recycled_only: bool = False,
    ) -> Quote | None:
        """根据回复中的图片标识查找语录，优先走 md5 链路，失败后回退 basename。"""
        try:
            quote_query = cls.recycled_quotes if recycled_only else cls.active_quotes
            if reply_image_md5:
                logger.info(
                    f"根据回复图片 md5 查找语录 - 群组: {group_id}, md5: {reply_image_md5}",
                    "群聊语录",
                )

                exact_candidates = await quote_query(
                    group_id=group_id,
                    image_path__iendswith=f"{reply_image_md5}.png",
                ).limit(10)
                if exact_candidates:
                    logger.debug(
                        f"回复图片 md5 首选粗筛命中 {len(exact_candidates)} 条候选",
                        "群聊语录",
                    )
                    if quote := cls._pick_quote_by_image_md5(
                        exact_candidates, reply_image_md5
                    ):
                        return quote
                else:
                    logger.debug("回复图片 md5 首选粗筛未命中", "群聊语录")

                fuzzy_candidates = await quote_query(
                    group_id=group_id,
                    image_path__icontains=reply_image_md5,
                ).limit(10)
                if fuzzy_candidates:
                    logger.debug(
                        f"回复图片 md5 兼容粗筛命中 {len(fuzzy_candidates)} 条候选",
                        "群聊语录",
                    )
                    if quote := cls._pick_quote_by_image_md5(
                        fuzzy_candidates, reply_image_md5
                    ):
                        return quote
                else:
                    logger.debug("回复图片 md5 兼容粗筛未命中", "群聊语录")

            if reply_image_basename:
                logger.debug(
                    f"回复图片查找回退到 basename 链路 - 群组: {group_id}, basename: {reply_image_basename}",
                    "群聊语录",
                )
                if recycled_only:
                    return await cls.find_quote_by_basename(
                        group_id,
                        reply_image_basename,
                        recycled_only=True,
                    )
                return await cls.find_quote_by_basename(group_id, reply_image_basename)

            logger.info(
                f"回复图片未提供可用标识，无法在群组 {group_id} 中定位语录。",
                "群聊语录",
            )
            return None
        except Exception as e:
            logger.error(
                f"根据回复图片查找语录时发生错误 - 群组: {group_id}, md5: {reply_image_md5}, basename: {reply_image_basename}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return None

    @staticmethod
    async def get_last_quote(group_id: str) -> Quote | None:
        """获取群组内最后保存的一条语录"""
        try:
            return (
                await QuoteService.active_quotes(group_id=group_id)
                .order_by("-id")
                .first()
            )
        except Exception:
            return None

    @classmethod
    async def get_quote_by_history_index(
        cls,
        group_id: str,
        history_index: int,
        keyword: str | None = None,
        user_id_filter: str | None = None,
        *,
        tag_only: bool = False,
    ) -> Quote | None:
        """按入库倒序获取第 N 条语录，可复用现有关键词与用户过滤语义。"""
        index = max(1, int(history_index))
        normalized_keyword = (keyword or "").strip()

        try:
            if normalized_keyword:
                quotes = await cls.search_quotes(
                    group_id,
                    normalized_keyword,
                    user_id_filter,
                    tag_only=tag_only,
                )
            elif user_id_filter:
                candidate_ids = await cls.get_quote_ids_for_user(
                    group_id, user_id_filter
                )
                quotes = await cls.get_quotes_by_ids_in_order(candidate_ids)
            else:
                return (
                    await cls.active_quotes(group_id=group_id)
                    .order_by("-id")
                    .offset(index - 1)
                    .first()
                )

            quotes.sort(key=lambda quote: quote.id, reverse=True)
            if len(quotes) < index:
                return None
            return quotes[index - 1]
        except Exception as e:
            logger.error(
                f"按倒序索引获取语录失败 - 群组: {group_id}, "
                f"索引: {history_index}, 关键词: {normalized_keyword}, "
                f"用户筛选: {user_id_filter}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return None

    @staticmethod
    async def get_all_quotes() -> list[Quote]:
        """获取所有语录"""
        try:
            logger.info("开始获取所有语录", "群聊语录")
            quotes = await QuoteService.active_quotes()
            logger.info(f"获取所有语录成功 - 总数: {len(quotes)}", "群聊语录")
            return quotes
        except Exception as e:
            logger.error(f"获取所有语录失败 - 错误: {e}", "群聊语录", e=e)
            return []

    @staticmethod
    async def add_tags(quote: Quote, tags: list[str]) -> bool:
        """为语录添加标签"""
        result = await QuoteService.add_manual_tags(quote, tags)
        return result.success and (
            bool(result.changed_tags) or not QuoteService.normalize_tags(tags)
        )

    @classmethod
    async def add_manual_tags(cls, quote: Quote, tags: list[str]) -> TagMutationResult:
        """为语录添加统一标签；方法名保留用于兼容旧调用方。"""
        original_tags = await cls.get_quote_tags(quote)
        try:
            normalized_tags = cls.normalize_tags(tags)
            logger.info(
                f"为语录 ID: {quote.id} 添加 tag: {normalized_tags}", "群聊语录"
            )

            async with in_transaction() as connection:
                # 锁内重新读取，避免并发请求都基于同一个旧列表整体覆盖。
                locked_quote = (
                    await cls.active_quotes(id=quote.id)
                    .select_for_update()
                    .using_db(connection)
                    .get()
                )
                if not hasattr(locked_quote, "tag_schema_version"):
                    current_tags = cls.get_manual_tags(locked_quote)
                    updated_tags = cls.normalize_tags(current_tags + normalized_tags)
                    changed_tags = [
                        tag for tag in updated_tags if tag not in current_tags
                    ]
                    locked_quote.manual_tags = updated_tags
                    await locked_quote.save(
                        using_db=connection,
                        update_fields=["manual_tags"],
                    )
                else:
                    current_tags = await cls._migrate_locked_quote(
                        locked_quote, connection
                    )
                    updated_tags = cls.normalize_tags(current_tags + normalized_tags)
                    changed_tags = [
                        tag for tag in updated_tags if tag not in current_tags
                    ]
                    for value in changed_tags:
                        tag = await cls._get_or_create_tag(
                            str(locked_quote.group_id),
                            value,
                            connection=connection,
                        )
                        await QuoteTagLink.get_or_create(
                            quote_id=locked_quote.id,
                            tag_id=tag.id,
                            using_db=connection,
                        )
                    locked_quote.tags = []
                    locked_quote.manual_tags = updated_tags
                    locked_quote.tag_schema_version = cls._tag_schema_version
                    await locked_quote.save(
                        using_db=connection,
                        update_fields=[
                            "tags",
                            "manual_tags",
                            "tag_schema_version",
                        ],
                    )

            quote.manual_tags = updated_tags
            if hasattr(quote, "tags"):
                quote.tags = []
            if hasattr(quote, "tag_schema_version"):
                quote.tag_schema_version = cls._tag_schema_version

            logger.info(
                f"语录 ID: {quote.id} tag 更新成功，现有标签: {updated_tags}",
                "群聊语录",
            )
            return TagMutationResult(True, updated_tags, changed_tags)
        except Exception as e:
            logger.error(
                f"为语录添加 tag 失败 - 语录 ID: {quote.id}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return TagMutationResult(False, original_tags, [])

    @staticmethod
    async def delete_tags(quote: Quote, tags: list[str]) -> bool:
        """删除语录的标签"""
        result = await QuoteService.delete_manual_tags(quote, tags)
        return result.success and (
            bool(result.changed_tags) or not QuoteService.normalize_tags(tags)
        )

    @classmethod
    async def delete_manual_tags(
        cls, quote: Quote, tags: list[str]
    ) -> TagMutationResult:
        """删除语录统一标签；互通成员按整组从当前语录移除。"""
        original_tags = await cls.get_quote_tags(quote)
        try:
            remove_values = cls.normalize_tags(tags)
            remove_normalized = {
                cls.normalize_tag_value(value) for value in remove_values
            }
            logger.info(
                f"从语录 ID: {quote.id} 删除 tag: {remove_values}", "群聊语录"
            )
            async with in_transaction() as connection:
                # 删除也必须基于锁内最新值，否则会把同时新增的 tag 一并覆盖掉。
                locked_quote = (
                    await cls.active_quotes(id=quote.id)
                    .select_for_update()
                    .using_db(connection)
                    .get()
                )
                if not hasattr(locked_quote, "tag_schema_version"):
                    current_tags = cls.get_manual_tags(locked_quote)
                    updated_tags = [
                        tag
                        for tag in current_tags
                        if cls.normalize_tag_value(tag) not in remove_normalized
                    ]
                    changed_tags = [
                        tag
                        for tag in current_tags
                        if cls.normalize_tag_value(tag) in remove_normalized
                    ]
                    locked_quote.manual_tags = updated_tags
                    await locked_quote.save(
                        using_db=connection,
                        update_fields=["manual_tags"],
                    )
                else:
                    current_tags = await cls._migrate_locked_quote(
                        locked_quote, connection
                    )
                    requested_tags = list(
                        await QuoteTag.filter(
                            group_id=str(locked_quote.group_id),
                            normalized_value__in=list(remove_normalized),
                        ).using_db(connection)
                    )
                    alias_keys = {
                        tag.alias_group_key
                        for tag in requested_tags
                        if tag.alias_group_key
                    }
                    removable_tag_ids = {tag.id for tag in requested_tags}
                    if alias_keys:
                        removable_tag_ids.update(
                            await QuoteTag.filter(
                                group_id=str(locked_quote.group_id),
                                alias_group_key__in=list(alias_keys),
                            )
                            .using_db(connection)
                            .values_list("id", flat=True)
                        )
                    linked_removals = list(
                        await QuoteTag.filter(
                            quote_links__quote_id=locked_quote.id,
                            id__in=list(removable_tag_ids),
                        )
                        .using_db(connection)
                        .values("id", "value")
                    )
                    changed_tags = [item["value"] for item in linked_removals]
                    if removable_tag_ids:
                        await QuoteTagLink.filter(
                            quote_id=locked_quote.id,
                            tag_id__in=list(removable_tag_ids),
                        ).using_db(connection).delete()
                    changed_normalized = {
                        cls.normalize_tag_value(value) for value in changed_tags
                    }
                    updated_tags = [
                        tag
                        for tag in current_tags
                        if cls.normalize_tag_value(tag) not in changed_normalized
                    ]
                    locked_quote.tags = []
                    locked_quote.manual_tags = updated_tags
                    locked_quote.tag_schema_version = cls._tag_schema_version
                    await locked_quote.save(
                        using_db=connection,
                        update_fields=[
                            "tags",
                            "manual_tags",
                            "tag_schema_version",
                        ],
                    )

            quote.manual_tags = updated_tags
            if hasattr(quote, "tags"):
                quote.tags = []
            if hasattr(quote, "tag_schema_version"):
                quote.tag_schema_version = cls._tag_schema_version

            logger.info(
                f"语录 ID: {quote.id} tag 删除成功，现有标签: {updated_tags}",
                "群聊语录",
            )
            return TagMutationResult(True, updated_tags, changed_tags)
        except Exception as e:
            logger.error(
                f"删除语录 tag 失败 - 语录 ID: {quote.id}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return TagMutationResult(False, original_tags, [])

    @classmethod
    def matches_deletion_keywords(cls, quote: Quote, keywords: list[str]) -> bool:
        """按普通查询相同的文本和 tag 范围匹配任一删除关键词。"""
        return any(
            keyword.strip() and cls._check_single_keyword_in_quote(keyword, quote)
            for keyword in keywords
        )

    @classmethod
    def _select_ids_without_record(
        cls, memory_key: str, candidate_ids: list[int], limit: int
    ) -> list[int]:
        """按 unseen-first 规则挑选 ID，但把写入历史延后到真正发送成功之后。"""
        if not candidate_ids or limit <= 0:
            return []

        recent_ids = cls._recent_quotes.get(memory_key) or []
        unseen_ids = [quote_id for quote_id in candidate_ids if quote_id not in recent_ids]
        selected_ids = list(unseen_ids) if unseen_ids else list(candidate_ids)
        random.shuffle(selected_ids)
        selected_ids = selected_ids[:limit]

        if len(selected_ids) < limit:
            # 当未看过的候选不足时再回填历史记录内的语录，
            # 这样一次多张请求也尽量保持“先展示没发过的内容”。
            fallback_ids = [
                quote_id for quote_id in candidate_ids if quote_id not in selected_ids
            ]
            random.shuffle(fallback_ids)
            selected_ids.extend(fallback_ids[: limit - len(selected_ids)])

        return selected_ids

    @classmethod
    def select_quote_ids_without_record(
        cls, memory_key: str, candidate_ids: list[int], limit: int
    ) -> list[int]:
        """为随机多张路径挑选 ID，避免把整群 ORM 实体一次性加载到内存。"""
        return cls._select_ids_without_record(memory_key, candidate_ids, limit)

    @classmethod
    def select_quotes_without_record(
        cls, memory_key: str, quotes: list[Quote], limit: int
    ) -> list[Quote]:
        """在完成文件校验前先挑选候选，避免坏文件污染最近记录。"""
        if not quotes or limit <= 0:
            return []

        quote_by_id = {quote.id: quote for quote in quotes}
        candidate_ids = [quote.id for quote in quotes]
        selected_ids = cls._select_ids_without_record(memory_key, candidate_ids, limit)
        return [quote_by_id[quote_id] for quote_id in selected_ids if quote_id in quote_by_id]

    @classmethod
    def record_recent_quote_ids(cls, memory_key: str, quote_ids: list[int]) -> None:
        """只有真正发送成功的语录才写入去重历史，避免无效记录污染最近窗口。"""
        if not quote_ids:
            return

        history = list(cls._recent_quotes.get(memory_key) or [])
        history.extend(quote_ids)
        # 多张请求会一次写入多个 ID，这里统一只保留最近窗口，
        # 防止一次 10 连立刻把同 key 的去重历史撑爆。
        cls._recent_quotes[memory_key] = history[-cls._max_history_per_key :]

    @classmethod
    def _select_and_record_quote(cls, memory_key: str, quotes: list[Quote]) -> Quote:
        """选择并记录单条语录。"""
        if not quotes:
            raise ValueError("语录列表为空")

        selected_quotes = cls.select_quotes_without_record(memory_key, quotes, 1)
        if not selected_quotes:
            raise ValueError("语录列表为空")
        selected_quote = selected_quotes[0]
        cls.record_recent_quote_ids(memory_key, [selected_quote.id])
        return selected_quote

    @classmethod
    async def increment_view_counts(cls, quote_ids: list[int]) -> None:
        """批量增加查看次数，避免多图发送时逐条 UPDATE 放大写压力。"""
        if not quote_ids:
            return

        try:
            unique_ids = list(dict.fromkeys(quote_ids))
            await cls.active_quotes(id__in=unique_ids).update(
                view_count=F("view_count") + 1
            )
            logger.debug(
                f"批量增加语录查看次数成功 - IDs: {unique_ids}",
                "群聊语录",
            )
        except Exception as e:
            logger.error(
                f"批量增加语录查看次数失败 - IDs: {quote_ids}, 错误: {e}",
                "群聊语录",
                e=e,
            )

    @classmethod
    async def search_quotes_for_deletion(
        cls, group_id: str, keywords: list[str] | None = None, **filters: Any
    ) -> list[Quote]:
        """根据关键词（OR逻辑）或其他条件搜索语录，用于批量删除。"""
        logger.info(
            f"开始搜索语录用于删除 - 群组: {group_id}, 关键词: {keywords}, 过滤器: {filters}",
            "群聊语录",
        )

        try:
            query = Q(group_id=group_id)

            if filters:
                for key, value in filters.items():
                    if value is not None:
                        query &= Q(**{key: value})

            candidates = await cls.active_quotes(query)
            final_matched_quotes = (
                [
                    quote
                    for quote in candidates
                    if cls.matches_deletion_keywords(quote, keywords)
                ]
                if keywords
                else list(candidates)
            )

            logger.info(
                f"找到 {len(final_matched_quotes)} 条与条件匹配的语录",
                "群聊语录",
            )
            return final_matched_quotes

        except Exception as e:
            logger.error(
                f"搜索语录用于删除时发生错误 - 群组: {group_id}, 关键词: {keywords}, "
                f"过滤器: {filters}, 错误: {e}",
                "群聊语录",
                e=e,
            )
            return []

    @staticmethod
    async def find_quotes_from_left_users(group_id: str, bot: Bot) -> list[Quote]:
        """查找指定群组中由已退群用户产生或记录的语录"""
        logger.info(f"开始查找群组 {group_id} 中已退群用户的语录", "群聊语录")
        try:
            uploaders = await QuoteService.active_quotes(
                group_id=group_id, uploader_user_id__not_isnull=True
            ).values_list("uploader_user_id", flat=True)
            quoted = await QuoteService.active_quotes(
                group_id=group_id, quoted_user_id__not_isnull=True
            ).values_list("quoted_user_id", flat=True)
            all_quote_users = set(uploaders) | set(quoted)

            current_members_info = await PlatformUtils.get_group_member_list(
                bot, group_id
            )
            current_member_ids = {
                str(member.user_id) for member in current_members_info
            }

            left_user_ids = all_quote_users - current_member_ids

            if not left_user_ids:
                logger.info(f"群组 {group_id} 中没有发现已退群用户的语录", "群聊语录")
                return []

            logger.info(
                f"在群组 {group_id} 中找到 {len(left_user_ids)} 个已退群用户留下的语录记录。"
            )

            left_user_quotes = await QuoteService.active_quotes(
                Q(group_id=group_id)
                & (
                    Q(uploader_user_id__in=list(left_user_ids))
                    | Q(quoted_user_id__in=list(left_user_ids))
                )
            ).all()

            return left_user_quotes
        except Exception as e:
            logger.error(f"查找已退群用户语录时发生错误: {e}", "群聊语录", e=e)
            return []

    @staticmethod
    async def generate_temp_quote(
        avatar_bytes: bytes,
        text: Any,
        author: str,
        variant: str | None = None,
        author_role: str | None = None,
        author_title: str | None = None,
        author_level: str | None = None,
        quoted_reply: Any = None,
    ) -> bytes:
        """生成临时语录图片"""
        try:
            logger.info(
                f"开始生成临时语录图片 - 作者: {author}, 皮肤(variant): {variant}",
                "群聊语录",
            )

            avatar_base64 = base64.b64encode(avatar_bytes).decode("utf-8")

            quote_card = QuoteCardData(
                avatar_data_url=f"data:image/png;base64,{avatar_base64}",
                text=text,
                author=author,
                author_role=author_role,
                author_title=author_title,
                author_level=author_level,
                quoted_reply=quoted_reply,
                variant=variant,
            )

            img_data = await ui.render(quote_card)

            return img_data
        except Exception as e:
            logger.error(f"生成临时语录图片失败: {e}", "群聊语录", e=e)
            raise e

    @staticmethod
    async def increment_view_count(quote_id: int) -> None:
        """增加语录的查看次数"""
        await QuoteService.increment_view_counts([quote_id])

    @staticmethod
    async def get_hottest_quotes(group_id: str, limit: int = 10) -> list[Quote]:
        """获取最热门的语录 (按查看次数)"""
        logger.debug(f"开始获取群组 {group_id} 最热门语录，数量: {limit}", "群聊语录")
        try:
            hottest_quotes = (
                await QuoteService.active_quotes(group_id=group_id)
                .order_by("-view_count")
                .limit(limit)
                .all()
            )

            logger.debug(
                f"成功获取群组 {group_id} 热门语录，共 {len(hottest_quotes)} 条",
                "群聊语录",
            )

            for i, quote in enumerate(hottest_quotes):
                quote_type = (
                    "图片语录"
                    if (
                        quote.image_path and not (quote.ocr_text or quote.recorded_text)
                    )
                    else "文本语录"
                )
                logger.debug(
                    f"热门语录 #{i + 1}: ID={quote.id}, 类型={quote_type}, 查看次数={quote.view_count}",
                    "群聊语录",
                )

            return hottest_quotes
        except Exception as e:
            logger.error(f"获取群组 {group_id} 热门语录失败: {e}", "群聊语录", e=e)
            return []

    @staticmethod
    async def generate_hottest_quotes_image(
        group_id: str, hottest_quotes: list[Quote], bot_self_id: str
    ) -> bytes | str:
        """使用HTML模板为热门语录列表生成一张汇总图片"""
        if not hottest_quotes:
            return f"群组 {group_id} 暂时没有热门语录。"

        logger.info(f"开始使用HTML模板生成群组 {group_id} 的热门语录图片", "群聊语录")

        quote_cards_data = []
        quoted_user_ids = list(
            {
                str(quote.quoted_user_id)
                for quote in hottest_quotes
                if quote.quoted_user_id
            }
        )
        user_info_by_id = {
            str(user_info.user_id): user_info
            for user_info in await GroupInfoUser.filter(
                group_id=str(group_id), user_id__in=quoted_user_ids
            )
        }

        avatar_semaphore = asyncio.Semaphore(5)

        async def load_avatar(user_id: str | None) -> str:
            if not user_id:
                return ""
            try:
                async with avatar_semaphore:
                    avatar_path = await avatar_service.get_avatar_path(
                        platform="qq", identifier=user_id
                    )
                    if not avatar_path:
                        return ""
                    async with aiofiles.open(avatar_path, "rb") as avatar_file:
                        avatar_data = await avatar_file.read()
                    return base64.b64encode(avatar_data).decode("utf-8")
            except Exception as e:
                logger.warning(f"获取用户 {user_id} 头像失败: {e}", "群聊语录")
                return ""

        avatar_values = await asyncio.gather(
            *(load_avatar(quote.quoted_user_id) for quote in hottest_quotes)
        )

        for i, (quote, avatar_base64) in enumerate(
            zip(hottest_quotes, avatar_values, strict=True)
        ):
            user_name = ""
            if quote.quoted_user_id:
                user_info = user_info_by_id.get(str(quote.quoted_user_id))
                user_name = (
                    user_info.user_name
                    if user_info and user_info.user_name
                    else quote.quoted_user_id
                )

            is_image_quote = quote.image_path and not (
                quote.ocr_text or quote.recorded_text
            )
            preview_text = quote.ocr_text or quote.recorded_text
            if is_image_quote:
                preview_text = "[图片语录]"
            elif not preview_text:
                preview_text = "[未知内容]"

            image_path = ""
            if is_image_quote:
                try:
                    absolute_path = resolve_quote_image_path(quote.image_path)
                    if os.path.exists(absolute_path):
                        image_path = f"file://{absolute_path}"
                except ValueError as path_error:
                    logger.warning(
                        f"热门语录图片路径越界 - ID: {quote.id}, "
                        f"路径: {quote.image_path}",
                        "群聊语录",
                        e=path_error,
                    )

            card_data = HotQuoteItemData(
                rank=i + 1,
                user_name=user_name,
                avatar_data_url=f"data:image/png;base64,{avatar_base64}"
                if avatar_base64
                else "",
                preview_text=preview_text,
                is_image_quote=is_image_quote,
                image_path=image_path,
                view_count=quote.view_count,
                quote_id=quote.id,
            )
            quote_cards_data.append(card_data)

        page_data = HotQuotesPageData(
            group_id=group_id,
            quotes=quote_cards_data,
        )

        try:
            pic_bytes = await ui.render(page_data)
            logger.info(f"群组 {group_id} 热门语录图片生成成功", "群聊语录")
            return pic_bytes
        except Exception as e:
            logger.error(f"使用HTML模板生成热门语录图片失败: {e}", "群聊语录", e=e)
            return "生成热门语录图片失败，请检查模板文件或日志。"

    @staticmethod
    async def get_most_prolific_uploaders(group_id: str, limit: int = 10) -> list[dict]:
        """获取最高产的语录上传用户"""
        logger.debug(f"获取群组 {group_id} 最高产上传用户，数量: {limit}", "群聊语录")
        prolific_users = (
            await QuoteService.active_quotes(
                group_id=group_id, uploader_user_id__not_isnull=True
            )
            .annotate(upload_count=Count("uploader_user_id"))
            .group_by("uploader_user_id")
            .order_by("-upload_count")
            .limit(limit)
            .values("uploader_user_id", "upload_count")
        )
        return prolific_users

    @staticmethod
    async def get_most_quoted_users(group_id: str, limit: int = 10) -> list[dict]:
        """获取被记录语录最多的用户"""
        logger.debug(f"获取群组 {group_id} 被记录最多用户，数量: {limit}", "群聊语录")
        quoted_users = (
            await QuoteService.active_quotes(
                group_id=group_id, quoted_user_id__not_isnull=True
            )
            .annotate(quote_count=Count("quoted_user_id"))
            .group_by("quoted_user_id")
            .order_by("-quote_count")
            .limit(limit)
            .values("quoted_user_id", "quote_count")
        )
        return quoted_users

    @classmethod
    async def generate_bar_chart_for_prolific_users(
        cls, group_id: str, data: list[dict], title_prefix: str
    ) -> bytes | str:
        """为高产用户生成柱状图"""
        if not data:
            return f"{title_prefix}数据为空"

        user_ids = [
            item.get("uploader_user_id") or item.get("quoted_user_id") for item in data
        ]
        counts = [item.get("upload_count") or item.get("quote_count") for item in data]

        valid_user_ids = [str(uid) for uid in user_ids if uid]
        user_info_by_id = {
            str(user_info.user_id): user_info
            for user_info in await GroupInfoUser.filter(
                group_id=str(group_id), user_id__in=valid_user_ids
            )
        }
        user_names = [
            "未知用户"
            if not uid
            else (
                user_info_by_id[str(uid)].user_name
                if str(uid) in user_info_by_id
                and user_info_by_id[str(uid)].user_name
                else str(uid)
            )
            for uid in user_ids
        ]

        user_names.reverse()
        counts.reverse()

        barh_data = Barh(
            category_data=user_names,
            data=counts,  # type: ignore
            title=f"群组 {group_id} {title_prefix}排行",
        )
        try:
            chart_image = await ChartUtils.barh(barh_data)
            return chart_image  # type: ignore
        except Exception as e:
            logger.error(f"生成 {title_prefix} 图表失败: {e}", "群聊语录", e=e)
            return f"生成 {title_prefix} 图表失败: {e}"

    @staticmethod
    def cut_sentence(text: str | None) -> list[str]:
        """使用 pkuseg 对文本进行分词，并去除标点符号等无用词"""
        if not text:
            logger.debug("分词文本为空", "群聊语录")
            return []

        cut_words = seg.cut(text)
        cut_words_list: list[str] = list(set(str(word) for word in cut_words))

        punctuation = ".,!?:;。，！？：；%$\n []()（）《》<>「」'''-_+=*&^#@~`"
        stopwords = [
            "的",
            "了",
            "是",
            "在",
            "我",
            "有",
            "和",
            "就",
            "不",
            "人",
            "都",
            "一",
            "一个",
            "上",
            "也",
            "很",
            "到",
            "说",
            "要",
            "去",
            "你",
            "会",
            "着",
            "没有",
            "看",
            "好",
            "自己",
            "这",
        ]
        remove_set = set(punctuation) | set(stopwords)

        new_words: list[str] = [
            word
            for word in cut_words_list
            if word not in remove_set and len(word.strip()) > 0
        ]

        if len(text) <= 10:
            if text.strip() and text.strip() not in remove_set:
                new_words.append(text.strip())

        return new_words
