"""T01 定价单一源回归：三处同改、按字段迁移、cap 联动、生效价下发。

每一条都对应一个真实会翻车的路径，删掉就会退回去：

- 三处价格不同步 → 在线扣 30、离线扣 20，用户断网重连看到余额跳变；
- 迁移用整组判据 → 管理员只改过 cap 的行被漏掉，售价停在 10 < 实测成本 25，卖一单亏一单；
- 迁移用「值相等」判幂等 → 管理员事后把价改回 10 会被二次迁移成 30；
- `cap` 下界只校验 `cap >= t2i` → 退差为负，结算时 `_assert_conservation` 当场 409；
- `/config` 下发目录价 → 管理员把视频价改成 200，按钮还写 300，等于对用户虚报价格。

全部离线：零出网、零付费。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from comicdrama import pricing, service
from comicdrama.errors import AppError
from comicdrama.pricing import (
    ACTION_PRICES,
    DEFAULT_DAILY_CAP,
    OLD_PRICING_DEFAULTS,
    PRICE_T2I,
    PRICE_VIDEO,
    UPSTREAM_COST_T2I,
    UPSTREAM_COST_VIDEO,
    cap_of,
    default_pricing,
    effective_price,
    migrate_pricing,
    normalize_video,
    pricing_stale,
)
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService
from comicdrama.store import DEFAULT_PRICING, DramaStore

REPO = Path(__file__).resolve().parents[1]
STORE_JS = REPO / "comicdrama" / "web" / "js" / "store.js"

# 老库的真实 schema：只有 6 列，没有 migrated_from / pricing_stale。
_OLD_PRICING_DDL = (
    "CREATE TABLE drama_pricing("
    "owner TEXT PRIMARY KEY,t2i INTEGER NOT NULL,cap INTEGER NOT NULL,"
    "qps REAL NOT NULL,concurrency INTEGER NOT NULL,video TEXT)"
)


@pytest.fixture
def served(tmp_path: Path) -> DramaService:
    """一个已开户的 DramaService（本地 mock Provider，零出网）。"""
    svc = DramaService(tmp_path / "drama.sqlite", MockDramaProvider(), admins=frozenset({"local"}))
    svc.store.install()
    return svc


def _old_db(path: Path, rows: list[tuple[str, int, int, int | None]]) -> None:
    """造一个「旧版本写出来的」drama.sqlite：缺两列、价还是旧默认。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_OLD_PRICING_DDL)
        for owner, t2i, cap, video in rows:
            conn.execute(
                "INSERT INTO drama_pricing(owner,t2i,cap,qps,concurrency,video) VALUES(?,?,?,?,?,?)",
                (owner, t2i, cap, 1.0, 5, video),
            )
        conn.commit()
    finally:
        conn.close()


def _pricing_rows(path: Path) -> dict[str, tuple[int, int, int | None, str, int]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["owner"]: (
                row["t2i"],
                row["cap"],
                normalize_video(row["video"]),
                row["migrated_from"],
                row["pricing_stale"],
            )
            for row in conn.execute("SELECT * FROM drama_pricing")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 验收 1：单一源


def test_action_prices_is_one_object_in_both_modules() -> None:
    """`service.ACTION_PRICES is pricing.ACTION_PRICES`：既有 `from service import` 不破。"""
    assert pricing.ACTION_PRICES is service.ACTION_PRICES
    assert service.DEFAULT_DAILY_CAP == pricing.DEFAULT_DAILY_CAP == 500
    # 目录价本身：图生视频 300、图 30（锚点候选图与关键帧同价）。
    assert ACTION_PRICES["image-to-video"] == PRICE_VIDEO == 300
    assert ACTION_PRICES["generateAssetCandidates"] == PRICE_T2I == 30


def test_sale_price_stays_above_the_measured_upstream_cost() -> None:
    """售价必须高于实测上游成本：调价调反了要当场红，而不是等月结对账才发现。

    这条比具体数字更值钱。2026-11-11 实测文生图 25 积分/张，旧售价 20 是
    「每张亏 5」；用户裁决统一按 +20% 毛利，才改成 30 / 300。
    """
    assert PRICE_T2I > UPSTREAM_COST_T2I, "售价必须高于上游成本，否则每张都在亏"
    assert PRICE_VIDEO > UPSTREAM_COST_VIDEO


def test_cap_follows_one_point_two_rule_with_ceiling() -> None:
    """cap = ceil(售价 × 1.2)：图生视频 360、关键帧 36、小额动作保持既有上限。"""
    assert cap_of(PRICE_T2I) == 36
    assert cap_of(PRICE_VIDEO) == 360
    # 向上取整而不是地板除：3→4（既有断言）、2→3，避免把小额动作的上限降下去。
    assert cap_of(3) == 4
    assert cap_of(2) == 3
    assert service._cap_of(3) == cap_of(3)


def test_daily_cap_stays_500_and_video_needs_one_freeze() -> None:
    """日上限保持 500：一次图生视频冻结 360，所以一天只够 1 次——已接受的现状。"""
    assert DEFAULT_DAILY_CAP == 500
    assert cap_of(PRICE_VIDEO) == 360
    assert 2 * cap_of(PRICE_VIDEO) > DEFAULT_DAILY_CAP  # 第 2 次必然 409，不是 bug


# ---------------------------------------------------------------- 验收 2：迁移


@pytest.mark.parametrize(
    ("before", "expected"),
    [
        # 三列都是旧默认 → t2i / cap 各自升到新基线；video 原样写回。
        # 为什么 video 不填 300：`video` 是「该 owner 的覆盖价」，NULL = 没有覆盖、
        # 跟随目录价。写成 300 等于凭空造一条谁都没设过的 override 记录，且与
        # `_seed()` 的 NULL 变成两种表示（改目录价时这批 owner 还不跟着变）。
        # 生效价由 `effective_price()` 兜底成 300，所以保持 NULL 不等于视频免费。
        ((10, 12, None), (30, 36, None)),
        # cap 被管理员改过且已高于新默认派生值：不能跟着 t2i 一起动。
        ((10, 40, None), (30, 40, None)),
        # cap 被改到 30，低于新默认派生值 36：末尾的 max 兜底必须把它抬到 36，
        # 否则 cap_of(30)=36 > cap=30，结算退差为负会当场触发守恒断言 409。
        ((10, 30, None), (30, 36, None)),
        # t2i 被管理员改过（15）：只把 cap 抬到新默认 36。
        # 36 仍 ≥ cap_of(15)=18，不变量成立。
        ((15, 12, None), (15, 36, None)),
        # 三个字段都不是旧默认 → 一个都不动（max(30, cap_of(15)=18) 仍是 30）。
        ((15, 30, 250), (15, 30, 250)),
    ],
)
def test_migration_is_field_wise(
    tmp_path: Path, before: tuple[int, int, int | None], expected: tuple[int, int, int | None]
) -> None:
    path = tmp_path / "old.sqlite"
    _old_db(path, [("legacy", *before)])

    store = DramaStore(path)
    store.install()  # 补列 + 按字段迁移
    t2i, cap, video, marker, stale = _pricing_rows(path)["legacy"]

    assert (t2i, cap, video) == expected
    # marker 记的是**原值**快照（null 记成 "null"，不是迁移后的 300）。
    assert marker == f"{before[0]}/{before[1]}/{'null' if before[2] is None else before[2]}"
    # 不变量：任何一行都必须满足 cap >= cap_of(t2i)，否则结算退差为负会卡死。
    assert cap >= cap_of(t2i)
    assert stale == (1 if pricing_stale({"t2i": t2i, "video": video}) else 0)
    store.close()


def test_migrated_owner_keeps_null_video_but_charges_catalogue_price(tmp_path: Path) -> None:
    """老库迁移后 video 仍是 NULL，但生效价必须是 300（两者一起断言才有区分度）。"""
    path = tmp_path / "old.sqlite"
    _old_db(path, [("legacy", 10, 12, None)])
    store = DramaStore(path)
    store.install()
    # 老库的这个 owner 早已开户：账本行存在，只是计价是旧默认值。
    store.run(
        lambda db: db.execute(
            "INSERT INTO drama_ledger(owner,balance,frozen) VALUES('legacy',980,0)"
        )
    )
    state = store.state("legacy")

    assert state["pricing"]["video"] is None
    assert effective_price("image-to-video", state["pricing"]) == PRICE_VIDEO == 300
    assert state["pricing"]["migratedFrom"] == "10/12/null"
    store.close()


def test_migration_is_idempotent_and_survives_admin_editing_back(tmp_path: Path) -> None:
    """重复 install() 不改值；管理员把价改回 10 也不会被二次迁移。"""
    path = tmp_path / "old.sqlite"
    _old_db(path, [("legacy", 10, 12, None), ("other", 10, 30, None)])

    store = DramaStore(path)
    store.install()
    first = _pricing_rows(path)

    store.install()  # 幂等：第二次不改变任何值
    assert _pricing_rows(path) == first

    # 管理员事后把价改回 10（migrated_from 非空 → 迁移必须跳过这行）。
    store.run(
        lambda db: db.execute(
            "UPDATE drama_pricing SET t2i=10, cap=12, video=NULL WHERE owner='legacy'"
        )
    )
    store.install()
    assert _pricing_rows(path)["legacy"][:3] == (10, 12, None)
    store.close()


def test_fresh_owner_is_seeded_from_the_single_source(tmp_path: Path) -> None:
    """新开户直接按 pricing 默认值落库，且标记为 seed（不会被当成待迁移老行）。"""
    path = tmp_path / "new.sqlite"
    store = DramaStore(path)
    store.install()
    store.ensure_owner("alice")
    t2i, cap, video, marker, stale = _pricing_rows(path)["alice"]
    assert (t2i, cap) == (PRICE_T2I, cap_of(PRICE_T2I)) == (30, 36)
    assert video is None  # 「未单独设置」= 走目录价 300，不是 0 积分
    # 「没设」与「设成 300」必须能被区分开：字段是 NULL，生效价却是 300。
    assert effective_price("image-to-video", {"video": video}) == PRICE_VIDEO == 300
    assert marker == pricing.MIGRATED_FROM_SEED
    assert stale == 0
    assert DEFAULT_PRICING == default_pricing()
    store.close()


def test_migrate_pricing_helper_matches_store_behaviour() -> None:
    """纯函数层单独钉一遍，防止有人只改 store 里的 SQL 而漏掉判据。"""
    # video 原样写回：NULL 进、NULL 出（生效价由 effective_price 兜底成 300）。
    assert migrate_pricing(*OLD_PRICING_DEFAULTS.values(), None) == (
        30,
        36,
        None,
        "10/12/null",
    )
    # cap 永远被兜底抬到 cap_of(t2i)：手填 1 也不会产出 cap < cap_of(30)。
    assert migrate_pricing(PRICE_T2I, 1, 300)[1] == cap_of(PRICE_T2I) == 36
    # 已显式设过覆盖价的行，迁移不动它。
    assert migrate_pricing(10, 12, 250)[2] == 250


# ---------------------------------------------------------------- 验收 3：cap 校验


async def test_save_pricing_rejects_cap_below_one_point_two_times(
    served: DramaService,
) -> None:
    """`cap=20 / t2i=20` 必须 400：这不是少扣钱，是结算时守恒断言 409。"""
    with pytest.raises(AppError) as caught:
        await served.action("local", "savePricing", [{"t2i": 20, "cap": 20}], None, None)
    assert caught.value.code == "invalid_pricing"
    assert caught.value.status == 400
    # 非法那次不能留下半截写入。
    assert (await served.state("local"))["pricing"]["cap"] == cap_of(PRICE_T2I)
    # 合法组合（cap 恰好等于硬上限）必须通过。
    await served.action("local", "savePricing", [{"t2i": 20, "cap": 24}], None, None)
    assert (await served.state("local"))["pricing"]["cap"] == cap_of(20) == 24


async def test_save_pricing_defaults_cap_and_keeps_video(served: DramaService) -> None:
    """未传 cap → 跟随售价；未传 video → 保持现值，不再被硬写 None。"""
    await served.action("local", "savePricing", [{"t2i": 40, "video": 260}], None, None)
    state = await served.state("local")
    assert state["pricing"]["t2i"] == 40
    assert state["pricing"]["cap"] == cap_of(40) == 48
    assert state["pricing"]["video"] == 260
    assert state["pricing"]["stale"] is False  # 260 ≥ 上游成本 250

    # 只改 qps：video 必须还在（旧实现会在这里抹成 None）。
    await served.action("local", "savePricing", [{"t2i": 40, "cap": 48, "qps": 2}], None, None)
    state = await served.state("local")
    assert state["pricing"]["video"] == 260
    assert state["pricing"]["qps"] == 2

    # 把图生视频降到成本线以下：只置 stale 标记，不阻断保存。
    await served.action("local", "savePricing", [{"t2i": 40, "cap": 48, "video": 200}], None, None)
    state = await served.state("local")
    assert state["pricing"]["video"] == 200
    assert state["pricing"]["stale"] is True

    # 显式传 null 才清空 → 回落到目录价。
    await served.action("local", "savePricing", [{"t2i": 40, "cap": 48, "video": None}], None, None)
    state = await served.state("local")
    assert state["pricing"]["video"] is None
    assert effective_price("image-to-video", state["pricing"]) == PRICE_VIDEO


async def test_save_pricing_rejects_illegal_video(served: DramaService) -> None:
    with pytest.raises(AppError) as caught:
        await served.action("local", "savePricing", [{"t2i": 20, "video": 0}], None, None)
    assert caught.value.code == "invalid_field"


# ---------------------------------------------------------------- 验收 4：生效价


@pytest.mark.parametrize(
    ("video", "expected"),
    [
        (200, 200),  # 管理员改价生效
        (None, 300),  # 未设置 → 目录价
        ("300", 300),  # TEXT affinity 读回来的字符串形态（200 的情况见下条）
        ("200", 200),  # 字符串数字必须被认出来，否则改价静默失效
        (True, 300),  # bool 是 int 子类，但不能当 1 积分卖
        (0, 300),
        (-5, 300),
        ("abc", 300),
    ],
)
def test_effective_price_video_override(video: object, expected: int) -> None:
    assert effective_price("image-to-video", {"video": video}) == expected


def test_effective_price_keeps_table_semantics(served: DramaService) -> None:
    # 表里没有的动作 → None → 不调上游、不扣费（既有语义不能变）。
    assert effective_price("no-such-action", {"video": 200}) is None
    # 非视频动作不受 video 覆盖影响。
    assert effective_price("tts-preview", {"video": 200}) == ACTION_PRICES["tts-preview"]
    assert served._effective_price("image-to-video", {"pricing": {"video": 200}}) == 200
    assert served._effective_price("image-to-video", {"pricing": {"video": None}}) == 300


# ---------------------------------------------------------------- 验收 5：三处一致


def test_three_price_places_share_the_same_numbers() -> None:
    """后端扣费表 / 后端落库默认 / 前端离线默认，三处必须是同一组数字。"""
    source = STORE_JS.read_text(encoding="utf-8")
    found = re.search(r"A\.DEFAULT_PRICING\s*=\s*\{([^}]*)\}", source)
    assert found is not None, "store.js 里找不到 A.DEFAULT_PRICING"
    body = found.group(1)
    fields = dict(
        (key.strip(), value.strip())
        for key, value in (pair.split(":") for pair in body.split(",") if pair.strip())
    )
    assert int(fields["t2i"]) == ACTION_PRICES["generateAssetCandidates"] == PRICE_T2I
    assert int(fields["cap"]) == cap_of(PRICE_T2I) == default_pricing()["cap"]
    assert fields["video"] == "null"  # 「未单独设置」，走目录价
    assert int(fields["concurrency"]) == default_pricing()["concurrency"]


# ---------------------------------------------------------------- /config 下发


async def test_config_reports_effective_prices_and_keyframe_fields(
    served: DramaService,
) -> None:
    cfg = await served.config("local")
    assert cfg["prices"]["image-to-video"] == PRICE_VIDEO == 300
    assert cfg["prices"]["generateAssetCandidates"] == PRICE_T2I
    assert cfg["keyframe_price"] == PRICE_T2I
    assert cfg["keyframe_cap"] == cap_of(PRICE_T2I) == 36
    assert cfg["daily_cap"] == 500
    assert cfg["pricing_stale"] is False

    # 管理员改价后，按钮价必须立刻跟着变（否则等于对用户虚报价格）。
    await served.action("local", "savePricing", [{"t2i": 20, "video": 200}], None, None)
    cfg = await served.config("local")
    assert cfg["prices"]["image-to-video"] == 200


async def test_dispatch_charges_the_effective_price(served: DramaService) -> None:
    """改价后扣费与预占都要用生效价，cap 由 cap_of(生效价) 派生。"""
    await served.action("local", "savePricing", [{"t2i": 20, "video": 200}], None, None)
    body = await served.action("local", "script-doctor", [], None, None)
    charge = body["state"]["charges"][-1]
    assert (charge["action"], charge["price"], charge["cap"]) == (
        "script-doctor",
        ACTION_PRICES["script-doctor"],
        cap_of(ACTION_PRICES["script-doctor"]),
    )


# ---------------------------------------------------------------- stale 标记


@pytest.mark.parametrize(
    ("pricing_dict", "expected"),
    [
        ({"t2i": PRICE_T2I, "video": None}, False),
        ({"t2i": UPSTREAM_COST_T2I, "video": None}, False),
        ({"t2i": UPSTREAM_COST_T2I - 1, "video": None}, True),
        ({"t2i": PRICE_T2I, "video": UPSTREAM_COST_VIDEO - 1}, True),
        ({"t2i": PRICE_T2I, "video": UPSTREAM_COST_VIDEO}, False),
    ],
)
def test_pricing_stale_is_a_read_only_flag(pricing_dict: dict[str, Any], expected: bool) -> None:
    assert pricing_stale(pricing_dict) is expected


def test_pricing_module_does_not_import_drama_modules() -> None:
    """pricing 必须是叶子：import 任何 drama 模块都会让 store -> service 成环。"""
    source = (REPO / "comicdrama" / "pricing.py").read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("from .", "from comicdrama", "import comicdrama"))
    ]
    assert imports == [], f"pricing.py 不能 import drama 模块：{imports}"


async def test_state_pricing_document_is_json_serialisable(served: DramaService) -> None:
    state = await served.state("local")
    json.dumps(state["pricing"])  # 不能有 sqlite Row 之类的非 JSON 类型
