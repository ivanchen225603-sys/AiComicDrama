"""用户自带 Liblib 密钥后的真实模式：首帧来源、上游可拉取的地址、成片裁剪、不扣平台积分、
不混入示例素材、未配置配音时明确告知。全部离线运行，不出网。"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama import compose
from comicdrama.errors import AppError
from comicdrama.media_store import DramaMediaStore
from comicdrama.providers.hybrid import HybridDramaProvider
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

AK, SK = "ak-demo-0123456789", "sk-demo-abcdefghijk"


class RemoteNamedMock(MockDramaProvider):
    """离线出图 / 出视频，但以真实模式的名字出现（模拟「已接 Liblib」）。"""

    name = "remote"


@pytest.fixture
def service(tmp_path: Path):
    svc = DramaService(
        tmp_path / "drama.sqlite", MockDramaProvider(), admins=frozenset({"local"}), autopump=False
    )
    svc.store.install()
    return svc


async def act(svc: DramaService, name: str, *args: Any):
    return await svc.action("local", name, list(args), None, None)


def proj(state: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


def use_byok(svc: DramaService, monkeypatch: pytest.MonkeyPatch) -> MockDramaProvider:
    svc.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK})
    fake = RemoteNamedMock()
    fake.attach(svc.media_store)
    monkeypatch.setattr(svc, "provider_for", lambda owner: fake)
    return fake


# ---------------- 上游能拉到的公网地址 ----------------


def test_public_token_resolves_only_the_signed_file(tmp_path: Path):
    store = DramaMediaStore(tmp_path / "media")
    saved = store.store("alice", "image", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    asset_id = saved["url"].rsplit("/", 1)[-1]
    token = store.public_token("alice", asset_id)
    assert store.resolve_public(token).read_bytes().startswith(b"\x89PNG")
    body, sig = token.split(".")
    with pytest.raises(AppError):
        store.resolve_public(body + "." + "0" * len(sig))  # 篡改签名
    forged = base64.urlsafe_b64encode(f"bob|{asset_id}|9999999999".encode()).decode().rstrip("=")
    with pytest.raises(AppError):
        store.resolve_public(forged + "." + sig)  # 换账号
    expired = store.public_token("alice", asset_id, ttl_seconds=-1)
    with pytest.raises(AppError):
        store.resolve_public(expired)


async def test_public_route_serves_without_login(tmp_path: Path):
    import httpx

    from comicdrama.app import create_app
    from comicdrama.config import Settings

    app = create_app(Settings(data_dir=tmp_path / "api", drama_autopump=False))
    async with app.router.lifespan_context(app):
        svc = app.state.drama
        saved = svc.media_store.store("local", "image", b"\x89PNG\r\n\x1a\n" + b"1" * 64)
        token = svc.media_store.public_token("local", saved["url"].rsplit("/", 1)[-1])
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8001") as s:
            ok = await s.get(f"/api/v1/drama/public/{token}")
            assert ok.status_code == 200 and ok.content.startswith(b"\x89PNG")
            bad = await s.get("/api/v1/drama/public/not-a-token")
            assert bad.status_code == 404


# ---------------- 首帧来源 ----------------


@pytest.mark.needs_ffmpeg
async def test_first_frame_prefers_the_shots_own_keyframe(service: DramaService):
    state = await service.state("local")
    shot = proj(state)["shots"][1]  # 引用了 @角色:林砚
    anchor = proj(state)["assets"]["characters"][0]
    await act(service, "generateAssetCandidates", "characters", anchor["id"])
    await act(service, "submitTasks", [shot["id"]])
    for _ in range(4):
        await act(service, "tick")
    state = await service.state("local")
    shot = next(s for s in proj(state)["shots"] if s["id"] == shot["id"])
    assert shot["imageUrl"]
    item = (await act(service, "image-to-video", shot["id"], ""))["result"]["payload"]
    assert item["first_frame_asset_id"] == shot["imageUrl"].rsplit("/", 1)[-1]
    assert item["first_frame_bytes"] > 0


@pytest.mark.needs_ffmpeg
async def test_shot_without_images_still_gets_a_video_offline(service: DramaService):
    state = await service.state("local")
    shot = proj(state)["shots"][0]
    item = (await act(service, "image-to-video", shot["id"], ""))["result"]["payload"]
    assert item["video_url"]


# ---------------- 成片按分镜时长裁剪 ----------------


def test_compose_trims_each_clip_to_its_shot_duration(tmp_path: Path, monkeypatch):
    store = DramaMediaStore(tmp_path / "media")
    urls = [
        store.store("local", "video", b"\x00\x00\x00\x18ftypmp42" + bytes([i]) * 64)["url"]
        for i in range(3)
    ]
    project = {
        "shots": [
            {"id": f"S{i}", "dur": d, "videoUrl": u}
            for i, (d, u) in enumerate(zip([2, 3, 4], urls, strict=True))
        ],
        "timeline": {"order": ["S0", "S1", "S2"]},
        "audio": {},
    }
    plan = compose.build_plan(project, store, "local")
    assert plan.clip_seconds == [2.0, 3.0, 4.0]
    seen: dict[str, str] = {}

    def fake_run(args):
        if "concat" in args:
            listing = Path(args[args.index("-i") + 1])
            seen["listing"] = listing.read_text(encoding="utf-8")
        raise AppError("stop", "stop", 500)

    monkeypatch.setattr(compose, "_run", fake_run)
    monkeypatch.setattr(compose, "_binary", lambda value: "ffmpeg")
    with pytest.raises(AppError):
        compose.render_film(plan, ffmpeg="ffmpeg")
    assert seen["listing"].count("outpoint") == 3
    assert "outpoint 2.000" in seen["listing"] and "outpoint 4.000" in seen["listing"]


# ---------------- 自带密钥：不扣平台积分、不受平台每日上限 ----------------


@pytest.mark.needs_ffmpeg
async def test_byok_generation_costs_no_platform_credits(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    use_byok(service, monkeypatch)
    service.daily_cap = 100  # 平台每日上限再低也不影响用户自己的 Liblib 账户
    before = (await service.state("local"))["ledger"]
    shots = proj(await service.state("local"))["shots"]
    for shot in shots[:4]:
        await act(service, "image-to-video", shot["id"], "")
    await act(service, "script-doctor")
    await act(service, "submitTasks", [s["id"] for s in shots[:2]])
    for _ in range(6):
        await act(service, "tick")
    state = await service.state("local")
    assert state["ledger"]["balance"] == before["balance"]
    assert state["ledger"]["frozen"] == 0
    settled = [c for c in state["charges"] if c["state"] == "settled"]
    assert settled and all(c["cost"] == 0 for c in settled), "仍留扣费凭据（0 积分）供追踪"
    cfg = await service.config("local")
    assert cfg["byok"] is True and cfg["keyframe_price"] == 0
    assert all(v == 0 for v in cfg["prices"].values())


async def test_without_byok_prices_are_unchanged(service: DramaService):
    cfg = await service.config("local")
    assert cfg["byok"] is False and cfg["prices"]["image-to-video"] > 0


# ---------------- 切到真实模式：不混入示例素材 ----------------


@pytest.mark.needs_ffmpeg
async def test_drop_sample_media_clears_samples_but_keeps_uploads(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    state = await service.state("local")
    shot = proj(state)["shots"][0]
    await act(service, "submitTasks", [shot["id"]])
    for _ in range(4):
        await act(service, "tick")
    await act(service, "image-to-video", shot["id"], "")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    up = await service.upload(
        "local", "reference", "林砚.png", png, asset_type="characters", asset_name="林砚"
    )
    upload_url = up["uploads"][0]["url"]
    state = await service.state("local")
    s0 = next(s for s in proj(state)["shots"] if s["id"] == shot["id"])
    assert s0["imageUrl"] and s0["videoUrl"]

    result = (await act(service, "dropSampleMedia"))["result"]
    assert result["dropped"] >= 2
    state = await service.state("local")
    s0 = next(s for s in proj(state)["shots"] if s["id"] == shot["id"])
    assert s0["imageUrl"] == "" and s0["videoUrl"] == ""
    lin = next(a for a in proj(state)["assets"]["characters"] if a["name"] == "林砚")
    assert lin["imageUrl"] == upload_url, "用户上传的参考图不能被当成示例删掉"

    # 示例模式留下的关键帧任务不挡真实模式重新生成
    use_byok(service, monkeypatch)
    assert (await act(service, "submitTasks", [shot["id"]]))["result"] == 1


@pytest.mark.needs_ffmpeg
async def test_first_save_of_liblib_keys_drops_samples_over_http(tmp_path: Path):
    import httpx

    from comicdrama.app import create_app
    from comicdrama.config import Settings

    app = create_app(Settings(data_dir=tmp_path / "api", drama_autopump=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8001") as s:
            st = (await s.get("/api/v1/drama/state")).json()
            sid = proj(st)["shots"][0]["id"]
            await s.post("/api/v1/drama/action", json={"type": "image-to-video", "args": [sid]})
            r = await s.put(
                "/api/v1/drama/credentials",
                json={"liblib_access_key": AK, "liblib_secret_key": SK},
            )
            assert r.json()["droppedSamples"] >= 1
            st = (await s.get("/api/v1/drama/state")).json()
            assert proj(st)["shots"][0]["videoUrl"] == ""
            again = await s.put("/api/v1/drama/credentials", json={"liblib_access_key": AK + "x"})
            assert again.json()["droppedSamples"] == 0, "只在首次切换时清理"


# ---------------- 未配置配音服务 ----------------


def test_hybrid_without_tts_says_so_instead_of_faking_voice():
    hybrid = HybridDramaProvider(object(), MockDramaProvider())
    with pytest.raises(AppError) as caught:
        hybrid.tts_preview("林砚", "声线 A", "你好", owner="local")
    assert caught.value.code == "drama_tts_config"


async def test_tts_error_in_real_mode_refunds_and_leaves_no_freeze(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    service.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK})
    hybrid = HybridDramaProvider(object(), MockDramaProvider())
    monkeypatch.setattr(service, "provider_for", lambda owner: hybrid)
    voice = proj(await service.state("local"))["audio"]["voices"][0]["id"]
    with pytest.raises(AppError) as caught:
        await act(service, "tts-preview", voice)
    assert caught.value.code == "drama_tts_config"
    state = await service.state("local")
    assert state["ledger"]["frozen"] == 0
    assert not [c for c in state["charges"] if c["state"] in ("in_flight", "pending_review")]
    assert time.time() > 0
