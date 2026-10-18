from __future__ import annotations

import asyncio

import nonebot
import pytest
from tortoise import Tortoise

nonebot.init()

from zhenxun.services.cache import CacheRegistry, CacheRoot, cache_config
from zhenxun.services.cache.config import CacheMode
from zhenxun.services.data_access import DataAccess

# isort: split
# 服务栈需先于 models 导入：user_console 与 services 之间存在循环导入
from zhenxun.models.user_console import UserConsole
from zhenxun.models.user_gold_log import UserGoldLog
from zhenxun.utils.enum import CacheType, GoldHandle
from zhenxun.utils.exception import InsufficientGold


@pytest.fixture
async def user_console_db(tmp_path):
    db_path = tmp_path / "user_console.sqlite3"
    await Tortoise.init(
        db_url=f"sqlite://{db_path}",
        modules={
            "models": [
                "zhenxun.models.user_console",
                "zhenxun.models.user_gold_log",
            ]
        },
        timezone="Asia/Shanghai",
    )
    await Tortoise.generate_schemas()
    UserConsole._uid_counter = None
    UserConsole._uid_lock = asyncio.Lock()
    yield
    UserConsole._uid_counter = None
    UserConsole._uid_lock = asyncio.Lock()
    await Tortoise.close_connections()


@pytest.fixture
def user_cache_memory():
    """让 UserConsole 使用进程内存缓存，避免测试读写真实 Redis"""
    cache_mode = cache_config.cache_mode
    cache_backend = CacheRoot._cache_backend
    cache_config.cache_mode = CacheMode.MEMORY
    CacheRoot._cache_backend = None
    CacheRegistry.register(CacheType.USERS, UserConsole)
    CacheRoot.enable()
    yield
    cache_config.cache_mode = cache_mode
    CacheRoot._cache_backend = cache_backend
    CacheRoot.enabled = cache_mode != CacheMode.NONE


@pytest.mark.asyncio
async def test_existing_user_does_not_consume_uid(user_console_db):
    first, created = await UserConsole.get_or_create_user("first", "qq")
    assert created is True

    for _ in range(10):
        existing, created = await UserConsole.get_or_create_user("first", "qq")
        assert created is False
        assert existing.uid == first.uid

    second, created = await UserConsole.get_or_create_user("second", "qq")
    assert created is True
    assert second.uid == first.uid + 1


@pytest.mark.asyncio
async def test_concurrent_same_user_creates_one_record(user_console_db):
    results = await asyncio.gather(
        *(UserConsole.get_or_create_user("same", "qq") for _ in range(10))
    )

    assert sum(created for _, created in results) == 1
    assert len({user.uid for user, _ in results}) == 1
    assert await UserConsole.filter(user_id="same").count() == 1


@pytest.mark.asyncio
async def test_concurrent_users_receive_unique_contiguous_uids(user_console_db):
    users = await asyncio.gather(
        *(UserConsole.get_user(f"user_{index}", "qq") for index in range(10))
    )

    assert sorted(user.uid for user in users) == list(range(1, 11))
    assert await UserConsole.all().count() == 10


@pytest.mark.asyncio
async def test_uid_collision_refreshes_and_retries(monkeypatch, user_console_db):
    await UserConsole.create(user_id="existing", platform="qq", uid=1)
    allocated_uids = iter([1, 2])
    allocation_count = 0

    async def get_new_uid(cls) -> int:
        nonlocal allocation_count
        allocation_count += 1
        return next(allocated_uids)

    monkeypatch.setattr(UserConsole, "get_new_uid", classmethod(get_new_uid))

    user, created = await UserConsole.get_or_create_user("new", "qq")

    assert created is True
    assert user.uid == 2
    assert allocation_count == 2


@pytest.mark.asyncio
async def test_add_and_reduce_gold_validate_amounts_and_write_logs(user_console_db):
    user = await UserConsole.get_user("wallet_user", "qq")
    user.gold = 100
    await user.save(update_fields=["gold"])

    await UserConsole.add_gold("wallet_user", 20, "test:add", "qq")
    await UserConsole.reduce_gold(
        "wallet_user", 30, GoldHandle.PLUGIN, "test:reduce", "qq"
    )

    await user.refresh_from_db()
    logs = await UserGoldLog.filter(user_id="wallet_user").order_by("id")
    assert user.gold == 90
    assert [(log.gold, log.handle, log.source) for log in logs] == [
        (20, GoldHandle.GET, "test:add"),
        (30, GoldHandle.PLUGIN, "test:reduce"),
    ]
    for invalid in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="增加金币数量必须为正整数"):
            await UserConsole.add_gold("wallet_user", invalid, "invalid", "qq")
        with pytest.raises(ValueError, match="扣除金币数量必须为正整数"):
            await UserConsole.reduce_gold(
                "wallet_user", invalid, GoldHandle.PLUGIN, "invalid", "qq"
            )


@pytest.mark.asyncio
async def test_reduce_gold_is_atomic_under_contention(user_console_db):
    user = await UserConsole.get_user("spender", "qq")
    user.gold = 100
    await user.save(update_fields=["gold"])

    results = await asyncio.gather(
        *(
            UserConsole.reduce_gold(
                "spender", 30, GoldHandle.PLUGIN, f"charge:{index}", "qq"
            )
            for index in range(4)
        ),
        return_exceptions=True,
    )

    await user.refresh_from_db()
    assert user.gold == 10
    assert sum(result is None for result in results) == 3
    assert sum(isinstance(result, InsufficientGold) for result in results) == 1
    assert await UserGoldLog.filter(user_id="spender").count() == 3


@pytest.mark.asyncio
async def test_gold_log_failure_rolls_back_balance(monkeypatch, user_console_db):
    user = await UserConsole.get_user("rollback", "qq")
    user.gold = 100
    await user.save(update_fields=["gold"])

    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("log failure")

    monkeypatch.setattr(UserGoldLog, "create", fail_create)

    with pytest.raises(RuntimeError, match="log failure"):
        await UserConsole.add_gold("rollback", 20, "test", "qq")
    await user.refresh_from_db()
    assert user.gold == 100

    with pytest.raises(RuntimeError, match="log failure"):
        await UserConsole.reduce_gold(
            "rollback", 20, GoldHandle.PLUGIN, "test", "qq"
        )
    await user.refresh_from_db()
    assert user.gold == 100


@pytest.mark.asyncio
async def test_transfer_log_failure_rolls_back_both_balances(
    monkeypatch, user_console_db
):
    sender = await UserConsole.get_user("sender", "qq")
    receiver = await UserConsole.get_user("receiver", "qq")
    sender.gold = 150
    receiver.gold = 20
    await UserConsole.bulk_update([sender, receiver], ["gold"])

    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("log failure")

    monkeypatch.setattr(UserGoldLog, "create", fail_create)

    with pytest.raises(RuntimeError, match="log failure"):
        await UserConsole.transfer_gold("sender", "receiver", 30, "qq")

    await sender.refresh_from_db()
    await receiver.refresh_from_db()
    assert sender.gold == 150
    assert receiver.gold == 20
    assert await UserGoldLog.all().count() == 0


@pytest.mark.asyncio
async def test_admin_gold_adjustments_allow_negative_balances(user_console_db):
    user = await UserConsole.get_user("managed", "qq")

    assert await UserConsole.set_gold(
        "managed", -50, "superuser:set:operator", "qq"
    ) == (100, -50)
    assert await UserConsole.adjust_gold(
        "managed", -25, "superuser:adjust:operator", "qq"
    ) == (-50, -75)
    assert await UserConsole.set_gold(
        "managed", -75, "superuser:set:operator", "qq"
    ) == (-75, -75)

    await user.refresh_from_db()
    logs = await UserGoldLog.filter(user_id="managed").order_by("id")
    assert user.gold == -75
    assert [(log.gold, log.handle, log.source) for log in logs] == [
        (-150, GoldHandle.ADJUST, "superuser:set:operator"),
        (-25, GoldHandle.ADJUST, "superuser:adjust:operator"),
    ]
    with pytest.raises(ValueError, match="金币调整数量不能为 0"):
        await UserConsole.adjust_gold("managed", 0, "superuser:adjust:operator", "qq")


@pytest.mark.asyncio
async def test_gold_mutations_invalidate_auth_cache(user_cache_memory, user_console_db):
    """金币变动后鉴权路径(DataAccess 缓存)必须读到新余额"""
    dao = DataAccess(UserConsole)
    user = await UserConsole.get_user("cached", "qq")
    user.gold = 100
    await user.save(update_fields=["gold"])
    # 鉴权路径的读取会写入缓存
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 100

    # 签到 / 加金币
    await UserConsole.add_gold("cached", 20, "sign_in", "qq")
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 120

    # 插件扣费
    await UserConsole.reduce_gold("cached", 30, GoldHandle.PLUGIN, "test:cost", "qq")
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 90

    # 钱包转账：转出与转入双方都要失效
    payer = await UserConsole.get_user("cached_payer", "qq")
    payer.gold = 50
    await payer.save(update_fields=["gold"])
    assert (await dao.safe_get_or_none(user_id="cached_payer")).gold == 50
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 90

    await UserConsole.transfer_gold("cached_payer", "cached", 10, "qq")
    assert (await dao.safe_get_or_none(user_id="cached_payer")).gold == 40
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 100

    # 管理员设置 / 调整
    await UserConsole.set_gold("cached", 7, "superuser:set:operator", "qq")
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 7
    await UserConsole.adjust_gold("cached", 3, "superuser:adjust:operator", "qq")
    assert (await dao.safe_get_or_none(user_id="cached")).gold == 10
