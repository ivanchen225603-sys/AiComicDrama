"""星流文生图官方通道（seedreamV4）的离线验收：四道本地守卫 + 请求形状 + 失败即退款。

**测试不做任何付费调用。** 全部用例都把上游替换成桩：`_post` / `poll` / `fetch`
一律 monkeypatch，断言的是「我们发出去的请求长什么样」与「本地拒绝时钱怎么退」，
不是「上游确实能出图」。

事实分级（不得写成已验证）：
- 已实测（零成本探测）：提交路径 `/api/generate/seedreamV4` 存在、模型名
  `doubao-seedream-4-5-251128`、模板 `0b6bad2fd350433ebb5abc7eb91f2ec9`、
  参数形状 `{templateUuid, generateParams{model,prompt,width,height,imgCount,
  sequentialImageGeneration,referenceImages?}}`、总像素区间
  `3,686,400 ~ 16,777,216`、参数校验失败（`100050`）**不扣费**。
- 未验证：三档尺寸「正好等于下限」是否被上游接受（只测到 2,073,600 被拒）、
  像素档位与单价的关系、参考图实际生效效果、`imgCount > 1` 的计费。

交付边界（写进本文件是为了让下一个人别误判）：关键帧必然带参考图，而
`referenceImages` 只收公网 https URL，所以在**没有公网 https 部署**的环境里，
official 通道的关键帧会在 `_public_url` 处以 `drama_config` 本地拒绝（503、
全额退款、未出网）。这是预期行为，不是缺陷。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import comicdrama.providers.remote as remote_module
from comicdrama.config import Settings
from comicdrama.errors import AppError
from comicdrama.liblib.client import LibTVMediaProvider
from comicdrama.liblib.contracts import (
    PollResult,
    ProviderError,
    RemoteAsset,
    size_rejection,
)
from comicdrama.media_store import DramaMediaStore
from comicdrama.providers.remote import (
    _SEEDREAM_FRAMES,
    _SEEDREAM_MAX_PIXELS,
    _SEEDREAM_MIN_PIXELS,
    RemoteDramaConfig,
    RemoteDramaProvider,
)
from comicdrama.service import DramaService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
SEED_BALANCE = 980

# 产品默认项目设定：1080×1920 = 2,073,600 像素，低于上游下限 3,686,400。
DEFAULT_SETTINGS = {"ratio": "9:16 竖屏", "res": "1080 × 1920 / 24fps", "style": "精致国漫"}


def _official(tmp_path: Path, **overrides: Any) -> RemoteDramaProvider:
    base: dict[str, Any] = {
        "llm_base_url": "http://127.0.0.1/v1",
        "llm_model": "m",
        "t2i_channel": "official",
        "public_base_url": "https://drama.example.com",
    }
    base.update(overrides)
    return RemoteDramaProvider(RemoteDramaConfig(**base), DramaMediaStore(tmp_path / "media"))


def _shot(**overrides: Any) -> dict[str, Any]:
    shot: dict[str, Any] = {
        "id": "S1",
        "prompt": "雨夜冷蓝调，撑黑伞站定",
        "project_settings": dict(DEFAULT_SETTINGS),
        "anchor_refs": ["@角色:林砚"],
        "anchor_asset_id": "asset-anchor",
        "first_frame_asset_id": "asset-anchor",
    }
    shot.update(overrides)
    return shot


class _Recorder:
    """抓一次真实 `submit` 发出的请求体（在 `_post` 这一层抓，才看得到 generateParams）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, path: str, payload: dict, credentials: tuple[str, str]) -> dict:
        self.calls.append({"path": path, "payload": payload})
        return {"generateUuid": "upstream-1"}


def _stub_transport(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """把上游整条链路替换成桩：提交抓包、状态即成功、素材立即下载。"""

    async def poll(self, upstream_task_id: str) -> PollResult:
        return PollResult(
            state="succeeded",
            percent=100,
            upstream_state="5",
            assets=(RemoteAsset(url="https://cdn.example.com/a.png", kind="image"),),
            upstream_points=13,
            account_balance=987,
        )

    async def fetch(self, asset: RemoteAsset) -> bytes:
        return PNG

    monkeypatch.setattr(LibTVMediaProvider, "_post", recorder.post)
    monkeypatch.setattr(LibTVMediaProvider, "poll", poll)
    monkeypatch.setattr(LibTVMediaProvider, "fetch", fetch)


def _forbid_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """断言「本地就拒」：一旦有人构造 client，立刻记下来并炸掉。"""
    built: list[dict[str, Any]] = []

    class _Forbidden:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            raise AssertionError("本地守卫必须拦在建 client 之前，绝不出网")

    monkeypatch.setattr(remote_module, "LibTVMediaProvider", _Forbidden)
    return built


# ---------------------------------------------------------------- 1. 尺寸三档与像素区间


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        # 原始尺寸已经等于官方档 -> adjusted=False（没有改动用户设定）
        ({"ratio": "9:16 竖屏", "res": "1440 × 2560"}, (1440, 2560, False)),
        ({"ratio": "16:9 横屏", "res": "2560 × 1440"}, (2560, 1440, False)),
        ({"ratio": "1:1", "res": "1920 × 1920"}, (1920, 1920, False)),
        # 比例未命中三档 -> 取最近档并标记 adjusted
        # 4/3≈1.333：与 1:1 差 0.333、与 16:9 差 0.444，最近的是 1:1
        ({"ratio": "4:3", "res": "1600 × 1200"}, (1920, 1920, True)),
    ],
)
def test_seedream_size_maps_official_frames(
    tmp_path: Path, settings: dict[str, Any], expected: tuple[int, int, bool]
) -> None:
    assert _official(tmp_path)._seedream_size(settings) == expected


def test_seedream_size_normalises_the_product_default_resolution(tmp_path: Path) -> None:
    """默认 1080×1920 只有 2,073,600 像素，必须归一化到 1440×2560 才可能出图。

    这条是 T04 的核心：`e66bedb` 之后通用 `validate()` 会放行 1080×1920
    （它作为一张图片完全合法），所以归一化只能发生在这个通道私有实现里。
    """
    provider = _official(tmp_path)
    width, height, adjusted = provider._seedream_size(DEFAULT_SETTINGS)
    assert (width, height) == (1440, 2560)
    assert adjusted is True, "改了用户设定就必须如实标记"
    # 只有 res、没有 ratio 时也要能解析出比例来
    assert provider._seedream_size({"res": "1080 × 1920 / 24fps"}) == (1440, 2560, True)


def test_seedream_size_rejects_unparsable_settings(tmp_path: Path) -> None:
    """两处都解析不出比例 -> 本地拒绝，绝不悄悄回落到 1:1 方图。"""
    provider = _official(tmp_path)
    with pytest.raises(AppError) as caught:
        provider._seedream_size({"ratio": "", "res": ""})
    assert caught.value.code == "drama_t2i_size"
    assert str(_SEEDREAM_MIN_PIXELS) in caught.value.message


def test_official_frames_sit_on_the_upstream_floor_and_pass_the_generic_rules() -> None:
    """把「上游像素下限」与「通用尺寸合法性」两套规则的交叉点锁死。

    手改档位（比如换成 1280×720）会同时打红这里：像素低于下限，
    或比例不在白名单，任一侧被改动都会立刻暴露。
    """
    assert len(_SEEDREAM_FRAMES) == 3
    for (ratio_w, ratio_h), (width, height) in _SEEDREAM_FRAMES.items():
        assert width % 2 == 0 and height % 2 == 0, "奇数边会被通用规则拒绝"
        assert width * height == _SEEDREAM_MIN_PIXELS, "三档是零余量档，正好等于下限"
        assert _SEEDREAM_MIN_PIXELS <= width * height <= _SEEDREAM_MAX_PIXELS
        assert size_rejection(width, height) is None, "官方档必须能过通用尺寸规则"
        assert abs(width / height - ratio_w / ratio_h) < 1e-9


def test_strict_policy_rejects_the_default_resolution_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """strict = 原样提交：1080×1920 不在上游区间就本地拒绝，并提示切 normalize。"""
    provider = _official(tmp_path, t2i_size_policy="strict")
    built = _forbid_client(monkeypatch)
    with pytest.raises(AppError) as caught:
        provider.keyframe(_shot(), owner="local")
    assert caught.value.code == "drama_t2i_size"
    assert "normalize" in caught.value.message
    assert built == [], "严格策略的拒绝必须发生在建 client 之前"


# ---------------------------------------------------------------- 2. 参考图条数：硬报错不切片


def _refs(count: int) -> list[str]:
    return [f"ref-{index}" for index in range(count)]


@pytest.mark.parametrize("count", [14, 15])
def test_reference_limit_is_the_configured_value_and_keeps_the_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """上限内必须按**输入顺序**完整提交；判据是配置值，不是写死的 14。

    `max_refs=15` 时必须放行 15 张——否则「14」就成了硬编码，
    以后上游放宽口径只能改代码。
    """
    provider = _official(tmp_path, t2i_official_max_refs=count)
    recorder = _Recorder()
    _stub_transport(monkeypatch, recorder)
    monkeypatch.setenv("LIBTV_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("LIBTV_SECRET_KEY", "sk-test")

    official = provider._text_to_image_official(
        "雨夜冷蓝调",
        owner="local",
        reference_asset_ids=_refs(count),
        settings=dict(DEFAULT_SETTINGS),
        usage={},
    )
    assert len(recorder.calls) == 1, "上限内的请求必须真的发出去"
    submitted = recorder.calls[0]["payload"]["generateParams"]["referenceImages"]
    assert submitted == [
        f"https://drama.example.com/api/v1/drama/assets/ref-{index}" for index in range(count)
    ], "不重排、不切片"
    assert official["reference_images"] == submitted
    assert official["reference_mode"] == "reference_image"


def test_reference_overflow_is_a_local_error_with_counts_in_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """15 张 > 14：`drama_config` 硬报错，消息带实际张数与上限，且一个字节都没出网。"""
    provider = _official(tmp_path, t2i_official_max_refs=14)
    built = _forbid_client(monkeypatch)
    with pytest.raises(AppError) as caught:
        provider._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=[f"ref-{index}" for index in range(15)],
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    message = caught.value.message
    assert caught.value.code == "drama_config"
    assert "15 张" in message and "14 张" in message
    # 语义必须写明「不会自动丢弃」：静默少传一张等于悄悄改出图内容
    assert "不会自动丢弃多余参考图" in message
    assert built == [], "条数守卫必须在建 client 之前"


# ---------------------------------------------------------------- 3. 公网基址

POSITIONAL_MESSAGE = "publicly reachable"


def test_missing_public_base_url_is_rejected_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _official(tmp_path, public_base_url="")
    built = _forbid_client(monkeypatch)
    with pytest.raises(AppError) as caught:
        provider._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=["ref-0"],
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    assert caught.value.code == "drama_config"
    assert "COMIC_DRAMA_PUBLIC_BASE_URL" in caught.value.message
    assert POSITIONAL_MESSAGE in caught.value.message
    assert built == []


def test_non_https_public_base_url_has_its_own_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「没配」与「协议错」必须是两段不同的文案，否则测试无法区分两者。"""
    provider = _official(tmp_path, public_base_url="http://drama.example.com")
    built = _forbid_client(monkeypatch)
    with pytest.raises(AppError) as caught:
        provider._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=["ref-0"],
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    assert caught.value.code == "drama_config"
    assert "must be https" in caught.value.message
    assert POSITIONAL_MESSAGE not in caught.value.message, "两段文案不能互相伪装"
    assert built == []

    empty = _official(tmp_path / "empty", public_base_url="")
    with pytest.raises(AppError) as missing:
        empty._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=["ref-0"],
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    assert missing.value.message != caught.value.message


def test_no_references_needs_no_public_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无参考资产时本来就不需要公网基址：这不是降级，是本来就不需要。"""
    provider = _official(tmp_path, public_base_url="")
    _stub_transport(monkeypatch, _Recorder())
    monkeypatch.setenv("LIBTV_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("LIBTV_SECRET_KEY", "sk-test")
    item = provider.asset_candidates(
        {"id": "a1", "name": "林砚", "seed": 1, "project_settings": dict(DEFAULT_SETTINGS)},
        owner="local",
    )
    payload = item["payload"]
    assert payload["reference_images"] == []
    assert payload["reference_mode"] == "prompt_only"
    assert "reference_images_skipped" not in payload
    assert payload["channel"] == "official"


# ---------------------------------------------------------------- 4. 请求形状


def test_official_request_carries_plain_generate_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓真实 `submit` 打出的请求体：官方通道靠 generateParams 的平铺字段。"""
    provider = _official(tmp_path)
    recorder = _Recorder()
    _stub_transport(monkeypatch, recorder)
    monkeypatch.setenv("LIBTV_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("LIBTV_SECRET_KEY", "sk-test")

    shot = _shot(
        anchor_asset_id="asset-anchor",
        # 参考图用的是锚点图的素材文件编号（anchor_media_id），不是资产编号
        anchor_media_id="asset-anchor",
        anchor_refs=["@角色:林砚"],
        first_frame_asset_id="asset-first",
    )
    item = provider.keyframe(shot, owner="local")

    assert len(recorder.calls) == 1, "提交只发生一次：写请求绝不自动重提"
    call = recorder.calls[0]
    assert call["path"] == "/api/generate/seedreamV4"
    body = call["payload"]
    assert body["templateUuid"] == "0b6bad2fd350433ebb5abc7eb91f2ec9"
    params = body["generateParams"]
    assert params["model"] == "doubao-seedream-4-5-251128"
    # 提交的是归一化后的尺寸，不是 1080×1920
    assert (params["width"], params["height"]) == (1440, 2560)
    assert params["imgCount"] == 1
    assert params["sequentialImageGeneration"] == "disabled"
    # 参考图按「锚点 -> 首帧」去重后的顺序原样提交，不重排、不切片
    assert params["referenceImages"] == [
        "https://drama.example.com/api/v1/drama/assets/asset-anchor",
        "https://drama.example.com/api/v1/drama/assets/asset-first",
    ]
    # plain 模式：prompt 直接平铺在 generateParams 里，没有节点这一层
    assert "prompt" in params and "雨夜冷蓝调" in params["prompt"]
    assert not any(isinstance(value, dict) and "inputs" in value for value in params.values())

    payload = item["payload"]
    assert payload["channel"] == "official"
    assert payload["size_adjusted"] is True
    assert (payload["requested_width"], payload["requested_height"]) == (1080, 1920)
    assert (payload["width"], payload["height"]) == (1440, 2560)
    assert payload["reference_mode"] == "reference_image"
    assert payload["prompt_language"] == "zh"
    assert payload["prompt_language_note"]
    assert payload["upstream_points"] == 13
    assert payload["account_balance"] == 987
    assert payload["image_url"].startswith("/api/v1/drama/assets/")


def test_workflow_channel_keeps_its_own_request_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认通道不受影响：仍是 node 模式 + comfyui 端点。"""
    provider = _official(
        tmp_path,
        t2i_channel="workflow",
        t2i_template_uuid="template-t2i",
        t2i_prompt_node="node-1",
    )
    recorder = _Recorder()
    _stub_transport(monkeypatch, recorder)
    monkeypatch.setenv("LIBTV_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("LIBTV_SECRET_KEY", "sk-test")

    item = provider.keyframe(_shot(anchor_refs=[], anchor_asset_id=""), owner="local")
    assert recorder.calls[0]["path"] == "/api/generate/comfyui/app"
    params = recorder.calls[0]["payload"]["generateParams"]
    # 工作流通道：prompt 进节点 inputs，尺寸走 imageSize，不带官方平铺字段
    assert params["node-1"]["inputs"]["text"].startswith("雨夜冷蓝调")
    assert params["imageSize"] == {"width": 1080, "height": 1920}
    assert "sequentialImageGeneration" not in params
    assert "channel" not in item["payload"], "workflow 通道不新增 official 字段"


def test_prompt_rewrite_seam_raises_instead_of_being_ignored(tmp_path: Path) -> None:
    """配了改写地址就显式报错：静默忽略会让运维以为改写已经生效。"""
    provider = _official(tmp_path, t2i_prompt_rewrite_url="https://rewrite.example.com")
    with pytest.raises(NotImplementedError, match="reserved seam"):
        provider._t2i_prompt("雨夜冷蓝调", dict(DEFAULT_SETTINGS))


def test_guard_order_is_size_then_references_then_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四道守卫的顺序是行为契约：报错必须指向**第一道**不满足的守卫。

    否则排障会被误导——比如尺寸和参考图都错时报「缺公网基址」，
    运维会去配基址，改完还是出不了图。
    """
    built = _forbid_client(monkeypatch)

    # 尺寸错 + 条数错 + 缺基址：先报尺寸
    strict = _official(tmp_path, t2i_size_policy="strict", public_base_url="")
    with pytest.raises(AppError) as size_error:
        strict._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=_refs(15),
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    assert size_error.value.code == "drama_t2i_size"

    # 尺寸合法 + 条数错 + 缺基址：报条数（条数在基址之前）
    normal = _official(tmp_path / "normal", public_base_url="")
    with pytest.raises(AppError) as count_error:
        normal._text_to_image_official(
            "雨夜冷蓝调",
            owner="local",
            reference_asset_ids=_refs(15),
            settings=dict(DEFAULT_SETTINGS),
            usage={},
        )
    assert count_error.value.code == "drama_config"
    assert "15 张" in count_error.value.message
    assert built == []


# ---------------------------------------------------------------- 5. 失败即退款


async def test_guard_failure_refunds_the_reservation_in_full(tmp_path: Path) -> None:
    """本地守卫失败 = 明确没出网 -> 整笔退还预占、凭据 released、不留待复核。"""
    service = DramaService(tmp_path / "drama.sqlite", admins=frozenset({"local"}), autopump=False)
    service.store.install()
    service.set_provider(_official(tmp_path, t2i_size_policy="strict"))
    state = await service.state("local")
    pid = state["activeProjectId"]
    asset = state["projects"][0]["assets"]["characters"][0]

    with pytest.raises(AppError) as caught:
        await service.action(
            "local", "generateAssetCandidates", ["characters", asset["id"]], pid, "req-guard"
        )
    assert caught.value.code == "drama_t2i_size"

    after = await service.state("local")
    assert after["ledger"]["frozen"] == 0, "失败后不能还挂着冻结"
    assert after["ledger"]["balance"] == SEED_BALANCE, "本地拒绝必须全额退回"
    charge = after["charges"][-1]
    assert charge["state"] == "released"
    assert charge["cost"] == 0
    # 走的是既有的 LOCAL_FAILURE_CODES 退款路径，没有新增资金路径
    assert [x["kind"] for x in after["ledger"]["tx"]][-2:] == ["reserve", "release"]


async def test_official_channel_is_reported_in_the_service_config(tmp_path: Path) -> None:
    service = DramaService(tmp_path / "drama.sqlite", admins=frozenset({"local"}), autopump=False)
    service.store.install()
    assert (await service.config("local"))["channels"] == {}, "mock 没有通道概念"
    service.set_provider(_official(tmp_path))
    assert (await service.config("local"))["channels"] == {
        "image-to-video": "workflow",
        "keyframe": "official",
    }
    service.set_provider(_official(tmp_path, i2v_channel="official"))
    assert (await service.config("local"))["channels"]["image-to-video"] == "official"


# ---------------------------------------------------------------- 6. 上游业务原因（G3）


def test_upstream_business_reason_survives_the_app_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """502 里必须看得到上游的子原因，否则「像素下限是否含等号」这类问题无法从线上判断。"""
    provider = _official(tmp_path)
    monkeypatch.setenv("LIBTV_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("LIBTV_SECRET_KEY", "sk-super-secret")

    recorder = _Recorder()
    monkeypatch.setattr(LibTVMediaProvider, "_post", recorder.post)

    async def poll(self, upstream_task_id: str) -> PollResult:
        raise ProviderError(
            "upstream_rejected",
            "Total pixels must be between 3,686,400 and 16,777,216",
            outcome="rejected",
        )

    monkeypatch.setattr(LibTVMediaProvider, "poll", poll)

    with pytest.raises(AppError) as caught:
        provider.keyframe(_shot(), owner="local")
    assert caught.value.code == "drama_upstream"
    assert "3,686,400" in caught.value.message, "子原因必须带出来"
    assert "upstream_rejected" in caught.value.message
    assert "sk-super-secret" not in caught.value.message, "绝不回显凭据"


# ---------------------------------------------------------------- 7. 配置校验


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "drama_provider": "remote",
        "drama_llm_base_url": "http://127.0.0.1/v1",
        "drama_llm_model": "m",
        "drama_t2i_channel": "official",
        "drama_t2i_official_template_uuid": "0b6bad2fd350433ebb5abc7eb91f2ec9",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_official_channel_does_not_require_the_workflow_template() -> None:
    """官方通道不该被工作流的模板/节点卡住启动（两者是两套契约）。"""
    settings = _settings()
    assert settings.drama_t2i_channel == "official"
    assert settings.drama_t2i_official_max_refs == 14
    assert settings.drama_t2i_size_policy == "normalize"
    # 反证：切回 workflow 且没有模板，启动期就必须失败
    with pytest.raises(ValueError, match="templateUuid"):
        _settings(drama_t2i_channel="workflow")


def test_official_channel_requires_a_template_and_bounded_refs() -> None:
    with pytest.raises(ValueError, match="OFFICIAL_TEMPLATE_UUID"):
        _settings(drama_t2i_official_template_uuid="")
    # 上游口径 1–14：0 与 15 都必须在启动期被拒
    with pytest.raises(ValueError):
        _settings(drama_t2i_official_max_refs=0)
    with pytest.raises(ValueError):
        _settings(drama_t2i_official_max_refs=15)
    with pytest.raises(ValueError):
        _settings(drama_t2i_size_policy="auto")


def test_official_t2i_does_not_need_public_base_url_at_startup() -> None:
    """缺公网基址只影响「带参考图的那一步」，不该让整个服务起不来。"""
    settings = _settings(drama_public_base_url="")
    assert settings.drama_public_base_url == ""


# 端到端「真的出一张图」不在本文件范围内：测试**不做任何付费调用**。
# 服务层与 Provider 的契约由 `test_guard_failure_refunds_the_reservation_in_full`
# （失败即全额退款）与上面的请求形状用例共同锁定。
