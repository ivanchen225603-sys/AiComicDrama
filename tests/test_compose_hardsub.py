"""硬字幕烧录与输出分辨率选项的验收用例（全部真跑本机 ffmpeg）。

覆盖两类新能力，以及「默认路径一个字节都不能变」的回归锁：
- `subtitle_mode=burn`：字幕进画面、输出不再有字幕流、视频轨必须重编码；
- `resolution=720p/1080p`：只缩不放，短边（竖屏宽度）不超过上限；
- 两者同时开启：两条滤镜一起生效，顺序是「先缩放再烧字幕」；
- `render_film` 的直通捷径（只看有没有配音/BGM/字幕就原样返回 concat 结果）在需要
  重编码时必须被绕开，否则用户选的分辨率会被静默丢弃。

事实分级：本文件断言的都是「已实测」的本机行为——期望值（720/1280、无字幕流、
烧录帧的亮度上限）全部按实测算出的固定值写死，不做前后自比。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from comicdrama.compose import (
    DEFAULT_RESOLUTION,
    DEFAULT_SUBTITLE_MODE,
    RESOLUTION_CAPS,
    SUBTITLE_MODES,
    FilmPlan,
    mux_args,
    needs_reencode,
    parse_resolution,
    parse_subtitle_mode,
    render_film,
    scale_filter,
    subtitle_filter,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="硬字幕/分辨率验收必须真跑 ffmpeg")
needs_probe = pytest.mark.skipif(FFPROBE is None, reason="需要 ffprobe 核验轨道与分辨率")

SUBTITLE_TEXT = "烧进画面的字幕 HARDSUB 123"


def _run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[-800:]
    return proc


def _make_clip(path: Path, width: int, height: int, seconds: float = 2.0) -> Path:
    """生成纯色竖屏素材：底色全是黑，烧字幕后的像素统计才能证明字真的进了画面。"""
    _run(
        [
            FFMPEG or "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:d={seconds:.3f}:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            f"{seconds:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return path


def _write_srt(path: Path) -> Path:
    path.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{SUBTITLE_TEXT}\n", encoding="utf-8")
    return path


def _streams(path: Path) -> list[dict[str, Any]]:
    proc = _run([FFPROBE or "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)])
    streams: list[dict[str, Any]] = json.loads(proc.stdout.decode("utf-8"))["streams"]
    return streams


def _codec_types(path: Path) -> list[str]:
    return [str(item["codec_type"]) for item in _streams(path)]


def _video_size(path: Path) -> tuple[int, int]:
    stream = next(item for item in _streams(path) if item["codec_type"] == "video")
    return int(stream["width"]), int(stream["height"])


def _frame_luma(path: Path, at: float = 1.0) -> dict[str, float]:
    """取指定时刻一帧的 YMIN/YMAX：纯黑画面 YMIN=YMAX=16，白字烧进去 YMAX 会逼近 235。"""
    proc = _run(
        [
            FFMPEG or "ffmpeg",
            "-loglevel",
            "info",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "signalstats,metadata=print",
            "-f",
            "null",
            "-",
        ]
    )
    stats: dict[str, float] = {}
    for line in proc.stderr.decode("utf-8", errors="replace").splitlines():
        if "lavfi.signalstats." not in line:
            continue
        key, _, value = line.split("lavfi.signalstats.", 1)[1].partition("=")
        if key in {"YMIN", "YMAX", "YAVG"}:
            stats[key] = float(value.strip())
    return stats


def _pairs(args: list[str]) -> set[tuple[str, str]]:
    """把参数序列折成相邻 (选项, 值) 对，便于断言某个选项确实配了某个值。"""
    return {(args[index], args[index + 1]) for index in range(len(args) - 1)}


def _mux(base: Path, output: Path, srt: Path, **kwargs: Any) -> list[str]:
    args = mux_args(
        FFMPEG or "ffmpeg",
        base,
        output,
        subtitles=[{"in": "00:00", "out": "00:02", "text": SUBTITLE_TEXT}],
        srt=srt,
        total_seconds=2.0,
        **kwargs,
    )
    _run(args)
    return args


# ---------------------------------------------------------------- 纯函数：常量与归一化
def test_parsers_fall_back_to_defaults_without_raising():
    """非法值（空、None、大写的感叹号、未支持的 4k）一律退回默认，绝不抛异常。"""
    assert SUBTITLE_MODES == ("soft", "burn")
    assert RESOLUTION_CAPS == {"720p": 720, "1080p": 1080}
    assert DEFAULT_SUBTITLE_MODE == "soft"
    assert DEFAULT_RESOLUTION == "source"

    for raw in ("", None, "BURN!", "4k", 123, "  "):
        assert parse_subtitle_mode(raw) == "soft", raw
    assert parse_subtitle_mode("burn") == "burn"
    assert parse_subtitle_mode(" SOFT ") == "soft"

    for raw in ("", None, "BURN!", "4k", 0, "  "):
        assert parse_resolution(raw) == "source", raw
    assert parse_resolution("720p") == "720p"
    assert parse_resolution("1080P") == "1080p"


def test_scale_filter_only_shrinks_and_keeps_even_height():
    """宽度取 min(iw, cap) 只缩不放；高度 -2 让 ffmpeg 取偶数（H.264 要求）。"""
    assert scale_filter("source") == ""
    assert scale_filter("720p") == "scale=w='min(iw,720)':h=-2:flags=lanczos"
    assert scale_filter("1080p") == "scale=w='min(iw,1080)':h=-2:flags=lanczos"
    # 非法分辨率等价于 source：不缩放，也不该让参数变脏。
    assert scale_filter("4k") == ""

    assert needs_reencode("soft", "source") is False
    assert needs_reencode("burn", "source") is True
    assert needs_reencode("soft", "720p") is True
    assert needs_reencode("burn", "1080p") is True
    assert needs_reencode("BURN!", "4k") is False


# ---------------------------------------------------------------- 回归锁：默认参数不变
def test_default_mode_keeps_stream_copy_and_soft_subtitle_mapping(tmp_path: Path):
    """默认（soft + source）仍是「视频直通 + 字幕软封装」，一个新开关都不许漏出来。"""
    args = mux_args(
        "ffmpeg",
        tmp_path / "base.mp4",
        tmp_path / "out.mp4",
        subtitles=[{"in": "00:00", "out": "00:02", "text": SUBTITLE_TEXT}],
        srt=tmp_path / "subs.srt",
        total_seconds=2.0,
    )
    pairs = _pairs(args)
    # 无配音、无 BGM 时：base=0、srt=1、静音底轨=2。
    assert ("-map", "1:s") in pairs, "软字幕轨仍要封进容器"
    assert ("-c:s", "mov_text") in pairs
    assert ("-c:v", "copy") in pairs, "默认不重编码"
    assert "-vf" not in args, "默认不该有视频滤镜"
    assert "libx264" not in args
    assert "-movflags" not in args
    # 末尾那条 -t 才是输出时长锁（前面 lavfi 输入也有一个 -t，不能直接 index）。
    assert args[-3:] == ["-t", "2.000", str(tmp_path / "out.mp4")]


# ---------------------------------------------------------------- 烧录
@needs_ffmpeg
@needs_probe
def test_burn_mode_removes_subtitle_stream_and_reencodes_video(tmp_path: Path):
    """burn：字幕进画面，输出不能再有字幕流，视频轨必须是重编码而不是 direct copy。"""
    base = _make_clip(tmp_path / "base.mp4", 1080, 1920)
    srt = _write_srt(tmp_path / "subs.srt")

    soft_out = tmp_path / "soft.mp4"
    soft_args = _mux(base, soft_out, srt)
    assert "-c:s" in soft_args
    assert "subtitle" in _codec_types(soft_out), "软字幕应当作为一条独立字幕轨存在"
    soft_luma = _frame_luma(soft_out)
    assert soft_luma["YMAX"] <= 30, "软字幕不进画面：画面仍是纯黑（YMAX 实测 16）"

    burn_out = tmp_path / "burn.mp4"
    burn_args = _mux(base, burn_out, srt, subtitle_mode="burn")
    assert "subtitle" not in _codec_types(burn_out), "烧录后不该再有一条重复的软字幕轨"
    assert ("-c:v", "copy") not in _pairs(burn_args)
    assert "-c:v" in burn_args and "libx264" in burn_args
    assert "-movflags" in burn_args and "+faststart" in burn_args
    burn_luma = _frame_luma(burn_out)
    assert burn_luma["YMAX"] >= 200, "白字烧进画面后亮度上限应接近 235（实测 243）"
    assert _video_size(burn_out) == (1080, 1920), "burn 不改分辨率"


# ---------------------------------------------------------------- 分辨率
@needs_ffmpeg
@needs_probe
def test_resolution_cap_downscales_and_never_upscales(tmp_path: Path):
    """1080 宽竖屏压到 720 宽；720 宽素材选 1080p 不会被放大（仍 720 宽）。"""
    big = _make_clip(tmp_path / "big.mp4", 1080, 1920)
    small = _make_clip(tmp_path / "small.mp4", 720, 1280)
    srt = _write_srt(tmp_path / "subs.srt")

    to_720 = tmp_path / "to_720.mp4"
    _mux(big, to_720, srt, resolution="720p")
    assert _video_size(to_720) == (720, 1280), "1080x1920 按短边上限缩到 720x1280"

    to_1080 = tmp_path / "to_1080.mp4"
    _mux(small, to_1080, srt, resolution="1080p")
    assert _video_size(to_1080) == (720, 1280), "小素材不放大：宽度仍是 720"

    source = tmp_path / "source.mp4"
    _mux(big, source, srt, resolution="source")
    assert _video_size(source) == (1080, 1920), "source 保持原始分辨率"


# ---------------------------------------------------------------- 两者同时开启
@needs_ffmpeg
@needs_probe
def test_burn_with_720p_applies_both_scale_and_subtitle(tmp_path: Path):
    """burn + 720p：分辨率对、字也真的进画面，不能只有一个生效。

    顺序必须是「先缩放再烧字幕」：反过来字会跟着画面一起缩小，字号看起来小一圈。
    """
    base = _make_clip(tmp_path / "base.mp4", 1080, 1920)
    srt = _write_srt(tmp_path / "subs.srt")
    out = tmp_path / "burn720.mp4"
    args = _mux(base, out, srt, subtitle_mode="burn", resolution="720p")

    vf = args[args.index("-vf") + 1]
    assert vf.index("scale=") < vf.index("subtitles="), "先缩放再烧字幕"
    assert _video_size(out) == (720, 1280)
    assert "subtitle" not in _codec_types(out)
    assert _frame_luma(out)["YMAX"] >= 200, "缩放后字幕仍要画在画面里"


# ---------------------------------------------------------------- 路径转义
def test_subtitle_filter_escapes_windows_drive_colon():
    srt = Path("C:/字幕 目录 with space/subs file.srt")
    assert subtitle_filter(srt) == r"subtitles='C\:/字幕 目录 with space/subs file.srt'"


@needs_ffmpeg
@needs_probe
def test_burn_subtitles_from_path_with_spaces(tmp_path: Path):
    """用本机临时路径验证含空格与中文的字幕文件能烧进画面。"""
    folder = tmp_path / "字幕 目录 with space"
    folder.mkdir()
    srt = _write_srt(folder / "subs file.srt")

    base = _make_clip(tmp_path / "base.mp4", 320, 240)
    out = tmp_path / "spaced.mp4"
    args = _mux(base, out, srt, subtitle_mode="burn")
    assert subtitle_filter(srt) in args[args.index("-vf") + 1]
    assert _frame_luma(out)["YMAX"] >= 200


# ---------------------------------------------------------------- render_film 直通捷径
@needs_ffmpeg
@needs_probe
def test_render_film_bypasses_passthrough_when_reencode_is_needed(tmp_path: Path):
    """没有配音/BGM/字幕时 render_film 原本直接返回 concat 结果（只搬 bytes）。

    这条捷径不会重编码，所以「只改分辨率」的请求一旦踩上去就会被静默忽略；
    需要重编码时必须绕开它。烧字幕时因为有字幕项本来就走 mux，这里一并验证字节确实变了。
    """
    clip = _make_clip(tmp_path / "clip.mp4", 1080, 1920)
    plan = FilmPlan(clips=[clip], total_seconds=2.0)

    passthrough = render_film(plan, ffmpeg=FFMPEG or "ffmpeg")
    passthrough_path = tmp_path / "passthrough.mp4"
    passthrough_path.write_bytes(passthrough)
    assert _video_size(passthrough_path) == (1080, 1920)

    scaled = render_film(plan, ffmpeg=FFMPEG or "ffmpeg", resolution="720p")
    scaled_path = tmp_path / "scaled.mp4"
    scaled_path.write_bytes(scaled)
    assert _video_size(scaled_path) == (720, 1280), "分辨率必须真的生效，不能被直通吞掉"
    assert scaled != passthrough

    plan.subtitles = [{"in": "00:00", "out": "00:02", "text": SUBTITLE_TEXT}]
    burned = render_film(plan, ffmpeg=FFMPEG or "ffmpeg", subtitle_mode="burn")
    burned_path = tmp_path / "burned.mp4"
    burned_path.write_bytes(burned)
    assert burned != clip.read_bytes()
    assert "subtitle" not in _codec_types(burned_path)
    assert _frame_luma(burned_path)["YMAX"] >= 200
