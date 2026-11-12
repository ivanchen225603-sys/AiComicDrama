"""用户自己填写的模型密钥（Liblib 图片 / 视频，可选的文本模型）。

设计边界：
- 按 owner 隔离存放在 drama.sqlite 的 `drama_credentials` 表；**绝不**进入 `/state`、
  导出文档、审计日志或任何错误信息。页面只拿到打码后的视图。
- 保存时空字符串表示「保持原值」，只有显式清除才会删掉，避免用户改模型名时
  不得不把密钥重新粘贴一遍。
- 连通性检测只访问用户自己填写的端点，并且只用不产生费用的接口
  （Liblib 查询一个不存在的任务；文本模型列出可用模型）。
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any

from .errors import AppError
from .store import DramaStore

_TABLE = (
    "CREATE TABLE IF NOT EXISTS drama_credentials("
    "owner TEXT PRIMARY KEY,data TEXT NOT NULL DEFAULT '{}',updated_at REAL NOT NULL DEFAULT 0)"
)

SECRET_FIELDS = ("liblib_access_key", "liblib_secret_key", "llm_api_key", "tts_api_key")
PLAIN_FIELDS = ("llm_base_url", "llm_model", "tts_base_url", "tts_model", "tts_voice")
_TTS_FIELDS = ("tts_base_url", "tts_model", "tts_api_key", "tts_voice")
_MAX_LEN = 512


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 6 else "••••"


def _clean(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) > _MAX_LEN or any(ch in text for ch in "\r\n\t"):
        raise AppError("invalid_credentials", f"{field} 格式不正确", 400)
    return text


class CredentialStore:
    def __init__(self, store: DramaStore) -> None:
        self.store = store

    def _ensure(self, db: sqlite3.Connection) -> None:
        db.execute(_TABLE)

    def get(self, owner: str) -> dict[str, str]:
        def op(db: sqlite3.Connection) -> dict[str, str]:
            self._ensure(db)
            row = db.execute(
                "SELECT data FROM drama_credentials WHERE owner=?", (owner,)
            ).fetchone()
            if row is None:
                return {}
            try:
                data = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                return {}
            return {k: str(v) for k, v in data.items() if isinstance(v, str)}

        return self.store.run(op)

    def save(self, owner: str, fields: dict[str, Any]) -> dict[str, Any]:
        current = self.get(owner)
        merged = dict(current)
        for key in SECRET_FIELDS:
            if key in fields:
                text = _clean(fields.get(key), key)
                if text:  # 空 = 保持原值
                    merged[key] = text
        for key in PLAIN_FIELDS:
            if key in fields:
                merged[key] = _clean(fields.get(key), key)
        base = merged.get("llm_base_url", "")
        if base and not (base.startswith("https://") or base.startswith("http://")):
            raise AppError(
                "invalid_credentials", "文本模型地址必须以 http:// 或 https:// 开头", 400
            )
        if bool(merged.get("liblib_access_key")) != bool(merged.get("liblib_secret_key")):
            raise AppError(
                "invalid_credentials", "Liblib 的 AccessKey 与 SecretKey 需要同时填写", 400
            )
        llm_parts = [merged.get("llm_base_url"), merged.get("llm_model"), merged.get("llm_api_key")]
        if any(llm_parts) and not all(llm_parts):
            raise AppError(
                "invalid_credentials", "文本模型需要同时填写接口地址、模型名称和 API Key", 400
            )
        tts_parts = [merged.get(k) for k in _TTS_FIELDS]
        if any(tts_parts) and not all(tts_parts):
            raise AppError(
                "invalid_credentials", "配音需要同时填写接口地址、模型、音色和 API Key", 400
            )
        tts_base = merged.get("tts_base_url", "")
        if tts_base and not (tts_base.startswith("https://") or tts_base.startswith("http://")):
            raise AppError(
                "invalid_credentials", "配音接口地址必须以 http:// 或 https:// 开头", 400
            )
        clean = {k: v for k, v in merged.items() if v}

        def op(db: sqlite3.Connection) -> None:
            self._ensure(db)
            db.execute(
                "INSERT INTO drama_credentials(owner,data,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                (owner, json.dumps(clean, ensure_ascii=False), time.time()),
            )

        self.store.run(op)
        return self.view(owner)

    def clear(self, owner: str, scope: str = "all") -> dict[str, Any]:
        current = self.get(owner)
        if scope == "liblib":
            for key in ("liblib_access_key", "liblib_secret_key"):
                current.pop(key, None)
        elif scope == "llm":
            for key in ("llm_base_url", "llm_model", "llm_api_key"):
                current.pop(key, None)
        elif scope == "tts":
            for key in _TTS_FIELDS:
                current.pop(key, None)
        else:
            current = {}

        def op(db: sqlite3.Connection) -> None:
            self._ensure(db)
            db.execute(
                "INSERT INTO drama_credentials(owner,data,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                (owner, json.dumps(current, ensure_ascii=False), time.time()),
            )

        self.store.run(op)
        return self.view(owner)

    def view(self, owner: str) -> dict[str, Any]:
        data = self.get(owner)
        return {
            "liblib": {
                "configured": bool(data.get("liblib_access_key") and data.get("liblib_secret_key")),
                "access_key": _mask(data.get("liblib_access_key", "")),
                "secret_key": _mask(data.get("liblib_secret_key", "")),
            },
            "llm": {
                "configured": bool(
                    data.get("llm_base_url") and data.get("llm_model") and data.get("llm_api_key")
                ),
                "base_url": data.get("llm_base_url", ""),
                "model": data.get("llm_model", ""),
                "api_key": _mask(data.get("llm_api_key", "")),
            },
            "tts": {
                "configured": all(data.get(k) for k in _TTS_FIELDS),
                "base_url": data.get("tts_base_url", ""),
                "model": data.get("tts_model", ""),
                "voice": data.get("tts_voice", ""),
                "api_key": _mask(data.get("tts_api_key", "")),
            },
        }


# ---------------------------------------------------------------- 连通性检测（零费用接口）


def check_liblib(access: str, secret: str, base_url: str) -> dict[str, Any]:
    """查询一个不存在的任务：签名正确时上游返回「任务不存在」，不会产生费用。"""
    from .liblib.client import STATUS_PATH, LibTVMediaProvider

    client = LibTVMediaProvider(
        base_url=base_url,
        template_uuid="connectivity-check",
        prompt_mode="plain",
        credentials=(access, secret),
        timeout=10.0,
    )
    raw = client._post(STATUS_PATH, {"generateUuid": "0" * 32}, (access, secret))
    if raw.get("_transport_error"):
        return {"ok": False, "message": "无法连接 Liblib，请检查服务器网络"}
    status = raw.get("_http_error")
    if status in (401, 403):
        return {"ok": False, "message": "AccessKey 或 SecretKey 不正确"}
    if status:
        return {"ok": False, "message": f"Liblib 返回 HTTP {status}"}
    code = raw.get("code")
    if code in (0, 100051, "0", "100051"):
        return {"ok": True, "message": "Liblib 连接正常"}
    return {"ok": False, "message": str(raw.get("msg") or f"Liblib 返回错误码 {code}")[:120]}


def check_llm(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """列出可用模型：只读接口，不产生调用费用。"""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            body = json.loads(response.read(1 << 20).decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "message": "API Key 不正确或没有权限"}
        if exc.code == 404:
            return {"ok": False, "message": "接口地址不正确（应指向 /v1）"}
        return {"ok": False, "message": f"文本模型返回 HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"ok": False, "message": "无法连接文本模型接口，请检查地址与网络"}
    ids = [str(x.get("id")) for x in (body.get("data") or []) if isinstance(x, dict)]
    if ids and model not in ids:
        return {"ok": True, "message": f"连接正常，但没有找到模型「{model}」，请确认名称"}
    return {"ok": True, "message": "文本模型连接正常"}
