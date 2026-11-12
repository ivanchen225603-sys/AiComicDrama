import os
import shutil
from pathlib import Path

import pytest

from comicdrama.sample_media import _FONT_CANDIDATES

ROOT = Path(__file__).resolve().parents[1]

# HTTP 测试一律打回环地址。开发机设置了 HTTP_PROXY 时，httpx 默认会把回环请求
# 交给代理；这里把回环地址并进 NO_PROXY（保留已有条目），让结果不依赖宿主网络配置。
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_entries = [item.strip() for item in (os.environ.get("NO_PROXY") or "").split(",") if item.strip()]
_entries.extend(item for item in _LOOPBACK if item not in _entries)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(_entries)


def _media_toolchain_missing() -> str | None:
    """离线示例素材由 FFmpeg 现场生成，需要 ffmpeg、ffprobe 与一款中文字体。"""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if not any(Path(font).is_file() for font in _FONT_CANDIDATES):
        missing.append("中文字体")
    return "、".join(missing) or None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "needs_ffmpeg: 依赖 FFmpeg 生成示例素材或合成成片；缺少工具链时跳过"
    )


def pytest_report_header(config: pytest.Config) -> str:
    missing = _media_toolchain_missing()
    if missing:
        return f"媒体工具链：缺少 {missing}，标记为 needs_ffmpeg 的用例将跳过（CI 中全部运行）"
    return "媒体工具链：FFmpeg、ffprobe 与中文字体齐全"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    missing = _media_toolchain_missing()
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"需要 FFmpeg 工具链，本机缺少 {missing}")
    for item in items:
        if "needs_ffmpeg" in item.keywords:
            item.add_marker(skip)
