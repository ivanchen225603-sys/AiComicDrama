"""服务配置。全部可用 `COMIC_` 前缀的环境变量或 `.env` 覆盖，例如 `COMIC_PORT=8080`。"""

import ipaddress
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PRINCIPAL = r"[A-Za-z0-9_-]{1,64}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COMIC_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        allow_inf_nan=False,
    )

    # ---------- 服务与访问控制 ----------
    host: str = "127.0.0.1"
    port: int = Field(8001, ge=1024, le=65535)
    data_dir: Path = Path(".runtime")
    # local = 本机访问即本机账号（离线演示）；bearer = 服务端配置的静态令牌。
    auth_mode: Literal["local", "bearer"] = "local"
    # 公网部署（Nginx 反向代理）时访客都从本机转发进来，会被当成本机账号。
    # 打开后未登录的请求一律 401，每个访客必须注册 / 登录自己的账号。
    require_login: bool = False
    # 允许的访问域名（Host 头），默认只允许本机地址。
    # 例如 COMIC_ALLOWED_HOSTS='["drama.example.com"]'
    allowed_hosts: list[str] = Field(default_factory=list)
    bearer_principals: dict[str, str] = Field(default_factory=dict, exclude=True)
    request_bytes: int = Field(65536, ge=1024, le=1048576)
    rate_per_minute: int = Field(120, ge=1, le=100000)
    # 可以给账户加积分、处理待复核任务的管理员账号。
    credit_admins: list[str] = Field(default_factory=list)

    # ---------- Liblib 开放平台 ----------
    libtv_base_url: str = "https://openapi.liblibai.cloud"
    libtv_access_key_env: str = "LIBTV_ACCESS_KEY"
    libtv_secret_key_env: str = "LIBTV_SECRET_KEY"

    # ---------- 生成能力 ----------
    # mock = 本机示例素材；remote = 使用部署方在环境变量里配置的全局密钥。
    # 用户在「模型设置」页填写的自有密钥不受这个开关影响。
    drama_provider: Literal["mock", "remote"] = "mock"
    drama_llm_base_url: str = ""  # 指到 /v1，代码补 /chat/completions
    drama_llm_model: str = ""
    drama_llm_key_env: str = "DRAMA_LLM_API_KEY"
    drama_llm_timeout: float = Field(60.0, ge=1, le=300)
    drama_tts_url: str = ""
    drama_tts_key_env: str = "DRAMA_TTS_API_KEY"
    drama_tts_timeout: float = Field(60.0, ge=1, le=300)
    # 文生图：workflow = ComfyUI 工作流（需模板与节点）；official = 官方 seedreamV4。
    drama_t2i_channel: Literal["workflow", "official"] = "workflow"
    drama_t2i_template_uuid: str = ""
    drama_t2i_prompt_node: str = ""
    # 关键帧的参考图节点；留空时参考图只进提示词。
    drama_t2i_reference_node: str = ""
    drama_t2i_reference_field: str = "image"
    drama_t2i_extra_nodes: str = "{}"
    drama_t2i_official_template_uuid: str = "0b6bad2fd350433ebb5abc7eb91f2ec9"
    drama_t2i_official_model: str = "doubao-seedream-4-5-251128"
    drama_t2i_official_submit_path: str = "/api/generate/seedreamV4"
    # 官方接口接受 1–14 张参考图；超限直接报错，绝不截断。
    drama_t2i_official_max_refs: int = Field(14, ge=1, le=14)
    # normalize = 按画幅比例换算到官方尺寸档；strict = 越界即拒。
    drama_t2i_size_policy: Literal["normalize", "strict"] = "normalize"
    # 提示词改写服务地址：暂未实现，非空时调用会显式报错。
    drama_t2i_prompt_rewrite_url: str = ""
    # 图生视频：workflow = ComfyUI 工作流；official = 官方 Kling 接口。
    drama_i2v_channel: Literal["workflow", "official"] = "workflow"
    drama_i2v_template_uuid: str = ""
    drama_i2v_prompt_node: str = ""
    drama_i2v_extra_nodes: str = "{}"
    # 首帧图节点；留空表示工作流不接受首帧图，传了图会显式报错而不是悄悄丢掉。
    drama_i2v_first_frame_node: str = ""
    drama_i2v_first_frame_field: str = "image"
    drama_i2v_official_template_uuid: str = "180f33c6748041b48593030156d2a71d"
    drama_i2v_model: str = "kling-v2-5-turbo"
    drama_i2v_mode: str = "pro"
    drama_i2v_sound: str = "off"
    # 视频生成需要：Liblib 通过这个公网 https 地址拉取首帧图片。
    drama_public_base_url: str = ""
    drama_media_timeout: float = Field(900.0, ge=30, le=3600)
    drama_poll_seconds: float = Field(2.0, ge=0.2, le=30)
    drama_ffmpeg: str = "ffmpeg"

    # ---------- 运营 ----------
    # 每个账号每天最多消耗的积分（0 = 不限）。
    drama_daily_cap: int = Field(500, ge=0, le=100000)
    # 服务端自动推进生成任务，前端不必反复触发。
    drama_autopump: bool = True
    drama_pump_seconds: float = Field(2.0, ge=0.2, le=60)
    # 每个账号的素材空间上限（MB，0 = 不限）。
    drama_media_quota_mb: int = Field(0, ge=0, le=102400)
    # AI 生成内容标识文案与位置。
    drama_ai_label: str = "AI 生成"
    drama_ai_disclosure: str = ""
    drama_ai_label_positions: str = "container_metadata"
    # 内容审核：local = 本地敏感词表；null = 不审核。
    drama_audit_provider: Literal["local", "null"] = "local"
    drama_audit_blocklist: str = ""
    # 上游判定违规后的积分处理：hold = 冻结待复核；refund = 退回；settle = 照常结算。
    drama_audit_reject_action: Literal["hold", "refund", "settle"] = "hold"
    # 成片字幕：soft = 字幕轨；burn = 烧录进画面。
    drama_subtitle_mode: Literal["soft", "burn"] = "soft"
    drama_output_resolution: Literal["source", "720p", "1080p"] = "source"
    # 生成素材保留天数（0 = 永久保留）。
    drama_media_retention_days: int = Field(0, ge=0, le=3650)
    # 仅示例模式生效的一次性故障注入（逗号分隔动作名），用于演示「上游超时 -> 待复核」。
    drama_mock_fault: str = ""

    @field_validator("drama_audit_reject_action", mode="before")
    @classmethod
    def _clamp_reject_action(cls, value: object) -> str:
        """拼错时回退到积分可回滚的 hold，而不是让服务启动失败。"""
        text = str(value or "").strip().lower()
        return text if text in ("hold", "refund", "settle") else "hold"

    @field_validator("drama_subtitle_mode", mode="before")
    @classmethod
    def _clamp_subtitle_mode(cls, value: object) -> str:
        """拼错时回退到不需要重编码的 soft。"""
        text = str(value or "").strip().lower()
        return text if text in ("soft", "burn") else "soft"

    @field_validator("drama_output_resolution", mode="before")
    @classmethod
    def _clamp_output_resolution(cls, value: object) -> str:
        """拼错时回退到 source（不缩放）。"""
        text = str(value or "").strip().lower()
        return text if text in ("source", "720p", "1080p") else "source"

    @model_validator(mode="after")
    def safety(self):
        if any(
            not re.fullmatch(_PRINCIPAL, v) or v in ("public", "system", "local")
            for v in self.bearer_principals.values()
        ):
            raise ValueError("bearer owners must be valid non-reserved principal IDs")
        loopback = self.host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            pass
        if self.auth_mode == "local" and not loopback and not self.require_login:
            # 打开 require_login 后不存在本机账号回退，可以安全地绑定到容器 / 反向代理。
            raise ValueError(
                "non-loopback binding requires bearer identity or COMIC_REQUIRE_LOGIN=true"
            )
        if self.auth_mode == "bearer" and (
            not self.bearer_principals or any(len(k) < 24 for k in self.bearer_principals)
        ):
            raise ValueError("bearer mode requires tokens of at least 24 characters")
        if any(not re.fullmatch(_PRINCIPAL, value) for value in self.credit_admins):
            raise ValueError("credit admins must be valid principal IDs")
        if self.auth_mode == "bearer" and not set(self.credit_admins) <= set(
            self.bearer_principals.values()
        ):
            raise ValueError("bearer mode requires credit admins to be bearer principals")
        if not self.libtv_access_key_env or not self.libtv_secret_key_env:
            raise ValueError("Liblib credential environment variable names must not be empty")
        if self.drama_provider == "remote":
            self._check_remote()
        return self

    def _check_remote(self) -> None:
        if not self.drama_llm_base_url or not self.drama_llm_model:
            raise ValueError("remote provider requires an LLM base url and model")
        if self.drama_t2i_channel == "official":
            if not self.drama_t2i_official_template_uuid:
                raise ValueError("official t2i requires COMIC_DRAMA_T2I_OFFICIAL_TEMPLATE_UUID")
        elif not self.drama_t2i_template_uuid or not self.drama_t2i_prompt_node:
            raise ValueError("workflow t2i requires a templateUuid and prompt node")
        for name in ("drama_i2v_extra_nodes", "drama_t2i_extra_nodes"):
            try:
                value = json.loads(getattr(self, name))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} must be a JSON object") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a JSON object")
        if self.drama_i2v_channel == "official":
            # 配错在启动时就失败，不要等到用户点了「图生视频」才发现。
            base = self.drama_public_base_url.strip()
            if not base:
                raise ValueError(
                    "official image-to-video requires COMIC_DRAMA_PUBLIC_BASE_URL "
                    "(Liblib only accepts a publicly reachable first-frame URL)"
                )
            if not base.startswith("https://"):
                raise ValueError("COMIC_DRAMA_PUBLIC_BASE_URL must start with https://")
