"""真实 LibTV 适配器的 HTTP 协议层验证：127.0.0.1 假服务 + 真实 urlopen。

事实边界（务必与上游实测区分）：
- 本文件让 ``urllib.request.urlopen`` 真的发请求、真的收响应，补齐
  ``tests/test_libtv_provider_offline.py`` 里全部 stub 掉 ``_post`` / ``_get`` 的空白。
- 签名算法来自第三方公开资料交叉印证，不是官方页面直接给出；这里只能证明
  「我方实现与交叉印证资料自洽，且签名确实随真实 HTTP 请求发出」。
- **不能**据此写成「已验证可接入 LibTV」「上游已验证」或「生产可用」。上游是否接受
  签名、真实响应包装、templateUuid/节点参数、单次积分与对账、OSS 白名单、7 天 URL、
  真实 QPS-1/并发-5 仍必须充值后实测。
- 全程假密钥、本地回环、零付费、零公网；只有取图环节在测试内 stub ``_get``，
  ``_assert_public_url`` 的 SSRF 校验保持原样并真实执行。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import traceback
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from comicdrama.liblib.assets import PNG_MAGIC
from comicdrama.liblib.client import STATUS_PATH, SUBMIT_PATH, LibTVMediaProvider
from comicdrama.liblib.contracts import MAX_ASSET_BYTES, ProviderError, RemoteAsset
from comicdrama.liblib.rate_limiter import RateLimiter

ACCESS_ENV = "LIBTV_ACCESS_KEY"
SECRET_ENV = "LIBTV_SECRET_KEY"
ACCESS_KEY = "test-access-key"
SECRET_KEY = "test-secret-key"

# 保留域名（RFC 2606）；_get 被 stub，因此不会产生任何 DNS 查询。
# 带 query 是为了验证 safe_source() 会把临时签名从审计字段里剥掉。
_ASSET_URL = "https://oss.example.invalid/a.png?Signature=fake&Expires=1893456000"
_ASSET_URL_BARE = "https://oss.example.invalid/a.png"
_DEFAULT = {
    "code": 0,
    "data": {
        "generateUuid": "uuid-loopback",
        "generateStatus": 5,
        "percentCompleted": 1.0,
        "images": [{"imageUrl": _ASSET_URL, "auditStatus": 1}],
    },
}


@dataclass(frozen=True, slots=True)
class Response:
    """假服务的一个响应场景；队列耗尽后回落到 default。"""

    body: bytes | dict
    status: int = 200
    delay: float = 0.0
    drop: bool = False


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}"
        self.requests: list[dict] = []
        self.responses: list[Response] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
        self.default = Response(_DEFAULT)

    def loopback_host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        self._serve()

    def do_GET(self):
        self._serve()

    def _serve(self):
        srv = self.server
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with srv.lock:
                srv.requests.append(
                    {
                        "method": self.command,
                        "raw_path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                response = srv.responses.pop(0) if srv.responses else srv.default
            if response.delay:
                time.sleep(response.delay)
            if response.drop:
                # 客户端会收到连接中断 / 读超时；不写任何响应体。
                self.close_connection = True
                self.connection.close()
                return
            payload = (
                response.body
                if isinstance(response.body, bytes)
                else json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            # pyproject.toml 把 RuntimeWarning / PytestUnraisableExceptionWarning
            # 升级为 error；handler 线程里任何未捕获异常都会让整个测试失败。
            with srv.lock:
                srv.errors.append(traceback.format_exc())
            self.close_connection = True


def _offline_env(monkeypatch) -> None:
    """三层零出网准备：清代理、限定 no_proxy、强制重建 opener、放假凭据。"""
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    # urlopen 会缓存全局 opener；ProxyHandler 在 build_opener 时读取代理环境。
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)
    monkeypatch.setenv(ACCESS_ENV, ACCESS_KEY)
    monkeypatch.setenv(SECRET_ENV, SECRET_KEY)


@pytest.fixture
def fake_server(monkeypatch):
    _offline_env(monkeypatch)
    srv = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _provider(srv, *, timeout: float = 2.0, rate_limiter=None):
    return LibTVMediaProvider(
        base_url=srv.base_url,
        template_uuid="t",
        prompt_node="6",
        timeout=timeout,
        rate_limiter=rate_limiter or RateLimiter(qps=100.0, concurrency=5),
    )


@pytest.fixture
def provider(fake_server):
    return _provider(fake_server)


def _enqueue(srv, *responses: Response) -> None:
    with srv.lock:
        srv.responses.extend(responses)


def _header(request: dict, name: str) -> str:
    """urllib 会把 header 名 capitalize 成 Content-type / Content-length。"""
    return next(v for k, v in request["headers"].items() if k.lower() == name.lower())


def _signature(path: str, timestamp: str, nonce: str) -> str:
    content = f"{path}&{timestamp}&{nonce}"
    digest = hmac.new(SECRET_KEY.encode(), content.encode(), hashlib.sha1).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def _submit(provider):
    return await provider.submit(
        task_id="t-http-1", request_id="r-http-1", prompt="风电场夕阳", width=512, height=512
    )


def _unused_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ------------------------------------------------------------------ A 签名与请求


@pytest.mark.asyncio
async def test_submit_signature_is_sent_over_real_http_and_recomputes(fake_server, provider):
    """端到端重算签名：只证明我方实现自洽，不证明上游接受该签名。"""
    result = await _submit(provider)
    assert result.upstream_task_id == "uuid-loopback"
    assert result.state == "queued"
    assert len(fake_server.requests) == 1

    parts = urlsplit(fake_server.requests[0]["raw_path"])
    query = parse_qs(parts.query)
    assert parts.path == SUBMIT_PATH
    assert set(query) == {"AccessKey", "Signature", "Timestamp", "SignatureNonce"}
    assert query["AccessKey"][0] == ACCESS_KEY
    timestamp, nonce = query["Timestamp"][0], query["SignatureNonce"][0]
    assert timestamp.isdigit() and len(timestamp) == 13
    assert abs(time.time() - int(timestamp) / 1000) < 5
    assert len(nonce) == 32
    assert query["Signature"][0] == _signature(parts.path, timestamp, nonce)
    assert "=" not in query["Signature"][0]
    # 反证：签名 content 用的是 uriPath，不是完整 URL。
    wrong = hmac.new(
        SECRET_KEY.encode(),
        f"{fake_server.base_url}{parts.path}&{timestamp}&{nonce}".encode(),
        hashlib.sha1,
    ).digest()
    assert query["Signature"][0] != base64.urlsafe_b64encode(wrong).decode().rstrip("=")
    assert SECRET_KEY not in fake_server.requests[0]["raw_path"]
    assert fake_server.errors == []


@pytest.mark.asyncio
async def test_request_headers_and_json_body_over_real_http(fake_server, provider):
    await _submit(provider)
    request = fake_server.requests[0]
    assert request["method"] == "POST"
    assert _header(request, "Content-Type") == "application/json; charset=utf-8"
    assert _header(request, "Accept") == "application/json"
    assert _header(request, "User-Agent")
    assert _header(request, "Host") == fake_server.loopback_host()
    assert int(_header(request, "Content-Length")) == len(request["body"])
    body = json.loads(request["body"].decode("utf-8"))
    assert body["templateUuid"] == "t"
    assert body["generateParams"]["6"]["inputs"]["text"] == "风电场夕阳"
    assert body["generateParams"]["imageSize"] == {"width": 512, "height": 512}


@pytest.mark.asyncio
async def test_status_path_uses_its_own_signature_content(fake_server, provider):
    result = await provider.poll("uuid-loopback")
    assert result.state == "succeeded"
    parts = urlsplit(fake_server.requests[0]["raw_path"])
    query = parse_qs(parts.query)
    assert parts.path == STATUS_PATH
    timestamp, nonce = query["Timestamp"][0], query["SignatureNonce"][0]
    assert query["Signature"][0] == _signature(STATUS_PATH, timestamp, nonce)
    assert query["Signature"][0] != _signature(SUBMIT_PATH, timestamp, nonce)
    assert json.loads(fake_server.requests[0]["body"]) == {"generateUuid": "uuid-loopback"}


@pytest.mark.asyncio
async def test_all_fake_server_requests_are_loopback_only(fake_server, provider, monkeypatch):
    """额外用 socket.connect 守卫拒绝任何非回环地址，作为零出网的第二层证据。"""
    real_connect = socket.socket.connect

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("egress blocked: only loopback is allowed in this test")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    await _submit(provider)
    await provider.poll("uuid-loopback")
    assert fake_server.base_url.startswith("http://127.0.0.1:")
    assert len(fake_server.requests) == 2
    for request in fake_server.requests:
        assert _header(request, "Host") == fake_server.loopback_host()
        assert urlsplit(request["raw_path"]).path in {SUBMIT_PATH, STATUS_PATH}
    assert fake_server.errors == []


# ------------------------------------------------------------------ B 响应包装


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"code": 0, "data": {"generateUuid": "uuid-envelope"}}, "uuid-envelope"),
        ({"generateUuid": "uuid-bare"}, "uuid-bare"),
    ],
)
async def test_code_data_envelope_and_bare_object_both_unwrap_over_http(
    fake_server, provider, body, expected
):
    _enqueue(fake_server, Response(body))
    result = await _submit(provider)
    assert result.upstream_task_id == expected
    assert len(fake_server.requests) == 1  # 写请求绝不自动重提
    assert fake_server.errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, "0", 200, "200", None])
async def test_code_allowlist_values_are_accepted_over_http(fake_server, provider, code):
    _enqueue(fake_server, Response({"code": code, "data": {"generateUuid": "uuid-allow"}}))
    result = await _submit(provider)
    assert result.upstream_task_id == "uuid-allow"


@pytest.mark.asyncio
async def test_nonzero_code_is_rejected_over_http(fake_server, provider):
    _enqueue(fake_server, Response({"code": 1001, "data": {"generateUuid": "ignored"}}))
    with pytest.raises(ProviderError) as caught:
        await _submit(provider)
    assert caught.value.code == "upstream_rejected"
    assert caught.value.outcome == "rejected"
    assert len(fake_server.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected_code,expected_outcome",
    [
        ({"code": 0, "foo": "bar"}, "upstream_rejected", "rejected"),
        ({"code": 0, "data": {}}, "submit_response_lost", "unknown"),
    ],
)
async def test_unexpected_response_shape_is_rejected_over_http(
    fake_server, provider, body, expected_code, expected_outcome
):
    """两种 200 响应必须分开归类。

    _unwrap 识别不出形状 -> rejected；data 是 dict 但缺 generateUuid -> unknown
    （收到 200 却拿不到句柄，同样禁止自动重提）。非 dict JSON 则在 _post 里
    直接变成 transport error，见 C 组测试。
    """
    _enqueue(fake_server, Response(body))
    with pytest.raises(ProviderError) as caught:
        await _submit(provider)
    assert caught.value.code == expected_code
    assert caught.value.outcome == expected_outcome
    if expected_code == "upstream_rejected":
        assert "unexpected upstream response shape" in caught.value.message
    assert len(fake_server.requests) == 1


# ------------------------------------------------------------------ C HTTP 错误归一化


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "poll"])
@pytest.mark.parametrize("status", [400, 401, 429, 500, 502, 503])
async def test_http_4xx_and_5xx_are_normalized_over_http(fake_server, provider, status, method):
    _enqueue(fake_server, Response({"error": "synthetic"}, status=status))
    with pytest.raises(ProviderError) as caught:
        if method == "submit":
            await _submit(provider)
        else:
            await provider.poll("uuid-loopback")
    if method == "submit":
        assert caught.value.code == (
            "upstream_rejected" if status < 500 else "submit_response_lost"
        )
        assert caught.value.outcome == ("rejected" if status < 500 else "unknown")
    else:
        assert caught.value.code == ("poll_rejected" if status < 500 else "poll_transient")
        assert caught.value.outcome == ("rejected" if status < 500 else "transient")
    assert len(fake_server.requests) == 1


@pytest.mark.asyncio
async def test_transport_error_on_dropped_connection_over_http(fake_server, provider):
    _enqueue(fake_server, Response(b"", drop=True))
    with pytest.raises(ProviderError) as caught:
        await _submit(provider)
    assert caught.value.code == "submit_response_lost"
    assert caught.value.outcome == "unknown"
    assert len(fake_server.requests) == 1


@pytest.mark.asyncio
async def test_timeout_is_passed_to_urlopen_over_http(fake_server):
    slow = _provider(fake_server, timeout=0.1)
    _enqueue(fake_server, Response(b"", delay=0.5, drop=True))
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        await _submit(slow)
    elapsed = time.monotonic() - started
    assert elapsed < 0.45  # 确实按 timeout 提前返回，而不是等满 delay
    assert caught.value.code == "submit_response_lost"
    assert caught.value.outcome == "unknown"


@pytest.mark.asyncio
async def test_oversized_json_becomes_502_over_http(fake_server, provider):
    oversized = b"x" * ((1 << 20) + 1)
    _enqueue(fake_server, Response(oversized), Response(oversized))
    with pytest.raises(ProviderError) as caught:
        await _submit(provider)
    assert caught.value.code == "submit_response_lost"
    assert caught.value.outcome == "unknown"
    with pytest.raises(ProviderError) as polled:
        await provider.poll("uuid-loopback")
    assert polled.value.code == "poll_transient"
    assert polled.value.outcome == "transient"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b"[]", b"42", b"\xff\xfe"])
async def test_invalid_json_non_dict_bad_utf8_become_transport_over_http(
    fake_server, provider, body
):
    _enqueue(fake_server, Response(body))
    with pytest.raises(ProviderError) as caught:
        await _submit(provider)
    assert caught.value.code == "submit_response_lost"
    assert caught.value.outcome == "unknown"


@pytest.mark.asyncio
async def test_connection_refused_is_transport_error(monkeypatch):
    _offline_env(monkeypatch)
    port = _unused_loopback_port()
    refused = LibTVMediaProvider(
        base_url=f"http://127.0.0.1:{port}",
        template_uuid="t",
        prompt_node="6",
        timeout=0.5,
        rate_limiter=RateLimiter(qps=100.0, concurrency=5),
    )
    with pytest.raises(ProviderError) as caught:
        await _submit(refused)
    assert caught.value.code == "submit_response_lost"
    assert caught.value.outcome == "unknown"


# ------------------------------------------------------------------ D 状态映射


@pytest.mark.asyncio
async def test_status_sequence_1_to_5_over_real_http(fake_server, provider):
    _enqueue(
        fake_server,
        *[
            Response({"data": {"generateStatus": s, "percentCompleted": 0.42}})
            for s in (1, 2, 3, 4)
        ],
        Response(
            {
                "data": {
                    "generateStatus": 5,
                    "percentCompleted": 1.0,
                    "images": [{"imageUrl": _ASSET_URL, "auditStatus": 1}],
                }
            }
        ),
    )
    results = [await provider.poll("uuid-loopback") for _ in range(5)]
    assert [item.state for item in results] == [
        "queued",
        "running",
        "running",
        "running",
        "succeeded",
    ]
    assert [item.percent for item in results] == [0, 42, 95, 98, 100]
    assert [item.upstream_state for item in results] == ["1", "2", "3", "4", "5"]
    assert results[-1].cost_micro is None
    assert [asset.url for asset in results[-1].assets] == [_ASSET_URL]
    assert len(fake_server.requests) == 5
    assert all(urlsplit(r["raw_path"]).path == STATUS_PATH for r in fake_server.requests)
    assert fake_server.errors == []


@pytest.mark.asyncio
async def test_status_6_failure_over_real_http(fake_server, provider):
    _enqueue(
        fake_server,
        Response({"data": {"generateStatus": 6, "generateMsg": "审核未通过"}}),
    )
    failed = await provider.poll("uuid-loopback")
    assert failed.state == "failed"
    assert failed.error == "审核未通过"
    assert failed.terminal is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["5", True, 0, 7, 99, -1])
async def test_unreadable_generate_status_over_http(fake_server, provider, status):
    _enqueue(fake_server, Response({"data": {"generateStatus": status}}))
    with pytest.raises(ProviderError) as caught:
        await provider.poll("uuid-loopback")
    assert caught.value.code == "status_unknown"
    assert caught.value.outcome == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,status,expected",
    [
        (0.42, 2, 42),
        ("0.9", 2, 0),
        (True, 2, 0),
        (-1.0, 2, 0),
        (2.5, 2, 100),
        (0.42, 1, 0),  # 状态 1「等待执行」强制 0，不能显示 42%
    ],
)
async def test_percent_completed_edge_cases_over_http(
    fake_server, provider, value, status, expected
):
    _enqueue(fake_server, Response({"data": {"generateStatus": status, "percentCompleted": value}}))
    result = await provider.poll("uuid-loopback")
    assert result.percent == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "images",
    [
        [],
        [{"imageUrl": "http://oss.example.invalid/a.png"}],
    ],
)
async def test_success_without_https_image_is_unknown_over_http(fake_server, provider, images):
    _enqueue(fake_server, Response({"data": {"generateStatus": 5, "images": images}}))
    with pytest.raises(ProviderError) as caught:
        await provider.poll("uuid-loopback")
    assert caught.value.code == "result_missing"
    assert caught.value.outcome == "unknown"


# ------------------------------------------------------------------ E _get 与 fetch 边界


@pytest.mark.asyncio
async def test_get_returns_bytes_and_error_dicts_over_real_http(fake_server, provider):
    """直接测 _get 的 loopback HTTP 路径，不经 fetch、不绕过 SSRF 校验。"""
    url = f"{fake_server.base_url}/asset"
    _enqueue(
        fake_server,
        Response(PNG_MAGIC + b"get-bytes"),
        Response(b"missing", status=404),
        Response(b"", drop=True),
    )
    limit = MAX_ASSET_BYTES
    assert await asyncio.to_thread(provider._get, url, limit) == PNG_MAGIC + b"get-bytes"
    assert await asyncio.to_thread(provider._get, url, limit) == {"_http_error": 404}
    assert await asyncio.to_thread(provider._get, url, limit) == {"_transport_error": True}
    assert len(fake_server.requests) == 3
    assert all(r["method"] == "GET" for r in fake_server.requests)


@pytest.mark.asyncio
async def test_fetch_rejects_empty_or_oversized_asset(provider):
    """fetch 的空/超限分支；SSRF 校验真实执行（域名为普通非私有域名）。"""
    provider._get = lambda url, limit: b""  # type: ignore[method-assign]
    with pytest.raises(ProviderError) as caught:
        await provider.fetch(RemoteAsset(url=_ASSET_URL_BARE))
    assert caught.value.code == "asset_invalid"
    assert caught.value.outcome == "rejected"

    oversized = b"x" * (MAX_ASSET_BYTES + 1)
    provider._get = lambda url, limit: oversized  # type: ignore[method-assign]
    with pytest.raises(ProviderError) as caught:
        await provider.fetch(RemoteAsset(url=_ASSET_URL_BARE))
    assert caught.value.code == "asset_invalid"
    assert caught.value.outcome == "rejected"


# ------------------------------------------------------------------ F 限流器三入口


class RecordingLimiter:
    def __init__(self):
        self.inner = RateLimiter(qps=100.0, concurrency=5)
        self.calls = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.calls += 1
        async with self.inner.slot():
            yield


@pytest.mark.asyncio
async def test_rate_limiter_enters_submit_poll_and_fetch(fake_server):
    recorder = RecordingLimiter()
    limited = _provider(fake_server, rate_limiter=recorder)
    _enqueue(
        fake_server,
        Response({"code": 0, "data": {"generateUuid": "uuid-rate"}}),
        Response(
            {
                "data": {
                    "generateStatus": 5,
                    "percentCompleted": 1.0,
                    "images": [{"imageUrl": _ASSET_URL_BARE}],
                }
            }
        ),
    )
    limited._get = lambda url, limit: PNG_MAGIC + b"loopback"  # type: ignore[method-assign]
    submitted = await _submit(limited)
    polled = await limited.poll(submitted.upstream_task_id)
    data = await limited.fetch(polled.assets[0])
    assert data.startswith(PNG_MAGIC)
    assert recorder.calls == 3


def test_default_rate_limiter_is_used_when_not_injected():
    default = LibTVMediaProvider(template_uuid="t", prompt_node="6")
    assert isinstance(default.rate_limiter, RateLimiter)
    assert default.rate_limiter.qps == 1.0
    assert default.rate_limiter.concurrency == 5


# ------------------------------------------------------------------ G 配置开关零出网
