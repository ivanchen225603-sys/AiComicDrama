"""漫剧 Provider 抽象层与真实素材管线的离线自洽测试。

不调用任何收费接口：LLM/TTS 用回环 HTTP 桩，任务级媒体的协议层已由
tests/test_libtv_provider_http.py 覆盖，这里用假的 _generate 只验证本层的
转存与 payload 形状。所有断言都针对「代码自洽」，不得写成「上游已验证」。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from comicdrama import mock_media
from comicdrama.errors import AppError
from comicdrama.media_store import DramaMediaStore
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.providers.remote import RemoteDramaConfig, RemoteDramaProvider
from comicdrama.service import DramaService

WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/tts"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.write(WAV)
            return
        if "BAD_SHAPE" in json.dumps(body, ensure_ascii=False):
            content = json.dumps({"unexpected": "envelope"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(content.encode())
            return
        system = body.get("messages", [{}])[0].get("content", "")
        if "剧本医生" in system:
            data = {"findings": ["缺少冲突升级"], "scenes": ["旧地"], "dialogue_count": 2}
        elif "编剧" in system:
            data = {
                "layers": {
                    "logline": "一个雨夜的决定",
                    "outline": "起承转合",
                    "body": "【场景 1 · 旧地 · 夜】\n甲：你来了。",
                }
            }
        elif "抽取实体" in system:
            data = {
                "found": {
                    "characters": [{"name": "甲", "prompt": "冷调角色锚点"}],
                    "scenes": [{"name": "旧地", "prompt": "雨夜场景锚点"}],
                    "props": [],
                }
            }
        elif "分镜" in system:
            data = {"shots": [{"dur": 3, "desc": "对白近景", "prompt": "冷调"}]}
        elif "音色" in system:
            data = {"candidates": ["清冷女中音", "少年感"], "recommended": "少年感"}
        else:
            data = {}
        content = json.dumps({"choices": [{"message": {"content": json.dumps(data)}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(content.encode())

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def remote(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("DRAMA_LLM_TEST_KEY", "test-only")
    monkeypatch.setenv("DRAMA_TTS_TEST_KEY", "test-only")
    config = RemoteDramaConfig(
        llm_base_url=f"{stub}/v1",
        llm_model="stub-model",
        llm_key_env="DRAMA_LLM_TEST_KEY",
        tts_url=f"{stub}/tts",
        tts_key_env="DRAMA_TTS_TEST_KEY",
        t2i_template_uuid="template-t2i",
        t2i_prompt_node="node-1",
        i2v_template_uuid="template-i2v",
        i2v_prompt_node="node-2",
        poll_seconds=0.01,
    )
    return RemoteDramaProvider(config, DramaMediaStore(tmp_path / "drama_media"))


def test_mock_provider_text_actions_still_match_legacy_rules():
    """纯文本动作仍是旧 mock_media 的逐字段等价。

    第五项之后，媒体动作不再等价：它们要产出可播放的本地示例文件并打示例标识，
    因此单独由 test_mock_provider_media_actions_emit_deterministic_sample_files 验收。
    """
    provider = MockDramaProvider()
    layers = {
        "logline": "一个雨夜的决定",
        "outline": "起承转合",
        "body": "【场景 1 · 旧地 · 夜】\n甲：你来了。\n乙：我收到信了。",
    }
    pairs = [
        (provider.script_doctor(layers, owner="local"), mock_media.script_doctor(layers)),
        (
            provider.creative_start("idea", "一封信", owner="local"),
            mock_media.creative_start("idea", "一封信"),
        ),
        (provider.extract_assets(layers, owner="local"), mock_media.extract_assets(layers)),
        (provider.resplit_shots(layers, owner="local"), mock_media.resplit_shots(layers)),
        (
            provider.change_voice("甲", "声线 A", owner="local"),
            mock_media.change_voice("甲", "声线 A"),
        ),
        (provider.mark_audio_done([], [], owner="local"), mock_media.mark_audio_done([], [])),
    ]
    assert provider.name == "mock"
    for actual, expected in pairs:
        assert actual == expected


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="示例素材依赖本机 FFmpeg")
def test_mock_provider_media_actions_emit_deterministic_sample_files(tmp_path):
    """第五项：离线每个媒体动作都产出真实文件；同样输入得到同样的 URL 与内容。"""
    store = DramaMediaStore(tmp_path / "media")
    provider = MockDramaProvider(media_store=store)
    asset = {"id": "a1", "name": "甲", "seed": 1}
    shot = {"id": "S1", "dur": 3, "prompt": "冷调"}

    def check(item: dict[str, Any], url_key: str) -> str:
        payload = item["payload"]
        # 结果里的 provider 标记、sample 三件套与 note 必须与画面内 / 页面 / 容器同源。
        assert payload["sample"] is True
        assert payload["sample_label"] == "示例，非模型生成"
        assert payload["sample_provider"] == "mock-sample"
        assert "未出网" in payload["sample_hint"]
        assert item["note"] == payload["sample_hint"]
        url = str(payload[url_key])
        assert url.startswith("/api/v1/drama/assets/")
        path = store.path_for_url("local", url)
        assert path is not None and path.is_file() and path.stat().st_size > 0
        return url

    tts1 = check(provider.tts_preview("甲", "声线 A", "你好", owner="local"), "audio_url")
    tts2 = check(provider.tts_preview("甲", "声线 A", "你好", owner="local"), "audio_url")
    assert tts1 == tts2, "同样输入必须得到同一个示例素材"

    check(provider.asset_candidates(asset, owner="local"), "image_url")
    keyframe = provider.keyframe(shot, owner="local")
    check(keyframe, "image_url")
    assert keyframe["payload"]["shot_id"] == "S1"

    video = provider.image_to_video(shot, "16:9", owner="local")
    video_url = check(video, "video_url")
    assert video["payload"]["cover_url"].startswith("/api/v1/drama/assets/")

    # 成片与真实模式共用 compose.py：有视频轨就合成，没有就明确给 hint 而不静默留痕。
    project = {
        "id": "p1",
        "shots": [{"id": "S1", "dur": 3, "videoUrl": video_url}],
        "timeline": {"order": ["S1"]},
        "audio": {"subtitles": [], "voices": [], "bgm": {}},
    }
    film = provider.preview_film(project, owner="local")
    check(film, "video_url")
    assert film["payload"]["composed_clips"] == 1
    assert film["payload"]["alignment"] == "subtitle"
    assert film["payload"]["duration_seconds"] == 3.0


def test_media_store_owner_isolation_and_url_roundtrip(tmp_path):
    store = DramaMediaStore(tmp_path / "media")
    saved = store.store("alice", "audio", WAV)
    assert saved["url"].startswith("/api/v1/drama/assets/")
    assert store.count("alice", "audio") == 1
    assert store.count("bob", "audio") == 0
    assert store.path_for_url("alice", saved["url"]) == store.resolve("alice", saved["asset_id"])
    assert store.path_for_url("bob", saved["url"]) is None
    assert store.path_for_url("alice", "https://other.example/x") is None
    with pytest.raises(AppError):
        store.resolve("alice", "../etc")
    with pytest.raises(AppError):
        store.store("alice", "image", WAV)  # 跨类型载荷必须拒绝


def test_remote_llm_actions_return_model_output_with_state_machine_keys(remote):
    layers = {"body": "【场景 1 · 旧地 · 夜】\n甲：你来了。", "outline": "", "logline": ""}
    doctor = remote.script_doctor(layers, owner="local")
    assert doctor["provider"] == "remote"
    assert doctor["payload"]["findings"] == ["缺少冲突升级"]
    assert set(doctor["payload"]) >= {"scenes", "dialogue_count", "findings", "checked_chars"}

    start = remote.creative_start("idea", "一封信", owner="local")
    assert set(start["payload"]["layers"]) == {"logline", "outline", "body"}

    found = remote.extract_assets(layers, owner="local")["payload"]["found"]
    assert found["characters"][0]["name"] == "甲"

    shots = remote.resplit_shots(layers, owner="local")["payload"]["shots"]
    assert set(shots[0]) >= {"dur", "size", "desc", "prompt", "line", "move", "refs"}

    voice = remote.change_voice("甲", "声线 A", owner="local")["payload"]
    assert voice["to"] == "少年感" and "声线 A" not in voice["candidates"]


def test_remote_tts_persists_audio_and_returns_local_url(remote):
    item = remote.tts_preview("甲", "声线 A", "你好", owner="local")
    assert item["payload"]["audio_url"].startswith("/api/v1/drama/assets/")
    assert item["payload"]["assets"][0]["kind"] == "audio"
    assert remote.media_store.count("local", "audio") == 1
    assert remote.mark_audio_done([], [], owner="local")["payload"]["audio_files"] == 1


def test_remote_media_actions_persist_and_preview_composes(remote, monkeypatch):
    seen: list[dict[str, Any]] = []

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        # T02：工作流通道也把 `usage` 传进来了（上游真值与售价分开记账）。
        seen.append({"prompt": prompt, "width": width, "height": height})
        return PNG

    monkeypatch.setattr(remote, "_generate", fake_generate)
    # A1：项目设定必须真的进提示词与 submit 宽高，不能再是写死的 1024×1024 方图。
    candidate = remote.asset_candidates(
        {
            "id": "a1",
            "name": "甲",
            "seed": 1,
            "project_settings": {
                "ratio": "9:16 竖屏",
                "style": "精致国漫",
                "res": "1080 × 1920 / 24fps",
            },
        },
        owner="local",
    )
    assert candidate["payload"]["assets"][0]["kind"] == "image"
    assert "精致国漫" in seen[0]["prompt"] and "9:16" in seen[0]["prompt"]
    assert (seen[0]["width"], seen[0]["height"]) == (1080, 1920)
    assert candidate["payload"]["project_settings"]["style"] == "精致国漫"

    monkeypatch.setattr(remote, "_generate", lambda client, prompt, **kwargs: MP4)
    video = remote.image_to_video({"id": "S1", "dur": 3}, "16:9", owner="local")
    assert video["payload"]["video_url"].startswith("/api/v1/drama/assets/")
    assert remote.media_store.count("local", "video") == 1

    # 没有视频轨时明确降级，但绝不冒充 mock 成功。
    empty = remote.preview_film({"shots": [], "audio": {}}, owner="local")
    assert empty["provider"] == "remote" and empty["payload"]["video_url"] is None
    assert "图生视频" in empty["payload"]["hint"]

    project = {
        "name": "测试",
        # 镜头字段是 videoUrl（camel），与 store.py 的列映射一致；
        # 旧测试写 video_url，正是「写回的值读不回来」那个 bug 的同款写法。
        "shots": [{"dur": 3, "videoUrl": video["payload"]["video_url"]}],
        "audio": {},
    }
    monkeypatch.setattr(remote, "_compose", lambda clips, **kwargs: MP4)
    composed = remote.preview_film(project, owner="local")
    assert composed["payload"]["video_url"].startswith("/api/v1/drama/assets/")
    assert composed["payload"]["composed_clips"] == 1
    assert composed["payload"]["muxed"] == {"voice_tracks": 0, "bgm": False, "subtitles": 0}


def test_remote_never_silently_falls_back_to_mock(remote):
    with pytest.raises(AppError) as exc:
        remote.script_doctor({"body": "BAD_SHAPE"}, owner="local")
    # 上游包装形状不认识时必须显式失败，绝不能悄悄回退到本地规则。
    assert exc.value.code == "drama_llm_shape"


def test_service_config_reflects_injected_provider(tmp_path):
    service = DramaService(tmp_path / "drama.sqlite")
    asyncio.run(service.start())
    cfg = asyncio.run(service.config("local"))
    assert cfg["provider"] == "mock" and cfg["offline"] is True
    asyncio.run(service.close())


def test_remote_config_requires_explicit_templates(tmp_path):
    config = RemoteDramaConfig(llm_base_url="http://127.0.0.1/v1", llm_model="m")
    with pytest.raises(ValueError, match="templateUuid"):
        RemoteDramaProvider(config, DramaMediaStore(tmp_path / "media"))


def test_mux_args_write_ai_disclosure_into_container_metadata(remote, tmp_path):
    """AI 生成标识要落进成片文件本身，而不只是页面上的一个角标。

    用 -metadata 而不是 drawtext 烧字幕：不需要重新编码，-c:v copy 保持不变，
    成片里带着这句话，播放器/ffprobe 都能读出来。
    """
    from dataclasses import replace

    labelled = RemoteDramaProvider(
        replace(remote.cfg, ai_label="AI 生成"),
        DramaMediaStore(tmp_path / "labelled"),
    )
    args = labelled._mux_args(
        "ffmpeg",
        tmp_path / "base.mp4",
        tmp_path / "out.mp4",
        voices=[],
        bgm=None,
        subtitles=[],
        levels={},
        srt=None,
    )
    joined = " ".join(args)
    assert "-metadata" in joined
    assert "comment=AI 生成" in joined
    assert "非真人实拍" in joined
    # 不带音轨时仍然只拷贝视频流，不重编码
    assert "-c:v copy" in joined

    # 未配置标识时不得凭空写入空 metadata
    plain = remote._mux_args(
        "ffmpeg",
        tmp_path / "base.mp4",
        tmp_path / "out2.mp4",
        voices=[],
        bgm=None,
        subtitles=[],
        levels={},
        srt=None,
    )
    assert "-metadata" not in plain


# ---------------------------------------------------------------- 官方 Kling 图生视频通道


def _official(tmp_path, **overrides):
    base: dict[str, Any] = {
        "llm_base_url": "http://127.0.0.1/v1",
        "llm_model": "m",
        "t2i_template_uuid": "template-t2i",
        "t2i_prompt_node": "node-1",
        "i2v_channel": "official",
        "public_base_url": "https://drama.example.com",
    }
    base.update(overrides)
    config = RemoteDramaConfig(**base)
    return RemoteDramaProvider(config, DramaMediaStore(tmp_path / "media"))


def test_official_channel_refuses_without_public_base_url(tmp_path):
    """没有公网基址就别发请求：上游只会回 100030，白跑一趟还留着预占。"""
    provider = _official(tmp_path, public_base_url="")
    with pytest.raises(AppError) as caught:
        provider.image_to_video(
            {"id": "S1", "dur": 5, "first_frame_asset_id": "0123456789abcdef"},
            "9:16",
            owner="local",
        )
    assert caught.value.code == "drama_config"
    # 只断言 PUBLIC_BASE_URL 是不够的：https 校验的文案里也含这个词，
    # 那样「空基址」与「错协议」会互相伪装，短路掉前者测试照样绿。
    assert "publicly reachable" in caught.value.message


def test_official_channel_rejects_a_non_https_base_url(tmp_path):
    """http 基址上游取不到图，必须在本地就拒绝，而不是等 100030。"""
    provider = _official(tmp_path, public_base_url="http://drama.example.com")
    with pytest.raises(AppError) as caught:
        provider.image_to_video(
            {"id": "S1", "dur": 5, "first_frame_asset_id": "0123456789abcdef"},
            "9:16",
            owner="local",
        )
    assert caught.value.code == "drama_config"
    assert "must be https" in caught.value.message


def test_official_channel_refuses_without_a_first_frame_asset(tmp_path):
    provider = _official(tmp_path)
    with pytest.raises(AppError) as caught:
        provider.image_to_video({"id": "S1", "dur": 5}, "9:16", owner="local")
    assert caught.value.code == "drama_config"
    assert "first-frame" in caught.value.message


def test_official_channel_sends_public_start_frame_and_records_usage(tmp_path, monkeypatch):
    """首帧必须以公网 URL 提交；上游自报的积分与余额要落进返回体。"""
    provider = _official(tmp_path)
    seen: dict[str, Any] = {}

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        seen["prompt"] = prompt
        seen["submit_path"] = client.submit_path
        seen["status_path"] = client.status_path
        seen["prompt_mode"] = client.prompt_mode
        seen["params"] = dict(client.extra_nodes)
        if usage is not None:
            usage.update({"upstream_task_id": "u1", "upstream_points": 250, "account_balance": 211})
        return MP4

    monkeypatch.setattr(provider, "_generate", fake_generate)
    item = provider.image_to_video(
        {
            "id": "S1",
            "dur": 5,
            "prompt": "镜头缓慢推进",
            "first_frame_asset_id": "0123456789abcdef",
        },
        "9:16",
        owner="local",
    )
    assert seen["submit_path"] == "/api/generate/video/kling/img2video"
    assert seen["status_path"] == "/api/generate/status"
    assert seen["prompt_mode"] == "plain"
    assert _public_asset_of(seen["params"]["startFrame"]) == ("local", "0123456789abcdef")
    assert seen["params"]["duration"] == "5", "时长要跟着镜头走，不是写死"
    assert seen["params"]["mode"] == "pro"
    payload = item["payload"]
    assert payload["channel"] == "official"
    assert payload["upstream_points"] == 250
    assert payload["account_balance"] == 211
    assert payload["video_url"].startswith("/api/v1/drama/assets/")


def test_official_channel_switches_to_images_array_on_v2_6(tmp_path, monkeypatch):
    """v2-6 走 images 数组并支持原生音效，此时不能再传 startFrame。"""
    provider = _official(tmp_path, i2v_model="kling-v2-6")
    seen: dict[str, Any] = {}

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        seen.update(dict(client.extra_nodes))
        return MP4

    monkeypatch.setattr(provider, "_generate", fake_generate)
    provider.image_to_video(
        {"id": "S1", "dur": 8, "first_frame_asset_id": "0123456789abcdef"},
        "9:16",
        owner="local",
    )
    assert len(seen["images"]) == 1
    assert _public_asset_of(seen["images"][0]) == ("local", "0123456789abcdef")
    assert "startFrame" not in seen
    assert seen["sound"] == "off"
    assert seen["duration"] == "10", "8 秒要被上式到 10 秒档，上游只认 5 / 10"


@pytest.mark.parametrize(("dur", "expected"), [(None, "5"), (3, "5"), (5, "5"), (6, "10")])
def test_official_channel_snaps_duration_to_supported_tiers(tmp_path, monkeypatch, dur, expected):
    """duration 是离散枚举，不是任意整数。

    分镜默认 `dur` 就是 3 秒；原样发 "3" 会被上游判参数非法。这里要求一律落在
    5 / 10 两档上，并且返回体报的 duration_seconds 必须等于实际提交的那一档 ——
    「提交 5 秒、记录 3 秒」会让时长统计和成本对不上。
    """
    provider = _official(tmp_path)
    seen: dict[str, Any] = {}

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        seen.update(dict(client.extra_nodes))
        return MP4

    monkeypatch.setattr(provider, "_generate", fake_generate)
    shot: dict[str, Any] = {"id": "S1", "first_frame_asset_id": "0123456789abcdef"}
    if dur is not None:
        shot["dur"] = dur
    item = provider.image_to_video(shot, "9:16", owner="local")
    assert seen["duration"] == expected
    assert item["payload"]["duration_seconds"] == int(expected)


def _public_asset_of(url: str) -> tuple[str, str]:
    """首帧必须是免登录的签名地址（上游拉图不带我们的凭据），并指向正确的账号与文件。"""
    import base64

    prefix = "https://drama.example.com/api/v1/drama/public/"
    assert url.startswith(prefix), url
    body = url[len(prefix) :].split(".", 1)[0]
    owner, asset_id, _ = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")
    return owner, asset_id
