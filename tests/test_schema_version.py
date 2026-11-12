"""库结构版本：新库直接到最新版本、重复启动不重复迁移、老库无损升级、新版本的库拒绝降级。"""

import sqlite3
from pathlib import Path

import pytest

from comicdrama.store import SCHEMA_VERSION, DramaStore


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def test_new_database_starts_at_the_current_version(tmp_path: Path):
    store = DramaStore(tmp_path / "drama.sqlite")
    store.install()
    assert store.schema_version() == SCHEMA_VERSION
    store.db.close()


def test_install_is_idempotent(tmp_path: Path):
    path = tmp_path / "drama.sqlite"
    store = DramaStore(path)
    store.install()
    store.install()
    store.db.close()
    again = DramaStore(path)
    again.install()
    assert again.schema_version() == SCHEMA_VERSION
    again.db.close()


def test_old_database_is_upgraded_without_losing_rows(tmp_path: Path):
    path = tmp_path / "drama.sqlite"
    # 引入版本号之前的老库：user_version = 0，账本表还没有 retired_cost 列。
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE drama_ledger(owner TEXT PRIMARY KEY,"
            "balance INTEGER NOT NULL,frozen INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO drama_ledger VALUES('alice', 321, 7)")
    assert _user_version(path) == 0

    store = DramaStore(path)
    store.install()
    row = store.db.execute(
        "SELECT balance,frozen,retired_cost FROM drama_ledger WHERE owner='alice'"
    ).fetchone()
    assert tuple(row) == (321, 7, 0)
    assert store.schema_version() == SCHEMA_VERSION
    store.db.close()


def test_database_from_a_newer_version_is_refused(tmp_path: Path):
    path = tmp_path / "drama.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store = DramaStore(path)
    with pytest.raises(RuntimeError, match="请升级程序"):
        store.install()
    # 拒绝时不能动库：版本号保持原样
    assert store.schema_version() == SCHEMA_VERSION + 1
    store.db.close()
