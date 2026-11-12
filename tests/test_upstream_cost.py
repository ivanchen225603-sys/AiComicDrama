"""上游成本与售价分离（T02）：两张表各记「上游真值」，NULL 表示未知。

三条不可动摇的口径：

1. **售价侧不动。** `cost` 仍是向用户扣的积分（i2v 300 / t2i 30）；
   `upstream_points` 是上游实际扣的积分。两者分开记，毛利读时算。
2. **NULL = 未知，绝不用 0 顶替。** 老库、mock、上游没回传都必须是 NULL。
   用 0 会把「未知」和「免费」混为一谈，对账时毛利凭空变高。
3. **不存 `margin_points`。** 它是 `cost - upstream_points` 的纯函数，
   存了会在改价后失真；只在报表里算。

另有一条语义必须保留：**「上游已扣但本地结算失败」**（结果未知 → 待复核，`cost=0`
但 `upstream_points` 有值）。这不是脏数据，而是管理员判断「该退款还是该结算」的
唯一依据，不允许为了「对齐」把它抹平。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.media_store import DramaMediaStore
from comicdrama.pricing import PRICE_T2I, PRICE_VIDEO, UPSTREAM_COST_T2I
from comicdrama.providers.remote import RemoteDramaConfig, RemoteDramaProvider
from comicdrama.reconcile import reconcile_doc
from comicdrama.service import DramaService
from comicdrama.store import DramaStore

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _remote(tmp_path: Path) -> RemoteDramaProvider:
    """工作流通道的真实 Provider：不需要公网基址，方便测「每条付费路径都记成本」。"""
    config = RemoteDramaConfig(
        llm_base_url="http://127.0.0.1/v1",
        llm_model="m",
        t2i_template_uuid="template-t2i",
        t2i_prompt_node="node-1",
        i2v_template_uuid="template-i2v",
        i2v_prompt_node="node-2",
    )
    return RemoteDramaProvider(config, DramaMediaStore(tmp_path / "media"))


def _service(tmp_path: Path) -> DramaService:
    service = DramaService(tmp_path / "drama.sqlite", admins=frozenset({"local"}), autopump=False)
    service.store.install()
    return service


async def _act(service: DramaService, name: str, *args: Any, pid: str | None = None) -> Any:
    return await service.action("local", name, list(args), pid, None)


async def _first_shot(service: DramaService) -> tuple[str, str]:
    state = await service.state("local")
    project = state["projects"][0]
    return str(project["id"]), str(project["shots"][0]["id"])


def _drama(state: dict[str, Any]) -> dict[str, Any]:
    return reconcile_doc("local", state)["drama"]


# ---------------------------------------------------------------- 1. mock：全链路 NULL


@pytest.mark.needs_ffmpeg
async def test_mock_records_no_upstream_cost_anywhere(tmp_path: Path) -> None:
    """mock 不花上游钱，两列必须全是 NULL（不是 0），报表把笔数记为「未知」。"""
    service = _service(tmp_path)
    await _act(service, "adminCredit", 5000, "离线", "req-mock-credit")
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)
    await _act(service, "submitTasks", [shot], pid=pid)
    await _act(service, "tick", pid=pid)

    state = await service.state("local")
    charges = [c for c in state["charges"] if c["state"] == "settled"]
    assert charges, "至少要有一次已结算动作"
    assert all(c["upstreamPoints"] is None for c in charges)
    assert all(c["upstreamBalance"] is None for c in charges)
    settled_tasks = [t for t in state["tasks"] if t["settled"]]
    assert settled_tasks
    assert all(t["upstreamPoints"] is None for t in settled_tasks)

    drama = _drama(state)
    assert drama["unknownUpstreamCount"] == len(charges), "NULL 一律计入「未知」"
    assert drama["upstreamPointsTotal"] == 0
    assert drama["marginPointsTotal"] == 0
    assert f"已排除 {len(charges)} 笔" in drama["marginNote"]
    assert drama["settledCost"] > 0, "售价照旧记账，不能因为上游未知就归零"


# ---------------------------------------------------------------- 2. 真值落库


async def test_settled_charge_keeps_sale_price_and_upstream_cost_apart(tmp_path: Path) -> None:
    """售价 300 / 上游 250 → 毛利 50；两个数字各自独立，不互相覆盖。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None, "每条付费路径都必须把 usage 传下来"
        usage.update(upstream_task_id="u-1", upstream_points=250, account_balance=730)
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)

    state = await service.state("local")
    charge = state["charges"][-1]
    assert charge["state"] == "settled"
    assert charge["cost"] == PRICE_VIDEO, "售价侧不动"
    assert charge["upstreamPoints"] == 250
    assert charge["upstreamBalance"] == 730

    drama = _drama(state)
    assert drama["settledCost"] == PRICE_VIDEO
    assert drama["upstreamPointsTotal"] == 250
    assert drama["unknownUpstreamCount"] == 0
    assert drama["marginPointsTotal"] == PRICE_VIDEO - 250
    assert drama["marginNote"].startswith("毛利 = cost - upstream_points")


async def test_keyframe_task_records_upstream_cost(tmp_path: Path) -> None:
    """关键帧走任务不走凭据：两张表都要记，不能只记一边。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        usage.update(
            upstream_task_id="u-kf", upstream_points=UPSTREAM_COST_T2I, account_balance=717
        )
        return PNG

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "submitTasks", [shot], pid=pid)
    await _act(service, "tick", pid=pid)

    state = await service.state("local")
    task = state["tasks"][-1]
    assert task["state"] == "succeeded" and task["settled"] is True
    assert task["cost"] == PRICE_T2I
    assert task["upstreamPoints"] == UPSTREAM_COST_T2I
    assert task["upstreamBalance"] == 717


async def test_keyframe_asset_candidates_also_pass_usage(tmp_path: Path) -> None:
    """锚点候选图同样要记上游成本（它也是真付费动作）。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)
    seen: list[int] = []

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        seen.append(1)
        usage.update(upstream_points=UPSTREAM_COST_T2I, account_balance=700)
        return PNG

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, _ = await _first_shot(service)
    state = await service.state("local")
    asset = state["projects"][0]["assets"]["characters"][0]
    await _act(service, "generateAssetCandidates", "characters", asset["id"], pid=pid)

    after = await service.state("local")
    charge = after["charges"][-1]
    assert seen, "假上游必须被调用"
    assert charge["upstreamPoints"] == UPSTREAM_COST_T2I
    assert charge["cost"] == PRICE_T2I


async def test_missing_upstream_cost_is_never_estimated_from_the_budget_constants(
    tmp_path: Path,
) -> None:
    """上游没回传计费字段时必须是 NULL，**不许**用 `UPSTREAM_COST_*` 回填。

    `UPSTREAM_COST_T2I=25` / `UPSTREAM_COST_VIDEO=250` 是预算常量（用来判断毛利是否
    跌破成本），是「估」不是「实」。拿它们填 `upstream_points`，报表会变成
    「每一单都恰好不赚不亏」——看起来完整，实际全是编的。这条用例把这个诱惑钉死：
    付费动作跑成功、但没有计费字段时，消费侧必须显示「未知」。
    """
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        # 上游只回了素材，没有 pointsCost / accountBalance（社区模板的典型情况）
        assert usage is not None, "usage 必须一路传下来，哪怕上游什么都不报"
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)

    state = await service.state("local")
    charge = state["charges"][-1]
    assert charge["state"] == "settled"
    assert charge["cost"] == PRICE_VIDEO, "售价照旧"
    assert charge["upstreamPoints"] is None, "绝不能回填 UPSTREAM_COST_VIDEO"
    assert charge["upstreamBalance"] is None

    drama = _drama(state)
    assert drama["upstreamPointsTotal"] == 0, "未知不参与求和"
    assert drama["unknownUpstreamCount"] == 1
    assert drama["marginPointsTotal"] == 0
    assert "已排除 1 笔" in drama["marginNote"]


# ---------------------------------------------------------------- 3. 存档往返（洗白洞）


async def test_upstream_cost_survives_an_export_import_roundtrip(tmp_path: Path) -> None:
    """`replace_in` 是唯一的整档重写入口：`_state_in` 漏读就会被洗成 NULL。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        usage.update(upstream_points=250, account_balance=730)
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)
    before = (await service.state("local"))["charges"][-1]
    assert before["upstreamPoints"] == 250

    service.store.import_doc("local", service.store.export_doc("local"))
    after = (await service.state("local"))["charges"][-1]
    assert after["upstreamPoints"] == 250, "存档往返后上游成本不能变成 NULL"
    assert after["upstreamBalance"] == 730

    # 换一个全新的 service 挂同一个 sqlite：模拟重启
    fresh = DramaService(tmp_path / "drama.sqlite", admins=frozenset({"local"}), autopump=False)
    fresh.store.install()
    reloaded = (await fresh.state("local"))["charges"][-1]
    assert reloaded["upstreamPoints"] == 250


# ---------------------------------------------------------------- 4. 待复核：上游已扣但结果不明


async def test_pending_review_keeps_the_upstream_cost_while_cost_is_zero(tmp_path: Path) -> None:
    """钱花了、结果不明：`cost == 0` 但 `upstream_points == 250`，必须能查出来。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        # 上游已自报扣费，但结果不明（例如素材下载失败）——服务层必须把真值挂上去。
        error = AppError("drama_result_missing", "上游说成功但没有可转存素材", 502)
        error.usage = {"upstream_points": 250, "account_balance": 730}  # type: ignore[attr-defined]
        raise error

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    with pytest.raises(AppError) as caught:
        await _act(service, "image-to-video", shot, "", pid=pid)
    assert caught.value.code == "upstream_unknown"
    assert caught.value.status == 504

    state = await service.state("local")
    charge = state["charges"][-1]
    assert charge["state"] == "pending_review"
    assert charge["cost"] == 0, "结果不明不结算"
    assert charge["upstreamPoints"] == 250, "但上游已扣的钱必须留下来"
    assert state["ledger"]["frozen"] == 360, "预占仍冻结，等管理员复核"

    drama = _drama(state)
    assert drama["pendingReview"] == [
        {
            "id": charge["id"],
            "action": "image-to-video",
            "cost": 0,
            "upstreamPoints": 250,
        }
    ]
    # 待复核的钱不进「已结算毛利」，否则会把没定论的单子算成赚了
    assert drama["settledCharges"] == 0
    assert drama["marginPointsTotal"] == 0


# ---------------------------------------------------------------- 5. 迁移：老库两列为 NULL


def _drop_upstream_columns(path: Path) -> None:
    """把新库的两列删掉，模拟「升级前的老库」。"""
    with sqlite3.connect(path) as db:
        for table in ("drama_charge", "drama_tasks"):
            for column in ("upstream_points", "upstream_balance"):
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def test_old_database_gains_nullable_columns_and_stays_readable(tmp_path: Path) -> None:
    """老库升级后两列存在且为 NULL；报表不因 NULL 崩，并把笔数记成「未知」。"""
    path = tmp_path / "drama.sqlite"
    store = DramaStore(path)
    store.install()
    store.ensure_owner("local")
    try:
        _drop_upstream_columns(path)
    except sqlite3.OperationalError:  # pragma: no cover - 老 SQLite 不支持 DROP COLUMN
        pytest.skip("本机 SQLite 不支持 DROP COLUMN，无法构造升级前的老库")

    # 老库里先塞一笔历史凭据（升级前就存在，没有上游成本这回事）
    store.run(
        lambda db: db.execute(
            "INSERT INTO drama_charge(id,owner,project_id,action,request_id,price,cap,cost,"
            "state,result,note,created_at,day,target,source_provider) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "CH-old",
                "local",
                None,
                "script-doctor",
                "",
                2,
                3,
                2,
                "settled",
                "",
                "",
                0.0,
                "2026-01-01",
                "",
                "",
            ),
        )
    )

    upgraded = DramaStore(path)
    upgraded.install()  # 幂等迁移：补列，不重写历史行
    columns = {
        row["name"]
        for row in upgraded.run(lambda db: list(db.execute("PRAGMA table_info(drama_charge)")))
    }
    assert {"upstream_points", "upstream_balance"} <= columns

    state = upgraded.state("local")
    charge = next(c for c in state["charges"] if c["id"] == "CH-old")
    assert charge["upstreamPoints"] is None
    assert charge["upstreamBalance"] is None

    drama = _drama(state)
    assert drama["unknownUpstreamCount"] == 1, "老库的已结算凭据一律算「未知」"
    assert drama["marginPointsTotal"] == 0, "未知不能当 0 求和"
    assert "已排除 1 笔" in drama["marginNote"]
    upgraded.close()
    store.close()
