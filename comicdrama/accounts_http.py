"""账户 HTTP 边界：注册 / 登录 / 退出 / 改密码 / 身份查询。

刻意不写 `from __future__ import annotations`（与 drama/http.py 同一原因）：
FastAPI 会把 Annotated[Principal, Depends(...)] 字符串化后解析失败，导致 POST
路由把 identity 当成必填查询参数并返回 422。本文件不使用 X-* 头传身份，
身份一律来自同源 Cookie 或服务端 principal。

Cookie：HttpOnly + SameSite=Lax；`Secure` **只在 request.url.scheme == "https" 时**
才加，否则本地 http 演示 Cookie 不会回传。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .accounts import SESSION_COOKIE, SESSION_TTL_SECONDS, AccountStore
from .errors import AppError
from .models import StrictModel


class RegisterInput(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginInput(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class PasswordInput(StrictModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def mount_account_routes(
    app: Any,
    accounts: AccountStore,
    guard: Callable[[Request], Awaitable[None]],
    session: Callable[[Request], Awaitable[str]],
    admins: frozenset[str],
) -> None:
    router = APIRouter(prefix="/api/v1/drama/account", tags=["drama-account"])

    @router.post("/register", status_code=201)
    async def register(body: RegisterInput, request: Request):
        await guard(request)
        info = await asyncio.to_thread(accounts.register, body.username, body.password)
        return {"registered": True, **info}

    @router.post("/login")
    async def login(body: LoginInput, request: Request):
        await guard(request)
        token, info = await asyncio.to_thread(accounts.login, body.username, body.password)
        response = JSONResponse({"logged_in": True, **info})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request):
        await guard(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            raise AppError("account_session_required", "请先登录", 401)
        await asyncio.to_thread(accounts.logout, token)
        response = JSONResponse({"logged_out": True})
        # Max-Age=0 清 Cookie；属性必须与下发时一致（path / SameSite / HttpOnly）。
        response.set_cookie(
            SESSION_COOKIE,
            "",
            max_age=0,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @router.post("/password")
    async def password(body: PasswordInput, request: Request):
        owner = await session(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        result = await asyncio.to_thread(
            accounts.change_password, token, body.old_password, body.new_password
        )
        return {"changed": bool(result["changed"]), "owner": owner}

    @router.get("/me")
    async def me(request: Request):
        await session(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        return await asyncio.to_thread(accounts.me, token, admins)

    app.include_router(router)
