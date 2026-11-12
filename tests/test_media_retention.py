"""素材保留策略：清理必须先尊重引用。

为什么要单独锁这一层：`purgeMedia` 是不可逆的删除动作，而 `purge_older_than` 原本
**只看文件 mtime**。一个用了一个月的老镜头，它的视频文件 mtime 同样很老，于是清理会
把它删掉，而 `shots[].videoUrl` 还指着它。后果不是「少一个文件」而是静默错账：

- `compose.build_plan()` 对拿不到路径的镜头**静默跳过** → 成片悄悄少一段；
- `exportFilm` 才报 `film_missing`，那时已经查不出是哪一次清理导致的。

所以这里锁三件事：
1. 被引用的文件无论多老都不删，且报告里 `kept` 如实计数；
2. 预览与真删给出**同一份**分类结果（预览说 0 个可删，真删也必须 0 个）；
3. 没有被引用的老文件照旧删掉 —— 保护不能变成「什么都不删」。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.media_store import DramaMediaStore
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
TEN_DAYS = 10 * 86400


def _store(tmp_path: Path) -> DramaMediaStore:
    return DramaMediaStore(tmp_path / "media", quota_bytes=0)


def _age(store: DramaMediaStore, asset_id: str, seconds: float) -> None:
    """把文件 mtime 推到过去：清理只按 mtime 判定，这是唯一的输入。"""
    path = store.resolve("local", asset_id)
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_referenced_old_file_is_kept_and_counted(tmp_path: Path) -> None:
    """被引用的老文件必须留下，并被计入 `kept` 而不是 `files`。"""
    store = _store(tmp_path)
    keep = store.store("local", "image", PNG)["asset_id"]
    drop = store.store("local", "image", PNG)["asset_id"]
    _age(store, keep, TEN_DAYS)
    _age(store, drop, TEN_DAYS)

    report = store.purge_older_than("local", 7, keep_ids=frozenset({keep}))

    assert report["files"] == 1, "只有未被引用的那个该删"
    assert report["kept"] == 1, "被引用跳过要如实报出来，否则用户以为清理没生效"
    assert report["asset_ids"] == [drop]
    assert store.stats("local")["files"] == 1
    assert store.resolve("local", keep).exists(), "被引用的文件必须还在"
    with pytest.raises(AppError):
        store.resolve("local", drop)


def test_dry_run_reports_the_same_split_without_deleting(tmp_path: Path) -> None:
    """预览与真删必须是同一份分类：预览说删 1 kept 1，真删也得是 1 / 1。"""
    store = _store(tmp_path)
    keep = store.store("local", "image", PNG)["asset_id"]
    drop = store.store("local", "image", PNG)["asset_id"]
    _age(store, keep, TEN_DAYS)
    _age(store, drop, TEN_DAYS)

    preview = store.purge_older_than("local", 7, dry_run=True, keep_ids=frozenset({keep}))
    assert (preview["files"], preview["kept"]) == (1, 1)
    assert store.stats("local")["files"] == 2, "预览一个文件都不许动"

    real = store.purge_older_than("local", 7, keep_ids=frozenset({keep}))
    assert (real["files"], real["kept"]) == (preview["files"], preview["kept"])
    assert real["freed_bytes"] == preview["freed_bytes"], "预览报的字节数必须是对的"


def test_without_keep_ids_behaviour_is_unchanged(tmp_path: Path) -> None:
    """不传 `keep_ids` 时与改造前完全一致：老文件全删。保护不能变成「什么都不删」。"""
    store = _store(tmp_path)
    for _ in range(2):
        _age(store, store.store("local", "image", PNG)["asset_id"], TEN_DAYS)

    report = store.purge_older_than("local", 7)

    assert report["files"] == 2
    assert report["kept"] == 0
    assert store.stats("local")["files"] == 0


def test_young_files_are_never_victims_even_when_referenced(tmp_path: Path) -> None:
    """新文件本来就不在候选里：`kept` 不该把它们算成「被保护」。"""
    store = _store(tmp_path)
    fresh = store.store("local", "image", PNG)["asset_id"]

    report = store.purge_older_than("local", 7, keep_ids=frozenset({fresh}))

    assert report["files"] == 0
    assert report["kept"] == 0, "只在候选集内统计保护，新文件不算"
    assert store.stats("local")["files"] == 1


def test_negative_days_is_rejected_before_touching_anything(tmp_path: Path) -> None:
    """负数天数必须报错而不是「删全部」：这是不可逆动作的最后一道闸。"""
    store = _store(tmp_path)
    store.store("local", "image", PNG)

    with pytest.raises(AppError) as exc:
        store.purge_older_than("local", -1)

    assert exc.value.code == "invalid_field"
    assert store.stats("local")["files"] == 1


def test_non_hex_named_file_is_deleted_but_not_reported_as_asset(tmp_path: Path) -> None:
    """非 asset_id 命名的文件（如手工放进来的）照样清理，但不进 `asset_ids`。

    这条是既有口径：`asset_ids` 只用于回删 `drama_media` 行，把不认识的名字
    塞进去会让 `forget_media` 误删别人的记录。
    """
    store = _store(tmp_path)
    kept = store.store("local", "image", PNG)["asset_id"]
    _age(store, kept, TEN_DAYS)
    stray = store.root / "local" / "handwritten.png"
    stray.write_bytes(PNG)
    stamp = time.time() - TEN_DAYS
    os.utime(stray, (stamp, stamp))

    report = store.purge_older_than("local", 7, keep_ids=frozenset({kept}))

    assert report["files"] == 1
    assert report["asset_ids"] == [], "不认识的命名不许进 asset_ids"
    assert not stray.exists()


# ---------------------------------------------------------------- 服务层：引用清单


def _service(tmp_path: Path, retention_days: int = 0) -> DramaService:
    """装一份真服务。清理动作要过管理员闸门与 state 读回，只在 store 层测不到。"""
    service = DramaService(
        tmp_path / "retention.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        daily_cap=100000,
        autopump=False,
        retention_days=retention_days,
    )
    service.store.install()
    return service


def _live_url(service: DramaService) -> tuple[str, str]:
    """造一份真实的 local 素材，返回 (asset_id, url)。"""
    made = service.media_store.store("local", "image", PNG)
    return str(made["asset_id"]), str(made["url"])


def test_live_asset_ids_covers_every_reference_point(tmp_path: Path) -> None:
    """引用清单必须覆盖所有会被播放 / 合成读到的落点。

    这是本组最重要的一条：清单漏一个落点，用户点一次「清理历史素材」就在
    毫不知情时丢掉正在用的成片，而且要到 `exportFilm` 才暴露。所以这里把每个
    落点各放一个**互不相同**的素材，逐个断言它在清单里；外加一个外来 URL
    （不是本服务签发的）必须不在清单里——清理只保护自己的文件。
    """
    service = _service(tmp_path)
    try:
        ids = {
            name: _live_url(service)
            for name in (
                "upload",
                "image",
                "video",
                "history",
                "frame",
                "asset",
                "bgm",
                "voice",
                "subtitle",
                "film",
                "export",
            )
        }

        state = {
            "uploads": [{"url": ids["upload"][1]}],
            "projects": [
                {
                    "shots": [
                        {
                            "imageUrl": ids["image"][1],
                            "videoUrl": ids["video"][1],
                            "mediaHistory": [{"url": ids["history"][1], "kind": "video"}],
                        }
                    ],
                    "frames": {"S1": [ids["frame"][1]]},
                    "assets": {"characters": [{"imageUrl": ids["asset"][1]}]},
                    "audio": {
                        "bgm": {"url": ids["bgm"][1]},
                        "voices": [{"audio_url": ids["voice"][1]}],
                        "subtitles": [{"audio_url": ids["subtitle"][1]}],
                    },
                    "film": {"videoUrl": ids["film"][1]},
                    "exports": [{"filmUrl": ids["export"][1]}],
                }
            ],
        }

        live = service._live_asset_ids(state)

        for name, (asset_id, _url) in ids.items():
            assert asset_id in live, f"{name} 落点没有被算进引用清单，清理会误删正在用的文件"
        assert "https://example.com/outside.png" not in live
        # 外来地址不产生 id，不会被当成「保护名单」里的空字符串带进去。
        assert "" not in live
    finally:
        service.store.close()


async def test_purge_keeps_shot_referenced_asset_end_to_end(tmp_path: Path) -> None:
    """端到端：镜头正在用的老素材，真删之后文件仍然在、还能取到。

    只测 store 层证明不了这一步：引用清单是服务层算出来的，中间还隔着
    「动作 -> 管理员闸门 -> 读 state -> 组装 keep_ids」这条链。
    """
    service = _service(tmp_path)
    try:
        asset_id, url = _live_url(service)
        state = await service.state("local")
        shot_id = str(state["projects"][0]["shots"][0]["id"])

        def op(db: Any) -> None:
            db.execute("UPDATE drama_shots SET image_url=? WHERE id=?", (url, shot_id))

        service.store.run(op)
        _age(service.media_store, asset_id, TEN_DAYS)

        report = (await service.action("local", "purgeMedia", [7, False], None, None))["result"]

        assert report["files"] == 0, "被引用的文件不该被算进可删清单"
        assert report["kept"] == 1, "跳过的文件要如实报出来"
        assert service.media_store.resolve("local", asset_id).exists(), "镜头正在用的图必须还在"
        after = await service.state("local")
        shot = next(s for s in after["projects"][0]["shots"] if s["id"] == shot_id)
        assert shot["imageUrl"] == url, "地址与文件必须同时还在"
    finally:
        service.store.close()


async def test_purge_really_deletes_unreferenced_old_asset(tmp_path: Path) -> None:
    """没有被引用的老素材照旧删掉：保护不能变成「什么都不删」。"""
    service = _service(tmp_path)
    try:
        asset_id, _url = _live_url(service)
        _age(service.media_store, asset_id, TEN_DAYS)

        report = (await service.action("local", "purgeMedia", [7, False], None, None))["result"]

        assert report["files"] == 1
        assert report["asset_ids"] == [asset_id]
        # resolve 在文件不存在时抛 404，这条断言同时证明「文件真的没了」与
        # 「URL 换算不再能取到它」，而不是只看目录里少了一个文件名。
        with pytest.raises(AppError) as exc:
            service.media_store.resolve("local", asset_id)
        assert exc.value.code == "asset_missing"
    finally:
        service.store.close()


async def test_service_preview_matches_real_deletion(tmp_path: Path) -> None:
    """服务层预览与真删必须是同一份分类：预览说删 0 kept 1，真删也得是 0 / 1。"""
    service = _service(tmp_path)
    try:
        keep_id, keep_url = _live_url(service)
        state = await service.state("local")
        shot_id = str(state["projects"][0]["shots"][0]["id"])

        def op(db: Any) -> None:
            db.execute("UPDATE drama_shots SET image_url=? WHERE id=?", (keep_url, shot_id))

        service.store.run(op)
        _age(service.media_store, keep_id, TEN_DAYS)
        drop_id, _drop_url = _live_url(service)
        _age(service.media_store, drop_id, TEN_DAYS)

        preview = (await service.action("local", "purgeMedia", [7, None], None, None))["result"]
        assert preview["dry_run"] is True
        assert (preview["files"], preview["kept"]) == (1, 1)
        assert service.media_store.stats("local")["files"] == 2, "预览一个文件都不许动"

        real = (await service.action("local", "purgeMedia", [7, False], None, None))["result"]
        assert (real["files"], real["kept"]) == (preview["files"], preview["kept"])
        # 这两个素材是直接经 media_store 落盘的，没有 drama_media 日志行，
        # 所以 forgotten_rows 是 0 而不是 1——删文件与删日志是两件事，不许混为一谈。
        assert real["forgotten_rows"] == 0
        assert service.media_store.stats("local")["files"] == 1
    finally:
        service.store.close()


async def test_retention_days_default_and_negative_guard(tmp_path: Path) -> None:
    """不传天数时用部署方声明的保留天数；负数一律 400，不许变成「删全部」。"""
    service = _service(tmp_path, retention_days=30)
    try:
        asset_id, _url = _live_url(service)
        _age(service.media_store, asset_id, 10 * 86400)

        # 10 天 > 默认 7 天，但 < 部署方声明的 30 天：不传参数时必须按 30 天算，
        # 所以这个 10 天前的文件不该进候选。
        report = (await service.action("local", "purgeMedia", [], None, None))["result"]
        assert report["dry_run"] is True, "默认必须只预览"
        assert report["files"] == 0, "默认天数应取部署方声明的 30 天，而不是硬编码 7"

        with pytest.raises(AppError) as exc:
            await service.action("local", "purgeMedia", [-1, False], None, None)
        assert exc.value.code == "invalid_field"
        assert service.media_store.resolve("local", asset_id).exists()
    finally:
        service.store.close()
