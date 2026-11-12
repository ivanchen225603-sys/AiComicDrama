"""日上限的计数范围：只统计**真实上游**消费，本地模拟既不占额也不被额拦。

用户裁决原文：「日上限只管真实 Provider。今天上限只拦「会花上游真钱」的调用，
离线 mock 不占额。演示能跑完 8 镜，真机仍受 500 约束。」

这条口径有三个必须同时成立的半边，任一缺失都会把上限做成废纸或把演示堵死：

1. mock 不占额（`daily_spend` 排除 `source_provider='mock'`）——否则整条离线演示
   （种子剧本实测 7 镜 × 300 = 2100）在默认 500 下跑不完；
2. mock 也不被额拦（`_afford(simulated=True)`）——否则先跑过真实动作之后，离线演示
   会被真实消费的余额拦住，语义上等于「免费的东西也被限额」；
3. 真实 Provider 照旧被拦（上限不能改废）——`daily_cap=500`、视频预占 360，
   第 2 次必须 409。

另有一条最阴的：`source_provider` 必须在 `_state_in` 里读回来。否则每一次存档
（`replace_in` 是唯一整档重写入口）都会把 mock 凭据洗成「真实消费」（空值），
表现为「同一进程能跑、重启/存档之后就撞 500」的间歇性故障——见
`test_mock_charge_stays_simulated_after_a_state_reload`。

判据只用 `Provider.name == "mock"` 这个不变式；空值必须继续计入（漏算真钱比多算
一次严重得多，老库升级后「今日已用」突然归零等于给所有人开后门）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.pricing import DEFAULT_DAILY_CAP, PRICE_VIDEO, cap_of
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService
from comicdrama.store import DramaStore

SEED_BALANCE = 980


@pytest.fixture
def mock_service(tmp_path: Path):
    """默认 mock Provider + 默认日上限（不传 daily_cap）。"""
    service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    service.store.install()
    return service


@pytest.fixture
def remote_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 mock 的身份改成真实 Provider（只改身份，不改任何扣费逻辑）。"""
    monkeypatch.setattr(MockDramaProvider, "name", "remote")


async def act(service: DramaService, name: str, *args: Any) -> dict[str, Any]:
    return await service.action("local", name, list(args), None, None)


async def first_shot(service: DramaService) -> str:
    state = await service.state("local")
    pid = state["activeProjectId"]
    shots = next(p for p in state["projects"] if p["id"] == pid)["shots"]
    await act(service, "saveSettings", {"budget": 100000})
    return str(shots[0]["id"])


async def credit(service: DramaService, amount: int, request_id: str) -> None:
    """余额闸门没有改：整条演示（多镜 × 300）确实需要先充值（走正常业务入口）。"""
    await service.action("local", "adminCredit", [amount, "日上限口径测试", request_id], None, None)


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


# ---------------------------------------------------------------- 1. mock 不占额


@pytest.mark.needs_ffmpeg
async def test_mock_actions_do_not_count_against_daily_cap(mock_service: DramaService) -> None:
    """默认 500 下连续 3 次图生视频（名义 3×360=1080 > 500）必须全部成功。"""
    assert DEFAULT_DAILY_CAP == 500
    assert 3 * cap_of(PRICE_VIDEO) > DEFAULT_DAILY_CAP  # 名义消耗确实超限
    await credit(mock_service, 5000, "req-mock-3x")
    shot = await first_shot(mock_service)

    for index in range(3):
        body = await act(mock_service, "image-to-video", shot, "")
        assert body["result"]["kind"] == "image-to-video", f"第 {index + 1} 次被拦"

    assert mock_service.store.daily_spend("local", today()) == 0
    assert (await mock_service.config("local"))["daily_used"] == 0


# ---------------------------------------------------------------- 2. 真实照样被拦


@pytest.mark.needs_ffmpeg
async def test_real_provider_still_hits_daily_cap(tmp_path: Path, remote_named: None) -> None:
    """身份改成非 mock 后，第 2 次图生视频必须 409，且消息里带 500。"""
    service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
        daily_cap=500,
    )
    service.store.install()
    await credit(service, 5000, "req-real-2x")
    shot = await first_shot(service)

    await act(service, "image-to-video", shot, "")  # 第一次冻结 360，花 300
    with pytest.raises(AppError) as caught:
        await act(service, "image-to-video", shot, "")  # 300 + 360 > 500
    assert caught.value.code == "daily_cap_exceeded"
    assert "500" in caught.value.message
    assert service.store.daily_spend("local", today()) == PRICE_VIDEO


# ---------------------------------------------------------------- 3. 不占额 ≠ 不拦


@pytest.mark.needs_ffmpeg
async def test_mock_spend_does_not_shadow_real_calls(tmp_path: Path, monkeypatch) -> None:
    """同一库上先跑满若干笔离线动作，再切真实身份，第一次真实动作仍必须放行。

    这条才真正证明「不占额」，而不只是「不拦」：如果只是 `_afford` 跳过检查、
    但 `daily_spend` 仍把 mock 算进去，那么切到真实模式后第一次就会被拦住。
    """
    mock_service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    mock_service.store.install()
    await credit(mock_service, 20000, "req-shadow-mock")
    shot = await first_shot(mock_service)
    for _ in range(4):  # 名义 4×360 = 1440，远超 500
        await act(mock_service, "image-to-video", shot, "")
    assert mock_service.store.daily_spend("local", today()) == 0

    monkeypatch.setattr(MockDramaProvider, "name", "remote")
    real_service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    real_service.store.install()
    body = await act(real_service, "image-to-video", shot, "")
    assert body["result"]["kind"] == "image-to-video"
    assert real_service.store.daily_spend("local", today()) == PRICE_VIDEO


# ---------------------------------------------------------------- 4. 存档不能洗身份


@pytest.mark.needs_ffmpeg
async def test_mock_charge_stays_simulated_after_a_state_reload(tmp_path: Path) -> None:
    """mock 跑 1 笔 → 存档/重读 → 换新实例再跑 1 笔：`daily_spend` 仍须为 0。

    打的是 `_state_in` 漏读 `source_provider` 的洞：`replace_in` 按内存里的 charge
    字典整档重写，读不到身份就会把 mock 凭据写成空值 = 「真实消费」，于是第二趟
    开始占日上限。不修就会表现为「新版第一次跑没问题，存档之后偶尔撞 500」。
    """
    path = tmp_path / "drama.sqlite"
    first = DramaService(path, MockDramaProvider(), admins=frozenset({"local"}), autopump=False)
    first.store.install()
    await credit(first, 5000, "req-reload")
    shot = await first_shot(first)
    await act(first, "image-to-video", shot, "")

    # 模拟「存档 + 重启后从库里重读」：走真实的 export -> import 往返（服务端存档
    # 与导入都是这条 `replace_in` 路径），再换一个全新的 service 实例挂同一个 sqlite。
    state = first.store.state("local")
    charges = state["charges"]
    assert charges and charges[-1]["sourceProvider"] == "mock", "快照里必须带着身份"
    first.store.import_doc("local", first.store.export_doc("local"))

    second = DramaService(path, MockDramaProvider(), admins=frozenset({"local"}), autopump=False)
    second.store.install()
    assert second.store.state("local")["charges"][-1]["sourceProvider"] == "mock"
    body = await act(second, "image-to-video", shot, "")
    assert body["result"]["kind"] == "image-to-video"
    assert second.store.daily_spend("local", today()) == 0


# ---------------------------------------------------------------- 5. store 层口径


def test_daily_spend_treats_unknown_source_as_real(tmp_path: Path) -> None:
    """空身份（老库/导入）必须继续计入；`mock` 必须不计入。"""
    path = tmp_path / "drama.sqlite"
    store = DramaStore(path)
    store.install()
    store.ensure_owner("local")
    day = today()

    def insert(db, suffix: str, provider: str, cost: int) -> None:
        db.execute(
            "INSERT INTO drama_charge(id,owner,project_id,action,request_id,price,cap,cost,"
            "state,result,note,created_at,day,target,source_provider) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"CH-{suffix}",
                "local",
                None,
                "script-doctor",
                "",
                cost,
                cost,
                cost,
                "settled",
                "",
                "",
                0.0,
                day,
                "",
                provider,
            ),
        )
        db.execute(
            "INSERT INTO drama_tasks(id,project_id,owner,shot_id,type,state,percent,reserve,"
            "cost,settled,picked,frames,idem,charge_id,upstream_task_id,note,created_at,"
            "settled_day,source_provider) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"T-{suffix}",
                "p-x",
                "local",
                "s-1",
                "keyframe",
                "succeeded",
                100,
                cost,
                cost,
                1,
                None,
                "[]",
                "idem",
                "",
                None,
                "",
                0.0,
                day,
                provider,
            ),
        )

    store.run(lambda db: insert(db, "real", "", 7))  # 空身份 → 真实消费
    assert store.daily_spend("local", day) == 14  # charge 7 + task 7

    store.run(lambda db: insert(db, "mock", "mock", 300))
    assert store.daily_spend("local", day) == 14, "mock 行不能计入"

    store.run(lambda db: insert(db, "remote", "remote", 5))
    assert store.daily_spend("local", day) == 24, "remote 行必须计入"
    store.close()


@pytest.mark.needs_ffmpeg
async def test_charge_identity_is_provider_name_not_channel(
    tmp_path: Path, remote_named: None
) -> None:
    """落库的身份必须是 `Provider.name` 原值，不能是厂商名/通道名。

    `_settle_legacy_tasks` 拿这个字段做等值比较（`origin != current_provider`），
    记通道名（libtv / seedream / kling…）会把同模式（都是 remote）下正当在跑的
    任务误判成「切换模式遗留」，于是全额退还预占并取消任务。
    """
    service = DramaService(
        tmp_path / "drama.sqlite", MockDramaProvider(), admins=frozenset({"local"}), autopump=False
    )
    service.store.install()
    shot = await first_shot(service)
    body = await act(service, "image-to-video", shot, "")
    charge = body["state"]["charges"][-1]

    assert charge["sourceProvider"] == "remote"  # Provider.name 原值
    assert charge["sourceProvider"] not in {"libtv", "seedream", "kling-v2-5-turbo"}
    # 关键帧任务走的是同一个字段口径（`prov.name`），两处必须一致。
    await act(service, "submitTasks", None)
    state = await service.state("local")
    assert {t["sourceProvider"] for t in state["tasks"]} <= {"remote"}
