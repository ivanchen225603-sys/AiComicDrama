"""HTTP 路由：身份一律取服务端认定的账号，绝不采信请求体里的 owner。

注意：本文件刻意不写 `from __future__ import annotations`，否则 FastAPI 无法解析
Annotated[Principal, Depends(principal)]，POST 路由会把 identity 当成查询参数返回 422。
"""

import asyncio
import mimetypes
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, Response
from pydantic import Field

from .credentials import check_liblib, check_llm
from .models import Principal, StrictModel
from .reconcile import reconcile_doc
from .reports import report_csv
from .service import DramaService


class ActionInput(StrictModel):
    type: str = Field(min_length=1, max_length=64)
    args: list[Any] = Field(default_factory=list)
    project_id: str | None = Field(default=None, max_length=64)
    # 幂等键。同一 (owner, request_id) 只会真正执行并扣费一次，
    # 重复提交回放上次结果——生成动作被连点或客户端超时重试时这是唯一安全路径。
    request_id: str | None = Field(default=None, max_length=64)


class CredentialInput(StrictModel):
    """空字符串 = 保持原值；清除走 DELETE。"""

    liblib_access_key: str | None = Field(default=None, max_length=512)
    liblib_secret_key: str | None = Field(default=None, max_length=512)
    llm_base_url: str | None = Field(default=None, max_length=512)
    llm_model: str | None = Field(default=None, max_length=512)
    llm_api_key: str | None = Field(default=None, max_length=512)
    tts_base_url: str | None = Field(default=None, max_length=512)
    tts_model: str | None = Field(default=None, max_length=512)
    tts_api_key: str | None = Field(default=None, max_length=512)
    tts_voice: str | None = Field(default=None, max_length=512)


class ImportInput(StrictModel):
    doc: dict[str, Any]


def _window(days: int) -> int:
    """把 `?days=` 收进 [1, 365]。

    刻意不抛 422：窗口是个「看多少天」的展示偏好，用户手滑传 0 或 9999 时
    夹一下比报错合理；真正的边界错误（days<1）交给这里兜住，报表函数内部
    那条 ValueError 就永远不会从 HTTP 冒成 500。
    """
    value = int(days or 0)
    return 1 if value < 1 else min(value, 365)


def mount_drama_routes(
    app,
    service_getter: Callable[[], DramaService],
    principal,
):
    router = APIRouter(prefix="/api/v1/drama", tags=["drama"])
    identity_dep = Annotated[Principal, Depends(principal)]

    @router.get("/config")
    async def config(identity: identity_dep):
        return await service_getter().config(identity.owner)

    # ---- 用户自填的模型密钥：只返回打码视图，原值永不下发 ----
    @router.get("/credentials")
    async def get_credentials(identity: identity_dep):
        svc = service_getter()
        return await asyncio.to_thread(svc.credentials.view, identity.owner)

    @router.put("/credentials")
    async def save_credentials(body: CredentialInput, identity: identity_dep):
        svc = service_getter()
        fields = body.model_dump(exclude_none=True)
        before = await asyncio.to_thread(svc.credentials.view, identity.owner)
        view = await asyncio.to_thread(svc.credentials.save, identity.owner, fields)
        dropped = 0
        if view["liblib"]["configured"] and not before["liblib"]["configured"]:
            # 刚切到真实生成：把离线示例画面 / 提示音从项目里摘掉，避免混进真实成片。
            done = await svc.action(identity.owner, "dropSampleMedia", [], None, None)
            dropped = int((done.get("result") or {}).get("dropped") or 0)
        return {**view, "droppedSamples": dropped}

    @router.delete("/credentials")
    async def clear_credentials(identity: identity_dep, scope: str = "all"):
        svc = service_getter()
        scope = scope if scope in ("all", "liblib", "llm", "tts") else "all"
        return await asyncio.to_thread(svc.credentials.clear, identity.owner, scope)

    @router.post("/credentials/test")
    async def test_credentials(identity: identity_dep):
        svc = service_getter()
        creds = await asyncio.to_thread(svc.credentials.get, identity.owner)
        out: dict[str, Any] = {}
        if creds.get("liblib_access_key") and creds.get("liblib_secret_key"):
            base = (
                getattr(svc.remote_template, "libtv_base_url", "")
                or "https://openapi.liblibai.cloud"
            )
            out["liblib"] = await asyncio.to_thread(
                check_liblib, creds["liblib_access_key"], creds["liblib_secret_key"], base
            )
        if creds.get("llm_base_url") and creds.get("llm_api_key"):
            out["llm"] = await asyncio.to_thread(
                check_llm, creds["llm_base_url"], creds["llm_api_key"], creds.get("llm_model", "")
            )
        if creds.get("tts_base_url") and creds.get("tts_api_key"):
            out["tts"] = await asyncio.to_thread(
                check_llm, creds["tts_base_url"], creds["tts_api_key"], creds.get("tts_model", "")
            )
            out["tts"]["message"] = out["tts"]["message"].replace("文本模型", "配音服务")
        return out

    @router.get("/state")
    async def state(identity: identity_dep):
        return await service_getter().state(identity.owner)

    @router.post("/action")
    async def action(
        body: ActionInput,
        identity: identity_dep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        # 请求体里的 request_id 优先；否则用标准 Idempotency-Key 头。
        request_id = body.request_id or (idempotency_key or "").strip() or None
        return await service_getter().action(
            identity.owner,
            body.type,
            body.args,
            body.project_id,
            request_id,
        )

    @router.post("/reset")
    async def reset(identity: identity_dep):
        return await service_getter().reset(identity.owner)

    @router.get("/public/{token}")
    async def public_asset(token: str):
        # 给上游平台拉参考图 / 首帧用：无需登录，但只认服务端签发且未过期的令牌。
        path = service_getter().media_store.resolve_public(token)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @router.get("/assets/{asset_id}")
    async def asset(asset_id: str, identity: identity_dep):
        # 只按服务端 principal 的 owner 目录找文件；asset_id 不含路径分隔符，
        # 请求体/路径里伪造的 owner 一律不采信。
        path = service_getter().media_store.resolve(identity.owner, asset_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @router.post("/uploads")
    async def upload_media(
        request: Request,
        identity: identity_dep,
        kind: str = "",
        name: str = "",
        asset_type: str = "",
        asset_name: str = "",
    ):
        """只开放 bgm / reference 两类。上传本身不扣积分、不触发生成动作。

        刻意用 `application/octet-stream` 原始 body + 查询串传元数据，而不是
        multipart：Form/File 要额外装 python-multipart，干净导出树的离线门禁
        不保证有这个包。文件内容按魔数判定，不信 Content-Type 与文件名后缀。

        返回的是与 /state 同形的完整状态，前端上传成功后直接灌进 A.DB 即可，
        不需要再发一次拉取请求。owner 一律取服务端 principal。
        """
        data = await request.body()
        return await service_getter().upload(
            identity.owner,
            kind,
            (name or "").strip() or "未命名文件",
            data,
            asset_type=asset_type,
            asset_name=asset_name,
        )

    @router.delete("/uploads/{upload_id}")
    async def delete_upload(upload_id: str, identity: identity_dep):
        return await service_getter().delete_upload(identity.owner, upload_id)

    @router.get("/export")
    async def export_doc(identity: identity_dep):
        return await service_getter().export_doc(identity.owner)

    @router.post("/import")
    async def import_doc(body: ImportInput, identity: identity_dep):
        return await service_getter().import_doc(identity.owner, body.doc)

    @router.get("/reconcile")
    async def reconcile(identity: identity_dep):
        """账本只读概览。"""
        service = service_getter()
        state = await service.state(identity.owner)
        day = time.strftime("%Y-%m-%d", time.localtime())
        used = await asyncio.to_thread(service.store.daily_spend, identity.owner, day)
        return reconcile_doc(
            identity.owner,
            state,
            daily_cap=service.daily_cap,
            daily_used=used,
        )

    @router.get("/report/daily")
    async def daily_report_route(identity: identity_dep, days: int = 30):
        """按日对账报表：本地售价 vs 上游实扣，含差异、缺数项与定价建议。

        管理员专属（闸门在 DramaService.daily_report 里）。只读：不写库，
        `pricingSuggestion.applied` 恒为 False —— 看报表不会改任何价格。
        """
        return await service_getter().daily_report(identity.owner, days=_window(days))

    @router.get("/report/daily.csv")
    async def daily_report_csv(identity: identity_dep, days: int = 30):
        report = await service_getter().daily_report(identity.owner, days=_window(days))
        stamp = time.strftime("%Y%m%d", time.localtime())
        return Response(
            content=report_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="drama-daily-{stamp}.csv"'},
        )

    app.include_router(router)
