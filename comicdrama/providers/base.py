"""漫剧生成能力的 Provider 契约。

本模块只定义接口与结果形状，不含任何网络代码、不读取凭据。
10 个方法一一对应 mock_media.py 的 10 个确定性函数；返回体统一为
{"provider", "kind", "label", "payload", "note"}，前端与状态机按同一形状消费。

重要边界：这些方法是**同步**的。DramaService 会把调用安排在 SQLite 写事务之外
（见 service.py::_action_sync），因此真实适配器可以出网、轮询、等待数分钟，
而不会把 BEGIN IMMEDIATE 的写锁一并占住。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..labels import AI_DISCLOSURE, DEFAULT_AI_LABEL
from ..sample_media import SAMPLE_HINT, SAMPLE_LABEL

# ---------------------------------------------------------------- 合规标识
# 常量的唯一来源在 ../labels.py 与 ../sample_media.py；这里重新导出，
# 让既有的 `from .providers.base import AI_DISCLOSURE` 继续可用。
# 技术标识 ≠ 画面水印，也 ≠ 备案已完成；法务与备案口径仍需业务确认。
__all__ = [
    "AI_DISCLOSURE",
    "DEFAULT_AI_LABEL",
    "GENERATING_ACTIONS",
    "SAMPLE_HINT",
    "SAMPLE_LABEL",
    "DramaProvider",
    "result",
]

# 会触发生成（可能出网）的动作名。service.py 据此决定是否需要在事务外准备。
GENERATING_ACTIONS: frozenset[str] = frozenset(
    {
        "script-doctor",
        "creative-start",
        "extract-assets",
        "resplit-shots-v2",
        "tts-preview",
        "change-voice",
        "mark-audio-done",
        "preview-film",
        "image-to-video",
        "generateAssetCandidates",
    }
)


class DramaProvider(Protocol):
    """10 个生成能力的同步契约。

    name 用于 /api/v1/drama/config 的 provider 标记与前端事实分级；
    "mock" 是唯一离线实现，其余实现必须显式配置才被装配。
    """

    name: str

    # owner 是服务端 principal，用于把真实素材转存到该 owner 的隔离目录；
    # provider 绝不采信请求体里的 owner。离线 mock 忽略此参数。
    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]: ...
    def creative_start(self, kind: str, text: str, *, owner: str) -> dict[str, Any]: ...
    def extract_assets(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]: ...
    def resplit_shots(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]: ...
    def tts_preview(
        self, character: str, voice: str, text: str, *, owner: str
    ) -> dict[str, Any]: ...
    def change_voice(self, character: str, current: str, *, owner: str) -> dict[str, Any]: ...
    def mark_audio_done(
        self,
        voices: list[dict[str, Any]],
        subtitles: list[dict[str, Any]],
        *,
        owner: str,
    ) -> dict[str, Any]: ...
    def preview_film(self, project: dict[str, Any], *, owner: str) -> dict[str, Any]: ...
    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]: ...
    def asset_candidates(self, asset: dict[str, Any], *, owner: str) -> dict[str, Any]: ...
    def keyframe(
        self, shot: dict[str, Any], *, owner: str, first_frame: bytes | None = None
    ) -> dict[str, Any]:
        """给一个镜头出关键帧图，返回 payload.image_urls（按顺序）+ payload.image_url。

        真实实现走文生图；离线实现走确定性示例图。两者都必须返回本服务签发的
        本地素材 URL，由 service 写回镜头。调用被安排在 owner 锁之外（两段式），
        因此可以出网数分钟而不阻塞同一用户保存剧本。

        多候选：`shot["count"]` 是本次要几张（1..4，由 service 从任务行带下来）。
        `payload["image_urls"]` 是**本次真正产出的全部候选**，`payload["image_url"]`
        始终等于第一张——保留它是为了向后兼容（老调用方与老测试只读这一个字段），
        绝不能因为多了 image_urls 就把 image_url 删掉或置空。
        """
        ...


def result(
    provider: str,
    kind: str,
    label: str,
    payload: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    """真实适配器也必须用这个形状，否则前端与 _media 落库会错位。"""
    return {
        "provider": provider,
        "kind": kind,
        "label": label,
        "payload": payload,
        "note": note,
    }
