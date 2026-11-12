"""用户自填配音（OpenAI 兼容语音接口）、公网部署开关（必须登录 / 允许域名）。全部离线。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from comicdrama.config import Settings
from comicdrama.errors import AppError
from comicdrama.providers.hybrid import HybridDramaProvider
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.providers.remote import RemoteDramaConfig, RemoteDramaProvider
from comicdrama.service import DramaService

AK, SK = "ak-demo-0123456789", "sk-demo-abcdefghijk"
TTS = {
    "tts_base_url": "https://api.siliconflow.cn/v1",
    "tts_model": "FunAudioLLM/CosyVoice2-0.5B",
    "tts_voice": "voice-a, voice-b，voice-c",
    "tts_api_key": "tts-demo-key-123456",
}


@pytest.fixture
def service(tmp_path: Path):
    svc = DramaService(
        tmp_path / "drama.sqlite", MockDramaProvider(), admins=frozenset({"local"}), autopump=False
    )
    svc.store.install()
    svc.set_remote_template(RemoteDramaConfig(llm_base_url="", llm_model="", require_llm=False))
    return svc


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, kind: str = "audio/mpeg"):
        super().__init__(data)
        self.headers = {"Content-Type": kind}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_tts_fields_are_all_or_nothing_and_masked(service: DramaService):
    with pytest.raises(AppError):
        service.credentials.save("local", {"tts_base_url": TTS["tts_base_url"]})
    view = service.credentials.save("local", TTS)["tts"]
    assert view["configured"] is True
    assert TTS["tts_api_key"] not in json.dumps(view)
    assert view["voice"] == TTS["tts_voice"]
    assert service.credentials.clear("local", "tts")["tts"]["configured"] is False


def test_tts_key_routes_voice_to_the_real_endpoint(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    service.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK, **TTS})
    prov = service.provider_for("local")
    assert isinstance(prov, HybridDramaProvider)
    assert prov.tts is prov.media and prov.channels["voice"] == "tts"
    cfg = prov.media.cfg
    assert cfg.tts_mode == "openai" and cfg.tts_voices == ("voice-a", "voice-b", "voice-c")

    sent: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=0):
        sent.append(
            {
                "url": request.full_url,
                "body": json.loads(request.data),
                "auth": request.headers.get("Authorization"),
            }
        )
        return _Resp(b"ID3" + b"\x00" * 64)

    monkeypatch.setattr("comicdrama.providers.remote.urllib.request.urlopen", fake_urlopen)
    out = prov.tts_preview("林砚", "声线 A", "那封信，比船晚到了十一年。", owner="local")
    assert out["payload"]["audio_url"].startswith("/api/v1/drama/assets/")
    assert sent[0]["url"] == "https://api.siliconflow.cn/v1/audio/speech"
    assert sent[0]["body"]["model"] == TTS["tts_model"]
    assert sent[0]["body"]["input"].startswith("那封信")
    assert sent[0]["body"]["voice"] in cfg.tts_voices
    assert sent[0]["auth"] == "Bearer " + TTS["tts_api_key"]
    # 同一角色每次都拿到同一个音色
    prov.tts_preview("林砚", "声线 A", "我还是来了。", owner="local")
    assert sent[1]["body"]["voice"] == sent[0]["body"]["voice"]


def test_tts_errors_are_explicit_and_never_stored_as_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import urllib.error

    from comicdrama.media_store import DramaMediaStore

    cfg = RemoteDramaConfig(
        llm_base_url="",
        llm_model="",
        require_llm=False,
        tts_url="https://x/v1",
        tts_mode="openai",
        tts_model="m",
        tts_voices=("v",),
        secrets={"DRAMA_TTS_API_KEY": "k"},
        t2i_channel="official",
    )
    prov = RemoteDramaProvider(cfg, DramaMediaStore(tmp_path / "m"))
    replies: list[Any] = [
        _Resp(b'{"error":"quota"}', "application/json"),
        urllib.error.HTTPError("https://x", 401, "no", {}, None),
    ]

    def fake_urlopen(request, timeout=0):
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("comicdrama.providers.remote.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AppError) as caught:
        prov.tts_preview("a", "v", "t", owner="local")
    assert caught.value.code == "drama_tts_unavailable"
    with pytest.raises(AppError) as caught:
        prov.tts_preview("a", "v", "t", owner="local")
    assert caught.value.code == "drama_tts_config"


def test_container_binding_requires_login():
    with pytest.raises(ValueError):
        Settings(host="0.0.0.0")
    assert Settings(host="0.0.0.0", require_login=True).require_login is True


async def test_require_login_rejects_anonymous_and_accepts_a_session(tmp_path: Path):
    import httpx

    from comicdrama.app import create_app

    app = create_app(
        Settings(
            data_dir=tmp_path / "api",
            drama_autopump=False,
            require_login=True,
            allowed_hosts=["drama.example.com"],
        )
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://drama.example.com") as s:
            assert (await s.get("/api/v1/drama/config")).status_code == 401, (
                "反向代理来的匿名访客不能当成本机账号"
            )
            headers = {"Origin": "http://drama.example.com"}
            r = await s.post(
                "/api/v1/drama/account/register",
                json={"username": "demo_user", "password": "Passw0rd!demo"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            r = await s.post(
                "/api/v1/drama/account/login",
                json={"username": "demo_user", "password": "Passw0rd!demo"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            cfg = await s.get("/api/v1/drama/config", headers=headers)
            assert cfg.status_code == 200 and cfg.json()["owner"] != "local"
        async with httpx.AsyncClient(transport=transport, base_url="http://evil.example.com") as s:
            assert (await s.get("/api/v1/drama/config")).status_code == 400, "未允许的域名被拒绝"
