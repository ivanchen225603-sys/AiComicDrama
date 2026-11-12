"""按能力组合的 Provider：用户填了哪类密钥，哪类能力就走真实模型，其余回落到内置实现。

- 图片（形象图、关键帧）与视频、成片合成：用户填了 Liblib 密钥 → 真实 Provider。
- 文本（剧本生成 / 诊断 / 提取角色 / 分镜 / 音色推荐）：填了文本模型 → 真实 LLM；
  没填 → 内置规则（不报错，保证整条流程能走完）。
- 配音：部署方配置了 TTS 端点才走真实配音，否则用内置示例语音。

name 固定为 "remote"：只要接了真实图片 / 视频，就按真实模式计费与标识
（单日上限、AI 生成标识、导出不再是示例）。
"""

from __future__ import annotations

from typing import Any

from ..errors import AppError


class HybridDramaProvider:
    name = "remote"

    def __init__(self, media: Any, fallback: Any, *, text: Any = None, tts: Any = None) -> None:
        self.media = media
        self.fallback = fallback
        self.text = text
        self.tts = tts

    @property
    def channels(self) -> dict[str, str]:
        base = dict(getattr(self.media, "channels", {}) or {})
        base["text"] = "llm" if self.text is not None else "builtin"
        base["voice"] = "tts" if self.tts is not None else "builtin"
        return base

    def _text(self) -> Any:
        return self.text if self.text is not None else self.fallback

    # ---- 文本 ----
    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        return self._text().script_doctor(layers, owner=owner)

    def creative_start(self, kind: str, text: str, *, owner: str) -> dict[str, Any]:
        return self._text().creative_start(kind, text, owner=owner)

    def extract_assets(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        return self._text().extract_assets(layers, owner=owner)

    def resplit_shots(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        return self._text().resplit_shots(layers, owner=owner)

    def change_voice(self, character: str, current: str, *, owner: str) -> dict[str, Any]:
        return self._text().change_voice(character, current, owner=owner)

    # ---- 配音 ----
    def tts_preview(self, character: str, voice: str, text: str, *, owner: str) -> dict[str, Any]:
        if self.tts is None:
            # 真实模式下不再用示例提示音冒充配音：明确告知，成片只带字幕与背景音乐。
            raise AppError("drama_tts_config", "未配置配音服务", 503)
        return self.tts.tts_preview(character, voice, text, owner=owner)

    def mark_audio_done(
        self, voices: list[dict[str, Any]], subtitles: list[dict[str, Any]], *, owner: str
    ) -> dict[str, Any]:
        return self.media.mark_audio_done(voices, subtitles, owner=owner)

    # ---- 图片 / 视频 / 成片 ----
    def preview_film(self, project: dict[str, Any], *, owner: str) -> dict[str, Any]:
        return self.media.preview_film(project, owner=owner)

    def image_to_video(
        self, shot: dict[str, Any], size: str, *, owner: str, first_frame: bytes | None = None
    ) -> dict[str, Any]:
        return self.media.image_to_video(shot, size, owner=owner, first_frame=first_frame)

    def asset_candidates(self, asset: dict[str, Any], *, owner: str) -> dict[str, Any]:
        return self.media.asset_candidates(asset, owner=owner)

    def keyframe(
        self, shot: dict[str, Any], *, owner: str, first_frame: bytes | None = None
    ) -> dict[str, Any]:
        return self.media.keyframe(shot, owner=owner, first_frame=first_frame)

    def __getattr__(self, name: str) -> Any:
        # 其他可选能力（如 keyframe 的多候选参数探测）沿用真实 Provider 的实现。
        return getattr(self.media, name)
