"""本地 mock 生成能力：让前端每个按钮都有真实的服务端执行结果，但不调用任何模型。

事实分级（不要改写）：
- 这里全部是**确定性本地规则 / 占位数据**，不是 LLM / TTS / 图生视频 / 渲染结果。
- 每个返回值都带 provider="mock" 与明确的占位说明，前端据此展示，不得写成「模型已生成」。
- 可插拔 Provider 抽象已就位（comicdrama/providers/）：DramaService 的 10 个
  生成动作统一走 DramaProvider，本文件由 MockDramaProvider 逐方法转发；真实实现
  RemoteDramaProvider 只在显式配置 drama_provider=remote 时装配。
  下方 CAPABILITIES 的 switch 值是「按能力切分」的设计命名，实际实现收敛在一个
  RemoteDramaProvider 里，并没有这 6 个独立类。
- 剩下的「最后一公里」= 付费开通 + 填凭据 + 按首次实测核对上游响应形状与计费字段。
  动作签名、状态机与积分口径保持不变。
"""

from __future__ import annotations

import re
from typing import Any

_PROVIDER = "mock"
_DISCLAIMER = "本地规则 / 占位生成，不是模型输出；付费开通真实模型后由 Provider 替换"

# 每个能力对应：前端动作名 -> 依赖的真实模型 -> 真实实现所在位置。
# 注意 `switch` 一律写真实类名：实际实现只有 MockDramaProvider 与
# RemoteDramaProvider 两个，不存在下面这种「按能力切分的 6 个类」——
# 旧版把设计命名单列成类名，读起来像已经有 6 个可切换实现，属于误导。
CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "action": "script-doctor",
        "name": "运行剧本医生",
        "model": "LLM 剧本诊断",
        "switch": "RemoteDramaProvider.script_doctor",
        "cost": "LLM 调用",
    },
    {
        "action": "creative-start",
        "name": "创作三选一「使用」",
        "model": "LLM 剧本扩写",
        "switch": "RemoteDramaProvider.creative_start",
        "cost": "LLM 调用",
    },
    {
        "action": "extract-assets",
        "name": "从剧本抽取",
        "model": "LLM 实体抽取",
        "switch": "RemoteDramaProvider.extract_assets",
        "cost": "LLM 调用",
    },
    {
        "action": "resplit-shots-v2",
        "name": "换一版分镜",
        "model": "LLM 分镜拆分",
        "switch": "RemoteDramaProvider.resplit_shots",
        "cost": "LLM 调用",
    },
    {
        "action": "tts-preview",
        "name": "试听",
        "model": "TTS 语音合成",
        "switch": "RemoteDramaProvider.tts_preview",
        "cost": "TTS 调用",
    },
    {
        "action": "change-voice",
        "name": "换音色",
        # 旧版写「TTS 音色库」是错的：这条走的是 LLM 文本推荐，不调 TTS。
        "model": "LLM 音色推荐（不调用 TTS）",
        "switch": "RemoteDramaProvider.change_voice",
        "cost": "LLM 调用",
    },
    {
        "action": "mark-audio-done",
        "name": "标记完成",
        "model": "无（纯本地状态写入）",
        "switch": "服务端本地动作",
        "cost": "免费",
    },
    {
        "action": "preview-film",
        "name": "预览成片",
        "model": "本地 FFmpeg 合成（非模型）",
        "switch": "RemoteDramaProvider._compose",
        "cost": "免费（本地 CPU）",
    },
    {
        "action": "image-to-video",
        "name": "图生视频",
        "model": "图生视频 / videos[] 节点",
        "switch": "RemoteDramaProvider.image_to_video",
        "cost": "媒体生成",
    },
    {
        "action": "generateAssetCandidates",
        "name": "生成候选锚点图",
        "model": "文生图",
        "switch": "RemoteDramaProvider.asset_candidates",
        "cost": "媒体生成",
    },
)

LAST_MILE: tuple[str, ...] = (
    "付费开通上游真实漫剧模型（LLM 剧本 / TTS 配音 / 图生视频 / 渲染合成）",
    "取得并配置上游凭据环境变量（本功能阶段不读、不存任何真实 key）",
    "实测 LLM 端点是否支持 JSON 输出、核对响应包装形态",
    "实测上游单次真实计费字段（官方「参数说明」表当前仍为「暂无数据」）",
    "实测 videos[] 节点参数、模板 uuid、响应包装形态与 OSS 白名单",
    "把 drama 演示账本接到上游真实账单并做对账（当前是内部计价规则）",
    "按首次付费实测结果核对真实配音 / 视频素材的转存链路（上游 URL 只有 7 天时效）",
)

_VOICE_CANDIDATES = (
    "声线 A · 清冷女中音",
    "声线 B · 低沉男声",
    "声线 C · 中性旁白",
    "声线 D · 少年感",
    "声线 E · 温润女声",
)


def _result(kind: str, label: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": _PROVIDER,
        "kind": kind,
        "label": label,
        "payload": payload,
        "note": _DISCLAIMER,
    }


def script_doctor(layers: dict[str, Any]) -> dict[str, Any]:
    """确定性规则检查：不产生任何「AI 建议」的假象，只做可枚举的结构检查。"""
    body = str(layers.get("body") or "")
    outline = str(layers.get("outline") or "")
    logline = str(layers.get("logline") or "")
    scenes = re.findall(r"【场景\s*([^】]+)】", body)
    lines = [x for x in body.splitlines() if x.strip()]
    dialogue = [x for x in lines if "：" in x and not x.startswith("（")]
    findings: list[str] = []
    if len(logline) < 12:
        findings.append("故事设定少于 12 字，建议补清「谁 / 想要什么 / 障碍」")
    if not scenes:
        findings.append("剧本正文未发现【场景 …】标记，无法按场景拆分分镜")
    if len(dialogue) < 2:
        findings.append("可识别对白少于 2 条，配音与字幕轨会接近空轨")
    if outline and len(outline) < 20:
        findings.append("分集大纲过短，建议按「起 / 承 / 转 / 合」补齐")
    if len(scenes) >= 2:
        findings.append(f"共识别 {len(scenes)} 个场景、{len(dialogue)} 条对白，可按场景边界拆分")
    if not findings:
        findings.append("结构检查未发现明显缺口；本结果只是规则检查，不是模型审美判断")
    return _result(
        "script-doctor",
        "剧本结构检查（本地规则）",
        {
            "scenes": scenes,
            "dialogue_count": len(dialogue),
            "findings": findings,
            "checked_chars": len(body),
        },
    )


def creative_start(kind: str, text: str) -> dict[str, Any]:
    """按入口类型把用户输入套进三层剧本模板；不做任何生成式扩写。"""
    seed = (text or "").strip() or "一封迟到的信，在雨夜被重新送回"
    templates: dict[str, dict[str, str]] = {
        "idea": {
            "logline": f"{seed}。主人公必须在一个夜晚做出决定。",
            "outline": "开场：线索出现；推进：人物回到旧地；对峙：两人隔着距离对话；收束：留一盏灯。",
            "body": f"【场景 1 · 旧地 · 夜 · 雨】\n（旁白）{seed}。\n"
            "【场景 2 · 门廊 · 夜 · 雨】\n甲：你还是来了。\n乙：我收到信了。",
        },
        "novel": {
            "logline": f"改编自长文：{seed[:40]}",
            "outline": "按原文段落顺序切分：第一场建立环境，第二场引入冲突，第三场留出余韵。",
            "body": f"【场景 1 · 环境建立】\n{seed[:120]}\n【场景 2 · 冲突】\n（此处保留原文对白）",
        },
        "script": {
            "logline": "已有剧本，直接以正文作为制作基准。",
            "outline": "以用户提供的剧本正文为准，不自动改写。",
            "body": seed,
        },
    }
    layers = templates.get(kind, templates["idea"])
    return _result(
        "creative-start",
        "创作起点骨架（本地模板）",
        {
            "kind": kind,
            "layers": layers,
            "hint": "模板只是可编辑起点；真实扩写需付费开通 LLM Provider",
        },
    )


def extract_assets(layers: dict[str, Any]) -> dict[str, Any]:
    """从剧本正文里做确定性的实体抽取（正则 + 白名单），不调用 LLM。"""
    body = str(layers.get("body") or "")
    scenes = re.findall(r"【场景\s*\d+\s*·\s*([^·】]+)", body)
    roles = re.findall(r"^([^\s：（）【】]{2,6})：", body, flags=re.MULTILINE)
    props = [w for w in ("信", "信封", "伞", "灯", "钥匙", "照片") if w in body]
    unique_roles = list(dict.fromkeys(roles))
    unique_scenes = list(dict.fromkeys(s.strip() for s in scenes))
    found = {
        "characters": [{"name": r, "prompt": f"角色 {r} 外形锚点（待补）"} for r in unique_roles],
        "scenes": [{"name": s, "prompt": f"场景 {s} 冷调锚点（待补）"} for s in unique_scenes],
        "props": [{"name": p, "prompt": f"道具 {p} 特写锚点（待补）"} for p in props],
    }
    return _result(
        "extract-assets",
        "剧本实体抽取（本地正则）",
        {
            "found": found,
            "counts": {k: len(v) for k, v in found.items()},
            "hint": "抽取结果是候选清单，需要人工确认后才会写入资产库",
        },
    )


def resplit_shots(layers: dict[str, Any]) -> dict[str, Any]:
    """按【场景】边界与对白行确定性拆出分镜草稿；不调用模型，也不自动覆盖已锁定镜头。"""
    body = str(layers.get("body") or "")
    if not body.strip():
        raise ValueError("剧本正文为空，无法拆分分镜")
    scenes = re.split(r"(?=【场景)", body)
    shots: list[dict[str, Any]] = []
    for block in scenes:
        block = block.strip()
        if not block:
            continue
        header = re.match(r"【场景\s*\d+\s*·\s*([^】]+)】", block)
        place = header.group(1).strip() if header else "未标注场景"
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        for line in lines[1:] or [lines[0]]:
            is_dialogue = "：" in line and not line.startswith("（")
            shots.append(
                {
                    "dur": 3 if is_dialogue else 2,
                    "size": "中近景" if is_dialogue else "全景",
                    "desc": f"{place} · {line[:40]}",
                    "prompt": f"{place} {'对白近景' if is_dialogue else '环境镜头'} 冷调 电影感",
                    "line": line if is_dialogue else "",
                    "move": "微推" if is_dialogue else "缓慢横移",
                    "refs": [],
                }
            )
    return _result(
        "resplit-shots",
        "分镜草稿（本地按场景拆分）",
        {
            "shots": shots[:24],
            "count": min(len(shots), 24),
            "hint": "草稿不覆盖已锁定镜头；需人工确认后逐条替换",
        },
    )


def tts_preview(character: str, voice: str, text: str) -> dict[str, Any]:
    return _result(
        "tts-preview",
        f"示例试听 · {character}",
        {
            "character": character,
            "voice": voice,
            "text": text[:80],
            "duration_seconds": round(max(1.0, len(text) * 0.18), 1),
            "audio_url": None,
            "hint": "离线产出确定性示例语音（本机合成音，不是 TTS）；URL 由 Provider 落盘后写回",
        },
    )


def change_voice(character: str, current: str) -> dict[str, Any]:
    candidates = [v for v in _VOICE_CANDIDATES if v != current]
    chosen = candidates[0] if candidates else _VOICE_CANDIDATES[0]
    return _result(
        "change-voice",
        f"音色候选 · {character}",
        {
            "character": character,
            "from": current,
            "to": chosen,
            "candidates": list(_VOICE_CANDIDATES),
            "hint": "候选来自本地固定音色表，不是上游音色库",
        },
    )


def mark_audio_done(
    voices: list[dict[str, Any]], subtitles: list[dict[str, Any]]
) -> dict[str, Any]:
    checked = sum(1 for s in subtitles if s.get("checked"))
    return _result(
        "mark-audio-done",
        "配音与字幕配置完成（本地状态）",
        {
            "voices": len(voices),
            "subtitles": len(subtitles),
            "checked_subtitles": checked,
            "audio_files": 0,
            "hint": "只标记本地配置完成，未生成任何音频轨文件",
        },
    )


def preview_film(project: dict[str, Any]) -> dict[str, Any]:
    shots = project.get("shots") or []
    total = sum(int(s.get("dur", 0)) for s in shots)
    audio = project.get("audio") or {}
    return _result(
        "preview-film",
        "示例成片 · 本机 FFmpeg 合成",
        {
            "name": project.get("name", ""),
            "clips": len(shots),
            "duration_seconds": total,
            "tracks": {
                "video": len(shots),
                "voice": len(audio.get("voices") or []),
                "subtitle": len(audio.get("subtitles") or []),
                "bgm": (audio.get("bgm") or {}).get("name", "未设置"),
            },
            "video_url": None,
            "hint": "离线走与真实模式相同的本机 FFmpeg 合成链；URL 由 Provider 落盘后写回",
        },
    )


def image_to_video(shot: dict[str, Any], size: str) -> dict[str, Any]:
    return _result(
        "image-to-video",
        "示例视频轨 · " + str(shot.get("id", "")),
        {
            "shot_id": shot.get("id"),
            "size": size,
            "duration_seconds": int(shot.get("dur", 3)),
            "video_url": None,
            "cover_url": None,
            "hint": "离线产出确定性静态画面短视频；URL 由 Provider 落盘后写回",
        },
    )


def asset_candidates(asset: dict[str, Any]) -> dict[str, Any]:
    seed = int(asset.get("seed", 1)) + 7
    return _result(
        "asset-candidates",
        "示例锚点图 · " + str(asset.get("name", "")),
        {
            "asset_id": asset.get("id"),
            "seed": seed,
            "count": 1,
            "hint": "离线产出确定性示例图；URL 由 Provider 落盘后写回",
        },
    )


def keyframe(shot: dict[str, Any]) -> dict[str, Any]:
    """关键帧示例图。签名与 mock_media 其它函数一致，URL 由 Provider 落盘后写回。

    `count` 原样回显本次请求的候选张数（真的产出几张由 Provider 决定并写进
    payload.image_urls）：payload 里的数字必须和用户付的钱对得上，不能写死 1。
    """
    return _result(
        "keyframe",
        "示例关键帧 · " + str(shot.get("id", "")),
        {
            "shot_id": shot.get("id"),
            "image_url": None,
            "count": max(1, min(4, int(shot.get("count") or 1))),
            "hint": "离线产出确定性示例关键帧图；URL 由 Provider 落盘后写回",
        },
    )
