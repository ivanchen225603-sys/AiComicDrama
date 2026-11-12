"""素材本地留存：上游结果地址有时效性（官方文档为 7 天），必须服务端立即转存。

绝不把带签名的完整 URL 写进数据库：只保留 scheme://host/path，丢弃 query/fragment。
转存失败只能重试拉取同一个 URL，绝不重新提交生成任务。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..errors import AppError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 官方状态接口只给 imageUrl，不声明格式；mock 合成 PNG，真实上游常见 JPEG/WebP。
_MAGICS = (
    (PNG_MAGIC, "png"),
    (b"\xff\xd8\xff", "jpg"),
)
# WebM/Matroska 的 EBML 头；和图片、音频都不重叠。
_VIDEO_MAGICS = ((b"\x1a\x45\xdf\xa3", "webm"),)
_AUDIO_MAGICS = ((b"ID3", "mp3"), (b"OggS", "ogg"))


def image_extension(data: bytes) -> str:
    """按魔数判定本地留存扩展名；不信任 Content-Type 或 URL 后缀。"""
    for magic, extension in _MAGICS:
        if data.startswith(magic):
            return extension
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise AppError("asset_format", "only locally validated images are stored", 409)


def _is_iso_bmff(data: bytes) -> bool:
    """MP4/MOV/M4A 同属 ISO-BMFF：第 5~8 字节是 'ftyp'。

    前端 4 字节是 box 长度，所以不能从第 0 字节开始比对魔数。
    """
    return len(data) >= 12 and data[4:8] == b"ftyp"


def media_extension(kind: str, data: bytes) -> str:
    """按魔数判定留存扩展名；kind 决定接受哪一类容器。

    判错方向的代价不对称：把非媒体当媒体存下来，等于给下载端塞坏文件；
    所以宁可拒绝并要求人工核对，也不做「看起来像就放行」的宽松判定。
    """
    if kind == "image":
        return image_extension(data)
    if kind == "video":
        if _is_iso_bmff(data):
            return "mov" if data[8:12] == b"qt  " else "mp4"
        for magic, extension in _VIDEO_MAGICS:
            if data.startswith(magic):
                return extension
        raise AppError("asset_format", "only locally validated videos are stored", 409)
    if kind == "audio":
        for magic, extension in _AUDIO_MAGICS:
            if data.startswith(magic):
                return extension
        if _is_iso_bmff(data):
            return "m4a"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return "wav"
        # 无 ID3 头的裸 MP3 只能靠帧同步判：11 个 1 位。
        if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
            return "mp3"
        raise AppError("asset_format", "only locally validated audio is stored", 409)
    raise AppError("asset_kind", f"unsupported asset kind: {kind}", 409)


def safe_source(url: str | None) -> str | None:
    """剥除 query/fragment，避免把临时签名或访问令牌写进审计数据。"""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return f"opaque:{parts.scheme}"[:2048]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:2048]
