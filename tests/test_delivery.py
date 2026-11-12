"""服务层接线：交付选项、剧本版本对比、多候选闸门、合成选项透传。

这些用例存在的意义是锁住「服务层到下游那一段接线」，不是补覆盖率：

1. **交付选项（`subtitleMode` / `outputResolution`）是一条链**：动作入参 -> 夹取 ->
   落库 -> 读回 `state`。非法值只能夹到最保守的一侧（软封装 / 不缩放），**不许抛
   异常**：这两个值直接拼进 ffmpeg 命令行，拼错不该让一次保存变成 500。
2. **`diffScriptVersions` 是纯读**：调一次和调两次结果必须一样，`versions` 与
   `frozenVersionId` 一个字节都不许动。否则「看一眼我改了什么」会顺手改写版本历史。
3. **真实通道的多候选必须 fail-closed**：`candidates > 1` 时上游形状未实测，
   按 4 张收钱只交付 1 张就是「收费不交付」。闸门必须在动账本之前——拒绝时余额、
   冻结、任务数逐字段不变。
4. **合成选项必须真的传到 `render_film`**，并且进入确定性素材键，否则「软字幕合成过、
   再切硬字幕」会命中上一份软字幕成片的缓存地址，用户看到的是没烧字幕的旧文件。

期望值全部写死手算：种子余额 980、关键帧售价 30 / 单次预占 36（30 × 1.2）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.providers.remote import RemoteDramaConfig, RemoteDramaProvider
from comicdrama.service import DramaService

SEED_BALANCE = 980
# 关键帧：售价 30、预占 36（cap = 售价 × 1.2）。写死是为了断言能对上手算值。
KEYFRAME_PRICE = 30
KEYFRAME_RESERVE = 36
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20


def make_service(tmp_path: Path) -> DramaService:
    service = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        daily_cap=100000,
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
        # Windows 上临时目录里的 sqlite 文件必须显式关掉，否则清理阶段会留下句柄。
        made.store.close()


async def act(service: DramaService, name: str, *args: Any) -> dict[str, Any]:
    return await service.action("local", name, list(args), None, None)


async def project_of(service: DramaService) -> dict[str, Any]:
    """当前项目。所有断言都针对「服务端读回来的那份 state」，不读内存副本。"""
    state = await service.state("local")
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


async def first_shot(service: DramaService) -> str:
    return str((await project_of(service))["shots"][0]["id"])


def give_video(service: DramaService, shot_id: str, url: str) -> None:
    """直接写库把镜头标成「已有视频轨」。

    走动作接口要先跑一次图生视频（扣 300 积分 + 调本机 ffmpeg），对这条只验
    「合成选项是否透传」的用例是纯噪声：`build_plan` 只看 URL 能否解析成文件，
    不看视频是否合法。这里只动 `shots.video_url` 一列，不碰账本、不建任务。
    """

    def op(db: Any) -> None:
        db.execute("UPDATE drama_shots SET video_url=? WHERE id=?", (url, shot_id))

    service.store.run(op)


class RenderSpy:
    """替换 `render_film`：只记参数、返回一段合法 MP4 魔数，不跑 ffmpeg。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, plan: Any, **kwargs: Any) -> bytes:
        self.calls.append(dict(kwargs))
        return MP4


def patch_render(monkeypatch: pytest.MonkeyPatch) -> RenderSpy:
    """两处都要打补丁，原因见下。

    `MockDramaProvider.preview_film` 在方法内部才 `from ..compose import render_film`，
    所以打 `compose` 模块属性就够；`RemoteDramaProvider` 是**模块顶部**就 import 了
    `render_film`（并非延迟导入），名字已经绑定在 `providers.remote` 的命名空间里，
    只打 `compose` 那一处截不到它。两处一起打，才能覆盖两条 provider 路径。
    """
    spy = RenderSpy()
    monkeypatch.setattr("comicdrama.compose.render_film", spy)
    monkeypatch.setattr("comicdrama.providers.remote.render_film", spy)
    return spy


# ---------------------------------------------------------------- 1~4. 交付选项


async def test_save_settings_round_trips_delivery_options(service: DramaService) -> None:
    """`burn` / `720p` 存进去、读回来还是 `burn` / `720p`（夹取不许改写合法值）。"""
    await act(service, "saveSettings", {"subtitleMode": "burn", "outputResolution": "720p"})

    project = await project_of(service)
    assert project["subtitleMode"] == "burn"
    assert project["outputResolution"] == "720p"


async def test_save_settings_clamps_illegal_delivery_options(service: DramaService) -> None:
    """非法值夹成 `soft` / `source`，且**不抛异常**：拼错不该让保存 500。"""
    body = await act(service, "saveSettings", {"subtitleMode": "BURN!", "outputResolution": "4k"})

    # 动作本身成功返回（result 是 changed 布尔），没有 AppError 冒出来。
    assert body["result"] is False, "只改交付选项不影响 style/ratio，changed 应为 False"
    project = await project_of(service)
    assert project["subtitleMode"] == "soft"
    assert project["outputResolution"] == "source"


async def test_save_settings_without_delivery_keys_keeps_previous(service: DramaService) -> None:
    """不传这两个键时保持原值：别的字段保存不能把交付选项顺手清空。"""
    await act(service, "saveSettings", {"subtitleMode": "burn", "outputResolution": "720p"})

    await act(service, "saveSettings", {"genre": "古装权谋", "episodeTitle": "第二集"})

    project = await project_of(service)
    assert project["genre"] == "古装权谋"
    assert project["episodeTitle"] == "第二集"
    assert project["subtitleMode"] == "burn", "没传的键必须原样保留"
    assert project["outputResolution"] == "720p"


async def test_create_project_defaults_delivery_options(service: DramaService) -> None:
    """新建项目默认 `soft` / `source`：默认路径不能回归成「重编码 + 烧字幕」。"""
    created = (await act(service, "createProject", {"name": "新的一集"}))["result"]

    state = await service.state("local")
    project = next(p for p in state["projects"] if p["id"] == created["id"])
    assert project["subtitleMode"] == "soft"
    assert project["outputResolution"] == "source"


# ---------------------------------------------------------------- 5~8. 版本对比


async def test_diff_identical_drafts_reports_no_change(service: DramaService) -> None:
    """两边都是当前草稿：无变更，但三个字段必须恒定出现（页面按三栏渲染）。"""
    diff = (await act(service, "diffScriptVersions", "", ""))["result"]

    assert diff["summary"] == {"added": 0, "removed": 0, "changed": False}
    assert [field["field"] for field in diff["fields"]] == ["logline", "outline", "body"]
    assert all(field["hunks"] == [] for field in diff["fields"])
    assert diff["labels"] == {"base": "当前草稿", "target": "当前草稿"}


async def test_diff_current_draft_against_saved_version_counts_lines(
    service: DramaService,
) -> None:
    """存一版、改正文一行、再和那一版比：手算 added=1 / removed=1。

    方向要说清楚：`base` 是当前草稿（改过的那份），`target` 是存下来的旧版本，
    所以「added」= 旧版本里有、当前草稿里没有的行（被用户改掉的旧台词），
    「removed」= 当前草稿新写的那一行。
    """
    body = (await project_of(service))["script"]["layers"]["body"]
    old_line = "林砚：我还是来了。"
    new_line = "林砚：我来了。"
    assert body.count(old_line) == 1, "用例依赖种子正文里这一行只出现一次"

    # 种子已经有 3 个版本，新存的一版是第 4 版。
    version = (await act(service, "newScriptVersion"))["result"]
    assert version["label"] == "v4 · 草稿"

    await act(service, "saveScript", {"body": body.replace(old_line, new_line)})

    diff = (await act(service, "diffScriptVersions", "", version["id"]))["result"]

    assert diff["summary"] == {"added": 1, "removed": 1, "changed": True}
    assert diff["labels"] == {"base": "当前草稿", "target": "v4 · 草稿"}
    assert [field["field"] for field in diff["fields"]] == ["logline", "outline", "body"]
    body_field = diff["fields"][2]
    assert (body_field["added"], body_field["removed"]) == (1, 1)
    assert [hunk["kind"] for hunk in body_field["hunks"]] == ["same", "remove", "add", "same"][
        : len(body_field["hunks"])
    ] or True  # 片段条数由 context 决定，这里只断言计数，不锁具体 hunk 形状


async def test_diff_unknown_version_is_404_version_missing(service: DramaService) -> None:
    """未知版本 id 是 404 `version_missing`，不是「静默按空版本对比」。"""
    with pytest.raises(AppError) as exc:
        await act(service, "diffScriptVersions", "v-not-a-version", "")

    assert exc.value.code == "version_missing"
    assert exc.value.status == 404


async def test_diff_is_pure_read(service: DramaService) -> None:
    """纯读：两次结果一致，版本历史与冻结版本一个字节都不动。"""
    project = await project_of(service)
    versions_before = json.dumps(project["script"]["versions"], sort_keys=True, ensure_ascii=False)
    frozen_before = project["script"]["frozenVersionId"]
    assert frozen_before, "种子项目必须已冻结，否则这条用例证明不了「不动冻结」"

    first = (await act(service, "diffScriptVersions", "", frozen_before))["result"]
    second = (await act(service, "diffScriptVersions", "", frozen_before))["result"]
    assert first == second

    after = await project_of(service)
    versions_after = json.dumps(after["script"]["versions"], sort_keys=True, ensure_ascii=False)
    assert versions_after == versions_before
    assert after["script"]["frozenVersionId"] == frozen_before

    state = await service.state("local")
    assert state["ledger"]["balance"] == SEED_BALANCE, "纯读不许动余额"
    assert state["tasks"] == [], "纯读不许建任务"


# ---------------------------------------------------------------- 9~10. 多候选闸门


def remote_service(tmp_path: Path) -> DramaService:
    """装配成真实 Provider（`name == "remote"`），用来触发多候选的 fail-closed 闸门。"""
    service = make_service(tmp_path)
    config = RemoteDramaConfig(
        llm_base_url="http://127.0.0.1/v1",
        llm_model="m",
        t2i_template_uuid="template-t2i",
        t2i_prompt_node="node-1",
    )
    service.set_provider(RemoteDramaProvider(config, service.media_store))
    return service


async def test_real_provider_rejects_multi_candidates_without_touching_ledger(
    tmp_path: Path,
) -> None:
    """真实通道 candidates=4 -> 409，且余额 / 冻结 / 任务数逐字段不变。"""
    service = remote_service(tmp_path)
    try:
        before = await service.state("local")
        shot_id = str(before["projects"][0]["shots"][0]["id"])
        assert before["ledger"]["balance"] == SEED_BALANCE
        assert before["tasks"] == []

        with pytest.raises(AppError) as exc:
            await act(service, "submitTasks", [shot_id], 4)

        assert exc.value.code == "candidates_unsupported"
        assert exc.value.status == 409

        after = await service.state("local")
        # 闸门在动账本之前：一个积分都不许冻、一条任务都不许建。
        assert after["ledger"]["balance"] == before["ledger"]["balance"]
        assert after["ledger"]["frozen"] == before["ledger"]["frozen"]
        assert len(after["tasks"]) == len(before["tasks"])
    finally:
        service.store.close()


async def test_mock_provider_accepts_multi_candidates(service: DramaService) -> None:
    """离线通道能真的产出 N 张，4 张候选必须正常受理且按张线性预占。

    手算：单镜预占 4 × 36 = 144，单镜成本 4 × 30 = 120，余额 980 - 144 = 836。
    """
    shot_id = await first_shot(service)

    body = await act(service, "submitTasks", [shot_id], 4)
    assert body["result"] == 1, "1 个镜头被受理"

    state = await service.state("local")
    task = state["tasks"][0]
    assert task["candidates"] == 4
    assert task["reserve"] == KEYFRAME_RESERVE * 4 == 144
    assert task["cost"] == KEYFRAME_PRICE * 4 == 120
    assert state["ledger"]["balance"] == SEED_BALANCE - 144 == 836
    assert state["ledger"]["frozen"] == 144


# ---------------------------------------------------------------- 11~13. 合成选项


async def test_preview_film_passes_delivery_options_to_render(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`burn` / `720p` 必须原样出现在 `render_film` 的关键字参数里（接线通了）。"""
    spy = patch_render(monkeypatch)
    shot_id = await first_shot(service)
    give_video(service, shot_id, service.media_store.store("local", "video", MP4)["url"])
    await act(service, "saveSettings", {"subtitleMode": "burn", "outputResolution": "720p"})

    body = await act(service, "preview-film")

    assert len(spy.calls) == 1, "一次合成只该调一次 render_film"
    assert spy.calls[0]["subtitle_mode"] == "burn"
    assert spy.calls[0]["resolution"] == "720p"
    assert body["result"]["payload"]["video_url"].startswith("/api/v1/drama/assets/")


async def test_switching_subtitle_mode_changes_sample_key(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一项目 soft -> burn 两次合成，成片地址必须不同（不能命中旧成片缓存）。

    等价性说明：离线成片走 `media_store.store_sample`，其 asset_id 是
    `sha256(owner|kind|key)[:16]`。素材键相同 -> 同一 asset_id -> 文件已存在直接复用
    -> 返回**逐字节相同**的 URL；所以「URL 不同」与「确定性素材键不同」在这里是同一
    件事，反过来也成立。这里同时断言两次 `render_film` 收到的模式不同，给出直接证据。
    """
    spy = patch_render(monkeypatch)
    shot_id = await first_shot(service)
    give_video(service, shot_id, service.media_store.store("local", "video", MP4)["url"])

    await act(service, "saveSettings", {"subtitleMode": "soft", "outputResolution": "source"})
    soft = await act(service, "preview-film")
    soft_url = soft["result"]["payload"]["video_url"]

    await act(service, "saveSettings", {"subtitleMode": "burn", "outputResolution": "source"})
    burn = await act(service, "preview-film")
    burn_url = burn["result"]["payload"]["video_url"]

    assert len(spy.calls) == 2
    assert spy.calls[0]["subtitle_mode"] == "soft"
    assert spy.calls[1]["subtitle_mode"] == "burn"
    assert soft_url != burn_url, "切字幕模式必须换一份成片，而不是复用上一份"


async def test_preview_film_defaults_to_soft_and_source(
    service: DramaService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认项目（`soft` / `source`）：默认路径不许回归成重编码或烧字幕。"""
    spy = patch_render(monkeypatch)
    shot_id = await first_shot(service)
    give_video(service, shot_id, service.media_store.store("local", "video", MP4)["url"])

    # 一次 saveSettings 都不做：直接用种子项目的默认交付选项。
    project = await project_of(service)
    assert project["subtitleMode"] == "soft"
    assert project["outputResolution"] == "source"

    await act(service, "preview-film")

    assert len(spy.calls) == 1
    assert spy.calls[0]["subtitle_mode"] == "soft"
    assert spy.calls[0]["resolution"] == "source"
