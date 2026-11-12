"""LibTV / LiblibAI 任务级 API 适配器：完整实现，功能阶段默认不实例化、不出网。

事实边界（检查日 2026-11-09，未调用任何收费接口）：
【官方页面确认】
  POST /api/generate/comfyui/app、POST /api/generate/comfy/status；
  generateStatus 1~6；percentCompleted 为 0~1 浮点；images[].imageUrl；
  「地址有时效性：7天」；QPS-1、并发-5；100 积分 = 1 元；API 积分不可退款。
【第三方公开资料交叉印证，非官方页面直接给出】
  AccessKey / Signature / Timestamp / SignatureNonce 四个查询参数；
  content = uriPath & Timestamp & SignatureNonce，HMAC-SHA1(SecretKey)，
  URL-safe Base64 后去掉尾部 '='；Timestamp 为毫秒；系统时钟偏差 >300s 会被拒。
【代码已实现但结构未实测】
  视频/音频的返回容器（videos[] / audios[]）与 URL 字段名：官方页面只列了
  images[].imageUrl。_RESULT_CONTAINERS 按「容器名定 kind + 字段名多候选」探测，
  形状是为了不漏收素材，不代表上游真实字段已知。充值后必须核对真实返回。
【待最后一公里实测，不得写成已验证】
  提交响应的外层包装形式（code/data 或裸对象）、templateUuid 与节点参数、
  单次实际积分消耗字段、webhook、任务超时、素材域名白名单。
  官方 /apis/workflow 的「参数说明」表格当前为「暂无数据」。

因此本模块：凭据只在调用时按环境变量名读取；submit 永不自动重试；
任何提交阶段的网络异常一律判为 unknown（上游可能已执行）；结果成功即由
上层立即转存，绝不把 7 天 URL 留给用户下载。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .base import BaseMediaProvider
from .contracts import (
    AssetKind,
    PollResult,
    ProviderError,
    RemoteAsset,
    SubmitResult,
    asset_limit,
)
from .rate_limiter import RateLimiter

SUBMIT_PATH = "/api/generate/comfyui/app"
STATUS_PATH = "/api/generate/comfy/status"
_MAX_JSON_BYTES = 1 << 20

# 官方平台口径：100 积分 = 1 元（API 积分与会员积分是两套体系，不通用）。
# 故 1 积分 = 0.01 元 = 10_000 微元。用于把上游 pointsCost 换算成 cost_micro。
_MICRO_PER_POINT = 10_000

# 官方状态：1 等待执行 / 2 执行中 / 3 已生图 / 4 审核中 / 5 成功 / 6 失败
_RUNNING_CODES: dict[int, int] = {1: 0, 2: 0, 3: 95, 4: 98}

# 官方状态接口文档只列出 images[].imageUrl；视频/音频节点返回的容器名与字段名
# 未在官方页面给出，属「最后一公里实测」项。这里按「容器名决定 kind、URL 字段名
# 多候选」探测：漏掉 videos 会把已经成功的视频任务判成 result_missing，
# 进而把积分冻结进 held 等人工核对，代价远高于多写几条候选字段。
_RESULT_CONTAINERS: tuple[tuple[str, tuple[str, ...], AssetKind], ...] = (
    ("images", ("imageUrl", "url", "fileUrl"), "image"),
    ("videos", ("videoUrl", "url", "fileUrl"), "video"),
    ("audios", ("audioUrl", "url", "fileUrl"), "audio"),
)


def _collect_result_assets(data: dict) -> list[RemoteAsset]:
    """从成功响应里收集可转存素材；只接受 https，按 URL 去重。

    容器名决定 kind，不让 URL 字段名反推类型：同一份返回里 images 和 videos
    可能都出现 url 这个键，靠字段名猜会把视频当图片存。
    """
    assets: list[RemoteAsset] = []
    seen: set[str] = set()
    for container, fields, kind in _RESULT_CONTAINERS:
        items = data.get(container)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = None
            for field in fields:
                candidate = item.get(field)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    url = candidate
                    break
            if url is None or url in seen:
                continue
            seen.add(url)
            assets.append(
                RemoteAsset(
                    url=url,
                    kind=kind,
                    meta={"auditStatus": str(item.get("auditStatus", ""))},
                )
            )
    return assets


class LibTVMediaProvider(BaseMediaProvider):
    name = "libtv"

    def __init__(
        self,
        *,
        base_url: str = "https://openapi.liblibai.cloud",
        access_key_env: str = "LIBTV_ACCESS_KEY",
        secret_key_env: str = "LIBTV_SECRET_KEY",
        template_uuid: str,
        prompt_node: str = "",
        prompt_field: str = "text",
        # 提示词落地方式。工作流通道要把 prompt 塞进某个节点的 inputs；
        # 官方视频通道（Kling）用的是平铺的 generateParams.prompt，
        # 没有节点这层概念。两者请求体的外层都是 {templateUuid, generateParams}，
        # 所以只在 generateParams 内部组装时分叉，HTTP 与签名完全共用。
        prompt_mode: str = "node",
        # 提交路径随通道变化：工作流是 /api/generate/comfyui/app，
        # 官方视频是 /api/generate/video/kling/img2video。
        submit_path: str = SUBMIT_PATH,
        # 轮询路径同理。实测两个状态端点查同一个任务都返回完整结果，
        # 但仍按通道分开写，避免上游哪天把两者拆开时静默失败。
        status_path: str = STATUS_PATH,
        extra_nodes: dict[str, Any] | None = None,
        timeout: float = 15.0,
        rate_limiter: RateLimiter | None = None,
        credentials: tuple[str, str] | None = None,
    ):
        if prompt_mode not in {"node", "plain"}:
            raise ValueError("prompt_mode must be 'node' or 'plain'")
        if not template_uuid:
            raise ValueError("libtv requires a configured templateUuid")
        if prompt_mode == "node" and not prompt_node:
            raise ValueError("libtv workflow mode requires a prompt node id")
        self.base_url = base_url.rstrip("/")
        self.access_key_env = access_key_env
        self.secret_key_env = secret_key_env
        self.template_uuid = template_uuid
        self.prompt_node = prompt_node
        self.prompt_field = prompt_field
        self.prompt_mode = prompt_mode
        self.submit_path = submit_path
        self.status_path = status_path
        self.extra_nodes = dict(extra_nodes or {})
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter()
        # 用户在页面填写的密钥（按账号保存）。给了就用它，不再读环境变量。
        self._injected = credentials

    # ---------------------------------------------------------------- 凭据与签名

    def _credentials(self) -> tuple[str, str]:
        """优先用注入的密钥，其次按环境变量名读取；错误信息绝不携带值。"""
        if self._injected and self._injected[0] and self._injected[1]:
            return self._injected[0].strip(), self._injected[1].strip()
        access = os.environ.get(self.access_key_env, "").strip()
        secret = os.environ.get(self.secret_key_env, "").strip()
        if not access or not secret:
            raise ProviderError(
                "credentials_missing",
                f"set environment variables {self.access_key_env} and {self.secret_key_env}",
                outcome="rejected",
            )
        return access, secret

    def _signed_url(self, path: str, credentials: tuple[str, str]) -> str:
        access, secret = credentials
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        content = f"{path}&{timestamp}&{nonce}"
        digest = hmac.new(secret.encode(), content.encode(), hashlib.sha1).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        query = urllib.parse.urlencode(
            {
                "AccessKey": access,
                "Signature": signature,
                "Timestamp": timestamp,
                "SignatureNonce": nonce,
            }
        )
        return f"{self.base_url}{path}?{query}"

    # ---------------------------------------------------------------- HTTP（同步，在 to_thread 中执行）

    def _post(self, path: str, payload: dict, credentials: tuple[str, str]) -> dict:
        url = self._signed_url(path, credentials)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # 不回传响应体：可能含上游业务数据或凭据回显。
            return {"_http_error": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"_transport_error": True}
        if len(raw) > _MAX_JSON_BYTES:
            return {"_http_error": 502}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"_transport_error": True}
        return decoded if isinstance(decoded, dict) else {"_transport_error": True}

    def _get(self, url: str, limit: int) -> bytes | dict:
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                # 多读 1 字节只为判定是否超限。视频仍是一次性读入内存；
                # 改成流式落盘属于最后一公里优化，等真实素材体积确定后再做。
                return response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            return {"_http_error": exc.code}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"_transport_error": True}

    @staticmethod
    def _unwrap(payload: dict) -> tuple[dict, str | None]:
        """兼容 code/data 包装与裸对象；包装形式待最后一公里实测确认。"""
        code = payload.get("code")
        if code is not None and code not in (0, "0", 200, "200", None):
            return {}, "upstream rejected the request"
        data = payload.get("data")
        if isinstance(data, dict):
            return data, None
        if "generateUuid" in payload or "generateStatus" in payload:
            return payload, None
        return {}, "unexpected upstream response shape"

    @staticmethod
    def _assert_public_url(url: str) -> None:
        """基本 SSRF 防护；域名白名单需在最后一公里按实际 OSS 域名补齐。"""
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or not host:
            raise ProviderError(
                "asset_url_invalid", "only https asset URLs are accepted", outcome="rejected"
            )
        lowered = host.lower()
        if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
            raise ProviderError(
                "asset_url_invalid", "private asset hosts are not accepted", outcome="rejected"
            )
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            return
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise ProviderError(
                "asset_url_invalid", "private asset hosts are not accepted", outcome="rejected"
            )

    # ---------------------------------------------------------------- L2 契约

    async def submit(
        self, *, task_id: str, request_id: str, prompt: str, width: int, height: int
    ) -> SubmitResult:
        prompt, width, height = self.validate(prompt, width=width, height=height)
        credentials = self._credentials()
        generate_params: dict[str, Any] = dict(self.extra_nodes)
        if self.prompt_mode == "plain":
            # 官方视频通道（Kling）：prompt、首帧图、模型名、时长、档位都是
            # generateParams 的平铺字段；没有节点 id，也没有 imageSize
            # ——画幅跟随首帧图，传尺寸反而会被判成多余参数。
            generate_params["prompt"] = prompt
        else:
            node: dict[str, Any] = {"inputs": {self.prompt_field: prompt}}
            generate_params[self.prompt_node] = node
            generate_params.setdefault("imageSize", {"width": width, "height": height})
        payload = {"templateUuid": self.template_uuid, "generateParams": generate_params}
        # 只提交一次。写请求结果未知时绝不自动重提（上游可能已扣费/已排队）。
        async with self.rate_limiter.slot():
            raw = await asyncio.to_thread(self._post, self.submit_path, payload, credentials)
        if "_transport_error" in raw:
            raise ProviderError(
                "submit_response_lost", "提交响应丢失，上游可能已执行", outcome="unknown"
            )
        if "_http_error" in raw:
            status = raw["_http_error"]
            if 400 <= status < 500:
                raise ProviderError(
                    "upstream_rejected",
                    f"upstream rejected the submission (HTTP {status})",
                    outcome="rejected",
                )
            raise ProviderError(
                "submit_response_lost", "提交结果未知，禁止自动重提", outcome="unknown"
            )
        data, error = self._unwrap(raw)
        if error:
            raise ProviderError("upstream_rejected", error, outcome="rejected")
        upstream = data.get("generateUuid")
        if not isinstance(upstream, str) or not upstream:
            # 收到 200 但拿不到句柄：同样不能重提。
            raise ProviderError(
                "submit_response_lost", "提交响应缺少 generateUuid", outcome="unknown"
            )
        return SubmitResult(
            upstream_task_id=upstream, state="queued", percent=0, upstream_state="1"
        )

    async def poll(self, upstream_task_id: str) -> PollResult:
        credentials = self._credentials()
        async with self.rate_limiter.slot():
            raw = await asyncio.to_thread(
                self._post, self.status_path, {"generateUuid": upstream_task_id}, credentials
            )
        if "_transport_error" in raw or ("_http_error" in raw and raw["_http_error"] >= 500):
            raise ProviderError(
                "poll_transient", "status query temporarily failed", outcome="transient"
            )
        if "_http_error" in raw:
            raise ProviderError("poll_rejected", "status query was rejected", outcome="rejected")
        data, error = self._unwrap(raw)
        if error:
            raise ProviderError("poll_rejected", error, outcome="rejected")
        status = data.get("generateStatus")
        if type(status) is not int:
            raise ProviderError(
                "status_unknown", "upstream status is not readable", outcome="unknown"
            )
        raw_percent = data.get("percentCompleted", 0)
        percent = (
            int(max(0.0, min(1.0, float(raw_percent))) * 100)
            if isinstance(raw_percent, (int, float)) and not isinstance(raw_percent, bool)
            else 0
        )
        if status == 6:
            return PollResult(
                state="failed",
                percent=percent,
                upstream_state="6",
                error=str(data.get("generateMsg") or "upstream generation failed")[:256],
            )
        if status == 5:
            assets = _collect_result_assets(data)
            if not assets:
                # 成功但没有任何可转存素材：不能当成功结算，转人工复核。
                raise ProviderError(
                    "result_missing",
                    "upstream reported success without any storable asset URL",
                    outcome="unknown",
                )
            # 实测官方状态接口会带 pointsCost 与 accountBalance（单条 5 秒图生视频
            # 为 250 积分）。带上就能按真实用量结算，不必再一律按预占挂「待对账」。
            points = data.get("pointsCost")
            points = points if type(points) is int and points >= 0 else None
            balance = data.get("accountBalance")
            balance = balance if type(balance) is int else None
            return PollResult(
                state="succeeded",
                percent=100,
                upstream_state="5",
                assets=tuple(assets),
                cost_micro=points * _MICRO_PER_POINT if points is not None else None,
                upstream_points=points,
                account_balance=balance,
            )
        if status not in _RUNNING_CODES:
            raise ProviderError(
                "status_unknown", f"unmapped generateStatus {status}", outcome="unknown"
            )
        # 「等待执行」与上游进度字段矛盾时以状态为准，不能显示 42% 还在排队。
        if status == 1:
            percent = 0
        return PollResult(
            state="queued" if status == 1 else "running",
            percent=max(percent, _RUNNING_CODES[status]),
            upstream_state=str(status),
        )

    async def fetch(self, asset: RemoteAsset) -> bytes:
        limit = asset_limit(asset.kind)
        if limit is None:
            # 上游给了契约外的媒体形态：不能猜上限，也不能存成别的东西。
            raise ProviderError(
                "asset_kind_invalid",
                f"unsupported asset kind: {asset.kind}",
                outcome="rejected",
            )
        self._assert_public_url(asset.url)
        async with self.rate_limiter.slot():
            result = await asyncio.to_thread(self._get, asset.url, limit)
        if isinstance(result, dict):
            raise ProviderError(
                "asset_fetch_transient",
                "asset download failed; retry the same URL",
                outcome="transient",
            )
        if not result or len(result) > limit:
            raise ProviderError(
                "asset_invalid",
                "asset is empty or exceeds the retention limit",
                outcome="rejected",
            )
        return result
