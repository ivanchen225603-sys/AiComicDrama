"""关键帧多候选、选帧真正生效、产物历史与回退。

三条口径（全部写死手算，不与自己比）：
1. 候选按张线性计价：单镜预占 = 张数 × cap_of(30)=36，单镜成本 = 张数 × 30。
   上游按张计费，收一张的钱产四张等于平台自己吃下那三张的成本。
2. `pickFrame` 必须真的改变镜头用的那张图（以前只写 task.picked，画面一点没变）。
3. 每次成功把 URL 写回镜头都进产物历史（最新在前，最多 10 条），
   `restoreShotMedia` 只能回到历史里出现过的地址，且恢复本身也记一条。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.pricing import PRICE_T2I, cap_of
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

SEED_BALANCE = 980
T2I_CAP = cap_of(PRICE_T2I)
HISTORY_MAX = 10

# 预置的「上一版产物」地址：用来证明 image-to-video 的封面图不会在已有画面时
# 额外插一条历史，从而让「两条视频 → 两条历史」这条断言成立。
OLD_IMAGE = "/api/v1/drama/assets/00000000000000bb"


def make_service(tmp_path: Path) -> DramaService:
    service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    service.store.install()
    return service


@pytest.fixture
def service(tmp_path: Path) -> Iterator[DramaService]:
    made = make_service(tmp_path)
    try:
        yield made
    finally:
        made.store.close()


async def act(service: DramaService, name: str, *args: Any) -> dict[str, Any]:
    return await service.action("local", name, list(args), None, None)


async def project_of(service: DramaService) -> dict[str, Any]:
    state = await service.state("local")
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


async def shot_of(service: DramaService, shot_id: str) -> dict[str, Any]:
    return next(s for s in (await project_of(service))["shots"] if s["id"] == shot_id)


async def shots(service: DramaService, count: int | None = None) -> list[str]:
    ids = [s["id"] for s in (await project_of(service))["shots"]]
    return ids if count is None else ids[:count]


def give_image(service: DramaService, shot_id: str, url: str = OLD_IMAGE) -> None:
    """直接写库把镜头标成「已有画面」，不动账本、不建任务。"""

    def op(db: Any) -> None:
        db.execute("UPDATE drama_shots SET image_url=? WHERE id=?", (url, shot_id))

    service.store.run(op)


async def settle_one(service: DramaService) -> dict[str, Any]:
    body = await act(service, "tick")
    return body["state"]


# ---------------------------------------------------------------- 7. 四张候选


@pytest.mark.needs_ffmpeg
async def test_four_candidates_charge_four_images(service: DramaService) -> None:
    """candidates=4：预占 144、成本 120，结算后余额 860，产出 4 张互不相同的图。

    手算：980 − 144 = 836（冻结 144）；结算核销 120、退 24 → 860。
    """
    shot_id = (await shots(service, 1))[0]
    body = await act(service, "submitTasks", [shot_id], 4)
    task = body["state"]["tasks"][0]
    assert task["candidates"] == 4
    assert task["reserve"] == 4 * T2I_CAP == 144
    assert task["cost"] == 4 * PRICE_T2I == 120
    assert body["state"]["ledger"]["balance"] == SEED_BALANCE - 144 == 836
    assert body["state"]["ledger"]["frozen"] == 144

    state = await settle_one(service)
    task = state["tasks"][0]
    assert task["state"] == "succeeded"
    assert state["ledger"]["balance"] == SEED_BALANCE - 120 == 860
    assert state["ledger"]["frozen"] == 0

    frames = task["frames"]
    assert len(frames) == 4
    assert len(set(frames)) == 4, "四张候选必须是四个不同地址"
    assert all(str(u).startswith("/api/v1/drama/assets/") for u in frames)
    assert task["picked"] == 0
    shot = await shot_of(service, shot_id)
    assert shot["imageUrl"] == frames[0]


# ---------------------------------------------------------------- 8. 默认一张


@pytest.mark.needs_ffmpeg
async def test_default_candidates_keeps_the_old_single_image_behaviour(
    service: DramaService,
) -> None:
    """不传 / 传 1：预占 36、成本 30、只产出 1 张，余额 950（与改造前一致）。"""
    ids = await shots(service, 2)
    first = await act(service, "submitTasks", [ids[0]])
    second = await act(service, "submitTasks", [ids[1]], 1)
    for body in (first, second):
        task = body["state"]["tasks"][-1]
        assert task["candidates"] == 1
        assert task["reserve"] == T2I_CAP == 36
        assert task["cost"] == PRICE_T2I == 30

    await settle_one(service)
    state = await settle_one(service)
    assert state["ledger"]["balance"] == SEED_BALANCE - 60 == 920
    assert state["ledger"]["frozen"] == 0
    assert [len(t["frames"]) for t in state["tasks"]] == [1, 1]
    assert {t["picked"] for t in state["tasks"]} == {0}


# ---------------------------------------------------------------- 9. 非法张数


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 1),
        ("", 1),
        (0, 1),
        (-3, 1),
        (9, 4),
        ("abc", 1),
        (2.5, 2),
    ],
)
async def test_illegal_candidate_counts_are_clamped_not_rejected(
    tmp_path: Path, raw: Any, expected: int
) -> None:
    """非法张数一律夹到 1..4 且**不抛异常**，并按夹后的张数收钱。"""
    service = make_service(tmp_path)
    try:
        shot_id = (await shots(service, 1))[0]
        body = await act(service, "submitTasks", [shot_id], raw)
        task = body["state"]["tasks"][0]
        assert task["candidates"] == expected
        assert task["reserve"] == expected * T2I_CAP
        assert task["cost"] == expected * PRICE_T2I
        assert body["state"]["ledger"]["frozen"] == expected * T2I_CAP
        assert body["state"]["ledger"]["balance"] == SEED_BALANCE - expected * T2I_CAP
    finally:
        service.store.close()


# ---------------------------------------------------------------- 10. 选帧生效


@pytest.mark.needs_ffmpeg
async def test_pick_frame_really_changes_the_shot_image(service: DramaService) -> None:
    """选第 3 张：task.picked=2 且**镜头用的图跟着换**（这是以前不生效的那条）。"""
    ids = await shots(service, 3)
    body = await act(service, "submitTasks", [ids[0]], 4)
    task_id = body["state"]["tasks"][0]["id"]

    # 未结算不能选：钱还在冻结里，frames 也是空的。
    with pytest.raises(AppError) as pending:
        await act(service, "pickFrame", task_id, 0)
    assert pending.value.code == "task_not_settled"
    assert pending.value.status == 409

    state = await settle_one(service)
    frames = state["tasks"][0]["frames"]
    assert (await shot_of(service, ids[0]))["imageUrl"] == frames[0]

    await act(service, "pickFrame", task_id, 2)
    shot = await shot_of(service, ids[0])
    assert shot["imageUrl"] == frames[2]
    assert shot["imageUrl"] != frames[0]
    assert (await service.state("local"))["tasks"][0]["picked"] == 2

    # 越界 / 任务不存在：都是客户端拿错了参数，必须 4xx 而不是静默夹取。
    with pytest.raises(AppError) as out_of_range:
        await act(service, "pickFrame", task_id, 4)
    assert out_of_range.value.code == "invalid_field"
    assert out_of_range.value.status == 400
    with pytest.raises(AppError) as missing:
        await act(service, "pickFrame", "T-not-a-task", 0)
    assert missing.value.code == "task_missing"
    assert missing.value.status == 404
    assert (await shot_of(service, ids[0]))["imageUrl"] == frames[2], "被拒的选帧不能改画面"


# ---------------------------------------------------------------- 11. 历史与回退


@pytest.mark.needs_ffmpeg
async def test_media_history_records_each_video_and_restore_is_undoable(
    service: DramaService,
) -> None:
    """两次视频 → 两条历史（最新在前）；恢复到旧地址后 videoUrl 变回旧值、历史 3 条。"""
    shot_id = (await shots(service, 1))[0]
    give_image(service, shot_id)

    first = await act(service, "image-to-video", shot_id, "")
    url_a = first["result"]["payload"]["video_url"]
    history = (await shot_of(service, shot_id))["mediaHistory"]
    assert len(history) == 1
    assert history[0]["url"] == url_a
    assert history[0]["kind"] == "video"

    # 改一句提示词再生成：mock 的素材键因此变化，产出另一个地址。
    await act(service, "updateShot", shot_id, {"prompt": "第二版：雨夜的港口"})
    second = await act(service, "image-to-video", shot_id, "")
    url_b = second["result"]["payload"]["video_url"]
    assert url_a != url_b

    shot = await shot_of(service, shot_id)
    assert shot["videoUrl"] == url_b
    assert [h["url"] for h in shot["mediaHistory"]] == [url_b, url_a]
    assert [h["kind"] for h in shot["mediaHistory"]] == ["video", "video"]

    await act(service, "restoreShotMedia", shot_id, url_a)
    shot = await shot_of(service, shot_id)
    assert shot["videoUrl"] == url_a, "恢复必须真的把镜头切回旧产物"
    assert [h["url"] for h in shot["mediaHistory"]] == [url_a, url_b, url_a]
    assert len(shot["mediaHistory"]) == 3, "恢复本身也要记一条，否则不可撤销"

    with pytest.raises(AppError) as rejected:
        await act(service, "restoreShotMedia", shot_id, "/api/v1/drama/assets/deadbeefdeadbeef")
    assert rejected.value.code == "invalid_field"
    assert rejected.value.status == 400
    assert (await shot_of(service, shot_id))["videoUrl"] == url_a


# ---------------------------------------------------------------- 12. 历史上限


@pytest.mark.needs_ffmpeg
async def test_media_history_keeps_only_the_latest_ten(service: DramaService) -> None:
    """超过 10 条只留最新 10 条，最新在前。"""
    shot_id = (await shots(service, 1))[0]
    give_image(service, shot_id)
    first = await act(service, "image-to-video", shot_id, "")
    url_a = first["result"]["payload"]["video_url"]
    await act(service, "updateShot", shot_id, {"prompt": "第二版：雨夜的港口"})
    second = await act(service, "image-to-video", shot_id, "")
    url_b = second["result"]["payload"]["video_url"]

    # 恢复是免费的，用它把历史推到 11 条（2 条视频 + 9 次恢复）。
    for index in range(9):
        await act(service, "restoreShotMedia", shot_id, url_a if index % 2 == 0 else url_b)

    history = (await shot_of(service, shot_id))["mediaHistory"]
    assert len(history) == HISTORY_MAX
    assert history[0]["url"] == url_a, "最新在前：最后一次恢复的是 url_a（index=8）"
    assert {h["url"] for h in history} == {url_a, url_b}
    assert all(h["kind"] == "video" for h in history)


# ---------------------------------------------------------------- 13. 存档往返


@pytest.mark.needs_ffmpeg
async def test_export_import_round_trips_media_history(service: DramaService) -> None:
    """导入导出必须带着产物历史往返，且非法条目整份拒绝（不是静默丢弃）。

    `replace_in` 是唯一的整档重写入口：`_state_in` / `_insert_project` 有一边漏了
    `mediaHistory`，档一存历史就没了——文件还在，回退入口却消失，属静默降级。
    """
    shot_id = (await shots(service, 1))[0]
    give_image(service, shot_id)
    await act(service, "image-to-video", shot_id, "")
    exported = (await shot_of(service, shot_id))["mediaHistory"]
    assert [h["kind"] for h in exported] == ["video"]

    doc = await service.export_doc("local")
    await service.import_doc("local", doc)
    assert (await shot_of(service, shot_id))["mediaHistory"] == exported

    bad = json.loads(json.dumps(doc))
    target = next(p for p in bad["projects"] if p["id"] == doc["activeProjectId"])
    next(s for s in target["shots"] if s["id"] == shot_id)["mediaHistory"] = [
        {"url": "/api/v1/drama/assets/00000000000000cc", "kind": "audio", "at": "11-12 04:00"}
    ]
    with pytest.raises(AppError) as caught:
        await service.import_doc("local", bad)
    assert caught.value.code == "invalid_field"
    assert caught.value.status == 400
    assert (await shot_of(service, shot_id))["mediaHistory"] == exported, "被拒的导入不能改库"
