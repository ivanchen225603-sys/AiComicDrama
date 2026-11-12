"""对账报表的 HTTP 边界：管理员闸门、只读性、CSV 导出。

聚合口径的正确性由 `tests/test_reports.py` 用纯函数逐条守住；这里只证明
三件属于「路由」的事：

1. 非管理员拿不到（闸门在服务层，所以两个入口都必须被挡住）；
2. 报表与 `/state` 看到的是同一份快照（不是另一本账、也不是缓存的旧数据）；
3. 导出是真正的附件下载，且行数与 `byDay` 一致、不带内部凭据 id。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from comicdrama.app import create_app
from comicdrama.config import Settings

ACCOUNT = "/api/v1/drama/account"
REPORT = "/api/v1/drama/report/daily"
REPORT_CSV = "/api/v1/drama/report/daily.csv"
PASSWORD = "offline-pass-1"


@pytest.fixture
async def api(tmp_path: Path) -> Iterator[SimpleNamespace]:
    settings = Settings(
        data_dir=tmp_path / "api",
        drama_autopump=False,
        drama_daily_cap=100000,
    )
    app = create_app(settings)
    clients: list[httpx.AsyncClient] = []

    def new_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        )
        clients.append(client)
        return client

    async with app.router.lifespan_context(app):
        try:
            yield SimpleNamespace(app=app, new_client=new_client)
        finally:
            for client in clients:
                await client.aclose()


async def registered_user(api: SimpleNamespace, username: str) -> httpx.AsyncClient:
    """注册并登录一个普通账户：它的 owner 是 `u_xxx`，不在管理员白名单里。"""
    client = api.new_client()
    created = await client.post(
        f"{ACCOUNT}/register", json={"username": username, "password": PASSWORD}
    )
    assert created.status_code == 201, created.text
    logged_in = await client.post(
        f"{ACCOUNT}/login", json={"username": username, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text
    return client


async def test_non_admin_cannot_read_the_daily_report(api: SimpleNamespace) -> None:
    """自助注册的账户不是管理员：报表里有毛利与上游成本，不能对它开放。"""
    client = await registered_user(api, "report-user")
    response = await client.get(REPORT)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "forbidden"


async def test_non_admin_cannot_export_the_daily_report(api: SimpleNamespace) -> None:
    """导出与查看是两条路由，闸门必须在服务层，不能只挂在其中一条上。"""
    client = await registered_user(api, "report-user-2")
    response = await client.get(REPORT_CSV)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "forbidden"


async def test_admin_sees_the_same_snapshot_the_state_endpoint_shows(
    api: SimpleNamespace,
) -> None:
    """本地演示身份 `local` 在 credit_admins 里，属于管理员。

    这条同时证明报表读的是**当前**快照：报表里的已结算笔数必须等于 `/state`
    里 `state == "settled"` 的条数。两边对不上就说明读了两本账或读了缓存。
    """
    client = api.new_client()
    report = await client.get(REPORT)
    assert report.status_code == 200, report.text
    body = report.json()
    state = (await client.get("/api/v1/drama/state")).json()
    settled = [charge for charge in state["charges"] if charge["state"] == "settled"]
    assert body["totals"]["orders"] == len(settled)
    for key in ("byDay", "byProvider", "totals", "pricingSuggestion", "generatedAt"):
        assert key in body, key


async def test_report_never_applies_a_price_change(api: SimpleNamespace) -> None:
    """看一百次报表也不能改价：`applied` 恒 False，且定价表原样不动。"""
    client = api.new_client()
    before = (await client.get("/api/v1/drama/config")).json()["prices"]
    for _ in range(3):
        body = (await client.get(REPORT)).json()
        assert body["pricingSuggestion"]["applied"] is False
        assert body["pricingSuggestion"]["requiresConfirmation"] is True
    after = (await client.get("/api/v1/drama/config")).json()["prices"]
    assert after == before, "报表不得改动任何生效价"


@pytest.mark.parametrize("days", [0, -5, 1, 9999])
async def test_days_window_is_clamped_not_rejected(api: SimpleNamespace, days: int) -> None:
    """`?days=` 是展示偏好，夹一下就好；绝不能因为手滑传 0 变成 500。"""
    client = api.new_client()
    response = await client.get(REPORT, params={"days": days})
    assert response.status_code == 200, response.text
    body = response.json()
    assert 1 <= body["days"] <= 365


async def test_csv_export_is_an_attachment_with_one_row_per_day(
    api: SimpleNamespace,
) -> None:
    client = api.new_client()
    report = (await client.get(REPORT)).json()
    response = await client.get(REPORT_CSV)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.rstrip("\n").split("\n")
    assert lines[0].startswith("date,orders,sale_points,upstream_points")
    assert len(lines) == 1 + len(report["byDay"])


async def test_csv_export_carries_no_internal_charge_ids(api: SimpleNamespace) -> None:
    client = api.new_client()
    state = (await client.get("/api/v1/drama/state")).json()
    ids = [str(charge.get("id") or "") for charge in state["charges"]]
    text = (await client.get(REPORT_CSV)).text
    for charge_id in ids:
        if charge_id:
            assert charge_id not in text, f"导出文件泄露了内部凭据 id：{charge_id}"
