"""离线演示扩展功能的验收用例（全程离线、零出网、零真实凭据、零上游费用）。

覆盖：
- 第四项：上传格式 / 大小 / 数量校验、owner 隔离、不扣积分、参考图绑定资产并用作首帧、
  BGM 进入合成计划、上传元数据纳入导入导出、HTTP raw-body 端点与跨用户访问。
- 第三项：配音按字幕入点对齐、过长裁断、过短静音补齐、无配音静音、
  超出总时长跳过、成片时长锁定。
- 第五项：确定性示例素材、缺少本机工具时明确报错、全新数据目录按演示脚本
  从剧本走到导出，成片可播放 / 可下载、轨道与时长、四处示例标识一致。

事实分级：本文件全部是「已实测」的本机行为；真实 Provider 关键帧属于
「代码完成·未验证」，由 test_remote_provider.py 与首单冒烟清单另行跟踪。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from comicdrama import sample_media
from comicdrama.compose import VoiceCue, mux_args
from comicdrama.errors import AppError
from comicdrama.media_store import DramaMediaStore
from comicdrama.pricing import DEFAULT_DAILY_CAP, PRICE_VIDEO
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.sample_media import (
    SAMPLE_HINT,
    SAMPLE_LABEL,
    sample_audio_bytes,
    sample_image_bytes,
    sample_key,
)
from comicdrama.service import UPLOAD_LIMITS, DramaService

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="本机 FFmpeg 是离线示例素材的硬前置")
needs_probe = pytest.mark.skipif(FFPROBE is None, reason="需要 ffprobe 核验成片轨道与时长")

# 有效容器头（media_store 按魔数判定格式，不信文件名后缀）。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28


@pytest.fixture
async def service(tmp_path: Path):
    # 不放开单日上限：日上限只对真实 Provider 计数（用户裁决），离线 mock 既不
    # 占额也不被额拦，所以整条演示（7 镜 × 300 = 2100 名义消费）在默认 500 下就能
    # 跑完——这正是那条裁决的证据。余额另说：余额闸门没改，种子 980 确实不够
    # 7 × 300，用正常业务入口（管理员充值）补足，不是绕过。
    svc = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    svc.store.install()
    # 演示要跑完整条链路：7 镜图生视频 = 7 × 300 = 2100，种子 980 必然不够，
    # 先用管理员充值把余额补足（充值是正常业务入口，不走后门改库）。
    await svc.action(
        "local", "adminCredit", [20000, "离线演示夹具", "seed-offline-demo"], None, None
    )
    # autopump=False：没有后台任务，不需要在另一个事件循环里 close。
    return svc


async def act(service: DramaService, name: str, *args: Any, project_id: str | None = None):
    return await service.action("local", name, list(args), project_id, None)


def project_of(state: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线演示期间任何 Python 侧出网都直接让测试失败。

    FFmpeg 是本机子进程、输入全是本地文件，不走这些入口；因此这里挡住的是
    「服务层偷偷去调上游」这条路，而不是本地合成。
    """

    def refused(*_a: Any, **_k: Any):
        raise AssertionError("离线演示期间禁止任何出网请求")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", refused)
    monkeypatch.setattr(urllib.request, "urlopen", refused)


# ---------------------------------------------------------------- 第四项：上传
async def test_upload_validation_isolation_and_no_charge(service: DramaService):
    """非法 kind / 空文件 / 伪格式被拒；正常上传不扣积分、按 owner 隔离。"""
    before = await service.state("local")
    ledger_before = json.loads(json.dumps(before["ledger"]))

    with pytest.raises(AppError) as caught:
        await service.upload("local", "video", "clip.mp4", PNG)
    assert caught.value.code == "upload_kind"

    with pytest.raises(AppError) as caught:
        await service.upload("local", "bgm", "empty.wav", b"")
    assert caught.value.code == "upload_empty"

    # 文件名叫 .png、Content-Type 也无所谓：魔数不对就拒绝。
    with pytest.raises(AppError) as caught:
        await service.upload("local", "reference", "fake.png", b"not-an-image" * 8)
    assert caught.value.code == "upload_format"

    state = await service.upload(
        "local", "reference", "林砚.png", PNG, asset_type="characters", asset_name="林砚"
    )
    assert state["ledger"] == ledger_before, "上传本身不扣积分，也不动冻结"
    upload = state["uploads"][0]
    assert upload["url"].startswith("/api/v1/drama/assets/")
    assert upload["assetId"], "带资产类型与名字时要绑定 / 新建资产"
    saved = service.media_store.path_for_url("local", upload["url"])
    assert saved is not None and saved.is_file()

    other = await service.state("bob")
    assert other["uploads"] == [], "上传记录必须按 owner 隔离"
    with pytest.raises(AppError) as caught:
        await service.delete_upload("bob", upload["id"])
    assert caught.value.code == "upload_missing"


async def test_upload_size_and_count_limits_are_enforced(service: DramaService):
    """超限与数量上限都有明确错误码，不是静默丢弃。"""
    too_big = b"RIFF" + b"\x00" * UPLOAD_LIMITS["bgm"]["max_bytes"]
    with pytest.raises(AppError) as caught:
        await service.upload("local", "bgm", "huge.wav", too_big)
    assert caught.value.code == "upload_too_large"

    max_count = UPLOAD_LIMITS["bgm"]["max_count"]
    for index in range(max_count):
        await service.upload("local", "bgm", f"track-{index}.wav", WAV)
    with pytest.raises(AppError) as caught:
        await service.upload("local", "bgm", "one-too-many.wav", WAV)
    assert caught.value.code == "upload_limit"
    assert len((await service.state("local"))["uploads"]) == max_count


async def test_upload_metadata_survives_export_import_and_rejects_external_url(
    service: DramaService,
):
    """上传元数据纳入导入导出；伪造外链 URL 不能通过 import_doc。"""
    state = await service.upload("local", "bgm", "雨声.wav", WAV)
    upload = state["uploads"][0]
    doc = await service.export_doc("local")
    assert len(doc["uploads"]) == 1
    assert doc["uploads"][0]["url"] == upload["url"]
    restored = await service.import_doc("local", json.loads(json.dumps(doc)))
    assert restored["uploads"][0]["url"] == upload["url"]

    forged = json.loads(json.dumps(doc))
    forged["uploads"][0]["url"] = "https://evil.example/rain.wav"
    with pytest.raises(AppError) as caught:
        await service.import_doc("local", forged)
    assert caught.value.code == "invalid_import"
    assert "uploads" in caught.value.message


async def test_deleting_upload_clears_bgm_and_asset_references(service: DramaService):
    """删除上传后，BGM 配置与资产锚点不能继续指向已被删除的文件。"""
    state = await service.upload(
        "local", "reference", "林砚.png", PNG, asset_type="characters", asset_name="林砚"
    )
    ref = state["uploads"][0]
    state = await service.upload("local", "bgm", "雨声.wav", WAV)
    bgm = next(x for x in state["uploads"] if x["kind"] == "bgm")
    pid = state["activeProjectId"]
    await act(service, "saveBgm", {"uploadId": bgm["id"]}, project_id=pid)

    state = await service.delete_upload("local", bgm["id"])
    assert project_of(state)["audio"]["bgm"]["url"] == ""
    state = await service.delete_upload("local", ref["id"])
    asset = next(a for a in project_of(state)["assets"]["characters"] if a["id"] == ref["assetId"])
    assert asset["imageUrl"] == ""


@needs_ffmpeg
async def test_uploaded_reference_is_used_as_first_frame(service: DramaService):
    """上传 → 绑定资产 → @引用 解析 → 首帧字节真的进了图生视频请求。"""
    png = sample_image_bytes("ref-upload", width=64, height=64, caption="林砚参考图")
    state = await service.upload(
        "local", "reference", "林砚.png", png, asset_type="characters", asset_name="林砚"
    )
    upload = state["uploads"][0]
    proj = project_of(state)
    asset = next(a for a in proj["assets"]["characters"] if a["id"] == upload["assetId"])
    assert asset["imageUrl"] == upload["url"]
    shot = next(s for s in proj["shots"] if "@角色:林砚" in s["refs"])
    video = await act(service, "image-to-video", shot["id"], "", project_id=proj["id"])
    payload = video["result"]["payload"]
    # 交给上游的是素材文件编号（拼公网地址用），不是资产编号
    assert payload["first_frame_asset_id"] == upload["url"].rsplit("/", 1)[-1]
    assert payload["first_frame_bytes"] == len(png)


@needs_ffmpeg
async def test_uploaded_bgm_enters_the_mix_plan(service: DramaService):
    """BGM 上传 → saveBgm({uploadId}) → build_plan 拿到本地文件参与 amix。"""
    audio = sample_audio_bytes("bgm-upload", seconds=2.0)
    state = await service.upload("local", "bgm", "雨声.wav", audio)
    upload = next(x for x in state["uploads"] if x["kind"] == "bgm")
    pid = state["activeProjectId"]
    await act(
        service,
        "saveBgm",
        {"uploadId": upload["id"], "voiceLevel": 100, "bgmLevel": 20, "ambient": "雨"},
        project_id=pid,
    )
    state = await service.state("local")
    proj = project_of(state)
    assert proj["audio"]["bgm"]["url"] == upload["url"]
    assert proj["audio"]["bgm"]["uploadId"] == upload["id"]
    from comicdrama.compose import build_plan

    plan = build_plan(proj, service.media_store, "local")
    assert plan.bgm is not None and plan.bgm.is_file()
    assert plan.levels == {"voice": 100, "bgm": 20}

    # 请求体里塞外链一律拒绝，只认本账号上传入口登记的本地音频。
    with pytest.raises(AppError) as caught:
        await act(service, "saveBgm", {"url": "https://evil.example/rain.wav"}, project_id=pid)
    assert caught.value.code == "bgm_url"


# ---------------------------------------------------------------- 第三项：逐句对齐
def test_mux_args_align_each_voice_to_its_subtitle_and_lock_duration(tmp_path: Path):
    """每句配音起点 = 字幕 in；过长 atrim 裁断；-t 锁总时长且不用 -shortest。"""
    voices = [
        VoiceCue(tmp_path / "a.wav", 2000, 3000, "第一句"),
        VoiceCue(tmp_path / "b.wav", 8000, 1500, "第二句"),
    ]
    subtitles = [
        {"in": "00:02", "out": "00:05", "text": "第一句"},
        {"in": "00:08", "out": "00:09.500", "text": "第二句"},
        {"in": "00:10", "out": "00:12", "text": "没有配音的一句"},
    ]
    args = mux_args(
        FFMPEG or "ffmpeg",
        tmp_path / "base.mp4",
        tmp_path / "out.mp4",
        voices=voices,
        subtitles=subtitles,
        srt=tmp_path / "subs.srt",
        total_seconds=12.0,
        sample=True,
    )
    joined = " ".join(str(x) for x in args)
    assert "adelay=2000:all=1" in joined
    assert "atrim=end=3.000" in joined
    assert "adelay=8000:all=1" in joined
    assert "atrim=end=1.500" in joined
    assert "amix=inputs=3:duration=first" in joined, "静音底轨 + 两句配音"
    assert "anullsrc=r=44100:cl=stereo" in joined, "无配音句子由静音底轨补齐"
    assert "-shortest" not in args, "字幕轨早结束会把成片截短"
    assert args[-3:] == ["-t", "12.000", str(tmp_path / "out.mp4")]
    assert f"comment={SAMPLE_HINT}" in args
    assert f"description={SAMPLE_LABEL}" in args


def test_build_plan_skips_voices_past_the_end_and_keeps_unvoiced_silent(
    tmp_path: Path,
):
    """超出总时长的句子不进混音并列入 skipped；没有配音的句子不阻断合成。"""
    from comicdrama.compose import build_plan

    store = DramaMediaStore(tmp_path / "media")
    audio = sample_audio_bytes("voice-cue", seconds=1.0) if FFMPEG else WAV
    clip = store.store("local", "audio", audio)["url"]
    project = {
        "shots": [{"id": "S1", "dur": 6}],
        "timeline": {"order": ["S1"]},
        "audio": {
            "subtitles": [
                {"in": "00:01", "out": "00:03", "text": "有配音"},
                {"in": "00:04", "out": "00:05", "text": "没配音"},
                {"in": "00:09", "out": "00:11", "text": "超出成片"},
            ],
            "voices": [],
            "bgm": {},
        },
    }
    # 只给第一句与第三句挂音频：第二句保持静音。
    project["audio"]["subtitles"][0]["audio_url"] = clip
    project["audio"]["subtitles"][2]["audio_url"] = clip
    plan = build_plan(project, store, "local")
    assert len(plan.voices) == 1
    assert plan.voices[0].start_ms == 1000
    assert plan.voices[0].limit_ms == 2000
    assert plan.skipped and "00:09" in plan.skipped[0]
    assert plan.total_seconds == 6.0


# ---------------------------------------------------------------- 第五项：示例素材
def test_sample_media_is_deterministic_and_flags_missing_tool(monkeypatch, tmp_path: Path):
    """同样输入得到同样 key；缺少合成工具时明确报错并给安装提示。"""
    assert sample_key("a", 1, None) == sample_key("a", 1, None)
    assert sample_key("a", 1, None) != sample_key("a", 2, None)

    monkeypatch.setattr(sample_media.shutil, "which", lambda _name: None)
    with pytest.raises(AppError) as caught:
        sample_media.sample_image_bytes("k", width=64, height=64, caption="测试")
    assert caught.value.code == "drama_ffmpeg"
    assert "FFmpeg" in caught.value.message and "中文字体" in caught.value.message


@needs_ffmpeg
def test_sample_image_bytes_are_byte_identical_for_the_same_input():
    first = sample_image_bytes("same", width=96, height=54, caption="示例，非模型生成")
    second = sample_image_bytes("same", width=96, height=54, caption="示例，非模型生成")
    assert first == second and first[:8] == PNG[:8]
    assert sample_image_bytes("other", width=96, height=54, caption="示例，非模型生成") != first


# ---------------------------------------------------------------- 第五项：完整演示脚本
@needs_ffmpeg
@needs_probe
async def test_offline_demo_runs_from_script_to_a_playable_export(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    """全新数据目录 + 离线 Provider：剧本 → 关键帧 → 视频轨 → 逐句配音 →
    上传 BGM/参考图 → 合成 → 导出，得到可播放、可下载的示例成片。

    只证明流程与本机合成链路可用，不代表模型效果；全程不出网、不读密钥。
    """
    block_network(monkeypatch)
    state = await service.state("local")
    pid = state["activeProjectId"]

    # --- 剧本到分镜（需求文档 §7 的演示脚本顺序） ---
    await act(service, "creative-start", "idea", "一封迟到的信", project_id=pid)
    await act(service, "script-doctor", project_id=pid)
    await act(service, "extract-assets", project_id=pid)
    await act(service, "resplit-shots-v2", project_id=pid)
    state = await service.state("local")
    shot_ids = [s["id"] for s in project_of(state)["shots"]]
    assert shot_ids

    # --- 关键帧任务：Provider 驱动，写回镜头真图 + 账本预占/结算 ---
    await act(service, "submitTasks", shot_ids, project_id=pid)
    for _ in range(len(shot_ids) + 2):
        if (await act(service, "tick", project_id=pid))["result"] is not True:
            break
    state = await service.state("local")
    assert all(t["state"] == "succeeded" for t in state["tasks"])
    assert state["ledger"]["frozen"] == 0
    assert all(s.get("imageUrl") for s in project_of(state)["shots"])
    assert "settle" in [x["kind"] for x in state["ledger"]["tx"]]

    # --- 上传：参考图绑定角色锚点 + BGM 进入混音 ---
    ref_png = sample_image_bytes("demo-ref", width=96, height=54, caption="林砚参考图")
    state = await service.upload(
        "local",
        "reference",
        "林砚.png",
        ref_png,
        asset_type="characters",
        asset_name="林砚",
    )
    ref = state["uploads"][0]
    bgm_wav = sample_audio_bytes("demo-bgm", seconds=2.0)
    state = await service.upload("local", "bgm", "雨声.wav", bgm_wav)
    bgm = next(x for x in state["uploads"] if x["kind"] == "bgm")
    await act(
        service,
        "saveBgm",
        {"uploadId": bgm["id"], "voiceLevel": 100, "bgmLevel": 20, "ambient": "雨"},
        project_id=pid,
    )

    # --- 逐句配音：显式绑定字幕句（按新增 id 取，避免和种子字幕文本撞车） ---
    before_subs = {s["id"] for s in project_of(await service.state("local"))["audio"]["subtitles"]}
    await act(
        service,
        "addSubtitle",
        {"in": "00:02", "out": "00:05", "text": "你收到信了。"},
        project_id=pid,
    )
    await act(
        service,
        "addSubtitle",
        {"in": "00:08", "out": "00:11", "text": "有些话成了告别。"},
        project_id=pid,
    )
    state = await service.state("local")
    proj = project_of(state)
    voice_id = proj["audio"]["voices"][0]["id"]
    targets = [s for s in proj["audio"]["subtitles"] if s["id"] not in before_subs]
    assert len(targets) == 2
    for sub in targets:
        item = await act(service, "tts-preview", voice_id, sub["id"], project_id=pid)
        assert item["result"]["payload"]["subtitle_id"] == sub["id"]
        assert item["result"]["payload"]["alignment"] == "subtitle"

    # --- 逐镜图生视频：第一镜显式选上传参考图，其余交给 @引用 自动解析 ---
    first_frame_hits = 0
    ref_media = ref["url"].rsplit("/", 1)[-1]
    for index, sid in enumerate(shot_ids):
        chosen = ref["assetId"] if index == 0 else ""
        item = await act(service, "image-to-video", sid, chosen, project_id=pid)
        payload = item["result"]["payload"]
        assert payload["video_url"].startswith("/api/v1/drama/assets/")
        if payload["first_frame_asset_id"] == ref_media:
            first_frame_hits += 1
            assert payload["first_frame_bytes"] == len(ref_png)
    assert first_frame_hits >= 1, "上传参考图必须能作为首帧（含 @引用 自动解析）"

    # 每镜 300 × len(shot_ids) 的名义消费远超默认日上限，而「今日已用」必须是 0：
    # 日上限只统计真实上游消费（用户裁决），离线 mock 不占额。夹具没有传
    # `daily_cap`，所以这里跑的就是默认 500——这一条就是那条裁决的证据。
    cfg = await service.config("local")
    assert cfg["daily_cap"] == DEFAULT_DAILY_CAP
    assert cfg["daily_used"] == 0
    assert len(shot_ids) * PRICE_VIDEO > DEFAULT_DAILY_CAP, "名义消费必须真的超过默认上限"

    # --- 合成 ---
    preview = await act(service, "preview-film", project_id=pid)
    result = preview["result"]
    payload = result["payload"]
    assert result["payload"]["sample"] is True
    assert result["payload"]["sample_label"] == SAMPLE_LABEL
    assert result["note"] == SAMPLE_HINT
    assert payload["muxed"]["bgm"] is True, "上传的 BGM 要进混音"
    assert payload["muxed"]["subtitles"] >= 2
    assert payload["alignment"] == "subtitle"
    starts = {c["start_ms"] for c in payload["voice_alignment"]}
    assert {2000, 8000} <= starts, "每句配音起始时间必须与字幕 in 一致"

    # --- 导出：记录指向同一个真实文件 ---
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)
    record = (await act(service, "exportFilm", project_id=pid))["result"]
    assert record["filmUrl"] == payload["video_url"]
    assert record["sampleLabel"] == SAMPLE_LABEL
    assert "示例成片" in record["artifact"]
    film = service.media_store.path_for_url("local", record["filmUrl"])
    assert film is not None and film.is_file() and film.stat().st_size > 0

    # --- ffprobe：轨道、时长、容器元数据里的示例标识 ---
    # ffprobe 是本机子进程，丢到线程池避免阻塞事件循环（ruff ASYNC221）。
    probe = await asyncio.to_thread(
        lambda: subprocess.run(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(film),
            ],
            capture_output=True,
            # 显式指定编码：ffprobe 输出 UTF-8 JSON，容器 metadata 里含中文标识。
            # 只写 text=True 会按本机 locale（中文 Windows 为 cp936）解码，
            # 换一台机器就 UnicodeDecodeError，属于测试自身的环境依赖。
            text=True,
            encoding="utf-8",
            check=True,
        )
    )
    info = json.loads(probe.stdout)
    codecs = {s["codec_name"] for s in info["streams"]}
    assert {"h264", "aac", "mov_text"} <= codecs
    total = float(sum(int(s["dur"]) for s in project_of(await service.state("local"))["shots"]))
    duration = float(info["format"]["duration"])
    assert abs(duration - total) < 0.15, f"成片 {duration}s 应等于镜头总时长 {total}s"
    assert SAMPLE_LABEL in info["format"]["tags"].get("comment", "")
    assert info["format"]["tags"].get("description") == SAMPLE_LABEL
    assert (await service.config("local"))["sample_label"] == SAMPLE_LABEL
    assert service.provider.name == "mock", "离线演示绝不能挂真实 Provider"


# ---------------------------------------------------------------- 回归修复


async def _compose_all(service: DramaService, pid: str) -> dict[str, Any]:
    state = await service.state("local")
    for shot in project_of(state)["shots"]:
        if not shot.get("videoUrl"):
            await act(service, "image-to-video", shot["id"], project_id=pid)
    return (await act(service, "preview-film", project_id=pid))["state"]


async def test_resplit_refuses_to_silently_discard_generated_media(service: DramaService):
    """未锁定镜头已有生成素材时，换一版分镜必须显式确认；拒绝时不扣费。"""
    state = await service.state("local")
    pid = state["activeProjectId"]
    draft = next(s for s in project_of(state)["shots"] if s["state"] != "locked")
    await act(service, "image-to-video", draft["id"], project_id=pid)
    before = (await service.state("local"))["ledger"]

    with pytest.raises(AppError) as caught:
        await act(service, "resplit-shots-v2", project_id=pid)
    assert caught.value.code == "resplit_discards_media"
    assert caught.value.status == 409
    state = await service.state("local")
    assert state["ledger"]["balance"] == before["balance"], "拒绝时预占必须原样退回"
    assert state["ledger"]["frozen"] == 0
    kept = next(s for s in project_of(state)["shots"] if s["id"] == draft["id"])
    assert kept.get("videoUrl"), "未确认前已生成的视频必须还在"

    state = (await act(service, "resplit-shots-v2", {"discardGenerated": True}, project_id=pid))[
        "state"
    ]
    shots = project_of(state)["shots"]
    assert draft["id"] not in [s["id"] for s in shots], "带素材的旧 id 不能被新镜头复用"
    assert all(s["state"] == "locked" or not s.get("videoUrl") for s in shots)


async def test_resplit_without_generated_media_needs_no_confirmation(service: DramaService):
    state = await service.state("local")
    pid = state["activeProjectId"]
    assert not any(
        s.get("videoUrl") or s.get("imageUrl")
        for s in project_of(state)["shots"]
        if s["state"] != "locked"
    )
    result = await act(service, "resplit-shots-v2", project_id=pid)
    assert result["result"]["payload"]["shots"]


async def test_new_shot_after_resplit_can_still_get_a_keyframe(service: DramaService):
    """旧草稿带着关键帧任务时，新镜头不能继承它的 id，否则再也提交不了关键帧。"""
    state = await service.state("local")
    pid = state["activeProjectId"]
    draft = next(s for s in project_of(state)["shots"] if s["state"] != "locked")
    await act(service, "toggleShotLock", draft["id"], project_id=pid)
    await act(service, "submitTasks", [draft["id"]], project_id=pid)
    for _ in range(4):
        await act(service, "tick", project_id=pid)
    await act(service, "toggleShotLock", draft["id"], project_id=pid)

    state = (await act(service, "resplit-shots-v2", {"discardGenerated": True}, project_id=pid))[
        "state"
    ]
    fresh = next(s for s in project_of(state)["shots"] if s["state"] != "locked")
    assert fresh["id"] != draft["id"]
    await act(service, "toggleShotLock", fresh["id"], project_id=pid)
    assert (await act(service, "submitTasks", [fresh["id"]], project_id=pid))["result"] == 1


async def test_export_is_refused_once_the_film_no_longer_matches_the_project(
    service: DramaService,
):
    """合成之后内容变了，成片即过期：状态里可见，导出被拒；重新合成后恢复。"""
    state = await service.state("local")
    pid = state["activeProjectId"]
    state = await _compose_all(service, pid)
    film = project_of(state)["film"]
    assert film["signature"] and film["stale"] is False
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)

    # 改一个镜头的时长：成片总时长不再对应。
    shot = project_of(state)["shots"][0]
    state = (
        await act(service, "updateShot", shot["id"], {"dur": int(shot["dur"]) + 1}, project_id=pid)
    )["state"]
    assert project_of(state)["film"]["stale"] is True
    with pytest.raises(AppError) as caught:
        await act(service, "exportFilm", project_id=pid)
    assert caught.value.code == "film_stale"
    assert project_of(await service.state("local"))["exports"] == []

    state = (await act(service, "preview-film", project_id=pid))["state"]
    assert project_of(state)["film"]["stale"] is False
    for index, done in enumerate(project_of(state)["qa"]):
        if not done:
            await act(service, "toggleQA", index, project_id=pid)
    record = (await act(service, "exportFilm", project_id=pid))["result"]
    assert record["filmUrl"] == project_of(state)["film"]["videoUrl"]
    assert record["duration"] == sum(int(s["dur"]) for s in project_of(state)["shots"])


async def test_film_goes_stale_when_the_storyboard_is_replaced(service: DramaService):
    """原始复现：8 镜成片 → 换一版分镜 → 导出不能再指向旧文件。"""
    # 这条用例要把 8 个镜头出两遍（旧分镜 + 新分镜）：16 × 300 = 4800 > 默认预算
    # 3000。预算闸门本身另有专门用例，这里只放开额度以验证「成片过期」语义。
    await act(service, "saveSettings", {"budget": 100000})
    state = await service.state("local")
    pid = state["activeProjectId"]
    await _compose_all(service, pid)
    state = (await act(service, "resplit-shots-v2", {"discardGenerated": True}, project_id=pid))[
        "state"
    ]
    assert project_of(state)["film"]["stale"] is True
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)
    # 新镜头还没有视频轨：先报「不完整」。
    with pytest.raises(AppError) as caught:
        await act(service, "exportFilm", project_id=pid)
    assert caught.value.code == "film_incomplete"
    # 补齐视频轨但不重新合成：旧成片仍然过期，照样不能导出。
    for shot in project_of(state)["shots"]:
        if not shot.get("videoUrl"):
            await act(service, "image-to-video", shot["id"], project_id=pid)
    with pytest.raises(AppError) as caught:
        await act(service, "exportFilm", project_id=pid)
    assert caught.value.code == "film_stale"


async def test_export_is_refused_while_some_shots_have_no_video(service: DramaService):
    """合成会跳过没有视频轨的镜头；这样的成片不完整，不能产出导出记录。"""
    state = await service.state("local")
    pid = state["activeProjectId"]
    shots = project_of(state)["shots"]
    await act(service, "image-to-video", shots[0]["id"], project_id=pid)
    await act(service, "preview-film", project_id=pid)
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)
    with pytest.raises(AppError) as caught:
        await act(service, "exportFilm", project_id=pid)
    assert caught.value.code == "film_incomplete"
    assert f"{len(shots) - 1} / {len(shots)}" in caught.value.message
    assert project_of(await service.state("local"))["exports"] == []


async def test_script_doctor_result_survives_later_generation(service: DramaService):
    """诊断之后再做别的生成动作，诊断结论仍要保留在项目上供剧本页展示。"""
    state = await service.state("local")
    pid = state["activeProjectId"]
    await act(service, "script-doctor", project_id=pid)
    voice = project_of(await service.state("local"))["audio"]["voices"][0]["id"]
    state = (await act(service, "tts-preview", voice, project_id=pid))["state"]
    last = project_of(state)["lastResult"]
    assert last["action"] == "tts-preview"
    kept = last["byAction"]["script-doctor"]
    assert kept["payload"]["findings"], "诊断结论被后续动作覆盖了"
    assert kept["at"]
