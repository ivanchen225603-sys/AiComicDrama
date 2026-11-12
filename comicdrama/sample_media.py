"""离线演示用的确定性「示例素材」生成器。

事实边界（不要改写）：这里产出的每一张图、每一段声音、每一段视频都是**本机
确定性规则合成**，不是模型输出，也不代表模型效果。它只用于证明「从剧本走到
一个可播放、可下载的成片」这条流程与本机合成链路是通的。

硬约束：
- 全程只调用本机 ffmpeg 与 Python 标准库，不出网、不读任何凭据、不产生费用；
- 同样输入（key / 文本 / 尺寸 / 时长）得到同样画面、同样波形、同样时长；
- 每个画面内都写明「示例，非模型生成」，字体缺失或 ffmpeg 缺失时显式报错，
  绝不静默退回「无媒体的旧行为」。

为什么用 drawtext + textfile 而不是手写 PNG 像素：中文标识必须真的画进画面，
而纯标准库没有可用的中文字形；textfile 还能避开 filter 字符串里对引号、冒号、
反斜杠的转义（Windows 字体路径的冒号尤其容易踩坑）。字体被复制成临时目录里的
相对文件名 font.ttf，ffmpeg 以该目录为 cwd 运行，路径里不带盘符。
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

from .errors import AppError

# 三处标识（画面内 / 页面上 / 结果 provider 标记）统一从这里取，避免文案漂移。
SAMPLE_LABEL = "示例，非模型生成"
SAMPLE_HINT = (
    "示例，非模型生成：由本机确定性规则合成，仅用于演示流程与合成链路，不代表模型效果；"
    "全程未出网、未读取密钥、未产生上游费用"
)
SAMPLE_PROVIDER_TAG = "mock-sample"

_FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

_FFMPEG_MISSING = (
    "本机缺少合成工具，无法产出离线示例成片。"
    "请安装 FFmpeg 8.x（含 libx264 / drawtext / FreeType）并确认 ffmpeg、ffprobe 在 PATH，"
    "同时安装任一中文字体（Windows 自带微软雅黑；Linux 可装 fonts-noto-cjk 或 wqy-microhei）。"
)
_TOOL_HINT = "请确认 ffmpeg 可正常执行、且本机装有可用的中文 TrueType 字体"


def sample_key(*parts: Any) -> str:
    """把输入折叠成确定性素材键：同样输入得到同样的画面与波形。"""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part if part is not None else "").encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]


def _seed_of(key: str) -> int:
    """把任意字符串归一化成稳定种子。

    生产路径传的是 sample_key() 的十六进制串，但调用方完全可能直接传
    「镜头描述」这类非 hex 文本；旧实现 int(key[:8], 16) 会抛 ValueError，
    被服务层包成莫名的「参数不合法」。这里统一再哈希一次，保持确定性。
    """
    return int(sample_key(key)[:8], 16)


def dimensions(settings: dict[str, Any]) -> tuple[int, int]:
    """项目设定 -> 生成宽高。离线示例图与真实 t2i 共用这一份换算。

    优先 `res`（如「1080 × 1920 / 24fps」）；没有就按 `ratio`（如「9:16 竖屏」）
    以 1024 为长边等比换算；都没有才回落 1024×1024。这是**本地约定**，不是上游
    事实：首单冒烟要拿套餐允许的分辨率再核一遍。
    """
    found = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", str(settings.get("res") or ""))
    if found:
        return int(found.group(1)), int(found.group(2))
    found = re.search(r"(\d+)\s*:\s*(\d+)", str(settings.get("ratio") or ""))
    if found:
        w, h = int(found.group(1)), int(found.group(2))
        if w > 0 and h > 0:
            scale = 1024 / max(w, h)
            w = max(64, round(w * scale))
            h = max(64, round(h * scale))
            return w - (w % 2), h - (h % 2)
    return 1024, 1024


def prompt_with_settings(prompt: str, settings: dict[str, Any]) -> str:
    """画风 / 画幅拼进提示词；分辨率走 dimensions() 的宽高参数。"""
    parts = [prompt]
    style = str(settings.get("style") or "").strip()
    ratio = str(settings.get("ratio") or "").strip()
    if style:
        parts.append(f"画风：{style}")
    if ratio:
        parts.append(f"画幅：{ratio}")
    return "；".join(parts)


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise AppError("drama_ffmpeg", _FFMPEG_MISSING, 503)
    return binary


def _font() -> bytes:
    for candidate in _FONT_CANDIDATES:
        try:
            data = Path(candidate).read_bytes()
        except OSError:
            continue
        if data:
            return data
    raise AppError("drama_ffmpeg", _FFMPEG_MISSING, 503)


def _run(args: list[str], folder: Path) -> None:
    try:
        proc = subprocess.run(args, cwd=str(folder), capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError("drama_ffmpeg", f"示例素材合成失败：{exc}。{_TOOL_HINT}", 503) from exc
    if proc.returncode != 0:
        raise AppError(
            "drama_ffmpeg",
            f"示例素材合成失败（ffmpeg {proc.returncode}）："
            f"{proc.stderr.decode(errors='replace')[:400]}。{_TOOL_HINT}",
            503,
        )


def _clamp(value: int, low: int, high: int, field: str) -> int:
    number = max(low, min(high, int(value)))
    if number % 2:
        number -= 1
    if number < low:
        raise AppError("sample_size", f"{field} 不能小于 {low}", 400)
    return number


# 示例画面配色（天空、地面、强调色），按 key 轮换。全部是 6 位十六进制 RGB。
_PALETTES: tuple[tuple[str, str, str], ...] = (
    ("1d3557", "0f1b2d", "f4a261"),  # 雨夜港口
    ("2b2d42", "14151f", "ef476f"),  # 霓虹街巷
    ("3a506b", "1c2541", "5bc0be"),  # 清晨海面
    ("5f0f40", "310e24", "fb8b24"),  # 黄昏
    ("264653", "1b3139", "e9c46a"),  # 森林
    ("4a4e69", "22223b", "c9ada7"),  # 室内暖光
)


def sample_image_bytes(
    key: str,
    *,
    width: int = 720,
    height: int = 1280,
    caption: str = "",
) -> bytes:
    """生成一张带画面内标识的示例 PNG。配色、构图、文字全部由 key 决定。

    缩略图会按中间区域裁切竖屏画面，所以关键内容（标识与说明）放在画面正中，
    上下两端只放氛围色块。
    """
    binary = _ffmpeg()
    font = _font()
    w = _clamp(width, 64, 1920, "width")
    h = _clamp(height, 64, 1920, "height")
    seed = _seed_of(key)
    sky, ground, accent = _PALETTES[seed % len(_PALETTES)]
    # 色块位置与亮度随 key 变化，同一配色的不同镜头也能一眼区分。
    shade = 0x18 + (seed // len(_PALETTES)) % 0x40
    accent_x = (seed // 7) % 55 + 8
    big = max(10, min(w, h) // 16)
    small = max(8, big * 3 // 4)
    per_line = max(4, int(w * 0.84) // big)
    # 画面里只写镜头内容本身；「；画风：…；画幅：…」是给模型的附加要求，不必印进示例图。
    caption_text = re.split(r"；(?:画风|画幅)：", str(caption or ""))[0][:60]
    lines = [caption_text[i : i + per_line] for i in range(0, len(caption_text), per_line)][:2]
    # 说明卡片按行数定高，放在略高于正中的位置，避开视频播放条；
    # 每行单独 drawtext，避免字体自带的超大行距。
    pad = big // 2
    step = big * 7 // 5
    card_h = pad * 2 + small * 8 // 5 + step * len(lines)
    card_y = max(0, h * 11 // 25 - card_h // 2)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "font.ttf").write_bytes(font)
        (folder / "badge.txt").write_text(SAMPLE_LABEL, encoding="utf-8")
        text_filters = []
        for index, line in enumerate(lines):
            (folder / f"line{index}.txt").write_text(line, encoding="utf-8")
            y = card_y + pad + small * 8 // 5 + step * index
            text_filters.append(
                f"drawtext=fontfile=font.ttf:textfile=line{index}.txt:fontcolor=white:"
                f"fontsize={big}:x=(w-text_w)/2:y={y}"
            )
        out = folder / "sample.png"
        draw = ",".join(
            [
                # 远景：地平线以下换色，上方一块随 key 偏移的强调色块（像远处的灯或窗）
                f"drawbox=x=0:y=ih*0.62:w=iw:h=ih*0.38:color=0x{ground}@0.95:t=fill",
                f"drawbox=x=0:y=ih*0.615:w=iw:h=ih*0.008:color=0x{accent}@0.85:t=fill",
                f"drawbox=x=iw*{accent_x}/100:y=ih*0.10:w=iw*0.26:h=ih*0.16:"
                f"color=0x{accent}@0.70:t=fill",
                f"drawbox=x=0:y=0:w=iw:h=ih:color=0x{shade:02x}{shade:02x}{shade:02x}@0.08:t=fill",
                # 正中的说明卡片：缩略图裁切后仍然可见
                f"drawbox=x=iw*0.04:y={card_y}:w=iw*0.92:h={card_h}:color=0x0b1020@0.72:t=fill",
                "drawtext=fontfile=font.ttf:textfile=badge.txt:fontcolor=0xffd28a:"
                f"fontsize={small}:x=(w-text_w)/2:y={card_y + pad}",
                *text_filters,
            ]
        )
        _run(
            [
                binary,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x{sky}:s={w}x{h}:r=25:d=1",
                "-vf",
                draw,
                "-frames:v",
                "1",
                out.name,
            ],
            folder,
        )
        return out.read_bytes()


def sample_audio_bytes(key: str, *, seconds: float = 3.0) -> bytes:
    """生成一段确定性示例语音（WAV）。

    不是 TTS：用固定基频 + 音节包络合成一段「提示音」，让人能听出这一句配音
    确实落在时间轴上的哪个位置。节拍与基频由 key 决定，同样输入逐字节相同。
    """
    import io

    duration = max(0.2, min(60.0, float(seconds)))
    rate = 16000
    frames = int(duration * rate)
    seed = _seed_of(key)
    base = 180.0 + (seed % 12) * 18.0
    syllable = 0.26
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        samples = bytearray()
        for index in range(frames):
            t = index / rate
            phase = t % syllable
            envelope = math.sin(math.pi * phase / syllable) ** 2
            # 每个音节换一次基频，形成可听的「语句」节奏，且完全确定。
            step = int(t / syllable) % 5
            freq = base * (1.0 + 0.08 * step)
            value = 0.42 * math.sin(2 * math.pi * freq * t)
            value += 0.12 * math.sin(2 * math.pi * freq * 2 * t)
            samples += struct.pack("<h", int(max(-1.0, min(1.0, value * envelope)) * 26000))
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


def sample_video_bytes(
    key: str,
    *,
    seconds: float = 3.0,
    width: int = 720,
    height: int = 1280,
    caption: str = "",
    image: bytes | None = None,
) -> bytes:
    """把示例图做成一段精确时长的静态画面 MP4（含静音音轨，便于 concat）。"""
    binary = _ffmpeg()
    picture = (
        image if image else sample_image_bytes(key, width=width, height=height, caption=caption)
    )
    duration = max(0.2, min(60.0, float(seconds)))
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "still.png").write_bytes(picture)
        out = folder / "clip.mp4"
        _run(
            [
                binary,
                "-y",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-i",
                "still.png",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "25",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-shortest",
                out.name,
            ],
            folder,
        )
        return out.read_bytes()
