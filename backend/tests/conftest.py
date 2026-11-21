"""测试配置：每个测试前重置 store 保证隔离。"""
import pytest
from app.store import store as _store, Store


def reset_store() -> None:
    """按构造 Store 的同一口径重建全部表（含迁移内部台账）。"""
    _store._tables.clear()
    from app.seed import SEED_ROWS
    from app.seed_migration import SEED_MIGRATION_BATCHES, SEED_MIGRATION_EVENTS
    for name, rows in SEED_ROWS.items():
        _store._tables[name] = [dict(r) for r in rows]
    _store._tables["_dedup_migrations"] = [dict(r) for r in SEED_MIGRATION_BATCHES]
    _store._tables["_migration_events"] = [dict(r) for r in SEED_MIGRATION_EVENTS]
    _store._tables["_partition_switch"] = []


@pytest.fixture(autouse=True)
def fresh_store():
    reset_store()
    yield