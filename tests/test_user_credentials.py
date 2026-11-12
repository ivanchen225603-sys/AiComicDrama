"""用户自填模型密钥：按账号保存、绝不外泄、按能力组合 Provider、零费用连通性检测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from comicdrama import credentials as creds_mod
from comicdrama.errors import AppError
from comicdrama.providers.hybrid import HybridDramaProvider
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.providers.remote import RemoteDramaConfig, RemoteDramaProvider
from comicdrama.service import DramaService

AK, SK, LLM_KEY = "ak-demo-0123456789", "sk-demo-abcdefghijk", "llm-demo-key-9876"


@pytest.fixture
def service(tmp_path: Path):
    svc = DramaService(
        tmp_path / "drama.sqlite", MockDramaProvider(), admins=frozenset({"local"}), autopump=False
    )
    svc.store.install()
    svc.set_remote_template(
        RemoteDramaConfig(
            llm_base_url="",
            llm_model="",
            require_llm=False,
            public_base_url="https://demo.example.com",
        )
    )
    return svc


def test_unconfigured_owner_keeps_the_deployment_default(service: DramaService):
    assert service.provider_for("local") is service.provider
    assert service.credentials.view("local")["liblib"]["configured"] is False


def test_liblib_only_routes_media_to_real_and_text_to_builtin(service: DramaService):
    service.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK})
    prov = service.provider_for("local")
    assert isinstance(prov, HybridDramaProvider)
    assert prov.name == "remote"
    assert isinstance(prov.media, RemoteDramaProvider)
    assert prov.text is None and prov.channels["text"] == "builtin"
    assert prov.media._libtv_credentials() == (AK, SK)
    # 文本走内置规则，不出网、不报错
    out = prov.script_doctor({"logline": "a", "outline": "b", "body": "正文"}, owner="local")
    assert out["provider"] == "mock"


def test_llm_key_switches_text_to_the_real_model(service: DramaService):
    service.credentials.save(
        "local",
        {
            "liblib_access_key": AK,
            "liblib_secret_key": SK,
            "llm_base_url": "https://api.deepseek.com/v1",
            "llm_model": "deepseek-chat",
            "llm_api_key": LLM_KEY,
        },
    )
    prov = service.provider_for("local")
    assert prov.text is prov.media
    assert prov.media._key(prov.media.cfg.llm_key_env) == LLM_KEY
    assert prov.channels["text"] == "llm"


def test_changing_keys_takes_effect_on_the_next_request(service: DramaService):
    service.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK})
    first = service.provider_for("local")
    assert service.provider_for("local") is first, "密钥未变时复用"
    service.credentials.save("local", {"liblib_access_key": AK + "x"})
    second = service.provider_for("local")
    assert second is not first
    assert second.media._libtv_credentials() == (AK + "x", SK)
    service.credentials.clear("local", "liblib")
    assert service.provider_for("local") is service.provider


def test_owners_are_isolated(service: DramaService):
    service.credentials.save("alice", {"liblib_access_key": AK, "liblib_secret_key": SK})
    assert service.provider_for("bob") is service.provider
    assert service.credentials.get("bob") == {}


def test_blank_secret_keeps_the_previous_value_and_partial_input_is_rejected(service: DramaService):
    service.credentials.save("local", {"liblib_access_key": AK, "liblib_secret_key": SK})
    service.credentials.save("local", {"liblib_access_key": "", "liblib_secret_key": ""})
    assert service.credentials.get("local")["liblib_secret_key"] == SK
    with pytest.raises(AppError) as caught:
        service.credentials.save("other", {"liblib_access_key": AK})
    assert caught.value.code == "invalid_credentials"
    with pytest.raises(AppError):
        service.credentials.save("other", {"llm_base_url": "https://x/v1", "llm_model": "m"})
    with pytest.raises(AppError):
        service.credentials.save(
            "other", {"llm_base_url": "ftp://x", "llm_model": "m", "llm_api_key": "k"}
        )
    with pytest.raises(AppError):
        service.credentials.save("other", {"liblib_access_key": "a\nb", "liblib_secret_key": "c"})


async def test_secrets_never_leave_through_state_config_or_export(service: DramaService):
    service.credentials.save(
        "local",
        {
            "liblib_access_key": AK,
            "liblib_secret_key": SK,
            "llm_base_url": "https://api.deepseek.com/v1",
            "llm_model": "deepseek-chat",
            "llm_api_key": LLM_KEY,
        },
    )
    blobs = [
        json.dumps(await service.state("local"), ensure_ascii=False),
        json.dumps(await service.config("local"), ensure_ascii=False),
        json.dumps(await service.export_doc("local"), ensure_ascii=False),
        json.dumps(service.credentials.view("local"), ensure_ascii=False),
    ]
    for blob in blobs:
        for secret in (AK, SK, LLM_KEY):
            assert secret not in blob
    cfg = await service.config("local")
    assert cfg["provider"] == "remote" and cfg["offline_demo"] is False
    assert cfg["credentials"]["liblib"]["access_key"].endswith(AK[-4:])
    assert cfg["public_base_url_set"] is True
    assert SK not in repr(service.provider_for("local").media.cfg), "密钥不能出现在 repr / 日志里"


async def test_generation_uses_the_owner_provider_and_stays_offline_for_others(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
):
    """填了密钥的账号，文本动作仍能离线完成；没填的账号完全不受影响。"""
    service.credentials.save("alice", {"liblib_access_key": AK, "liblib_secret_key": SK})
    body = await service.action("alice", "script-doctor", [], None, None)
    assert body["result"]["provider"] == "mock", "未填文本模型时回落内置规则"
    body = await service.action("bob", "script-doctor", [], None, None)
    assert body["result"]["provider"] == "mock"


def test_liblib_check_reads_the_free_status_query(monkeypatch: pytest.MonkeyPatch):
    from comicdrama.liblib import client as libtv

    replies: list[dict[str, Any]] = [
        {"code": 100051, "msg": "生图任务不存在"},
        {"_http_error": 401},
        {"_transport_error": True},
        {"code": 100010, "msg": "AccessKey已过期"},
    ]
    seen: list[str] = []

    def fake_post(self, path, payload, credentials):
        seen.append(path)
        assert credentials == (AK, SK)
        return replies.pop(0)

    monkeypatch.setattr(libtv.LibTVMediaProvider, "_post", fake_post)
    base = "https://openapi.liblibai.cloud"
    assert creds_mod.check_liblib(AK, SK, base)["ok"] is True
    assert "不正确" in creds_mod.check_liblib(AK, SK, base)["message"]
    assert "无法连接" in creds_mod.check_liblib(AK, SK, base)["message"]
    assert "过期" in creds_mod.check_liblib(AK, SK, base)["message"]
    assert set(seen) == {libtv.STATUS_PATH}, "只能调用查询接口，不能提交任务"


def test_llm_check_lists_models_without_calling_chat(monkeypatch: pytest.MonkeyPatch):
    import io
    import urllib.error

    calls: list[str] = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        if "bad" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)
        return Resp(json.dumps({"data": [{"id": "deepseek-chat"}]}).encode())

    monkeypatch.setattr(creds_mod.urllib.request, "urlopen", fake_urlopen)
    assert creds_mod.check_llm("https://api.x.com/v1", LLM_KEY, "deepseek-chat")["ok"] is True
    assert (
        "没有找到模型" in creds_mod.check_llm("https://api.x.com/v1", LLM_KEY, "gpt-x")["message"]
    )
    assert creds_mod.check_llm("https://bad.x.com/v1", LLM_KEY, "m")["ok"] is False
    assert all(url.endswith("/models") for url in calls)


async def test_http_routes_mask_and_clear(service: DramaService, tmp_path: Path):
    import httpx

    from comicdrama.app import create_app
    from comicdrama.config import Settings

    app = create_app(Settings(data_dir=tmp_path / "api", drama_autopump=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8001") as s:
            r = await s.put(
                "/api/v1/drama/credentials", json={"liblib_access_key": AK, "liblib_secret_key": SK}
            )
            assert r.status_code == 200 and AK not in r.text and SK not in r.text
            assert r.json()["liblib"]["configured"] is True
            cfg = (await s.get("/api/v1/drama/config")).json()
            assert cfg["provider"] == "remote" and SK not in json.dumps(cfg)
            assert SK not in (await s.get("/api/v1/drama/state")).text
            r = await s.put("/api/v1/drama/credentials", json={"liblib_access_key": AK})
            assert r.status_code == 200, "只改一项时另一项保持原值"
            r = await s.put("/api/v1/drama/credentials", json={"unknown": "x"})
            assert r.status_code == 422
            r = await s.delete("/api/v1/drama/credentials?scope=liblib")
            assert r.json()["liblib"]["configured"] is False
            assert (await s.get("/api/v1/drama/config")).json()["provider"] == "mock"
