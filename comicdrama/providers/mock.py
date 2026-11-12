"""离线 Provider：本地规则 + 确定性示例素材，全程不出网、不读凭据、不产生费用。

第五项的关键决定：离线与真实**共用同一套动作、状态机、资金规则、界面和合成链**，
差别只在素材来源。所以这里不再返回 `audio_url=None` / `video_url=None` 的占位说明，
而是真的产文件：
  - 候选锚点图 / 关键帧 / 图生视频 / 配音试听 -> sample_media.py 的确定性示例素材；
  - 成片 -> compose.py 的本机 FFmpeg 合成链（与 RemoteDramaProvider 同一份代码）。

所有媒体结果都带 `sample=True` / `sample_label=「示例，非模型生成」` / provider=mock，
并在画面内、页面上、结果里三处保持一致（见 sample_media.SAMPLE_LABEL）。

`faults` 仍然只在 mock 上生效：一次性故障注入用来复现「上游结果未知 -> 待复核」。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .. import mock_media
from ..errors import AppError
from ..media_store import DramaMediaStore
from ..sample_media import (
    SAMPLE_HINT,
    SAMPLE_LABEL,
    SAMPLE_PROVIDER_TAG,
    dimensions,
    prompt_with_settings,
    sample_audio_bytes,
    sample_image_bytes,
    sample_key,
    sample_video_bytes,
)

# 离线示例按项目比例等比缩到这个长边：画面比例与设定一致，但不必为了演示
# 去编 1080×1920 的十几段视频。真实模式仍用项目设定的原始宽高。
SAMPLE_LONG_EDGE = 720


class MockDramaProvider:
    name = "mock"
    sample = True

    def __init__(
        self,
        faults: str = "",
        media_store: DramaMediaStore | None = None,
        *,
        ffmpeg: str = "ffmpeg",
    ) -> None:
        """faults：逗号分隔的动作名，每个动作只在第一次被调用时抛超时。

        生产路径由 DramaService 注入隔离的 media_store；测试里直接构造时兜底用
        临时目录，保证「离线按钮一定有真实文件」这条性质不依赖调用姿势。
        """
        self._faults = {x.strip(): 1 for x in faults.split(",") if x.strip()}
        self.media_store = media_store or DramaMediaStore(
            Path(tempfile.gettempdir()) / "drama_mock_media"
        )
        self.ffmpeg = ffmpeg

    def attach(self, media_store: DramaMediaStore) -> None:
        """生产路径：把临时目录换成服务给该 owner 隔离的真实留存目录。"""
        self.media_store = media_store

    def _maybe_fault(self, action: str) -> None:
        # getattr 兜底：测试里常有子类重写 __init__ 又不调 super()，
        # 没有 _faults 时应当「不注入故障」而不是崩在 AttributeError 上。
        faults = getattr(self, "_faults", None) or {}
        left = faults.get(action, 0)
        if left:
            faults[action] = left - 1
            raise AppError(
                "drama_generation_timeout",
                f"mock 故障注入：{action} 上游超时（结果未知，预占需保留待复核）",
                504,
            )

    # ------------------------------------------------------------ 示例素材helper
    @staticmethod
    def _sample_size(settings: dict[str, Any]) -> tuple[int, int, tuple[int, int]]:
        full = dimensions(settings)
        scale = SAMPLE_LONG_EDGE / max(full)
        w = max(64, round(full[0] * scale))
        h = max(64, round(full[1] * scale))
        return w - (w % 2), h - (h % 2), full

    def _sample_image(
        self, owner: str, key: str, caption: str, settings: dict[str, Any]
    ) -> dict[str, Any]:
        w, h, full = self._sample_size(settings)
        image = sample_image_bytes(key, width=w, height=h, caption=caption)
        saved = self.media_store.store_sample(owner, "image", image, key)
        return {
            **saved,
            "width": w,
            "height": h,
            "project_width": full[0],
            "project_height": full[1],
        }

    @staticmethod
    def _stamp(item: dict[str, Any]) -> dict[str, Any]:
        """三处标识里「结果」这一处：payload 字段 + note 都与画面/页面同口径。"""
        item["payload"]["sample"] = True
        item["payload"]["sample_label"] = SAMPLE_LABEL
        item["payload"]["sample_provider"] = SAMPLE_PROVIDER_TAG
        item["payload"]["sample_hint"] = SAMPLE_HINT
        item["note"] = SAMPLE_HINT
        return item

    # ------------------------------------------------------------ 10 + 1 个能力
    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self._maybe_fault("script-doctor")
        return mock_media.script_doctor(layers)

    def creative_start(self, kind: str, text: str, *, owner: str) -> dict[str, Any]:
        self._maybe_fault("creative-start")
        return mock_media.creative_start(kind, text)

    def extract_assets(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self._maybe_fault("extract-assets")
        return mock_media.extract_assets(layers)

    def resplit_shots(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self._maybe_fault("resplit-shots-v2")
        return mock_media.resplit_shots(layers)

    def tts_preview(self, character: str, voice: str, text: str, *, owner: str) -> dict[str, Any]:
        self._maybe_fault("tts-preview")
        item = mock_media.tts_preview(character, voice, text)
        seconds = float(item["payload"]["duration_seconds"])
        key = sample_key("tts", character, voice, text, seconds)
        audio = sample_audio_bytes(key, seconds=seconds)
        saved = self.media_store.store_sample(owner, "audio", audio, key)
        item["payload"].update(
            {
                "audio_url": saved["url"],
                "duration_seconds": seconds,
                "assets": [saved],
                "sample_key": key,
            }
        )
        return self._stamp(item)

    def change_voice(self, character: str, current: str, *, owner: str) -> dict[str, Any]:
        self._maybe_fault("change-voice")
        return mock_media.change_voice(character, current)

    def mark_audio_done(
        self,
        voices: list[dict[str, Any]],
        subtitles: list[dict[str, Any]],
        *,
        owner: str,
    ) -> dict[str, Any]:
        self._maybe_fault("mark-audio-done")
        item = mock_media.mark_audio_done(voices, subtitles)
        # 示例语音是真实文件：按「已挂上音频 URL 的字幕句 + 音色行」统计，不再写死 0。
        item["payload"]["audio_files"] = sum(1 for s in subtitles if s.get("audio_url")) + sum(
            1 for v in voices if v.get("audio_url")
        )
        return item

    def preview_film(self, project: dict[str, Any], *, owner: str) -> dict[str, Any]:
        from ..compose import (  # 延迟导入，避开 providers 包初始化环
            build_plan,
            parse_resolution,
            parse_subtitle_mode,
            render_film,
        )

        self._maybe_fault("preview-film")
        item = mock_media.preview_film(project)
        plan = build_plan(project, self.media_store, owner)
        if not plan.clips:
            item["payload"]["hint"] = (
                "尚无可合成的视频轨：请先对每个镜头执行「图生视频」；"
                "离线示例成片同样走本机 FFmpeg，不会静默退回占位"
            )
            return self._stamp(item)
        # 交付选项读**项目**而不是部署配置：同一套部署里不同集可能要交付不同规格。
        # 非法值在 parse_* 里退回软字幕 / 不缩放，不会让一次合成 500。
        mode = parse_subtitle_mode(project.get("subtitleMode"))
        resolution = parse_resolution(project.get("outputResolution"))
        data = render_film(
            plan, ffmpeg=self.ffmpeg, subtitle_mode=mode, resolution=resolution, sample=True
        )
        key = sample_key(
            "film",
            project.get("id"),
            [str(c.name) for c in plan.clips],
            [str(v.path.name) + str(v.start_ms) for v in plan.voices],
            plan.bgm.name if plan.bgm else "",
            plan.total_seconds,
            # 交付选项必须进确定性键：否则「软字幕合成过、再切硬字幕」会命中
            # 上一份软字幕成片的缓存地址，用户看到的是没烧字幕的旧文件。
            mode,
            resolution,
        )
        saved = self.media_store.store_sample(owner, "video", data, key)
        item["payload"].update(
            {
                "video_url": saved["url"],
                "assets": [saved],
                "duration_seconds": plan.total_seconds,
                "composed_clips": len(plan.clips),
                "muxed": {
                    "voice_tracks": len(plan.voices),
                    "bgm": plan.bgm is not None,
                    "subtitles": len(plan.subtitles),
                },
                "alignment": plan.alignment,
                "voice_alignment": [
                    {
                        "text": v.label,
                        "start_ms": v.start_ms,
                        "limit_ms": v.limit_ms,
                    }
                    for v in plan.voices
                ],
                "skipped_voices": plan.skipped,
                "hint": "视频轨 concat + 逐句配音 adelay/atrim 对齐字幕 + BGM 混音 + "
                "字幕 mov_text 软封装；与真实模式共用 compose.py，示例素材非模型生成",
            }
        )
        return self._stamp(item)

    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        self._maybe_fault("image-to-video")
        item = mock_media.image_to_video(shot, size)
        settings = dict(shot.get("project_settings") or {})
        prompt = prompt_with_settings(
            str(shot.get("prompt") or shot.get("desc") or "电影感镜头"), settings
        )
        w, h, full = self._sample_size(settings)
        key = sample_key(
            "i2v",
            shot.get("id"),
            shot.get("dur"),
            size,
            prompt,
            shot.get("first_frame_asset_id"),
            len(first_frame) if first_frame else 0,
        )
        image = sample_image_bytes(key, width=w, height=h, caption=prompt[:60])
        cover = self.media_store.store_sample(owner, "image", image, key + "-cover")
        video = sample_video_bytes(
            key,
            seconds=float(int(shot.get("dur", 3) or 3)),
            width=w,
            height=h,
            caption=prompt[:60],
            image=image,
        )
        saved = self.media_store.store_sample(owner, "video", video, key)
        item["payload"].update(
            {
                "video_url": saved["url"],
                "cover_url": cover["url"],
                "duration_seconds": int(shot.get("dur", 3) or 3),
                "first_frame_bytes": len(first_frame) if first_frame else 0,
                "project_settings": settings,
                "width": w,
                "height": h,
                "project_width": full[0],
                "project_height": full[1],
                "prompt": prompt,
                "anchor_refs": [str(x) for x in (shot.get("anchor_refs") or [])],
                "anchor_asset_id": str(shot.get("anchor_asset_id") or ""),
                "first_frame_asset_id": str(shot.get("first_frame_asset_id") or ""),
                "assets": [saved, cover],
            }
        )
        return self._stamp(item)

    def asset_candidates(self, asset: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self._maybe_fault("generateAssetCandidates")
        item = mock_media.asset_candidates(asset)
        settings = dict(asset.get("project_settings") or {})
        prompt = prompt_with_settings(
            str(asset.get("prompt") or f"角色/场景锚点：{asset.get('name', '')}"), settings
        )
        key = sample_key(
            "anchor", asset.get("id"), asset.get("name"), item["payload"]["seed"], prompt
        )
        still = self._sample_image(owner, key, prompt[:60], settings)
        item["payload"].update(
            {
                "image_url": still["url"],
                "assets": [still],
                "project_settings": settings,
                "width": still["width"],
                "height": still["height"],
                "project_width": still["project_width"],
                "project_height": still["project_height"],
                "prompt": prompt,
                "sample_key": key,
            }
        )
        return self._stamp(item)

    def keyframe(
        self, shot: dict[str, Any], *, owner: str, first_frame: bytes | None = None
    ) -> dict[str, Any]:
        """离线关键帧：确定性示例图，参数与真实 t2i 完全同形。

        多候选（`shot["count"]`）时产出多张**互不相同**的确定性示例图：每张在同
        一组输入后面追加候选序号再派生种子，所以同一镜头同一批候选永远得到同样的
        地址（测试可以直接断言），而不同序号之间颜色/文字必然不同。
        count == 1 时不做任何额外派生，URL 与改造前逐字节一致。
        """
        self._maybe_fault("keyframe")
        item = mock_media.keyframe(shot)
        settings = dict(shot.get("project_settings") or {})
        prompt = prompt_with_settings(
            str(shot.get("prompt") or shot.get("desc") or "电影感关键帧"), settings
        )
        anchor = str(shot.get("anchor_asset_id") or "")
        count = max(1, min(4, int(shot.get("count") or 1)))
        base_key = sample_key(
            "keyframe",
            shot.get("id"),
            prompt,
            anchor,
            [str(x) for x in (shot.get("anchor_refs") or [])],
            shot.get("dur"),
        )
        stills = [
            self._sample_image(
                owner,
                base_key if count == 1 else sample_key(base_key, "candidate", index),
                prompt[:60],
                settings,
            )
            for index in range(count)
        ]
        first = stills[0]
        item["payload"].update(
            {
                # image_url 保留为第一张：向后兼容（老前端 / 老测试只读这一个字段）。
                "image_url": first["url"],
                "image_urls": [x["url"] for x in stills],
                "assets": stills,
                "prompt": prompt,
                "project_settings": settings,
                "width": first["width"],
                "height": first["height"],
                "project_width": first["project_width"],
                "project_height": first["project_height"],
                "anchor_refs": [str(x) for x in (shot.get("anchor_refs") or [])],
                "anchor_asset_id": anchor,
                "first_frame_asset_id": str(shot.get("first_frame_asset_id") or ""),
                "first_frame_bytes": len(first_frame) if first_frame else 0,
                "reference_mode": "sample_local",
                "sample_key": base_key,
            }
        )
        return self._stamp(item)
