"""LibTV 适配器的纯离线验证：所有 HTTP 被 monkeypatch，全程不出网、不用真实凭据。

这里验证的是「代码形状」而非「上游可接入」：签名算法来自第三方公开资料交叉印证，
提交/状态响应包装、节点参数、单次积分字段仍待最后一公里充值后实测。
"""

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import pytest

from comicdrama.liblib.client import STATUS_PATH, SUBMIT_PATH, LibTVMediaProvider
from comicdrama.liblib.contracts import ProviderError, RemoteAsset
from comicdrama.liblib.rate_limiter import RateLimiter

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ACCESS_ENV = "LIBTV_ACCESS_KEY"
SECRET_ENV = "LIBTV_SECRET_KEY"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv(ACCESS_ENV, "test-access-key")
    monkeypatch.setenv(SECRET_ENV, "test-secret-key")
    return LibTVMediaProvider(
        base_url="https://openapi.example.invalid",
        template_uuid="template-uuid-placeholder",
        prompt_node="6",
        prompt_field="text",
    )


def stub_post(provider, payload, *, calls=None):
    def fake(path, body, credentials):
        if calls is not None:
            calls.append((path, body, credentials))
        return payload

    provider._post = fake  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_credentials_are_read_by_name_and_never_echoed(monkeypatch):
    monkeypatch.delenv(ACCESS_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    provider = LibTVMediaProvider(template_uuid="t", prompt_node="6")
    with pytest.raises(ProviderError) as caught:
        await provider.submit(task_id="a", request_id="b", prompt="x", width=512, height=512)
    assert caught.value.code == "credentials_missing"
    assert caught.value.outcome == "rejected"
    assert ACCESS_ENV in caught.value.message and SECRET_ENV in caught.value.message
    assert "test-secret-key" not in caught.value.message


def test_signed_url_follows_cross_checked_algorithm(provider):
    url = provider._signed_url(SUBMIT_PATH, ("test-access-key", "test-secret-key"))
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.path == SUBMIT_PATH
    assert set(query) == {"AccessKey", "Signature", "Timestamp", "SignatureNonce"}
    timestamp, nonce = query["Timestamp"][0], query["SignatureNonce"][0]
    assert timestamp.isdigit() and len(timestamp) == 13
    assert len(nonce) == 32
    content = f"{SUBMIT_PATH}&{timestamp}&{nonce}"
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(b"test-secret-key", content.encode(), hashlib.sha1).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert query["Signature"][0] == expected
    assert "=" not in query["Signature"][0]
    assert "test-secret-key" not in url


@pytest.mark.asyncio
async def test_submit_builds_task_payload_and_returns_uuid(provider):
    calls = []
    stub_post(provider, {"code": 0, "data": {"generateUuid": "uuid-123"}}, calls=calls)
    result = await provider.submit(
        task_id="t1", request_id="r1", prompt="一只狐狸", width=512, height=512
    )
    assert result.upstream_task_id == "uuid-123"
    assert result.state == "queued"
    path, body, credentials = calls[0]
    assert path == SUBMIT_PATH
    assert body["templateUuid"] == "template-uuid-placeholder"
    assert body["generateParams"]["6"]["inputs"]["text"] == "一只狐狸"
    assert body["generateParams"]["imageSize"] == {"width": 512, "height": 512}
    assert credentials == ("test-access-key", "test-secret-key")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_code,expected_outcome",
    [
        ({"_transport_error": True}, "submit_response_lost", "unknown"),
        ({"_http_error": 400}, "upstream_rejected", "rejected"),
        ({"_http_error": 429}, "upstream_rejected", "rejected"),
        ({"_http_error": 502}, "submit_response_lost", "unknown"),
        ({"code": 1001, "data": {}}, "upstream_rejected", "rejected"),
        ({"code": 0, "data": {}}, "submit_response_lost", "unknown"),
    ],
)
async def test_submit_never_retries_and_classifies_outcome(
    provider, response, expected_code, expected_outcome
):
    calls = []
    stub_post(provider, response, calls=calls)
    with pytest.raises(ProviderError) as caught:
        await provider.submit(task_id="t1", request_id="r1", prompt="x", width=512, height=512)
    assert caught.value.code == expected_code
    assert caught.value.outcome == expected_outcome
    assert len(calls) == 1  # 写请求结果未知时绝不自动重提


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_state,expected_percent",
    [(1, "queued", 0), (2, "running", 42), (3, "running", 95), (4, "running", 98)],
)
async def test_running_status_mapping(provider, status, expected_state, expected_percent):
    stub_post(
        provider,
        {"data": {"generateStatus": status, "percentCompleted": 0.42}},
    )
    result = await provider.poll("uuid-123")
    assert result.state == expected_state
    assert result.percent == expected_percent
    assert result.terminal is False


@pytest.mark.asyncio
async def test_success_maps_images_and_leaves_cost_unconfirmed(provider):
    calls = []
    stub_post(
        provider,
        {
            "code": 0,
            "data": {
                "generateStatus": 5,
                "percentCompleted": 1.0,
                "images": [
                    {"imageUrl": "https://oss.example/a.png", "auditStatus": 3},
                    {"imageUrl": "http://internal/a.png", "auditStatus": 3},
                ],
            },
        },
        calls=calls,
    )
    result = await provider.poll("uuid-123")
    assert calls[0][0] == STATUS_PATH
    assert calls[0][1] == {"generateUuid": "uuid-123"}
    assert result.state == "succeeded" and result.percent == 100
    assert [asset.url for asset in result.assets] == ["https://oss.example/a.png"]
    assert result.cost_micro is None  # 官方未确认单次积分字段，须按预占结算并标记待对账


@pytest.mark.asyncio
async def test_success_without_images_is_unknown(provider):
    stub_post(provider, {"data": {"generateStatus": 5, "images": []}})
    with pytest.raises(ProviderError) as caught:
        await provider.poll("uuid-123")
    assert caught.value.code == "result_missing"
    assert caught.value.outcome == "unknown"


@pytest.mark.asyncio
async def test_failure_and_unknown_status(provider):
    stub_post(provider, {"data": {"generateStatus": 6, "generateMsg": "审核未通过"}})
    failed = await provider.poll("uuid-123")
    assert failed.state == "failed" and failed.error == "审核未通过"
    stub_post(provider, {"data": {"generateStatus": 99}})
    with pytest.raises(ProviderError) as caught:
        await provider.poll("uuid-123")
    assert caught.value.outcome == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,outcome",
    [
        ({"_transport_error": True}, "transient"),
        ({"_http_error": 503}, "transient"),
        ({"_http_error": 401}, "rejected"),
        ({"code": 1001}, "rejected"),
    ],
)
async def test_poll_transient_errors_are_retryable(provider, response, outcome):
    stub_post(provider, response)
    with pytest.raises(ProviderError) as caught:
        await provider.poll("uuid-123")
    assert caught.value.outcome == outcome


@pytest.mark.asyncio
async def test_fetch_rejects_private_or_plaintext_urls_without_network(provider):
    for url in (
        "http://oss.example/a.png",
        "https://localhost/a.png",
        "https://127.0.0.1/a.png",
        "https://192.168.1.8/a.png",
    ):
        with pytest.raises(ProviderError) as caught:
            await provider.fetch(RemoteAsset(url=url))
        assert caught.value.code == "asset_url_invalid"


@pytest.mark.asyncio
async def test_fetch_returns_bytes_offline(provider):
    provider._get = lambda url, limit: PNG_MAGIC + b"payload"  # type: ignore[method-assign]
    data = await provider.fetch(RemoteAsset(url="https://oss.example/a.png"))
    assert data.startswith(PNG_MAGIC)
    provider._get = lambda url, limit: {"_transport_error": True}  # type: ignore[method-assign]
    with pytest.raises(ProviderError) as caught:
        await provider.fetch(RemoteAsset(url="https://oss.example/a.png"))
    assert caught.value.outcome == "transient"


@pytest.mark.asyncio
async def test_rate_limiter_enforces_qps_and_concurrency():
    import asyncio
    import time

    limiter = RateLimiter(qps=20.0, concurrency=2)
    active, peak = 0, 0

    async def worker():
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    started = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(6)))
    elapsed = time.monotonic() - started
    assert peak <= 2
    assert elapsed >= 0.05 * 5 * 0.7  # 6 个时隙，每个 0.05s，允许调度误差


# ---------------------------------------------------------------- 官方视频通道（平铺提示词）


def video_provider(monkeypatch, *, model="kling-v2-5-turbo", with_image=True):
    """官方 Kling 通道的客户端：prompt_mode=plain，提交与轮询路径都换成视频那套。"""
    monkeypatch.setenv(ACCESS_ENV, "test-access-key")
    monkeypatch.setenv(SECRET_ENV, "test-secret-key")
    extra: dict[str, object] = {"model": model, "duration": "5", "mode": "pro"}
    if with_image:
        if model.startswith("kling-v2-6"):
            extra["images"] = ["https://cdn.example.com/first.png"]
            extra["sound"] = "off"
        else:
            extra["startFrame"] = "https://cdn.example.com/first.png"
    return LibTVMediaProvider(
        base_url="https://openapi.example.invalid",
        template_uuid="180f33c6748041b48593030156d2a71d",
        prompt_mode="plain",
        submit_path="/api/generate/video/kling/img2video",
        status_path="/api/generate/status",
        extra_nodes=extra,
    )


async def test_plain_mode_flattens_prompt_and_omits_image_size(monkeypatch):
    """官方通道的 generateParams 是平铺字段：没有节点层，也不该带 imageSize。

    画幅跟随首帧图，多传尺寸会被上游判成多余参数；节点层是工作流通道的约定。
    """
    client = video_provider(monkeypatch)
    calls = []
    stub_post(client, {"code": 0, "data": {"generateUuid": "uuid-abc"}}, calls=calls)
    result = await client.submit(
        task_id="t", request_id="r", prompt="镜头缓慢推进", width=1024, height=1024
    )
    path, body, _ = calls[0]
    assert path == "/api/generate/video/kling/img2video"
    assert body["templateUuid"] == "180f33c6748041b48593030156d2a71d"
    params = body["generateParams"]
    assert params["prompt"] == "镜头缓慢推进"
    assert params["startFrame"] == "https://cdn.example.com/first.png"
    assert params["duration"] == "5", "上游要的是字符串时长"
    assert "imageSize" not in params
    assert not any(isinstance(v, dict) for v in params.values()), "不许出现节点层"
    assert result.upstream_task_id == "uuid-abc"


def test_plain_mode_does_not_require_a_prompt_node(monkeypatch):
    monkeypatch.setenv(ACCESS_ENV, "k")
    monkeypatch.setenv(SECRET_ENV, "s")
    client = LibTVMediaProvider(template_uuid="t", prompt_mode="plain")
    assert client.prompt_node == ""
    # 工作流通道仍然必须给节点 id，否则请求体没有地方放提示词。
    with pytest.raises(ValueError):
        LibTVMediaProvider(template_uuid="t", prompt_mode="node")
    with pytest.raises(ValueError):
        LibTVMediaProvider(template_uuid="t", prompt_mode="neither")


async def test_poll_records_upstream_points_and_balance(provider):
    """实测官方状态接口带 pointsCost / accountBalance，必须落下来供计费对账。"""
    stub_post(
        provider,
        {
            "code": 0,
            "data": {
                "generateStatus": 5,
                "percentCompleted": 1.0,
                "pointsCost": 250,
                "accountBalance": 211,
                "videos": [{"videoUrl": "https://cdn.example.com/a.mp4", "auditStatus": 3}],
            },
        },
    )
    polled = await provider.poll("uuid-1")
    assert polled.state == "succeeded"
    assert polled.upstream_points == 250
    assert polled.account_balance == 211
    assert polled.cost_micro == 250 * 10_000, "1 积分 = 0.01 元 = 10000 微元"
    assert [a.kind for a in polled.assets] == ["video"]


async def test_poll_without_points_keeps_cost_unconfirmed(provider):
    """上游没给 pointsCost 时保持 None，让服务层按预占结算并标待对账。"""
    stub_post(
        provider,
        {
            "code": 0,
            "data": {
                "generateStatus": 5,
                "images": [{"imageUrl": "https://cdn.example.com/a.png"}],
            },
        },
    )
    polled = await provider.poll("uuid-2")
    assert polled.cost_micro is None
    assert polled.upstream_points is None
    assert polled.account_balance is None
