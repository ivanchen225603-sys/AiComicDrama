"""漫剧真实素材的本地留存。

上游返回的图片/音频/视频 URL 只有 7 天时效，且带签名；因此真实 Provider
必须在拿到 bytes 后**立即**转存到本地，只把本地 URL 交给前端，绝不把带签名的
完整地址写进数据库或页面。

使用 liblib/assets.py 的魔数判定与分档上限：不信任 Content-Type、
不信任 URL 后缀，判不出容器就拒绝保存（宁可不存，也不给下载端塞坏文件）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import AppError
from .liblib.assets import media_extension
from .liblib.contracts import asset_limit

_OWNER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ASSET_ID = re.compile(r"[0-9a-f]{16}\Z")
_LOCAL_URL = re.compile(r"/api/v1/drama/assets/([0-9a-f]{16})\Z")
_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".webp"),
    "video": (".mp4", ".mov", ".webm"),
    "audio": (".mp3", ".wav", ".ogg", ".m4a"),
}


def _check_owner(owner: str) -> str:
    if not _OWNER.fullmatch(owner):
        raise AppError("asset_path", "owner is not a safe identifier", 400)
    return owner


class DramaMediaStore:
    """同步实现：Provider 跑在 asyncio.to_thread 里，没有可复用的事件循环。"""

    def __init__(self, root: Path, *, quota_bytes: int | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # 每 owner 的磁盘配额。None / 0 表示不限（默认不限，避免误伤既有部署）。
        self.quota_bytes = int(quota_bytes) if quota_bytes else 0

    # ---------- 占用统计 ----------
    def _files(self, owner: str) -> list[Path]:
        folder = self.root / owner
        if not folder.is_dir():
            return []
        return [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]

    def usage(self, owner: str) -> int:
        """该 owner 已占用的字节数（直接量磁盘，不信数据库里的记账值）。"""
        _check_owner(owner)
        return sum(p.stat().st_size for p in self._files(owner))

    def stats(self, owner: str) -> dict[str, Any]:
        """给管理页用的占用快照：文件数、字节数、按容器类型分布。"""
        _check_owner(owner)
        files = self._files(owner)
        by_kind: dict[str, int] = {}
        for path in files:
            by_kind[path.suffix.lower()] = by_kind.get(path.suffix.lower(), 0) + 1
        return {
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "limit_bytes": self.quota_bytes,
            "by_extension": by_kind,
        }

    def _guard_quota(self, owner: str, incoming: int) -> None:
        if not self.quota_bytes:
            return
        used = self.usage(owner)
        if used + incoming > self.quota_bytes:
            raise AppError(
                "quota_exceeded",
                f"素材占用 {used} 字节，本次写入 {incoming} 字节会超过配额 "
                f"{self.quota_bytes} 字节；请先执行素材清理（purgeMedia）",
                409,
            )

    def _store_as(self, owner: str, kind: str, data: bytes, asset_id: str) -> dict[str, Any]:
        if not data:
            raise AppError("asset_empty", "refusing to store an empty media file", 409)
        limit = asset_limit(kind)
        if limit is None:
            raise AppError("asset_kind", f"unsupported asset kind: {kind}", 409)
        if len(data) > limit:
            raise AppError("asset_too_large", "media exceeds the local retention limit", 409)
        extension = media_extension(kind, data)
        folder = self.root / owner
        folder.mkdir(parents=True, exist_ok=True)
        existing = sorted(folder.glob(f"{asset_id}.*"))
        if existing:
            # 确定性素材重复生成时直接复用：既保证「同样输入同样 URL」，
            # 也不会把同一份文件在配额里算第二遍。
            return {
                "asset_id": asset_id,
                "kind": kind,
                "url": f"/api/v1/drama/assets/{asset_id}",
                "bytes": len(data),
                "provider_stored": True,
            }
        # 配额在写盘前校验：写到一半才发现超限会留下残文件。
        self._guard_quota(owner, len(data))
        target = folder / f"{asset_id}.{extension}"
        temporary = folder / f".{asset_id}.tmp"
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "asset_id": asset_id,
            "kind": kind,
            "url": f"/api/v1/drama/assets/{asset_id}",
            "bytes": len(data),
            "provider_stored": True,
        }

    def store(self, owner: str, kind: str, data: bytes) -> dict[str, Any]:
        """真实素材：随机 id，每次转存都是一份新文件。"""
        _check_owner(owner)
        return self._store_as(owner, kind, data, uuid.uuid4().hex[:16])

    def store_sample(self, owner: str, kind: str, data: bytes, key: str) -> dict[str, Any]:
        """离线示例素材：id 由 (owner, kind, key) 哈希派生。

        同一句台词、同一镜头、同一组设定反复演示会得到同一个 URL，测试可以
        直接断言素材地址与内容，而不需要处理 uuid 随机性。key 必须由调用方
        用确定性输入拼出（见 sample_media.sample_key）。
        """
        _check_owner(owner)
        asset_id = hashlib.sha256(f"{owner}|{kind}|{key}".encode()).hexdigest()[:16]
        saved = self._store_as(owner, kind, data, asset_id)
        self._mark_sample(owner, asset_id)
        saved["sample"] = True
        saved["deterministic_key"] = str(key)
        return saved

    # ---------- 示例素材登记 ----------
    # 离线示例文件和真实文件放在同一个目录里；切到真实模式时要能认出哪些是示例，
    # 才能把它们从镜头 / 成片里摘掉，避免真实成片里混进「示例」画面和提示音。
    def _mark_sample(self, owner: str, asset_id: str) -> None:
        index = self.root / owner / ".samples"
        known = self.sample_ids(owner)
        if asset_id not in known:
            with index.open("a", encoding="utf-8") as fh:
                fh.write(asset_id + "\n")

    def sample_ids(self, owner: str) -> frozenset[str]:
        _check_owner(owner)
        index = self.root / owner / ".samples"
        if not index.is_file():
            return frozenset()
        return frozenset(
            x.strip() for x in index.read_text(encoding="utf-8").splitlines() if x.strip()
        )

    def is_sample_url(self, owner: str, url: str) -> bool:
        match = _LOCAL_URL.fullmatch(str(url or ""))
        if match is None:
            return False
        return match.group(1) in self.sample_ids(owner)

    # ---------- 给上游平台的公网地址 ----------
    # Liblib 只能从公网拉参考图 / 首帧，拉取时不带我们的登录凭据，所以不能直接用
    # /api/v1/drama/assets（需要登录）。这里签发一个带有效期的免登录地址，只对
    # 指定账号的指定文件有效，过期即失效；签名密钥随数据目录生成并持久化。
    def _signing_key(self) -> bytes:
        path = self.root / ".public_signing_key"
        if not path.is_file():
            path.write_bytes(secrets.token_bytes(32))
        return path.read_bytes()

    def public_token(self, owner: str, asset_id: str, ttl_seconds: int = 6 * 3600) -> str:
        _check_owner(owner)
        if not _ASSET_ID.fullmatch(asset_id):
            raise AppError("asset_path", "invalid asset id", 400)
        expires = int(time.time()) + int(ttl_seconds)
        body = (
            base64.urlsafe_b64encode(f"{owner}|{asset_id}|{expires}".encode()).decode().rstrip("=")
        )
        sig = hmac.new(self._signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{body}.{sig}"

    def resolve_public(self, token: str) -> Path:
        body, _, sig = str(token or "").partition(".")
        expected = hmac.new(self._signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not body or not hmac.compare_digest(sig, expected):
            raise AppError("asset_missing", "media asset not found", 404)
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            owner, asset_id, expires = raw.split("|")
        except (ValueError, UnicodeDecodeError):
            raise AppError("asset_missing", "media asset not found", 404) from None
        if int(expires) < time.time():
            raise AppError("asset_missing", "media asset not found", 404)
        return self.resolve(owner, asset_id)

    def purge_older_than(
        self,
        owner: str,
        days: float,
        *,
        dry_run: bool = False,
        keep_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """删除 last-modified 早于 `days` 天的素材文件。

        为什么按 mtime 而不是数据库 created_at：素材是磁盘上的真东西，
        文件被拷贝/恢复后 mtime 才是可信的；数据库行可能已经和磁盘不一致。
        返回被删文件的 asset_id 列表，调用方据此同步删掉 drama_media 记录。

        `keep_ids` 是**仍被业务引用**的 asset_id 集合，这些文件一律不删。

        为什么必须有这道闸：本方法原本只看 mtime，于是一个用了一个月的老镜头，
        它的视频文件 mtime 也很老，清理时会被删掉——而 `shots[].videoUrl` 还指着它。
        后果不是「少一个文件」而是静默错账：`build_plan()` 对拿不到路径的镜头**静默跳过**，
        成片悄悄少一段；`exportFilm` 才报 `film_missing`，那时已经不知道是哪一次清理干的。
        引用关系只有服务层知道（`media_store` 看不到 state），所以由调用方传进来。
        """
        _check_owner(owner)
        if days < 0:
            raise AppError("invalid_field", "days 不能为负", 400)
        keep = keep_ids or frozenset()
        cutoff = time.time() - days * 86400
        victims: list[Path] = []
        kept: list[Path] = []
        freed = 0
        for path in self._files(owner):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if path.stem in keep:
                    kept.append(path)
                    continue
                victims.append(path)
                freed += path.stat().st_size
            except OSError:
                continue
        if not dry_run:
            for path in victims:
                path.unlink(missing_ok=True)
        return {
            "days": days,
            "dry_run": dry_run,
            "files": len(victims),
            "freed_bytes": freed,
            # 被引用而跳过的数量单独报：用户看到「0 个可清理」时要知道是被保护住了，
            # 而不是以为清理没生效。
            "kept": len(kept),
            "asset_ids": [p.stem for p in victims if _ASSET_ID.fullmatch(p.stem)],
        }

    def resolve(self, owner: str, asset_id: str) -> Path:
        """按 owner 目录查找，asset_id 不允许包含路径分隔符，因此无法越权或穿越。"""
        _check_owner(owner)
        if not _ASSET_ID.fullmatch(asset_id):
            raise AppError("asset_path", "invalid asset id", 400)
        matches = sorted((self.root / owner).glob(f"{asset_id}.*"))
        if not matches:
            raise AppError("asset_missing", "media asset not found", 404)
        return matches[0]

    def count(self, owner: str, kind: str) -> int:
        """按已留存文件的扩展名统计真实素材数（供 mark-audio-done 等动作使用）。"""
        _check_owner(owner)
        folder = self.root / owner
        if not folder.is_dir():
            return 0
        allowed = _EXTENSIONS.get(kind, ())
        return sum(1 for path in folder.iterdir() if path.suffix.lower() in allowed)

    def path_for_url(self, owner: str, url: str) -> Path | None:
        """把本服务签发的本地素材 URL 还原成文件；外来 URL 一律返回 None。"""
        match = _LOCAL_URL.fullmatch(url)
        if not match:
            return None
        try:
            return self.resolve(owner, match.group(1))
        except AppError:
            return None

    @staticmethod
    def asset_id_of_url(url: str) -> str:
        """只从本服务签发的 URL 里取 asset_id；外来 URL 返回空串。"""
        match = _LOCAL_URL.fullmatch(str(url or ""))
        return match.group(1) if match else ""

    def remove(self, owner: str, url: str) -> bool:
        """删除本 owner 的一份留存文件。外来 URL / 越权路径一律不碰。"""
        path = self.path_for_url(owner, str(url or ""))
        if path is None:
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True
