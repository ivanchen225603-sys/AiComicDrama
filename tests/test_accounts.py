"""账户与会话的 HTTP 边界：注册 / 登录 / 锁定 / 会话 / 改密 / 身份查询。

覆盖用户裁决里的身份凭证边界：
- 口令与令牌只以摘要 / 哈希形态入库，响应体与 Cookie 都不回吐；
- 失败限次（5 次后锁 15 分钟）、锁定期优先于正确密码、锁过期恢复干净计数窗口；
- 会话 Cookie 是 HttpOnly + SameSite=lax，令牌明文只出现一次；
- 改密只踢掉该 owner 的其它会话，当前会话保留；
- 新注册的 `u_xxx` 不在管理员白名单，`/me` 必须如实返回 admin=False。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from comicdrama.accounts import AccountStore
from comicdrama.app import create_app
from comicdrama.config import Settings

ACCOUNT = "/api/v1/drama/account"
OWNER_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
GOOD_PASSWORD = "correct-horse-1"


@pytest.fixture
async def api(tmp_path: Path) -> Iterator[SimpleNamespace]:
    settings = Settings(
        data_dir=tmp_path / "api",
        drama_autopump=False,
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
            yield SimpleNamespace(
                app=app,
                new_client=new_client,
                db=tmp_path / "api" / "drama.sqlite",
            )
        finally:
            for client in clients:
                await client.aclose()


def rows(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(db: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def error_code(response: httpx.Response) -> str:
    """服务端的错误码在 `error` 字段（AppError -> JSONResponse）。"""
    body = response.json()
    return str(body.get("error") or body.get("code") or "")


async def register(client: httpx.AsyncClient, username: str, password: str) -> dict[str, Any]:
    response = await client.post(
        f"{ACCOUNT}/register", json={"username": username, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(
    client: httpx.AsyncClient, username: str, password: str, expected: int = 200
) -> httpx.Response:
    response = await client.post(
        f"{ACCOUNT}/login", json={"username": username, "password": password}
    )
    assert response.status_code == expected, response.text
    return response


def session_cookie_header(response: httpx.Response) -> str:
    headers = [c for c in response.headers.get_list("set-cookie") if c.startswith("drama_session=")]
    assert len(headers) == 1, f"应下发且只下发一个会话 Cookie: {headers}"
    return headers[0]


async def test_register_returns_a_usable_owner_without_credentials(api: SimpleNamespace):
    client = api.new_client()
    body = await register(client, "alice", GOOD_PASSWORD)

    assert body["registered"] is True
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert body["owner"].startswith("u_")
    assert re.fullmatch(OWNER_PATTERN, body["owner"]), body["owner"]

    # 响应体不得回吐任何身份凭证材料。
    text = str(body)
    for leaked in ("password_hash", "salt", "token"):
        assert leaked not in text, f"注册响应泄漏 {leaked}: {text}"

    stored = rows(api.db, "SELECT * FROM drama_accounts WHERE username=?", ("alice",))
    assert len(stored) == 1
    assert stored[0]["owner"] == body["owner"]
    assert stored[0]["role"] == "user"
    assert stored[0]["password_hash"] != GOOD_PASSWORD


async def test_duplicate_username_is_rejected_with_conflict(api: SimpleNamespace):
    client = api.new_client()
    first = await register(client, "bob", GOOD_PASSWORD)

    response = await client.post(
        f"{ACCOUNT}/register", json={"username": "bob", "password": "another-pass-1"}
    )
    assert response.status_code == 409
    assert error_code(response) == "account_username_taken"
    assert len(rows(api.db, "SELECT * FROM drama_accounts WHERE username=?", ("bob",))) == 1
    assert (
        rows(api.db, "SELECT owner FROM drama_accounts WHERE username=?", ("bob",))[0]["owner"]
        == (first["owner"])
    )


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param("ab", GOOD_PASSWORD, id="username-too-short"),
        pytest.param("a b", GOOD_PASSWORD, id="username-with-space"),
        pytest.param("账号", GOOD_PASSWORD, id="username-non-ascii"),
        pytest.param("carol", "1234567", id="password-too-short"),
        pytest.param("carol", "a" * 129, id="password-too-long"),
    ],
)
async def test_invalid_credentials_are_rejected(api: SimpleNamespace, username: str, password: str):
    client = api.new_client()
    response = await client.post(
        f"{ACCOUNT}/register", json={"username": username, "password": password}
    )
    assert response.status_code == 400
    assert error_code(response) == "account_invalid"
    assert rows(api.db, "SELECT * FROM drama_accounts WHERE username=?", (username,)) == []


async def test_login_cookie_is_httponly_lax_and_only_the_digest_is_stored(
    api: SimpleNamespace,
):
    client = api.new_client()
    await register(client, "dave", GOOD_PASSWORD)
    response = await login(client, "dave", GOOD_PASSWORD)

    header = session_cookie_header(response)
    lowered = header.lower()
    assert "httponly" in lowered, header
    assert "samesite=lax" in lowered, header

    token = client.cookies.get("drama_session")
    assert token and len(token) >= 32
    stored = rows(api.db, "SELECT token_hash,owner FROM drama_sessions")
    assert len(stored) == 1
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert stored[0]["token_hash"] == digest
    assert token != stored[0]["token_hash"], "库里不能存明文令牌"
    assert header.count(token) == 1, "明文令牌只应作为 Cookie 值出现一次"
    assert token not in str(stored)
    for column_value in stored[0].values():
        assert str(column_value) != token


async def test_five_failures_lock_the_account_and_lock_beats_the_right_password(
    api: SimpleNamespace,
):
    client = api.new_client()
    account = await register(client, "erin", GOOD_PASSWORD)

    for attempt in range(5):
        response = await client.post(
            f"{ACCOUNT}/login", json={"username": "erin", "password": "wrong-pass-1"}
        )
        assert response.status_code == 401, f"第 {attempt + 1} 次应仍是 401"
        assert error_code(response) == "account_credentials_invalid"

    sixth = await client.post(
        f"{ACCOUNT}/login", json={"username": "erin", "password": "wrong-pass-1"}
    )
    assert sixth.status_code == 429
    assert error_code(sixth) == "account_locked"

    # 锁定期内即使密码正确也只能是 429：锁定优先于凭证校验。
    locked = await client.post(
        f"{ACCOUNT}/login", json={"username": "erin", "password": GOOD_PASSWORD}
    )
    assert locked.status_code == 429
    assert error_code(locked) == "account_locked"

    row = rows(
        api.db,
        "SELECT failed_count,locked_until FROM drama_accounts WHERE owner=?",
        (account["owner"],),
    )[0]
    assert row["failed_count"] == 5
    assert row["locked_until"] > time.time()


async def test_lock_expiry_restarts_a_clean_failure_window(api: SimpleNamespace):
    client = api.new_client()
    account = await register(client, "frank", GOOD_PASSWORD)
    for _ in range(5):
        await client.post(
            f"{ACCOUNT}/login", json={"username": "frank", "password": "wrong-pass-1"}
        )
    locked = rows(
        api.db, "SELECT locked_until FROM drama_accounts WHERE owner=?", (account["owner"],)
    )[0]
    assert locked["locked_until"] > time.time()

    # 锁定过期：把 locked_until 拨到过去，模拟 15 分钟之后。
    execute(
        api.db,
        "UPDATE drama_accounts SET locked_until=? WHERE owner=?",
        (time.time() - 60, account["owner"]),
    )

    response = await client.post(
        f"{ACCOUNT}/login", json={"username": "frank", "password": "wrong-pass-1"}
    )
    assert response.status_code == 401
    assert error_code(response) == "account_credentials_invalid"

    row = rows(
        api.db,
        "SELECT failed_count,locked_until FROM drama_accounts WHERE owner=?",
        (account["owner"],),
    )[0]
    assert row["failed_count"] == 1, "锁过期后必须从干净窗口重新计数"
    assert row["locked_until"] == 0, "错一次不能立刻再锁 15 分钟"


async def test_successful_login_clears_the_failure_counter(api: SimpleNamespace):
    client = api.new_client()
    account = await register(client, "grace", GOOD_PASSWORD)
    for _ in range(3):
        await client.post(
            f"{ACCOUNT}/login", json={"username": "grace", "password": "wrong-pass-1"}
        )
    assert (
        rows(api.db, "SELECT failed_count FROM drama_accounts WHERE owner=?", (account["owner"],))[
            0
        ]["failed_count"]
        == 3
    )

    await login(client, "grace", GOOD_PASSWORD)
    row = rows(
        api.db,
        "SELECT failed_count,locked_until FROM drama_accounts WHERE owner=?",
        (account["owner"],),
    )[0]
    assert row["failed_count"] == 0
    assert row["locked_until"] == 0


async def test_me_requires_a_session_and_reports_non_admin(api: SimpleNamespace):
    anonymous = api.new_client()
    response = await anonymous.get(f"{ACCOUNT}/me")
    assert response.status_code == 401
    assert error_code(response) == "account_session_required"

    client = api.new_client()
    account = await register(client, "heidi", GOOD_PASSWORD)
    await login(client, "heidi", GOOD_PASSWORD)
    response = await client.get(f"{ACCOUNT}/me")
    assert response.status_code == 200
    body = response.json()
    assert body["owner"] == account["owner"]
    assert body["role"] == "user"
    assert body["admin"] is False, "新建的 u_xxx 不在管理员白名单"


async def test_logout_invalidates_the_session_even_if_the_cookie_is_replayed(
    api: SimpleNamespace,
):
    client = api.new_client()
    await register(client, "ivan", GOOD_PASSWORD)
    await login(client, "ivan", GOOD_PASSWORD)
    token = client.cookies.get("drama_session")
    assert (await client.get(f"{ACCOUNT}/me")).status_code == 200

    response = await client.post(f"{ACCOUNT}/logout")
    assert response.status_code == 200
    assert response.json()["logged_out"] is True

    assert (await client.get(f"{ACCOUNT}/me")).status_code == 401
    assert rows(api.db, "SELECT * FROM drama_sessions") == []

    replay = api.new_client()
    response = await replay.get(f"{ACCOUNT}/me", headers={"cookie": f"drama_session={token}"})
    assert response.status_code == 401
    assert error_code(response) == "account_session_required"


async def test_password_change_keeps_the_current_session_and_drops_the_others(
    api: SimpleNamespace,
):
    first = api.new_client()
    second = api.new_client()
    await register(first, "judy", GOOD_PASSWORD)
    await login(first, "judy", GOOD_PASSWORD)
    await login(second, "judy", GOOD_PASSWORD)
    assert (await second.get(f"{ACCOUNT}/me")).status_code == 200

    response = await first.post(
        f"{ACCOUNT}/password",
        json={"old_password": GOOD_PASSWORD, "new_password": "brand-new-pass-9"},
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True

    assert (await first.get(f"{ACCOUNT}/me")).status_code == 200, "当前会话必须保留"
    assert (await second.get(f"{ACCOUNT}/me")).status_code == 401, "其它会话必须失效"

    fresh = api.new_client()
    old = await fresh.post(f"{ACCOUNT}/login", json={"username": "judy", "password": GOOD_PASSWORD})
    assert old.status_code == 401
    assert error_code(old) == "account_credentials_invalid"

    new = await fresh.post(
        f"{ACCOUNT}/login", json={"username": "judy", "password": "brand-new-pass-9"}
    )
    assert new.status_code == 200


async def test_password_change_with_a_wrong_old_password_changes_nothing(
    api: SimpleNamespace,
):
    client = api.new_client()
    await register(client, "ken", GOOD_PASSWORD)
    await login(client, "ken", GOOD_PASSWORD)

    response = await client.post(
        f"{ACCOUNT}/password",
        json={"old_password": "not-the-old-one", "new_password": "brand-new-pass-9"},
    )
    assert response.status_code == 401
    assert error_code(response) == "account_credentials_invalid"

    fresh = api.new_client()
    old = await fresh.post(f"{ACCOUNT}/login", json={"username": "ken", "password": GOOD_PASSWORD})
    assert old.status_code == 200, "原密码必须仍然有效"
    new = await fresh.post(
        f"{ACCOUNT}/login", json={"username": "ken", "password": "brand-new-pass-9"}
    )
    assert new.status_code == 401, "新密码不能被写入"


async def test_expired_session_never_authenticates(api: SimpleNamespace):
    client = api.new_client()
    await register(client, "laura", GOOD_PASSWORD)
    accounts = AccountStore(api.app.state.drama.store)

    token, _ = accounts.login("laura", GOOD_PASSWORD, ttl=-1)
    assert accounts.owner_for_session(token) is None

    live, _ = accounts.login("laura", GOOD_PASSWORD)
    assert accounts.owner_for_session(live) is not None


async def test_login_sweeps_expired_sessions(api: SimpleNamespace):
    stale = hashlib.sha256(b"stale-token").hexdigest()
    execute(
        api.db,
        "INSERT INTO drama_sessions(token_hash,owner,expires_at,created_at) VALUES(?,?,?,?)",
        (stale, "u_dead", time.time() - 1, time.time() - 100),
    )

    client = api.new_client()
    await register(client, "mallory", GOOD_PASSWORD)
    await login(client, "mallory", GOOD_PASSWORD)

    left = rows(api.db, "SELECT token_hash,expires_at FROM drama_sessions")
    assert [row["token_hash"] for row in left] != [stale]
    assert stale not in [row["token_hash"] for row in left]
    assert all(row["expires_at"] > time.time() for row in left), "表里不能留下已过期会话"
    assert len(left) == 1
