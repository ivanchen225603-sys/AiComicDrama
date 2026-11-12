"""本机成片合成链：离线示例与真实素材共用这一份实现。

第五项要求「成片合成在离线模式下走与真实模式相同的本机合成链路」，所以不能再把
FFmpeg 逻辑埋在 RemoteDramaProvider 里、mock 另写一套占位。这里只负责：
  build_plan：把项目状态翻译成一份可执行的合成计划（视频轨 / 逐句配音 / BGM / 字幕）
  render_film：按计划跑本机 ffmpeg，产出 MP4 bytes

两个 Provider 都调用同一组函数，差别只在素材来源：离线素材是 sample_media.py
生成的确定性示例文件，真实素材是 Provider 转存的模型产物。

配音对齐口径（写进需求文档，不要含糊）：
  1. 以字幕时间轴为准：每句配音的起始时间 = 该句字幕的 `in`。
  2. 配音比字幕时段长 -> 先 atrim 裁到该字幕的时长，再 adelay 到入点，不侵占下一句。
  3. 配音比字幕时段短 -> 不足部分由静音底轨补齐，不拉伸、不变调。
  4. 没有配音的字幕 -> 保持静音，不阻断合成。
  5. 起始时间超出成片总时长的句子 -> 不进混音，在结果的 hint 里如实列出。
  6. 总时长由「静音底轨 amix=duration=first + 输出 -t」锁定为镜头总时长；
     不能用 -shortest，字幕轨早结束会把整片截短。
  7. 旧数据兼容：若没有任何字幕级配音，才把音色行的试听音频按轨首（0）混入，
     并在结果里标 `alignment=legacy_track_head`，不冒充逐句对齐。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AppError
from .labels import AI_DISCLOSURE
from .sample_media import SAMPLE_HINT, SAMPLE_LABEL

_FFMPEG_MISSING = (
    "本机缺少合成工具，无法合成成片。请安装 FFmpeg 8.x（含 libx264 / mov_text / FreeType）"
    "并确认 ffmpeg、ffprobe 在 PATH 中；离线示例素材另需一个中文字体。"
)

# 字幕呈现方式：软封装（容器里一条可关的字幕轨）或烧录（字幕进画面，播放器关不掉）。
SUBTITLE_MODES: tuple[str, ...] = ("soft", "burn")
# 输出分辨率上限按短边（竖屏就是宽度）算；只缩不放，避免小素材被拉糊。
RESOLUTION_CAPS: dict[str, int] = {"720p": 720, "1080p": 1080}
DEFAULT_SUBTITLE_MODE = "soft"
DEFAULT_RESOLUTION = "source"


def parse_subtitle_mode(raw: Any) -> str:
    """字幕模式归一化。非法值退回 soft：部署方环境变量打错字不该让合成 500。

    大小写与首尾空白在配置侧常被随手写，这里统一归一化后再判定。
    """
    text = str(raw or "").strip().lower()
    return text if text in SUBTITLE_MODES else DEFAULT_SUBTITLE_MODE


def parse_resolution(raw: Any) -> str:
    """分辨率归一化。合法值只有 source 与 RESOLUTION_CAPS 的键，其余退回 source。"""
    text = str(raw or "").strip().lower()
    if text == "source" or text in RESOLUTION_CAPS:
        return text
    return DEFAULT_RESOLUTION


def scale_filter(resolution: str) -> str:
    """返回只缩不放的缩放滤镜串；source 不需要缩放，返回空串。

    宽度取 min(iw, cap) 而不是写死 cap：小素材放大只会变糊。
    高度写 -2 让 ffmpeg 按宽高比算并取偶数（H.264 的 yuv420p 要偶数高度）。
    表达式里的逗号必须用单引号包住，否则会被 filtergraph 当成滤镜链分隔符。
    """
    cap = RESOLUTION_CAPS.get(parse_resolution(resolution))
    if cap is None:
        return ""
    return f"scale=w='min(iw,{cap})':h=-2:flags=lanczos"


def subtitle_filter(srt: Path) -> str:
    """返回把 srt 烧进画面的 ffmpeg 滤镜串。

    filtergraph 里 `:` 是选项分隔符，Windows 盘符的冒号必须转义，且反斜杠路径要
    换成正斜杠；否则 ffmpeg 会把 `C:/...` 盘符后面的部分当成下一个选项。
    实测 ffmpeg 8.1.1：`subtitles='C\\:/dir with space/subs.srt'` 可跑通；
    不转义的 `subtitles=C:/...` 会报 `Unable to parse "original_size" option value`，
    双反斜杠转义同样报这个错，所以这里只做单层转义 + 单引号。
    """
    escaped = srt.as_posix().replace(":", "\\:")
    return f"subtitles='{escaped}'"


def needs_reencode(subtitle_mode: str, resolution: str) -> bool:
    """烧录或改分辨率都必须重编码视频轨；两者都不需要时保持 `-c:v copy`。"""
    return parse_subtitle_mode(subtitle_mode) == "burn" or parse_resolution(resolution) != "source"


def parse_clock(value: Any) -> int:
    """`MM:SS` / `HH:MM:SS` / 秒数 -> 毫秒。无法解析按 0，绝不抛 500。"""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        numbers = [int(part) for part in text.split(":")]
    except ValueError:
        return 0
    while len(numbers) < 3:
        numbers.insert(0, 0)
    hh, mm, ss = numbers[-3], numbers[-2], numbers[-1]
    return max(0, (hh * 3600 + mm * 60 + ss) * 1000)


def srt_time(value: Any) -> str:
    """把前端的 MM:SS / HH:MM:SS 统一成 SRT 的 HH:MM:SS,mmm。"""
    ms = parse_clock(value)
    hh, rest = divmod(ms, 3600000)
    mm, rest = divmod(rest, 60000)
    ss, milli = divmod(rest, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{milli:03d}"


def srt_text(subtitles: list[dict[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(subtitles, start=1):
        start = srt_time(item.get("in") or "0:0")
        end = srt_time(item.get("out") or "0:0")
        if end <= start:
            end = srt_time((parse_clock(item.get("in") or "0:0") + 3000) / 1000)
        blocks.append(f"{index}\n{start} --> {end}\n{str(item.get('text') or '').strip()}\n")
    return "\n".join(blocks)


@dataclass(frozen=True)
class VoiceCue:
    path: Path
    start_ms: int
    limit_ms: int
    label: str = ""


@dataclass
class FilmPlan:
    clips: list[Path] = field(default_factory=list)
    # 每段视频在成片里占多少秒（= 分镜时长）。真实视频最短 5 秒，分镜常是 2~4 秒，
    # 不裁剪的话后面的镜头会被总时长截掉，字幕也对不上。
    clip_seconds: list[float] = field(default_factory=list)
    voices: list[VoiceCue] = field(default_factory=list)
    bgm: Path | None = None
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=dict)
    total_seconds: float = 0.0
    alignment: str = "subtitle"
    skipped: list[str] = field(default_factory=list)

    @property
    def total_ms(self) -> int:
        return int(round(self.total_seconds * 1000))


def build_plan(project: dict[str, Any], media_store: Any, owner: str) -> FilmPlan:
    """把项目状态翻译成合成计划。只认本服务签发的本地素材 URL。"""
    shots = list(project.get("shots") or [])
    total = float(sum(max(0, int(s.get("dur", 0))) for s in shots))
    plan = FilmPlan(total_seconds=total)
    order = [str(x) for x in (project.get("timeline") or {}).get("order", [])]
    by_id = {str(s.get("id")): s for s in shots}
    ordered = [by_id[sid] for sid in order if sid in by_id]
    ordered += [s for s in shots if str(s.get("id")) not in {str(x.get("id")) for x in ordered}]
    for shot in ordered:
        path = media_store.path_for_url(owner, str(shot.get("videoUrl") or ""))
        if path is not None:
            plan.clips.append(path)
            plan.clip_seconds.append(float(max(0, int(shot.get("dur", 0)))))

    audio = project.get("audio") or {}
    subtitles = [
        s
        for s in audio.get("subtitles") or []
        if isinstance(s, dict) and str(s.get("text") or "").strip()
    ]
    plan.subtitles = subtitles
    bgm_cfg = audio.get("bgm") or {}
    plan.levels = {
        "voice": int(bgm_cfg.get("voiceLevel", 100) or 100),
        "bgm": int(bgm_cfg.get("bgmLevel", 25) or 25),
    }
    plan.bgm = media_store.path_for_url(owner, str(bgm_cfg.get("url") or ""))

    total_ms = plan.total_ms
    for sub in subtitles:
        url = str(sub.get("audio_url") or "")
        if not url:
            continue
        path = media_store.path_for_url(owner, url)
        if path is None:
            continue
        start = parse_clock(sub.get("in"))
        end = parse_clock(sub.get("out"))
        if start >= total_ms > 0:
            plan.skipped.append(f"{sub.get('in')} {str(sub.get('text'))[:12]}")
            continue
        window = max(200, (end - start) if end > start else 3000)
        plan.voices.append(
            VoiceCue(
                path=path,
                start_ms=start,
                limit_ms=max(200, min(window, total_ms - start)) if total_ms else window,
                label=str(sub.get("text") or "")[:20],
            )
        )
    if not plan.voices:
        # 旧数据 / 真实 TTS 只落在音色行的兼容路径。不冒充逐句对齐。
        for voice in audio.get("voices") or []:
            path = media_store.path_for_url(owner, str(voice.get("audio_url") or ""))
            if path is not None:
                plan.voices.append(
                    VoiceCue(path, 0, max(1000, total_ms), str(voice.get("character") or ""))
                )
        if plan.voices:
            plan.alignment = "legacy_track_head"
    return plan


def mux_args(
    binary: str,
    base: Path,
    output: Path,
    *,
    voices: list[Any] | None = None,
    bgm: Path | None = None,
    subtitles: list[dict[str, Any]] | None = None,
    levels: dict[str, int] | None = None,
    srt: Path | None = None,
    total_seconds: float = 0.0,
    ai_label: str = "",
    ai_disclosure: str = "",
    sample: bool = False,
    subtitle_mode: str = DEFAULT_SUBTITLE_MODE,
    resolution: str = DEFAULT_RESOLUTION,
) -> list[str]:
    """拼出「视频轨 + 逐句配音/BGM 混音 + 字幕」的 ffmpeg 参数。

    `voices` 既接受 VoiceCue，也接受裸 Path（裸 Path = 轨首对齐，只为兼容旧调用与
    旧数据）。单独成函数是为了在没有 ffmpeg 的机器上也能断言参数形状。

    `subtitle_mode` / `resolution` 取默认值时参数序列与本函数加入这两个开关之前
    完全一致（软字幕软封装 + `-c:v copy`）——线上已有「同参数产出逐字节相同成片」
    的断言，默认路径不能有任何偏移。
    """
    mode = parse_subtitle_mode(subtitle_mode)
    target = parse_resolution(resolution)
    reencode = needs_reencode(mode, target)
    cues: list[VoiceCue] = []
    for item in voices or []:
        if isinstance(item, VoiceCue):
            cues.append(item)
        else:
            cues.append(VoiceCue(Path(item), 0, max(1000, int(total_seconds * 1000) or 3000)))
    levels = levels or {}
    subtitles = subtitles or []
    total = max(0.0, float(total_seconds))
    if total <= 0:
        total = 3.0

    args = [binary, "-y", "-i", str(base)]
    for cue in cues:
        args += ["-i", str(cue.path)]
    bgm_index: int | None = None
    if bgm is not None:
        bgm_index = len(cues) + 1
        args += ["-stream_loop", "-1", "-i", str(bgm)]
    srt_index: int | None = None
    if srt is not None:
        srt_index = len(cues) + (1 if bgm is not None else 0) + 1
        args += ["-i", str(srt)]
    silence_index = len(cues) + (1 if bgm is not None else 0) + (1 if srt is not None else 0) + 1
    args += [
        "-f",
        "lavfi",
        "-t",
        f"{total:.3f}",
        "-i",
        "anullsrc=r=44100:cl=stereo",
    ]

    filters = [
        f"[{silence_index}:a]atrim=end={total:.3f},asetpts=PTS-STARTPTS[bed]",
    ]
    labels = ["[bed]"]
    for index, cue in enumerate(cues):
        volume = max(0.0, min(2.0, levels.get("voice", 100) / 100.0))
        filters.append(
            f"[{index + 1}:a]atrim=end={cue.limit_ms / 1000:.3f},"
            f"asetpts=PTS-STARTPTS,adelay={int(cue.start_ms)}:all=1,"
            f"volume={volume:.2f}[v{index}]"
        )
        labels.append(f"[v{index}]")
    if bgm_index is not None:
        volume = max(0.0, min(2.0, levels.get("bgm", 25) / 100.0))
        filters.append(
            f"[{bgm_index}:a]volume={volume:.2f},atrim=end={total:.3f},asetpts=PTS-STARTPTS[bgml]"
        )
        labels.append("[bgml]")
    filters.append(
        "".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0[aout]"
    )
    args += ["-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]"]
    if not reencode and srt_index is not None:
        args += ["-map", f"{srt_index}:s", "-c:s", "mov_text"]
    if reencode:
        # 顺序必须是「先缩放再烧字幕」：反过来字会跟着画面一起被缩小。
        # 重编码时不能再 -map srt:s：字幕已经进画面，再封一条轨就是重复字幕。
        video_filters: list[str] = []
        scale = scale_filter(target)
        if scale:
            video_filters.append(scale)
        if mode == "burn" and srt is not None:
            video_filters.append(subtitle_filter(srt))
        if video_filters:
            args += ["-vf", ",".join(video_filters)]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "192k"]
    # 披露文案必须与 `/config` 下发的 `ai_disclosure` 是同一个字符串（见 service.config）：
    # 页面说一套、文件里写另一套，等于对用户虚报。留空时回落到 labels.AI_DISCLOSURE。
    disclosure = str(ai_disclosure or AI_DISCLOSURE)
    comment = SAMPLE_HINT if sample else (f"{ai_label} · {disclosure}" if ai_label else "")
    if comment:
        args += ["-metadata", f"comment={comment}"]
        args += ["-metadata", f"description={SAMPLE_LABEL if sample else ai_label}"]
    # 不能用 -shortest：mov_text 字幕轨会在最后一句字幕的 out 处结束，
    # 于是整片被截成「最后一句字幕的结束时间」（实测 22 秒镜头被截成 19 秒）。
    # 音频总长已由静音底轨 amix=duration=first 锁成 total，视频轨 concat 也是
    # total；这里再用 -t 显式锁定输出时长，三条轨都不把成片拉长或截短。
    args += ["-t", f"{total:.3f}", str(output)]
    return args


def _binary(ffmpeg: str) -> str:
    found = shutil.which(ffmpeg)
    if not found:
        raise AppError("drama_ffmpeg", _FFMPEG_MISSING, 503)
    return found


def _run(args: list[str]) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError("drama_ffmpeg", f"ffmpeg failed: {exc}。{_FFMPEG_MISSING}", 503) from exc
    if proc.returncode != 0:
        raise AppError(
            "drama_ffmpeg",
            f"ffmpeg exited {proc.returncode}: {proc.stderr.decode(errors='replace')[:400]}。"
            f"{_FFMPEG_MISSING}",
            503,
        )


def render_film(
    plan: FilmPlan,
    *,
    ffmpeg: str = "ffmpeg",
    ai_label: str = "",
    ai_disclosure: str = "",
    sample: bool = False,
    subtitle_mode: str = DEFAULT_SUBTITLE_MODE,
    resolution: str = DEFAULT_RESOLUTION,
) -> bytes:
    """按计划跑本机 ffmpeg，返回 MP4 bytes。缺工具时明确报错，绝不静默降级。"""
    binary = _binary(ffmpeg)
    mode = parse_subtitle_mode(subtitle_mode)
    target = parse_resolution(resolution)
    reencode = needs_reencode(mode, target)
    if not plan.clips:
        raise AppError("film_no_clips", "没有可合成的视频轨：请先对每个镜头执行「图生视频」", 409)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        listing = folder / "list.txt"
        listing.write_text(
            "".join(
                f"file '{path.as_posix()}'\n"
                + (
                    f"outpoint {plan.clip_seconds[i]:.3f}\n"
                    if i < len(plan.clip_seconds) and plan.clip_seconds[i] > 0
                    else ""
                )
                for i, path in enumerate(plan.clips)
            ),
            encoding="utf-8",
        )
        base = folder / "base.mp4"
        _run(
            [
                binary,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(base),
            ]
        )
        needs_mux = bool(plan.voices or plan.bgm is not None or plan.subtitles)
        disclosure = str(ai_disclosure or AI_DISCLOSURE)
        comment = SAMPLE_HINT if sample else (f"{ai_label} · {disclosure}" if ai_label else "")
        # 重编码（烧字幕 / 改分辨率）必须走 mux 那条路：这条捷径只是搬 bytes，
        # 一旦在这里返回，用户选的烧字幕或分辨率就会被静默丢弃。
        if not needs_mux and not reencode:
            if not comment:
                return base.read_bytes()
            stamped = folder / "stamped.mp4"
            _run(
                [
                    binary,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(base),
                    "-c",
                    "copy",
                    "-metadata",
                    f"comment={comment}",
                    "-metadata",
                    f"description={SAMPLE_LABEL if sample else ai_label}",
                    str(stamped),
                ]
            )
            return stamped.read_bytes()
        srt = folder / "subs.srt"
        if plan.subtitles:
            srt.write_text(srt_text(plan.subtitles), encoding="utf-8")
        output = folder / "out.mp4"
        _run(
            mux_args(
                binary,
                base,
                output,
                voices=plan.voices,
                bgm=plan.bgm,
                subtitles=plan.subtitles,
                levels=plan.levels,
                srt=srt if plan.subtitles else None,
                total_seconds=plan.total_seconds,
                ai_label=ai_label,
                ai_disclosure=ai_disclosure,
                sample=sample,
                subtitle_mode=mode,
                resolution=target,
            )
        )
        if not output.exists() or output.stat().st_size == 0:
            raise AppError("drama_ffmpeg", "ffmpeg produced no output", 503)
        return output.read_bytes()
