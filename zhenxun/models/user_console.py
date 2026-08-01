import asyncio
from typing import ClassVar

from tortoise import fields
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from zhenxun.services.db_context import Model
from zhenxun.utils.enum import CacheType, GoldHandle
from zhenxun.utils.exception import InsufficientGold

from .user_gold_log import UserGoldLog


class UserConsole(Model):
    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """自增id"""
    user_id = fields.CharField(255, unique=True, description="用户id")
    """用户id"""
    uid = fields.IntField(description="UID", unique=True)
    """UID"""
    gold = fields.IntField(default=100, description="金币数量")
    """金币数量"""
    sign = fields.ReverseRelation["SignUser"]  # type: ignore
    """好感度"""
    props: dict[str, int] = fields.JSONField(default={})  # type: ignore
    """历史道具数据，运行时不再读写"""
    platform = fields.CharField(255, null=True, description="平台")
    """平台"""
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    """创建时间"""

    class Meta:  # pyright: ignore [reportIncompatibleVariableOverride]
        table = "user_console"
        table_description = "用户数据表"
        indexes = [("user_id",), ("uid",)]  # noqa: RUF012

    cache_type = CacheType.USERS
    """缓存类型"""
    cache_key_field = "user_id"
    """缓存键字段"""

    _uid_counter: ClassVar[int | None] = None
    _uid_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @staticmethod
    def _validate_integer(value: int, message: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(message)

    @classmethod
    async def get_or_create_user(
        cls, user_id: str, platform: str | None = None
    ) -> tuple["UserConsole", bool]:
        if user := await cls.get_or_none(user_id=user_id):
            return user, False

        for attempt in range(3):
            try:
                user = await cls.create(
                    user_id=user_id,
                    platform=platform,
                    uid=await cls.get_new_uid(),
                )
            except IntegrityError:
                if user := await cls.get_or_none(user_id=user_id):
                    return user, False
                async with cls._uid_lock:
                    cls._uid_counter = None
                if attempt >= 2:
                    raise
            else:
                return user, True
        raise RuntimeError("unreachable")

    @classmethod
    async def get_user(cls, user_id: str, platform: str | None = None) -> "UserConsole":
        """获取用户

        参数:
            user_id: 用户id
            platform: 平台.

        返回:
            UserConsole: UserConsole
        """
        user, _ = await cls.get_or_create_user(user_id=user_id, platform=platform)
        return user

    @classmethod
    async def get_new_uid(cls) -> int:
        """获取最新uid

        返回:
            int: 最新uid
        """
        async with cls._uid_lock:
            if cls._uid_counter is None:
                user = await cls.annotate().order_by("-uid").first()
                cls._uid_counter = user.uid if user else 0
            cls._uid_counter += 1
            return cls._uid_counter

    @classmethod
    async def add_gold(
        cls, user_id: str, gold: int, source: str, platform: str | None = None
    ):
        """添加金币

        参数:
            user_id: 用户id
            gold: 金币
            source: 来源
            platform: 平台.
        """
        cls._validate_integer(gold, "增加金币数量必须为正整数")
        if gold <= 0:
            raise ValueError("增加金币数量必须为正整数")
        await cls.get_or_create_user(user_id=user_id, platform=platform)
        async with in_transaction() as connection:
            updated = await cls.filter(user_id=user_id).using_db(connection).update(
                gold=F("gold") + gold
            )
            if not updated:
                raise RuntimeError(f"金币账户不存在: {user_id}")
            await UserGoldLog.create(
                using_db=connection,
                user_id=user_id,
                gold=gold,
                handle=GoldHandle.GET,
                source=source,
            )

    @classmethod
    async def reduce_gold(
        cls,
        user_id: str,
        gold: int,
        handle: GoldHandle,
        plugin_module: str,
        platform: str | None = None,
    ):
        """消耗金币

        参数:
            user_id: 用户id
            gold: 金币
            handle: 金币处理
            plugin_name: 插件模块
            platform: 平台.

        异常:
            InsufficientGold: 金币不足
        """
        cls._validate_integer(gold, "扣除金币数量必须为正整数")
        if gold <= 0:
            raise ValueError("扣除金币数量必须为正整数")
        await cls.get_or_create_user(user_id=user_id, platform=platform)
        async with in_transaction() as connection:
            updated = (
                await cls.filter(user_id=user_id, gold__gte=gold)
                .using_db(connection)
                .update(gold=F("gold") - gold)
            )
            if not updated:
                raise InsufficientGold()
            await UserGoldLog.create(
                using_db=connection,
                user_id=user_id,
                gold=gold,
                handle=handle,
                source=plugin_module,
            )

    @classmethod
    async def set_gold(
        cls,
        user_id: str,
        balance: int,
        source: str,
        platform: str | None = None,
    ) -> tuple[int, int]:
        """设置金币余额并记录调整流水。"""
        cls._validate_integer(balance, "金币余额必须为整数")
        await cls.get_or_create_user(user_id=user_id, platform=platform)
        async with in_transaction() as connection:
            user = (
                await cls.filter(user_id=user_id)
                .using_db(connection)
                .select_for_update()
                .get()
            )
            old_balance = user.gold
            if old_balance == balance:
                return old_balance, balance
            user.gold = balance
            await user.save(using_db=connection, update_fields=["gold"])
            await UserGoldLog.create(
                using_db=connection,
                user_id=user_id,
                gold=balance - old_balance,
                handle=GoldHandle.ADJUST,
                source=source,
            )
            return old_balance, balance

    @classmethod
    async def adjust_gold(
        cls,
        user_id: str,
        delta: int,
        source: str,
        platform: str | None = None,
    ) -> tuple[int, int]:
        """按差额调整金币余额并记录流水。"""
        cls._validate_integer(delta, "金币调整数量必须为整数")
        if delta == 0:
            raise ValueError("金币调整数量不能为 0")
        await cls.get_or_create_user(user_id=user_id, platform=platform)
        async with in_transaction() as connection:
            user = (
                await cls.filter(user_id=user_id)
                .using_db(connection)
                .select_for_update()
                .get()
            )
            old_balance = user.gold
            new_balance = old_balance + delta
            user.gold = new_balance
            await user.save(using_db=connection, update_fields=["gold"])
            await UserGoldLog.create(
                using_db=connection,
                user_id=user_id,
                gold=delta,
                handle=GoldHandle.ADJUST,
                source=source,
            )
            return old_balance, new_balance

    @classmethod
    async def transfer_gold(
        cls,
        from_user_id: str,
        to_user_id: str,
        gold: int,
        platform: str | None = None,
    ):
        """转账金币

        参数:
            from_user_id: 转出用户id
            to_user_id: 转入用户id
            gold: 金币
            platform: 平台.

        异常:
            ValueError: 金币数量非法或转账双方相同
            InsufficientGold: 金币不足
        """
        cls._validate_integer(gold, "转账金额必须为正整数")
        if gold <= 0:
            raise ValueError("转账金额必须为正整数")
        if from_user_id == to_user_id:
            raise ValueError("不能给自己转账")

        # 先确保双方用户存在，再在同一个事务内锁定两行，避免一边扣款成功另一边入账失败。
        await cls.get_or_create_user(from_user_id, platform)
        await cls.get_or_create_user(to_user_id, platform)
        async with in_transaction() as connection:
            # 固定锁顺序，降低两个用户互相转账时数据库死锁的概率。
            locked_users = {}
            for user_id in sorted((from_user_id, to_user_id)):
                locked_users[user_id] = (
                    await cls.filter(user_id=user_id)
                    .using_db(connection)
                    .select_for_update()
                    .get()
                )
            from_user = locked_users[from_user_id]
            to_user = locked_users[to_user_id]
            if from_user.gold < gold:
                raise InsufficientGold()

            from_user.gold -= gold
            to_user.gold += gold
            await from_user.save(using_db=connection, update_fields=["gold"])
            await to_user.save(using_db=connection, update_fields=["gold"])
            await UserGoldLog.create(
                using_db=connection,
                user_id=from_user_id,
                gold=gold,
                handle=GoldHandle.TRANSFER_OUT,
                source=f"wallet:{to_user_id}",
            )
            await UserGoldLog.create(
                using_db=connection,
                user_id=to_user_id,
                gold=gold,
                handle=GoldHandle.TRANSFER_IN,
                source=f"wallet:{from_user_id}",
            )

    @classmethod
    async def _run_script(cls):
        return [
            "CREATE INDEX idx_user_console_user_id ON user_console(user_id);",
            "CREATE INDEX idx_user_console_uid ON user_console(uid);",
        ]
