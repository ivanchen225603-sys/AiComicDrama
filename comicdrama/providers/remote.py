"""真实漫剧 Provider：10 个生成能力全部走上游，默认不实例化、不出网。

事实边界（2026-11-10，未调用任何收费接口，不得写成「已验证可用」）：
【按公开通用契约实现】
  - LLM 剧本/分镜/实体抽取/音色推荐：OpenAI 兼容 /chat/completions + JSON 输出；
    凭据按环境变量名在调用时读取。
  - 文生图 / 图生视频：复用 liblib.client.LibTVMediaProvider 的任务级
    API（submit/poll/fetch、签名、QPS-1/并发-5、7 天 URL 立即转存）。
  - 成片合成：本地 FFmpeg concat（用户已选定本地合成）。
【本地约定，待供应商确定，绝不当成上游事实】
  - TTS：POST 到可配置端点，直接返回音频 bytes；请求体与鉴权头形状未实测。
【充值后必须实测、目前只能防御性探测】
  - LLM 是否支持 response_format=json_object、响应包装形态；
  - LibTV templateUuid / prompt 节点 / videos[] 容器与 URL 字段 / 提交响应外层包装；
  - 单次真实计费字段、OSS 域名白名单、7 天 URL 时效。

因此本模块有一条硬规则：**任何上游响应形状不符都显式报错，绝不回退 mock**。
静默回退会让「真实生成」看起来成功、实际仍是占位，比直接失败更糟。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..compose import (
    FilmPlan,
    build_plan,
    mux_args,
    parse_resolution,
    parse_subtitle_mode,
    render_film,
    srt_text,
    srt_time,
)
from ..errors import AppError
from ..liblib.client import LibTVMediaProvider
from ..liblib.contracts import ProviderError
from ..liblib.rate_limiter import RateLimiter
from ..media_store import DramaMediaStore
from ..sample_media import dimensions, prompt_with_settings
from .base import result

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_SHOT_DEFAULTS: dict[str, Any] = {
    "dur": 3,
    "size": "中近景",
    "desc": "",
    "prompt": "",
    "line": "",
    "move": "微推",
    "refs": [],
}

# ---------------------------------------------------------------- seedreamV4 尺寸口径
# 官方文生图（seedreamV4）接受的总像素区间，**已实测**：
# 「Total pixels must be between 3,686,400 and 16,777,216」（参数校验顺序
# prompt -> model -> 尺寸 -> 像素；越界返回 100050 且**不扣费**）。
_SEEDREAM_MIN_PIXELS = 3_686_400
_SEEDREAM_MAX_PIXELS = 16_777_216

# 三档官方画幅。三档的宽高积**都正好等于下限 3,686,400（零余量）**。
# 边界**已实测（2026-11-11）**：1440×2560 被接受并出图，下载回来的 PNG 头实测
# 1440 × 2560 = 3,686,400 像素，所以下限是**闭区间**，三档常量不用乘 1.15。
# 像素档位与单价的关系仍未验证（只测了这一档，实测 25 积分/张），
# 所以不要按尺寸推算单价；`_seedream_size` 也不做自动放大梯。
_SEEDREAM_FRAMES: dict[tuple[int, int], tuple[int, int]] = {
    (9, 16): (1440, 2560),
    (16, 9): (2560, 1440),
    (1, 1): (1920, 1920),
}
_SEEDREAM_FALLBACK_ORDER: tuple[tuple[int, int], ...] = ((9, 16), (16, 9), (1, 1))

# 中文提示词的已知限制：不翻译（翻译要接一个未定的文本模型，属于第 3 步），
# 但必须显式标注，否则用户会把「画面和分镜描述有偏差」当成模型能力问题。
PROMPT_LANGUAGE_NOTE = "中文提示词：可出图，但画面内容可能与分镜描述存在偏差（已知限制）"


def _ratio_of(settings: dict[str, Any]) -> tuple[int, int] | None:
    """从项目设定解析画幅比例：先读 `ratio`（如「9:16 竖屏」），再退化到 `res`。

    两处都解析不出时返回 None，由调用方抛 `drama_t2i_size`——绝不回落到 1:1
    之类的默认值，那会让「设定写错了」变成「悄悄出了一张方图」。
    """
    match = re.search(r"(\d+)\s*[:：]\s*(\d+)", str(settings.get("ratio") or ""))
    if match is None:
        match = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", str(settings.get("res") or ""))
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _with_usage(error: AppError, usage: dict[str, Any] | None) -> AppError:
    """把已确认的上游自报用量挂到异常上，返回同一个异常对象。

    这是与 `DramaService._upstream_of()` 的**隐式契约**：服务层在
    `_hold_for_review` 与 `_finish_task` 里用 `getattr(exc, "usage", None)`
    读这个属性。属性名的失效方式是「改名后静默返回 None」，不会抛错也不会
    被类型检查发现，所以在本模块里这个字符串**只允许出现在这里一次**，
    不要在别处再 `setattr(error, "usage", ...)`。用量为空时不挂属性，
    服务层会把上游成本记成 NULL（未知），绝不用预算常量回填。
    """
    if usage:
        error.usage = dict(usage)  # type: ignore[attr-defined]
    return error


def _worst_audit_status(assets: Any) -> str | None:
    """从 `polled.assets`（`RemoteAsset.meta`）里取最严重的 `auditStatus`。

    "最严重"的定义刻意写死成两条，不做任何语义猜测：

    1. 只要有任意一个资产是 `"4"`，就取 `"4"`（本期唯一确认的"审核拒绝"）；
    2. 否则取**第一个非空**字符串（例如实测见过的 `"3"`，语义未定，只做留痕）；
    3. 全空（或没有资产、meta 不是 dict）返回 `None`——表示"不知道"，
       绝不写 `"0"` / `""` 顶替，否则下游会把"未知"当成"已通过"。
    """

    first: str | None = None
    for asset in assets or ():
        meta = getattr(asset, "meta", None)
        if not isinstance(meta, dict):
            continue
        value = meta.get("auditStatus")
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if cleaned == "4":
            return "4"
        if first is None:
            first = cleaned
    return first


@dataclass(frozen=True)
class RemoteDramaConfig:
    """只存环境变量「名」与端点，绝不存密钥明文。"""

    llm_base_url: str  # 指向 /v1，例如 https://api.example.com/v1
    llm_model: str
    llm_key_env: str = "DRAMA_LLM_API_KEY"
    llm_timeout: float = 60.0
    tts_url: str = ""
    tts_key_env: str = "DRAMA_TTS_API_KEY"
    tts_timeout: float = 60.0
    # "custom"：部署方自定的 {text, voice} 端点（既有）。
    # "openai"：OpenAI 兼容的 POST {tts_url}/audio/speech（OpenAI、硅基流动等都支持）。
    tts_mode: str = "custom"
    tts_model: str = ""
    # 可用音色（上游的音色 id）。多个时按角色名稳定分配，让不同角色声音不同。
    tts_voices: tuple[str, ...] = ()
    libtv_base_url: str = "https://openapi.liblibai.cloud"
    access_key_env: str = "LIBTV_ACCESS_KEY"
    secret_key_env: str = "LIBTV_SECRET_KEY"
    t2i_template_uuid: str = ""
    t2i_prompt_node: str = ""
    # 文生图「参考图/锚点图」节点。上游形状至今未实测，所以两个字段名都必须由
    # 配置给出；留空时锚点只以文本形式进提示词，并在结果里如实标注 prompt_only，
    # 绝不硬编码猜测的节点键。首单冒烟要按真实模板核对这两项。
    t2i_reference_node: str = ""
    t2i_reference_field: str = "image"
    t2i_extra_nodes: dict[str, Any] = field(default_factory=dict)
    # 文生图走哪条通道：
    #   "workflow" —— 社区 ComfyUI 工作流（既有实现），尺寸由模板决定，不归一化。
    #   "official" —— 官方星流文生图 seedreamV4（POST /api/generate/seedreamV4）。
    #                 模型名 / 模板 / 端点 / 总像素区间均已实测（见模块头与
    #                 `_SEEDREAM_MIN_PIXELS`），但**未做任何付费调用**。
    t2i_channel: str = "workflow"
    # 官方通道的模板 UUID 与模型名：实测为固定值，不随账户变化，故给默认值。
    t2i_official_template_uuid: str = "0b6bad2fd350433ebb5abc7eb91f2ec9"
    t2i_official_model: str = "doubao-seedream-4-5-251128"
    t2i_official_submit_path: str = "/api/generate/seedreamV4"
    # 上游口径 1–14 张参考图；本地上限是它内部的保守值，**超限硬报错、绝不截断**
    # （参考图决定角色一致性，静默少传一张等于悄悄改出图内容）。
    t2i_official_max_refs: int = 14
    # 尺寸策略：normalize = 按画幅比例换算到官方三档再提交（默认）；
    #          strict    = 不改写用户设定，原始尺寸不在上游像素区间就本地拒绝。
    t2i_size_policy: str = "normalize"
    # 提示词翻译/改写接缝：非空即表示「配置了但本期没实现」，调用时显式报错，
    # 绝不静默忽略（静默忽略会让运维以为改写已生效）。
    t2i_prompt_rewrite_url: str = ""
    i2v_template_uuid: str = ""
    i2v_prompt_node: str = ""
    # 上游图生视频的额外节点参数（如首帧图节点）形状未实测，由配置按实测结果注入，
    # 这里不硬编码任何猜测的字段名。
    i2v_extra_nodes: dict[str, Any] = field(default_factory=dict)
    # 首帧图节点。两个字段名都必须由配置给出：上游「图生视频」的节点形状至今
    # 未实测，硬编码任何猜测的键都是在编造契约。留空时若调用方给了首帧图，
    # Provider 会显式报错而不是丢掉图片。
    i2v_first_frame_node: str = ""
    i2v_first_frame_field: str = "image"
    # 图生视频走哪条通道：
    #   "workflow" —— 社区 ComfyUI 工作流（既有实现）。实测该通道在当前账户下
    #                 11 秒必失败：generateMsg="[-1]执行异常"、不扣费。保留只为兼容既有配置。
    #   "official" —— 官方 Kling 模型端点 /api/generate/video/kling/img2video。
    #                 参数与返回已端到端实测：提交成功、status=5、单条 5 秒 250 积分、
    #                 成片 5.04s / h264 / 无音轨。
    i2v_channel: str = "workflow"
    # 官方通道的模板 UUID。实测为固定值，不随账户变化，所以给默认值而不是强制配置。
    i2v_official_template_uuid: str = "180f33c6748041b48593030156d2a71d"
    i2v_model: str = "kling-v2-5-turbo"
    i2v_mode: str = "pro"  # v2-5 / v2-6 的 mode 必须是 pro
    i2v_sound: str = "off"  # 原生音效只有 v2-6 及以上支持
    # 官方通道只接受公网可访问的首帧图 URL：传本地路径或相对地址会返回
    # 100030「图片地址无法访问」。开放平台实测没有图片上传接口，
    # 所以由部署方给出对外基址，我们把 /api/v1/drama/assets/<id> 拼上去。
    public_base_url: str = ""
    media_timeout: float = 900.0
    poll_seconds: float = 2.0
    ffmpeg: str = "ffmpeg"
    # AI 生成标识：写进成片容器元数据（-metadata）。留空则跳过，不写空值。
    ai_label: str = ""
    # AI 生成披露文案。留空则由 compose 回落到 labels.AI_DISCLOSURE：
    # `/config` 下发的文案与实际写进容器的文案必须是同一个字符串，否则就是对用户
    # 虚报（页面写一套、文件里写另一套）。
    ai_disclosure: str = ""
    # 用户在页面填写的密钥，按「环境变量名 → 值」存放，命中则优先于环境变量。
    # 只在内存里随 Provider 存活，不写日志、不进 repr。
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    # 文本模型是否必需。按能力组合的部署里文本可以回落到内置规则，此时不要求配置。
    require_llm: bool = True


class RemoteDramaProvider:
    name = "remote"

    def __init__(self, config: RemoteDramaConfig, media_store: DramaMediaStore) -> None:
        if config.require_llm and (not config.llm_base_url or not config.llm_model):
            raise ValueError("remote drama provider requires an LLM endpoint and model")
        if config.t2i_channel == "official":
            # 官方通道不用工作流的模板/提示词节点，但必须有官方模板 UUID。
            # 要求工作流模板等于把「只想用官方文生图」的部署挡在启动之外。
            if not config.t2i_official_template_uuid:
                raise ValueError("official t2i requires a templateUuid for seedreamV4")
        elif not config.t2i_template_uuid or not config.t2i_prompt_node:
            raise ValueError("remote drama provider requires a t2i templateUuid and prompt node")
        self.cfg = config
        self.media_store = media_store
        # 整个客户端共享一个节流器：官方 QPS-1 / 并发-5 是按客户端整体计。
        self._limiter = RateLimiter(qps=1.0, concurrency=5)

    # ---------------------------------------------------------------- 底层调用

    def _libtv_credentials(self) -> tuple[str, str] | None:
        access = self.cfg.secrets.get(self.cfg.access_key_env, "")
        secret = self.cfg.secrets.get(self.cfg.secret_key_env, "")
        return (access, secret) if access and secret else None

    def _key(self, env_name: str) -> str:
        key = self.cfg.secrets.get(env_name) or os.environ.get(env_name)
        if not key:
            raise AppError(
                "drama_credentials", f"credential environment variable {env_name} is unset", 503
            )
        return key

    def _chat(self, system: str, user: str, required: tuple[str, ...]) -> dict[str, Any]:
        key = self._key(self.cfg.llm_key_env)
        url = f"{self.cfg.llm_base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.cfg.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.llm_timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AppError("drama_llm_unavailable", f"LLM request failed: {exc}", 502) from exc
        try:
            envelope = json.loads(raw)
            content = envelope["choices"][0]["message"]["content"]
            parsed = json.loads(_FENCE.sub("", content.strip()))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # 充值后第一件要核对的就是这里：端点是否支持 json_object、包装形态如何。
            raise AppError(
                "drama_llm_shape",
                "LLM response envelope not recognised; upstream shape is NOT VERIFIED",
                502,
            ) from exc
        if not isinstance(parsed, dict) or any(k not in parsed for k in required):
            raise AppError("drama_llm_payload", f"LLM JSON missing required keys {required}", 502)
        return parsed

    def _pick_voice(self, character: str, fallback: str) -> str:
        voices = [v for v in self.cfg.tts_voices if v]
        if not voices:
            return fallback
        digest = hashlib.sha256(str(character or "").encode("utf-8")).digest()
        return voices[digest[0] % len(voices)]

    def _tts_openai(self, text: str, voice: str) -> bytes:
        key = self._key(self.cfg.tts_key_env)
        body = {
            "model": self.cfg.tts_model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
        }
        request = urllib.request.Request(
            self.cfg.tts_url.rstrip("/") + "/audio/speech",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.tts_timeout) as response:
                kind = str(response.headers.get("Content-Type") or "")
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AppError("drama_tts_config", "配音服务密钥无效", 503) from exc
            raise AppError("drama_tts_unavailable", f"TTS HTTP {exc.code}", 502) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AppError("drama_tts_unavailable", f"TTS request failed: {exc}", 502) from exc
        if not data or "json" in kind:
            # 有的服务出错时仍回 200 + JSON，不能把错误信息当音频存下来。
            raise AppError("drama_tts_unavailable", "TTS returned no audio", 502)
        return data

    def _tts_audio(self, text: str, voice: str, character: str = "") -> bytes:
        if not self.cfg.tts_url:
            raise AppError("drama_tts_config", "TTS endpoint is not configured", 503)
        if self.cfg.tts_mode == "openai":
            return self._tts_openai(text, self._pick_voice(character, voice))
        key = self._key(self.cfg.tts_key_env)
        # 请求体形状是本地约定，供应商确认后要按真实契约改；不臆造上游专有字段。
        body = {"text": text, "voice": voice}
        request = urllib.request.Request(
            self.cfg.tts_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.tts_timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AppError("drama_tts_unavailable", f"TTS request failed: {exc}", 502) from exc

    def _libtv_client(self, template_uuid: str, prompt_node: str, extra: dict[str, Any]):
        return LibTVMediaProvider(
            base_url=self.cfg.libtv_base_url,
            access_key_env=self.cfg.access_key_env,
            secret_key_env=self.cfg.secret_key_env,
            template_uuid=template_uuid,
            prompt_node=prompt_node,
            extra_nodes=extra,
            rate_limiter=self._limiter,
            credentials=self._libtv_credentials(),
        )

    def _generate(
        self,
        client: LibTVMediaProvider,
        prompt: str,
        *,
        width: int = 1024,
        height: int = 1024,
        usage: dict[str, Any] | None = None,
    ) -> bytes:
        """任务级闭环：submit -> 有界 poll -> 立即 fetch（URL 只有 7 天）。

        width / height 来自项目设定（见 `_dimensions`），不再是写死的方图。
        上游模板若不接受该尺寸会在 submit 显式报错，绝不静默回退到 mock。

        usage 是一个可选的出参容器：上游在成功响应里自报 pointsCost / accountBalance 时
        由调用方填进去。用出参而不是改返回值，是为了不动另外三个只关心字节的调用点。

        实测（2026-11-11）：这两个字段**只在 status=5 终态出现**，submit 响应与
        running 阶段的轮询里都是 None（字段本身不存在）。所以只能在下面的
        `state == "succeeded"` 分支读，不要为了「早点拿到余额」在提交处或更早去读，
        那里读到的永远是 None，会被误写成「上游免费」。
        """

        async def run() -> bytes:
            submitted = await client.submit(
                task_id=uuid.uuid4().hex[:16],
                request_id=uuid.uuid4().hex[:16],
                prompt=prompt,
                width=width,
                height=height,
            )
            deadline = time.monotonic() + self.cfg.media_timeout
            while time.monotonic() < deadline:
                polled = await client.poll(submitted.upstream_task_id)
                if polled.state == "failed":
                    raise AppError(
                        "drama_generation_failed", polled.error or "upstream generation failed", 502
                    )
                if polled.state == "held":
                    raise AppError("drama_generation_held", "upstream result is indeterminate", 503)
                if polled.state == "succeeded":
                    # 先把上游自报用量记下来，再判素材：`status=5` 但拿不到可转存
                    # 素材时上游**已经扣费**，这个数字是管理员判断结算/退款的关键。
                    if usage is not None:
                        usage["upstream_task_id"] = submitted.upstream_task_id
                        usage["upstream_points"] = polled.upstream_points
                        usage["account_balance"] = polled.account_balance
                        # 上游审核态（`images[].auditStatus`）只在终态出现，和计费字段
                        # 同一时机。它是"最严重的那一个"，全空就不写这个键——保持
                        # 缺失 = 未知，绝不写 "0" / "" 假装已通过。
                        audit_status = _worst_audit_status(polled.assets)
                        if audit_status is not None:
                            usage["audit_status"] = audit_status
                    if not polled.assets:
                        error = AppError(
                            "drama_result_missing",
                            "upstream reported success without assets",
                            502,
                        )
                        # 结果未知但上游已扣费：把真值挂在异常上，服务层的
                        # 待复核凭据才能显示「上游已扣 N 积分」。
                        raise _with_usage(error, usage)
                    return await client.fetch(polled.assets[0])
                await asyncio.sleep(self.cfg.poll_seconds)
            raise AppError("drama_generation_timeout", "upstream task timed out", 504)

        try:
            # Provider 方法同步，且 DramaService 把生成放在 asyncio.to_thread 里，
            # 该线程没有运行中的事件循环，可以安全地起一个临时 loop。
            return asyncio.run(run())
        except ProviderError as exc:
            # 带上上游的业务子原因（例如「Total pixels must be between …」）：
            # 只报 `exc.code` 会让 502 变成无法归因的「media task failed」，
            # 首单排查（比如像素下限是否含等号）就只能靠猜。
            # 安全边界：`ProviderError.message` 只由本模块与 libtv 适配器构造，
            # 不含任何凭据值（libtv 的 `_post` 从不回传响应体，`_credentials`
            # 只报环境变量名），所以这里只做长度截断，不做额外拼装。
            detail = str(exc.message or "").strip()[:200]
            text = f"media task failed: {exc.code}"
            if detail:
                text = f"{text}: {detail}"
            error = AppError("drama_upstream", text, 502)
            # 同上：上游可能已经扣了钱（例如素材下载失败），把已知用量挂上去，
            # 供服务层落成「待复核 + 上游已扣 N 积分」，而不是一个纯 0。
            raise _with_usage(error, usage) from exc

    def _persist(self, owner: str, kind: str, data: bytes) -> dict[str, Any]:
        return self.media_store.store(owner, kind, data)

    @staticmethod
    def _note() -> str:
        return "由真实 Provider 生成；上游响应形状/计费字段仍需按首次付费实测结果核对"

    @staticmethod
    def _dimensions(settings: dict[str, Any]) -> tuple[int, int]:
        """项目设定 -> submit 宽高。与离线示例共用 sample_media.dimensions。"""
        return dimensions(settings)

    @staticmethod
    def _prompt_with_settings(prompt: str, settings: dict[str, Any]) -> str:
        """画风 / 画幅拼进提示词；分辨率走 submit 的宽高参数。"""
        return prompt_with_settings(prompt, settings)

    # ---------------------------------------------------------------- 官方文生图口径

    @property
    def channels(self) -> dict[str, str]:
        """当前真正在用的通道，供 `/config` 如实下发（mock 没有这个属性）。"""
        return {"image-to-video": self.cfg.i2v_channel, "keyframe": self.cfg.t2i_channel}

    def _seedream_size(self, settings: dict[str, Any]) -> tuple[int, int, bool]:
        """项目设定 -> seedreamV4 接受的 (宽, 高, 是否被调整)。

        为什么这一层必须存在：`e66bedb` 之后通用 `validate()` 用**有界规则**判尺寸，
        而产品默认 `1080×1920`（2,073,600 像素）作为一张图片完全合法，会被放行；
        但上游要求总像素 ≥ 3,686,400。所以「通用合法性」与「上游受理范围」是两件事，
        归一化只能发生在这里（唯一出口），不能塞进 `contracts.size_rejection()`
        ——那会让通用契约层带上某一家上游的业务事实。

        规则（不自行扩展）：
        1. 先解析画幅比例（`ratio`，退化到 `res`）；
        2. 比例恰好命中三档 -> 用该档；与项目原始尺寸不同则 `adjusted=True`；
        3. 未命中 -> 取「宽高比差绝对值最小」的档，`adjusted=True`；
        4. 两处都解析不出 -> `drama_t2i_size`（本地拒绝，未出网）；
        5. 返回前断言总像素落在上游区间，否则同样抛 `drama_t2i_size`；
        6. `t2i_size_policy == "strict"` 时**不改写**用户设定：原始尺寸不在区间内就拒绝。

        为什么 strict 也不悄悄归一化：strict 的语义就是「运维要求原样提交」，
        归一化会违背它；而拒绝的后果只是本地 400 + 全额退款。
        """
        raw_width, raw_height = self._dimensions(settings)
        policy = str(self.cfg.t2i_size_policy or "normalize")
        if policy == "strict":
            if not _SEEDREAM_MIN_PIXELS <= raw_width * raw_height <= _SEEDREAM_MAX_PIXELS:
                raise AppError(
                    "drama_t2i_size",
                    f"项目分辨率 {raw_width}×{raw_height}（{raw_width * raw_height} 像素）"
                    f"不在上游总像素区间 {_SEEDREAM_MIN_PIXELS}~{_SEEDREAM_MAX_PIXELS} 内；"
                    "当前策略是 strict（原样提交、不归一化）。"
                    "把 COMIC_DRAMA_T2I_SIZE_POLICY 设为 normalize，"
                    "本地会按画幅比例换算到官方三档再提交。",
                    400,
                )
            return raw_width, raw_height, False
        ratio = _ratio_of(settings)
        if ratio is None:
            raise AppError(
                "drama_t2i_size",
                "无法从项目设定解析画幅比例（ratio / res 均为空或非法），"
                f"上游要求总像素 {_SEEDREAM_MIN_PIXELS}~{_SEEDREAM_MAX_PIXELS}，本地已拒绝提交",
                400,
            )
        if ratio in _SEEDREAM_FRAMES:
            width, height = _SEEDREAM_FRAMES[ratio]
        else:
            target = ratio[0] / ratio[1]
            nearest = min(
                _SEEDREAM_FALLBACK_ORDER,
                key=lambda key: abs(key[0] / key[1] - target),
            )
            width, height = _SEEDREAM_FRAMES[nearest]
        if not _SEEDREAM_MIN_PIXELS <= width * height <= _SEEDREAM_MAX_PIXELS:
            # 防的是「以后有人手改了档位」：常量与断言必须互相守着。
            raise AppError(
                "drama_t2i_size",
                f"归一化后的尺寸 {width}×{height}（{width * height} 像素）越出上游区间 "
                f"{_SEEDREAM_MIN_PIXELS}~{_SEEDREAM_MAX_PIXELS}，本地已拒绝提交",
                400,
            )
        return width, height, (width, height) != (raw_width, raw_height)

    def _public_url(self, asset_id: str, owner: str = "") -> str:
        """本地素材 id -> 上游可访问的公网 https URL。

        开放平台没有图片上传接口，`referenceImages` 只收公网可访问的 https 地址
        （本地/内网地址返回 100030「图片不可访问」）。所以部署方必须给出对外基址。
        两段文案必须是**不同字符串**，否则「没配」与「协议错」在测试里无法区分。
        """
        base = self.cfg.public_base_url.strip().rstrip("/")
        if not base:
            raise AppError(
                "drama_config",
                "official text-to-image needs COMIC_DRAMA_PUBLIC_BASE_URL: Liblib only accepts "
                "publicly reachable reference image URLs (a local path returns 100030), "
                "and the open platform has no image upload endpoint",
                503,
            )
        if not base.startswith("https://"):
            raise AppError(
                "drama_config",
                "COMIC_DRAMA_PUBLIC_BASE_URL must be https, otherwise upstream cannot fetch "
                "the reference image",
                503,
            )
        if owner and len(asset_id) == 16 and all(ch in "0123456789abcdef" for ch in asset_id):
            # 素材文件：签发免登录、带有效期的地址，上游才拉得到。
            return f"{base}/api/v1/drama/public/{self.media_store.public_token(owner, asset_id)}"
        return f"{base}/api/v1/drama/assets/{asset_id}"

    def _t2i_prompt(self, prompt: str, settings: dict[str, Any]) -> str:
        """官方文生图**唯一**的提示词组装出口。

        本期不翻译（上游要英文是已知限制，翻译要接一个未定的文本模型），
        只把画风/画幅拼进去，并在 payload 里标注 `prompt_language=zh`。
        配了 `t2i_prompt_rewrite_url` 就显式 NotImplementedError：静默忽略
        会让运维以为改写已经生效。
        """
        if self.cfg.t2i_prompt_rewrite_url:
            raise NotImplementedError("prompt rewrite is a reserved seam, not implemented")
        return self._prompt_with_settings(prompt, settings)

    def _text_to_image_official(
        self,
        prompt: str,
        *,
        owner: str,
        reference_asset_ids: list[str],
        settings: dict[str, Any],
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        """官方 seedreamV4 出图：四道本地守卫全在出网之前，顺序不可调换。

        1. 尺寸（`_seedream_size`）：1080×1920 低于上游像素下限，必须归一化；
        2. 参考图条数：超限**硬报错，绝不切片**；
        3. `_public_url`：缺公网 https 基址就报错（有参考图时才有意义）；
        4. 才建 client 调 `_generate`。

        前三道抛的都是 `drama_config` / `drama_t2i_size`，两者都在
        `LOCAL_FAILURE_CODES` 里，`_unknown_outcome()` 会判「肯定没出网」，
        于是整笔退还预占、不进待复核、不重提上游——资金后果不需要额外代码。
        """
        width, height, adjusted = self._seedream_size(settings)

        limit = int(self.cfg.t2i_official_max_refs)
        if len(reference_asset_ids) > limit:
            raise AppError(
                "drama_config",
                f"本镜头已选 {len(reference_asset_ids)} 张参考图，超过本地上限 {limit} 张"
                "（上游口径 1–14 张；COMIC_DRAMA_T2I_OFFICIAL_MAX_REFS 是本地保守值）。"
                "请减少镜头里引用的角色/场景/道具后重试——不会自动丢弃多余参考图："
                "参考图直接影响角色一致性，静默少传一张等于悄悄改变出图内容。",
                400,
            )
        # 不切片：顺序就是用户引用顺序，重排或截断都会让「出图内容」和用户理解不一致。
        reference_images = [self._public_url(asset_id, owner) for asset_id in reference_asset_ids]

        extra: dict[str, Any] = {
            "model": self.cfg.t2i_official_model,
            "width": width,
            "height": height,
            "imgCount": 1,
            "sequentialImageGeneration": "disabled",
        }
        if reference_images:
            extra["referenceImages"] = reference_images
        client = LibTVMediaProvider(
            base_url=self.cfg.libtv_base_url,
            access_key_env=self.cfg.access_key_env,
            secret_key_env=self.cfg.secret_key_env,
            template_uuid=self.cfg.t2i_official_template_uuid,
            prompt_mode="plain",
            submit_path=self.cfg.t2i_official_submit_path,
            status_path="/api/generate/status",
            extra_nodes=extra,
            rate_limiter=self._limiter,
            credentials=self._libtv_credentials(),
        )
        image = self._persist(
            owner,
            "image",
            self._generate(client, prompt, width=width, height=height, usage=usage),
        )
        requested_width, requested_height = self._dimensions(settings)
        return {
            "image": image,
            "image_url": image["url"],
            "assets": [image],
            # width/height 是**实际提交给上游**的尺寸；requested_* 是项目设定里的原始值。
            # 两个都回显，是为了证明「没有偷换设定，只是提交前按上游口径归一化」。
            "width": width,
            "height": height,
            "requested_width": requested_width,
            "requested_height": requested_height,
            "size_adjusted": adjusted,
            "reference_images": reference_images,
            "reference_mode": "reference_image" if reference_images else "prompt_only",
            "upstream_task_id": usage.get("upstream_task_id"),
            "upstream_points": usage.get("upstream_points"),
            "account_balance": usage.get("account_balance"),
            # 上游审核态：缺失就是 None（未知），不补默认值。
            "audit_status": usage.get("audit_status"),
        }

    # ---------------------------------------------------------------- 10 个能力

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        data = self._chat(
            "你是短剧剧本医生。只输出 JSON："
            '{"findings":["..."],"scenes":["..."],"dialogue_count":0}。findings 是具体可执行的诊断。',
            json.dumps(layers, ensure_ascii=False),
            ("findings",),
        )
        body = str(layers.get("body") or "")
        findings = [str(x) for x in data.get("findings", [])]
        return result(
            self.name,
            "script-doctor",
            "剧本诊断 · LLM",
            {
                "scenes": [str(x) for x in data.get("scenes", [])],
                "dialogue_count": int(data.get("dialogue_count", 0)),
                "findings": findings,
                "checked_chars": len(body),
                "model": self.cfg.llm_model,
                "assets": [],
            },
            self._note(),
        )

    def creative_start(self, kind: str, text: str, *, owner: str) -> dict[str, Any]:
        data = self._chat(
            "你是短剧编剧。只输出 JSON："
            '{"layers":{"logline":"...","outline":"...","body":"..."}}。'
            "body 用【场景 N · 地点 · 时间】分场，含对白行「角色：台词」。",
            json.dumps({"kind": kind, "text": text}, ensure_ascii=False),
            ("layers",),
        )
        layers = data["layers"]
        for key in ("logline", "outline", "body"):
            if key not in layers:
                raise AppError("drama_llm_payload", f"creative-start layers missing {key}", 502)
        return result(
            self.name,
            "creative-start",
            "创作起点 · LLM 扩写",
            {"kind": kind, "layers": layers, "model": self.cfg.llm_model, "assets": []},
            self._note(),
        )

    def extract_assets(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        data = self._chat(
            "从剧本抽取实体。只输出 JSON："
            '{"found":{"characters":[{"name":"..","prompt":".."}],'
            '"scenes":[...],"props":[...]}}。prompt 是可用于文生图的外形/场景锚点。',
            json.dumps(layers, ensure_ascii=False),
            ("found",),
        )
        found: dict[str, list[dict[str, Any]]] = {}
        for key in ("characters", "scenes", "props"):
            items = data["found"].get(key, [])
            found[key] = [
                {"name": str(x.get("name", "")), "prompt": str(x.get("prompt", ""))}
                for x in items
                if isinstance(x, dict) and x.get("name")
            ]
        return result(
            self.name,
            "extract-assets",
            "剧本实体抽取 · LLM",
            {
                "found": found,
                "counts": {k: len(v) for k, v in found.items()},
                "model": self.cfg.llm_model,
                "assets": [],
            },
            self._note(),
        )

    def resplit_shots(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        data = self._chat(
            "把剧本拆成分镜。只输出 JSON："
            '{"shots":[{"dur":3,"size":"中近景","desc":"..","prompt":"..",'
            '"line":"..","move":"微推"}]}，最多 24 条。',
            json.dumps(layers, ensure_ascii=False),
            ("shots",),
        )
        raw_shots = data.get("shots", [])[:24]
        shots = []
        for index, item in enumerate(raw_shots):
            if not isinstance(item, dict):
                continue
            shot = dict(_SHOT_DEFAULTS) | {k: v for k, v in item.items() if k in _SHOT_DEFAULTS}
            shot["desc"] = str(shot["desc"] or f"镜头 {index + 1}")
            shot["dur"] = max(1, int(shot["dur"] or 3))
            shot["refs"] = []
            shots.append(shot)
        if not shots:
            raise AppError("drama_llm_payload", "storyboard returned no usable shots", 502)
        return result(
            self.name,
            "resplit-shots",
            "分镜拆分 · LLM",
            {"shots": shots, "count": len(shots), "model": self.cfg.llm_model, "assets": []},
            self._note(),
        )

    def tts_preview(self, character: str, voice: str, text: str, *, owner: str) -> dict[str, Any]:
        audio = self._persist(owner, "audio", self._tts_audio(text, voice, character))
        return result(
            self.name,
            "tts-preview",
            f"试听 · {character}",
            {
                "character": character,
                "voice": voice,
                "text": text[:80],
                "duration_seconds": round(max(1.0, len(text) * 0.18), 1),
                "audio_url": audio["url"],
                "assets": [audio],
            },
            self._note(),
        )

    def change_voice(self, character: str, current: str, *, owner: str) -> dict[str, Any]:
        data = self._chat(
            "为角色推荐备选配音音色。只输出 JSON："
            '{"candidates":["声线描述 1","声线描述 2","声线描述 3"],"recommended":".."}。'
            "不要包含当前音色。",
            json.dumps({"character": character, "current": current}, ensure_ascii=False),
            ("candidates",),
        )
        candidates = [str(x) for x in data.get("candidates", []) if str(x) != current]
        if not candidates:
            raise AppError("drama_llm_payload", "voice recommendation returned no candidate", 502)
        recommended = str(data.get("recommended") or candidates[0])
        return result(
            self.name,
            "change-voice",
            f"音色候选 · {character}",
            {
                "character": character,
                "from": current,
                "to": recommended,
                "candidates": candidates,
                "model": self.cfg.llm_model,
                "assets": [],
            },
            self._note(),
        )

    def mark_audio_done(
        self,
        voices: list[dict[str, Any]],
        subtitles: list[dict[str, Any]],
        *,
        owner: str,
    ) -> dict[str, Any]:
        # 统计真实已转存的音频文件数，而不是写死 0。
        audio_files = self.media_store.count(owner, "audio")
        return result(
            self.name,
            "mark-audio-done",
            "配音与字幕配置完成",
            {
                "voices": len(voices),
                "subtitles": len(subtitles),
                "checked_subtitles": sum(1 for s in subtitles if s.get("checked")),
                "audio_files": audio_files,
                "assets": [],
            },
            self._note(),
        )

    def preview_film(self, project: dict[str, Any], *, owner: str) -> dict[str, Any]:
        shots = project.get("shots") or []
        total = sum(int(s.get("dur", 0)) for s in shots)
        audio = project.get("audio") or {}
        tracks = {
            "video": len(shots),
            "voice": len(audio.get("voices") or []),
            "subtitle": len(audio.get("subtitles") or []),
            "bgm": (audio.get("bgm") or {}).get("name", "未设置"),
        }
        # 与离线示例共用 compose.build_plan：视频轨、逐句配音、BGM、字幕都从
        # 同一份项目状态解析，只是素材来源不同。
        plan = build_plan(project, self.media_store, owner)
        if not plan.clips:
            # 明确降级，但不冒充成功：仍是真实 Provider，只是没有可合成的输入。
            return result(
                self.name,
                "preview-film",
                "成片预览 · 暂无视频轨",
                {
                    "name": project.get("name", ""),
                    "clips": len(shots),
                    "duration_seconds": total,
                    "tracks": tracks,
                    "video_url": None,
                    "assets": [],
                    "hint": "尚无可合成的视频轨：请先对每个镜头执行「图生视频」",
                },
                self._note(),
            )
        rendered = self._persist(
            owner,
            "video",
            self._compose(
                plan,
                # 交付选项读项目设置：软/硬字幕与输出分辨率是「这一集交付成什么样」
                # 的属性，不该由部署配置一刀切。非法值在 compose 的 parse_* 里回退。
                subtitle_mode=parse_subtitle_mode(project.get("subtitleMode")),
                resolution=parse_resolution(project.get("outputResolution")),
            ),
        )
        return result(
            self.name,
            "preview-film",
            "成片预览 · 本地 FFmpeg 合成",
            {
                "name": project.get("name", ""),
                "clips": len(shots),
                "duration_seconds": plan.total_seconds,
                "tracks": tracks,
                "video_url": rendered["url"],
                "assets": [rendered],
                "composed_clips": len(plan.clips),
                "muxed": {
                    "voice_tracks": len(plan.voices),
                    "bgm": plan.bgm is not None,
                    "subtitles": len(plan.subtitles),
                },
                "alignment": plan.alignment,
                "voice_alignment": [
                    {"text": v.label, "start_ms": v.start_ms, "limit_ms": v.limit_ms}
                    for v in plan.voices
                ],
                "skipped_voices": plan.skipped,
                "hint": "视频轨 concat + 逐句配音 adelay/atrim 对齐字幕 + BGM 混音 + "
                "字幕 mov_text 软封装；配音长短规则见需求文档 §18",
            },
            self._note(),
        )

    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        if self.cfg.i2v_channel == "official":
            return self._image_to_video_official(shot, size, owner=owner, first_frame=first_frame)
        if not self.cfg.i2v_template_uuid or not self.cfg.i2v_prompt_node:
            raise AppError("drama_config", "image-to-video template is not configured", 503)
        extra = dict(self.cfg.i2v_extra_nodes)
        if first_frame:
            # 首帧图节点的 id 与字段名都来自配置，不在代码里猜上游字段。
            # 只给了图但没配节点时必须显式失败：静默丢掉首帧会让「图生视频」
            # 变成纯文生视频，而调用方以为用了图。
            if not self.cfg.i2v_first_frame_node:
                raise AppError(
                    "drama_config",
                    "received a first frame image but COMIC_DRAMA_I2V_FIRST_FRAME_NODE is unset; "
                    "refusing to guess the upstream node shape",
                    503,
                )
            extra[self.cfg.i2v_first_frame_node] = {
                "inputs": {
                    self.cfg.i2v_first_frame_field: base64.b64encode(first_frame).decode("ascii")
                }
            }
        client = self._libtv_client(self.cfg.i2v_template_uuid, self.cfg.i2v_prompt_node, extra)
        settings = dict(shot.get("project_settings") or {})
        width, height = self._dimensions(settings)
        prompt = self._prompt_with_settings(
            str(shot.get("prompt") or shot.get("desc") or "电影感镜头"), settings
        )
        usage: dict[str, Any] = {}
        video = self._persist(
            owner,
            "video",
            self._generate(client, prompt, width=width, height=height, usage=usage),
        )
        return result(
            self.name,
            "image-to-video",
            f"图生视频 · {shot.get('id', '')}",
            {
                "shot_id": shot.get("id"),
                "size": size,
                "duration_seconds": int(shot.get("dur", 3)),
                "video_url": video["url"],
                "cover_url": shot.get("cover_url"),
                "first_frame_bytes": len(first_frame) if first_frame else 0,
                "project_settings": settings,
                "width": width,
                "height": height,
                "anchor_refs": [str(x) for x in (shot.get("anchor_refs") or [])],
                "anchor_asset_id": str(shot.get("anchor_asset_id") or ""),
                "first_frame_asset_id": str(shot.get("first_frame_asset_id") or ""),
                # 上游真值：两条通道共用同一个 status 解析（`libtv.poll` 读
                # pointsCost / accountBalance），所以这里只如实透传。
                # 字段缺失就是 None（未知）——**绝不**用 UPSTREAM_COST_* 预算常量回填：
                # 那是「估」，填进来等于把未知伪装成已知。工作流模板是否真回传这两个
                # 字段尚未实测（只有官方 kling 那单验过），实测前一律按 NULL 处理。
                "upstream_task_id": usage.get("upstream_task_id"),
                "upstream_points": usage.get("upstream_points"),
                "account_balance": usage.get("account_balance"),
                # 上游审核态（`auditStatus`）：缺失即 None（未知），不补默认值。
                "audit_status": usage.get("audit_status"),
                "assets": [video],
            },
            self._note(),
        )

    def _image_to_video_official(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None,
    ) -> dict[str, Any]:
        """官方 Kling 通道：首帧图以公网 URL 提交，prompt 与参数平铺在 generateParams。

        与工作流通道的差别不只是端点：那条把图 base64 塞进某个节点的 inputs，
        这条必须是可公开访问的 URL；而且图生视频没有宽高参数 —— 画幅跟随首帧，
        多传 imageSize 会被判成多余参数。
        """
        asset_id = str(shot.get("first_frame_asset_id") or "")
        if not asset_id:
            raise AppError(
                "drama_config",
                "official image-to-video needs a generated first-frame asset, "
                "but none is attached to this shot",
                503,
            )
        base = self.cfg.public_base_url.strip().rstrip("/")
        if not base:
            raise AppError(
                "drama_config",
                "official image-to-video needs COMIC_DRAMA_PUBLIC_BASE_URL: "
                "Liblib only accepts a publicly reachable first-frame URL "
                "(a local path returns 100030)",
                503,
            )
        if not base.startswith("https://"):
            raise AppError(
                "drama_config",
                "COMIC_DRAMA_PUBLIC_BASE_URL must be https, otherwise upstream cannot fetch it",
                503,
            )
        first_frame_url = (
            f"{base}/api/v1/drama/public/{self.media_store.public_token(owner, asset_id)}"
        )
        # 上游的 duration 是离散枚举（5 / 10 秒），不是任意整数，且要字符串。
        # 分镜默认 `dur` 是 3 秒 —— 原样发 "3" 会被判参数非法（100050），
        # 所以向上取到最近的可选档：<=5 走 5 秒，超过走 10 秒。
        # 已实测的只有 5 秒（250 积分）；10 秒档未实测，故不设为默认。
        seconds = 10 if int(shot.get("dur") or 5) > 5 else 5
        duration = str(seconds)
        extra: dict[str, Any] = {
            "model": self.cfg.i2v_model,
            "duration": duration,
            "mode": self.cfg.i2v_mode,
        }
        if self.cfg.i2v_model.startswith("kling-v2-6"):
            # v2-6 用 images 数组并支持原生音效，此时传 startFrame 会被忽略。
            extra["images"] = [first_frame_url]
            extra["sound"] = self.cfg.i2v_sound
        else:
            extra["startFrame"] = first_frame_url
        client = LibTVMediaProvider(
            base_url=self.cfg.libtv_base_url,
            access_key_env=self.cfg.access_key_env,
            secret_key_env=self.cfg.secret_key_env,
            template_uuid=self.cfg.i2v_official_template_uuid,
            prompt_mode="plain",
            submit_path="/api/generate/video/kling/img2video",
            status_path="/api/generate/status",
            extra_nodes=extra,
            rate_limiter=self._limiter,
            credentials=self._libtv_credentials(),
        )
        settings = dict(shot.get("project_settings") or {})
        width, height = self._dimensions(settings)
        prompt = self._prompt_with_settings(
            str(shot.get("prompt") or shot.get("desc") or "电影感镜头"), settings
        )
        usage: dict[str, Any] = {}
        video = self._persist(
            owner,
            "video",
            self._generate(client, prompt, width=width, height=height, usage=usage),
        )
        return result(
            self.name,
            "image-to-video",
            f"图生视频 · {shot.get('id', '')}",
            {
                "shot_id": shot.get("id"),
                "size": size,
                # 报实际上游收到的那一档，而不是分镜里写的 nominal 值，
                # 否则「提交 5 秒 / 记录 3 秒」会让时长统计和成本对不上。
                "duration_seconds": seconds,
                "video_url": video["url"],
                "cover_url": shot.get("cover_url"),
                "channel": "official",
                "model": self.cfg.i2v_model,
                "first_frame_url": first_frame_url,
                # 首帧字节仍报出来：official 通道实际用的是 URL，
                # 这个数字只用于确认调用方确实选过一张图。
                "first_frame_bytes": len(first_frame) if first_frame else 0,
                "project_settings": settings,
                "width": width,
                "height": height,
                # 上游自报用量：250 积分/条（5 秒）已实测，可直接回填 ACTION_PRICES。
                "upstream_task_id": usage.get("upstream_task_id"),
                "upstream_points": usage.get("upstream_points"),
                "account_balance": usage.get("account_balance"),
                # 上游审核态（`auditStatus`）：缺失即 None（未知），不补默认值。
                "audit_status": usage.get("audit_status"),
                "assets": [video],
            },
            self._note(),
        )

    def asset_candidates(self, asset: dict[str, Any], *, owner: str) -> dict[str, Any]:
        """锚点候选图。official 通道不带参考图——它正是在**造**锚点。"""
        settings = dict(asset.get("project_settings") or {})
        raw_prompt = str(asset.get("prompt") or f"角色/场景锚点：{asset.get('name', '')}")
        prompt = self._prompt_with_settings(raw_prompt, settings)
        if self.cfg.t2i_channel == "official":
            official_usage: dict[str, Any] = {}
            official = self._text_to_image_official(
                self._t2i_prompt(raw_prompt, settings),
                owner=owner,
                reference_asset_ids=[],
                settings=settings,
                usage=official_usage,
            )
            return result(
                self.name,
                "asset-candidates",
                f"锚点图 · {asset.get('name', '')}",
                {
                    "asset_id": asset.get("id"),
                    "seed": int(asset.get("seed", 1)) + 7,
                    "count": 1,
                    "image_url": official["image_url"],
                    "project_settings": settings,
                    "channel": "official",
                    "requested_width": official["requested_width"],
                    "requested_height": official["requested_height"],
                    "width": official["width"],
                    "height": official["height"],
                    "size_adjusted": official["size_adjusted"],
                    "reference_images": official["reference_images"],
                    "reference_mode": official["reference_mode"],
                    "prompt_language": "zh",
                    "prompt_language_note": PROMPT_LANGUAGE_NOTE,
                    "upstream_task_id": official["upstream_task_id"],
                    "upstream_points": official["upstream_points"],
                    "account_balance": official["account_balance"],
                    "assets": official["assets"],
                    "hint": "官方 seedreamV4 单张真实候选图；锚点图本身不带参考图。"
                    + PROMPT_LANGUAGE_NOTE,
                },
                self._note(),
            )
        client = self._libtv_client(self.cfg.t2i_template_uuid, self.cfg.t2i_prompt_node, {})
        width, height = self._dimensions(settings)
        usage: dict[str, Any] = {}
        image = self._persist(
            owner,
            "image",
            self._generate(client, prompt, width=width, height=height, usage=usage),
        )
        return result(
            self.name,
            "asset-candidates",
            f"锚点图 · {asset.get('name', '')}",
            {
                "asset_id": asset.get("id"),
                "seed": int(asset.get("seed", 1)) + 7,
                "count": 1,
                "image_url": image["url"],
                "project_settings": settings,
                "width": width,
                "height": height,
                "upstream_task_id": usage.get("upstream_task_id"),
                "upstream_points": usage.get("upstream_points"),
                "account_balance": usage.get("account_balance"),
                # 上游审核态（`auditStatus`）：缺失即 None（未知），不补默认值。
                "audit_status": usage.get("audit_status"),
                "assets": [image],
                "hint": "首版单张真实候选图；候选张数需按套餐与单次计费实测后调整",
            },
            self._note(),
        )

    def keyframe(
        self,
        shot: dict[str, Any],
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        """真实关键帧：走与候选锚点图同一个 t2i 模板，产出真图写回镜头。

        项目比例 / 画风 / 分辨率 / @角色 锚点与图生视频用同一组参数助手：
        - 分辨率 -> submit 宽高；画风 / 画幅 -> 提示词；
        - @角色 引用与解析出的锚点资产 id -> 提示词，并在 payload 原样回显；
        - 锚点图像素级参考：只有配置了 `t2i_reference_node` 才注入 base64，
          未配置时明确标 `reference_mode=prompt_only`，不臆造上游节点形状。
        """
        if self.cfg.t2i_channel == "official":
            # 官方通道把参考图当 URL 提交，与工作流的 base64 节点是两套契约，
            # 所以整条路径分叉，`first_frame` 字节在这条路上不参与请求。
            return self._keyframe_official(shot, owner=owner)
        settings = dict(shot.get("project_settings") or {})
        width, height = self._dimensions(settings)
        refs = [str(x) for x in (shot.get("anchor_refs") or [])]
        anchor = str(shot.get("anchor_asset_id") or "")
        prompt = self._prompt_with_settings(
            str(shot.get("prompt") or shot.get("desc") or "电影感关键帧"), settings
        )
        if refs:
            prompt += "；角色/场景锚点：" + " / ".join(refs)
        if anchor:
            prompt += f"；锚点资产 {anchor}"
        extra = dict(self.cfg.t2i_extra_nodes)
        reference_mode = "prompt_only"
        if first_frame and self.cfg.t2i_reference_node:
            extra[self.cfg.t2i_reference_node] = {
                "inputs": {
                    self.cfg.t2i_reference_field: base64.b64encode(first_frame).decode("ascii")
                }
            }
            reference_mode = "reference_image"
        client = self._libtv_client(self.cfg.t2i_template_uuid, self.cfg.t2i_prompt_node, extra)
        usage: dict[str, Any] = {}
        image = self._persist(
            owner,
            "image",
            self._generate(client, prompt, width=width, height=height, usage=usage),
        )
        requested = max(1, min(4, int(shot.get("count") or 1)))
        return result(
            self.name,
            "keyframe",
            f"关键帧 t2i · {shot.get('id', '')}",
            {
                "shot_id": shot.get("id"),
                "image_url": image["url"],
                # 这条工作流通道一次提交产出**一张**图：`count` 如实回显本次请求的
                # 张数，`image_urls` 如实列出真正产出的张数（1 张）。两者不一致是
                # 事实，不是笔误——多图的节点形状未经实测，不能假装已经支持。
                "image_urls": [image["url"]],
                "assets": [image],
                "count": requested,
                "prompt": prompt,
                "project_settings": settings,
                "width": width,
                "height": height,
                "anchor_refs": refs,
                "anchor_asset_id": anchor,
                "first_frame_asset_id": str(shot.get("first_frame_asset_id") or ""),
                "reference_mode": reference_mode,
                "upstream_task_id": usage.get("upstream_task_id"),
                "upstream_points": usage.get("upstream_points"),
                "account_balance": usage.get("account_balance"),
                # 上游审核态（`auditStatus`）：缺失即 None（未知），不补默认值。
                "audit_status": usage.get("audit_status"),
                "hint": "真实文生图；上游 t2i 模板节点形状、计费字段与允许的分辨率"
                "仍需首单冒烟核对；锚点图像素级参考需显式配置 t2i_reference_node",
            },
            self._note(),
        )

    def _keyframe_official(self, shot: dict[str, Any], *, owner: str) -> dict[str, Any]:
        """官方 seedreamV4 关键帧：参考图按「锚点 -> 首帧」的顺序去重后原样提交。

        参考图顺序有语义（用户按 @角色 / 选图顺序表达「谁在图里」），所以这里
        只做去重，绝不重排、不切片。关键帧必然带参考图（`_shot_request` 会用
        @角色 解析出的锚点当首帧），所以在没有公网 https 部署的环境里，这条路
        会在 `_public_url` 处以 `drama_config` 本地拒绝——这是预期行为，不是缺陷。
        """
        settings = dict(shot.get("project_settings") or {})
        refs_text = [str(x) for x in (shot.get("anchor_refs") or [])]
        anchor = str(shot.get("anchor_asset_id") or "")
        raw_prompt = str(shot.get("prompt") or shot.get("desc") or "电影感关键帧")
        if refs_text:
            raw_prompt += "；角色/场景锚点：" + " / ".join(refs_text)
        if anchor:
            raw_prompt += f"；锚点资产 {anchor}"
        reference_asset_ids: list[str] = []
        # 参考图提交的是**文件编号**（anchor_media_id），不是资产编号：
        # 资产编号拼不出可访问的图片地址，上游会报 100030。
        for asset_id in (
            str(shot.get("anchor_media_id") or ""),
            str(shot.get("first_frame_asset_id") or ""),
        ):
            if asset_id and asset_id not in reference_asset_ids:
                reference_asset_ids.append(asset_id)
        usage: dict[str, Any] = {}
        prompt = self._t2i_prompt(raw_prompt, settings)
        official = self._text_to_image_official(
            prompt,
            owner=owner,
            reference_asset_ids=reference_asset_ids,
            settings=settings,
            usage=usage,
        )
        return result(
            self.name,
            "keyframe",
            f"关键帧 t2i · {shot.get('id', '')}",
            {
                "shot_id": shot.get("id"),
                "image_url": official["image_url"],
                # 同工作流通道：count 是本次请求的张数，image_urls 是真正产出的张数。
                # 官方通道一单一图（未做多图实测），这里如实列 1 张。
                "image_urls": [official["image_url"]],
                "assets": official["assets"],
                "count": max(1, min(4, int(shot.get("count") or 1))),
                "prompt": prompt,
                "project_settings": settings,
                "channel": "official",
                # width/height = 实际提交给上游的（可能是归一化后的）尺寸；
                # requested_* = 项目设定里的原始尺寸，两者都回显，便于核对。
                "width": official["width"],
                "height": official["height"],
                "requested_width": official["requested_width"],
                "requested_height": official["requested_height"],
                "size_adjusted": official["size_adjusted"],
                # 实际提交给上游的参考图；超限时本地直接报错，不存在「少传几张」的中间态。
                "reference_images": official["reference_images"],
                "reference_mode": official["reference_mode"],
                "anchor_refs": refs_text,
                "anchor_asset_id": anchor,
                "first_frame_asset_id": str(shot.get("first_frame_asset_id") or ""),
                "prompt_language": "zh",
                "prompt_language_note": PROMPT_LANGUAGE_NOTE,
                "upstream_task_id": official["upstream_task_id"],
                "upstream_points": official["upstream_points"],
                "account_balance": official["account_balance"],
                "hint": "官方 seedreamV4 真实文生图；尺寸按上游像素区间归一化后再提交，"
                "reference_images 是上游实际收到的参考图。未做付费实测。",
            },
            self._note(),
        )

    # 这几个方法保留名字与签名，是为了让既有测试/排障脚本继续可用；实现一律
    # 委托给 ../compose.py —— 离线示例与真实素材共用同一份 FFmpeg 链路，
    # 不允许这里再长出第二套「配音/BGM/字幕」规则。

    @staticmethod
    def _srt_time(value: str) -> str:
        return srt_time(value)

    def _mux_args(
        self,
        binary: str,
        base: Path,
        output: Path,
        *,
        voices: list[Path],
        bgm: Path | None,
        subtitles: list[dict[str, Any]],
        levels: dict[str, int],
        srt: Path | None,
        total_seconds: float = 0.0,
    ) -> list[str]:
        return mux_args(
            binary,
            base,
            output,
            voices=voices,
            bgm=bgm,
            subtitles=subtitles,
            levels=levels,
            srt=srt,
            total_seconds=total_seconds,
            ai_label=self.cfg.ai_label,
            ai_disclosure=self.cfg.ai_disclosure,
            sample=False,
        )

    def _compose(
        self,
        plan: FilmPlan | list[Path],
        *,
        voices: list[Path] | None = None,
        bgm: Path | None = None,
        subtitles: list[dict[str, Any]] | None = None,
        levels: dict[str, int] | None = None,
        subtitle_mode: str | None = None,
        resolution: str | None = None,
    ) -> bytes:
        if isinstance(plan, FilmPlan):
            resolved = plan
        else:
            # 兼容旧的「直接传 clips 列表」调用：按轨首对齐的旧语义构造计划。
            from ..compose import VoiceCue

            total = 3.0 * len(plan)
            resolved = FilmPlan(
                clips=[Path(p) for p in plan],
                voices=[VoiceCue(Path(v), 0, int(total * 1000)) for v in (voices or [])],
                bgm=Path(bgm) if bgm is not None else None,
                subtitles=list(subtitles or []),
                levels=dict(levels or {}),
                total_seconds=total,
            )
        return render_film(
            resolved,
            ffmpeg=self.cfg.ffmpeg,
            ai_label=self.cfg.ai_label,
            ai_disclosure=self.cfg.ai_disclosure,
            # 交付选项由调用方按项目设置传入；缺省（旧调用姿势 / 测试直接调用）
            # 走 compose 自己的默认值 soft + source，行为与改造前一致。
            subtitle_mode=subtitle_mode or "",
            resolution=resolution or "",
            sample=False,
        )

    @classmethod
    def _srt_text(cls, subtitles: list[dict[str, Any]]) -> str:
        return srt_text(subtitles)

    @staticmethod
    def _run(args: list[str]) -> None:
        from ..compose import _run as run

        run(args)
