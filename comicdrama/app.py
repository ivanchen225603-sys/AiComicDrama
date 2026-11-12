"""应用装配：服务、账户、支付、HTTP 路由与前端静态页。"""

import asyncio
import hmac
import ipaddress
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .accounts import SESSION_COOKIE, AccountStore
from .accounts_http import mount_account_routes
from .body_limit import BodyLimit
from .config import Settings
from .errors import AppError
from .http import mount_drama_routes
from .models import Principal
from .payments import PaymentService
from .payments_http import mount_payment_routes
from .providers import MockDramaProvider
from .providers.remote import RemoteDramaConfig, RemoteDramaProvider
from .service import DramaService

logger = logging.getLogger("comicdrama")
WEB_ROOT = Path(__file__).parent / "web"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'"
)


def _admins(settings: Settings) -> frozenset[str]:
    # 显式配置优先；本机演示模式默认本机账号为管理员；bearer 模式不配置就没有管理员。
    return frozenset(settings.credit_admins) or (
        frozenset({"local"}) if settings.auth_mode == "local" else frozenset()
    )


def _build_service(settings: Settings, admins: frozenset[str]) -> DramaService:
    drama = DramaService(
        settings.data_dir / "drama.sqlite",
        admins=admins,
        daily_cap=settings.drama_daily_cap,
        autopump=settings.drama_autopump,
        pump_seconds=settings.drama_pump_seconds,
        media_quota=settings.drama_media_quota_mb * 1024 * 1024,
        ai_label=settings.drama_ai_label,
        ai_disclosure=settings.drama_ai_disclosure,
        ai_label_positions=settings.drama_ai_label_positions,
        audit_provider=settings.drama_audit_provider,
        audit_blocklist=settings.drama_audit_blocklist,
        audit_reject_action=settings.drama_audit_reject_action,
        retention_days=settings.drama_media_retention_days,
    )
    if settings.drama_provider == "mock" and settings.drama_mock_fault:
        drama.set_provider(MockDramaProvider(faults=settings.drama_mock_fault))
    # 用户在页面填写自有密钥时使用的公共配置：只含部署方掌握的东西
    # （公网地址、超时、FFmpeg、标识文案、配音端点），不含任何密钥。
    drama.set_remote_template(
        RemoteDramaConfig(
            llm_base_url="",
            llm_model="",
            require_llm=False,
            llm_timeout=settings.drama_llm_timeout,
            tts_url=settings.drama_tts_url,
            tts_key_env=settings.drama_tts_key_env,
            tts_timeout=settings.drama_tts_timeout,
            libtv_base_url=settings.libtv_base_url,
            t2i_channel="official",
            i2v_channel="official",
            t2i_official_template_uuid=settings.drama_t2i_official_template_uuid,
            t2i_official_model=settings.drama_t2i_official_model,
            t2i_official_submit_path=settings.drama_t2i_official_submit_path,
            t2i_official_max_refs=settings.drama_t2i_official_max_refs,
            t2i_size_policy=settings.drama_t2i_size_policy,
            i2v_official_template_uuid=settings.drama_i2v_official_template_uuid,
            i2v_model=settings.drama_i2v_model,
            i2v_mode=settings.drama_i2v_mode,
            i2v_sound=settings.drama_i2v_sound,
            public_base_url=settings.drama_public_base_url,
            media_timeout=settings.drama_media_timeout,
            poll_seconds=settings.drama_poll_seconds,
            ffmpeg=settings.drama_ffmpeg,
            ai_label=settings.drama_ai_label,
            ai_disclosure=settings.drama_ai_disclosure,
        )
    )
    if settings.drama_provider == "remote":
        drama.set_provider(
            RemoteDramaProvider(
                RemoteDramaConfig(
                    llm_base_url=settings.drama_llm_base_url,
                    llm_model=settings.drama_llm_model,
                    llm_key_env=settings.drama_llm_key_env,
                    llm_timeout=settings.drama_llm_timeout,
                    tts_url=settings.drama_tts_url,
                    tts_key_env=settings.drama_tts_key_env,
                    tts_timeout=settings.drama_tts_timeout,
                    libtv_base_url=settings.libtv_base_url,
                    access_key_env=settings.libtv_access_key_env,
                    secret_key_env=settings.libtv_secret_key_env,
                    t2i_template_uuid=settings.drama_t2i_template_uuid,
                    t2i_prompt_node=settings.drama_t2i_prompt_node,
                    t2i_reference_node=settings.drama_t2i_reference_node,
                    t2i_reference_field=settings.drama_t2i_reference_field,
                    t2i_extra_nodes=json.loads(settings.drama_t2i_extra_nodes),
                    t2i_channel=settings.drama_t2i_channel,
                    t2i_official_template_uuid=settings.drama_t2i_official_template_uuid,
                    t2i_official_model=settings.drama_t2i_official_model,
                    t2i_official_submit_path=settings.drama_t2i_official_submit_path,
                    t2i_official_max_refs=settings.drama_t2i_official_max_refs,
                    t2i_size_policy=settings.drama_t2i_size_policy,
                    t2i_prompt_rewrite_url=settings.drama_t2i_prompt_rewrite_url,
                    i2v_template_uuid=settings.drama_i2v_template_uuid,
                    i2v_prompt_node=settings.drama_i2v_prompt_node,
                    i2v_extra_nodes=json.loads(settings.drama_i2v_extra_nodes),
                    i2v_first_frame_node=settings.drama_i2v_first_frame_node,
                    i2v_first_frame_field=settings.drama_i2v_first_frame_field,
                    i2v_channel=settings.drama_i2v_channel,
                    i2v_official_template_uuid=settings.drama_i2v_official_template_uuid,
                    i2v_model=settings.drama_i2v_model,
                    i2v_mode=settings.drama_i2v_mode,
                    i2v_sound=settings.drama_i2v_sound,
                    public_base_url=settings.drama_public_base_url,
                    media_timeout=settings.drama_media_timeout,
                    poll_seconds=settings.drama_poll_seconds,
                    ffmpeg=settings.drama_ffmpeg,
                    ai_label=settings.drama_ai_label,
                    ai_disclosure=settings.drama_ai_disclosure,
                ),
                drama.media_store,
            )
        )
    return drama


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    admins = _admins(settings)
    drama = _build_service(settings, admins)
    # 账户、会话与充值订单与项目数据共用同一个数据库。
    accounts = AccountStore(drama.store)
    payments = PaymentService(drama.store, drama)

    @asynccontextmanager
    async def lifespan(app):
        await drama.start()
        app.state.drama = drama
        try:
            yield
        finally:
            await drama.close()

    app = FastAPI(title="AI Comic Drama Studio", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, max_bytes=settings.request_bytes)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[settings.host, "127.0.0.1", "localhost", "[::1]", *settings.allowed_hosts],
    )
    rate: dict[str, tuple[int, float]] = {}

    async def same_origin(request: Request) -> None:
        """拒绝跨站请求，以及试图通过请求头自报身份的请求。"""
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise AppError("origin_forbidden", "cross-origin API access denied", 403)
        if any(
            name in request.headers for name in ("x-principal", "x-owner", "x-role", "x-scopes")
        ):
            raise AppError(
                "identity_spoof", "identity headers cannot grant principal or permissions", 403
            )

    async def authenticate(request: Request) -> Principal:
        await same_origin(request)
        session_token = request.cookies.get(SESSION_COOKIE, "")
        if session_token:
            owner = await asyncio.to_thread(accounts.owner_for_session, session_token)
            if owner:
                return Principal(owner=owner)
        if settings.require_login:
            # 公网部署：没有有效会话就不能回退成本机账号。
            raise AppError("account_session_required", "请先登录", 401)
        if settings.auth_mode == "local":
            if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
                raise AppError("local_only", "local identity requires a loopback peer", 403)
            return Principal(owner="local")
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            raise AppError("unauthorized", "server-configured bearer token required", 401)
        owner = next(
            (v for k, v in settings.bearer_principals.items() if hmac.compare_digest(k, token[7:])),
            None,
        )
        if not owner:
            raise AppError("unauthorized", "invalid bearer token", 401)
        return Principal(owner=owner)

    async def require_session(request: Request) -> str:
        """账户类动作必须有有效会话，不能回退到本机账号。"""
        await same_origin(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        owner = await asyncio.to_thread(accounts.owner_for_session, token) if token else None
        if not owner:
            raise AppError("account_session_required", "请先登录", 401)
        return owner

    async def principal(request: Request) -> Principal:
        identity = await authenticate(request)
        now = time.monotonic()
        count, start = rate.get(identity.owner, (0, now))
        if now - start >= 60:
            count, start = 0, now
        if count >= settings.rate_per_minute:
            raise AppError("rate_limit", "principal rate quota exceeded", 429)
        rate[identity.owner] = count + 1, start
        request.state.principal = identity.owner
        return identity

    @app.middleware("http")
    async def correlate(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CSP
        logger.info(
            "http request=%s method=%s status=%s",
            request.state.request_id,
            request.method,
            response.status_code,
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            {
                "error": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "request_id": getattr(request.state, "request_id", None),
            },
            status_code=exc.status,
            headers={"Retry-After": "1"} if exc.status in (429, 503) else None,
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": "validation_error",
                "details": [{"loc": x["loc"], "type": x["type"]} for x in exc.errors()],
            },
            status_code=422,
        )

    @app.exception_handler(TimeoutError)
    async def timeout(request, exc):
        return JSONResponse(
            {"error": "deadline_exceeded", "message": "total request deadline exceeded"},
            status_code=408,
        )

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"live": True}

    mount_drama_routes(app, lambda: drama, principal)
    mount_account_routes(app, accounts, same_origin, require_session, admins)
    mount_payment_routes(app, payments, principal)
    # 前端静态页挂在根路径，必须放在所有 API 路由之后。
    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
    return app
