"""漫剧生成动作的扣费、幂等、上限，以及资金与数据缺陷的反例回归。

这些用例的存在意义是「把复现过的反例固化成断言」，不是补覆盖率：
每一条都对应一个真实被踩到的行为差异，删掉就会退回去。

约定：
- 全部离线，零出网、零付费；Provider 一律是本地实现或测试桩。
- 账本口径是 drama.sqlite 的<b>整数积分</b>。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import ACTION_PRICES, DEFAULT_DAILY_CAP, DramaService, _cap_of


class EchoProvider(MockDramaProvider):
    """在 mock 基础上把「真实素材」注入结果的测试桩。

    只做一件事：给资产候选图 / 图生视频各返回一个本服务签发的本地 URL，
    用来验证「生成结果是否真的写回了对象」，而不需要任何上游。
    """

    def __init__(self, service_ref: list[DramaService]) -> None:
        self._ref = service_ref
        self.calls: list[str] = []

    def _image(self, owner: str) -> str:
        store = self._ref[0].media_store
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
        return store.store(owner, "image", png)["url"]

    def _video(self, owner: str) -> str:
        store = self._ref[0].media_store
        mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 24
        return store.store(owner, "video", mp4)["url"]

    def asset_candidates(self, asset: dict[str, Any], *, owner: str) -> dict[str, Any]:
        item = super().asset_candidates(asset, owner=owner)
        item["payload"]["image_url"] = self._image(owner)
        return item

    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        item = super().image_to_video(shot, size, owner=owner, first_frame=first_frame)
        item["payload"]["video_url"] = self._video(owner)
        return item

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self.calls.append("script-doctor")
        return super().script_doctor(layers, owner=owner)


class RemoteEcho(EchoProvider):
    """把 provider.name 伪装成非 mock，用来验证「接真实模型后才生效」的硬闸门。"""

    name = "remote-echo"

    def preview_film(self, project: dict[str, Any], *, owner: str) -> dict[str, Any]:
        # 这个替身注入的是假 MP4 字节，不能交给本机 FFmpeg 真合成：
        # 交给父类时隐去视频轨，让它走「无视频轨」分支，再补上成片地址。
        bare = dict(project) | {"shots": [dict(x) | {"videoUrl": ""} for x in project["shots"]]}
        item = super().preview_film(bare, owner=owner)
        item["payload"]["video_url"] = self._video(owner)
        return item


@pytest.fixture
def made(tmp_path: Path):
    """构造一个 DramaService，返回 (service, provider)，provider 是 EchoProvider。"""
    ref: list[DramaService] = []
    provider = EchoProvider(ref)
    service = DramaService(
        tmp_path / "drama.sqlite",
        provider,
        admins=frozenset({"local"}),
        daily_cap=DEFAULT_DAILY_CAP,
    )
    service.store.install()
    ref.append(service)
    return service, provider


@pytest.fixture
async def made_big(tmp_path: Path):
    """同上，但放开单日上限并补足余额。

    为什么需要它：图生视频售价 300、单次冻结 360，而单日上限按决策保持 500——
    8 个镜头逐个图生视频需要 2880，默认上限下第 2 次调用就 409；种子余额 980 也
    不够 8 × 300 = 2400。本文件里依赖「整套成片流程」的用例验的是导出/成片语义，
    **不是**日上限或余额闸门，所以必须放开；这两个闸门本身由
    `tests/test_residuals.py::test_reroll_respects_daily_cap` 等用例专门盯。
    """
    ref: list[DramaService] = []
    provider = EchoProvider(ref)
    service = DramaService(
        tmp_path / "drama.sqlite",
        provider,
        admins=frozenset({"local"}),
        daily_cap=100000,
    )
    service.store.install()
    ref.append(service)
    await service.action(
        "local", "adminCredit", [20000, "成片流程夹具", "seed-made-big"], None, None
    )
    return service, provider


async def act(
    service: DramaService,
    name: str,
    *args: Any,
    owner: str = "local",
    project_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await service.action(owner, name, list(args), project_id, request_id)


def ledger_of(body: dict[str, Any]) -> dict[str, Any]:
    return body["state"]["ledger"]


def project_of(body: dict[str, Any]) -> dict[str, Any]:
    state = body["state"]
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


# ---------------------------------------------------------------- 扣费
async def test_generation_actions_charge_the_ledger(made):
    """反例：连调生成动作曾经余额一分不动、流水一条不增。"""
    service, provider = made
    first = await act(service, "script-doctor")
    price = ACTION_PRICES["script-doctor"]
    assert ledger_of(first)["balance"] == 980 - price
    assert ledger_of(first)["frozen"] == 0  # 预占已在同一动作内结算掉
    assert len(first["state"]["charges"]) == 1

    kinds = [t["kind"] for t in ledger_of(first)["tx"]]
    assert kinds[-3:] == ["reserve", "settle", "refund"], kinds

    for _ in range(2):
        body = await act(service, "script-doctor")
    assert ledger_of(body)["balance"] == 980 - price * 3
    assert len(body["state"]["charges"]) == 3
    assert provider.calls == ["script-doctor"] * 3


async def test_single_action_cap_is_one_point_two_times_price(made):
    """单次上限 = 定价 × 1.2 向上取整：预占 cap 与结算 price 两个数都能被验证。"""
    service, _ = made
    body = await act(service, "saveScript", {"body": "【场景 1 · 夜】\n甲：走。"})
    pid = body["state"]["activeProjectId"]
    voice_id = project_of(body)["audio"]["voices"][0]["id"]
    charged = await act(service, "tts-preview", voice_id, project_id=pid)
    charge = charged["state"]["charges"][-1]
    assert charge["price"] == ACTION_PRICES["tts-preview"]
    assert charge["cap"] == _cap_of(ACTION_PRICES["tts-preview"])
    assert charge["cap"] == 4 and charge["price"] == 3


async def test_free_actions_do_not_touch_the_ledger(made):
    """纯本地动作（标记完成 / 成片编排）定价为 0，不能借机扣费。"""
    service, _ = made
    before = ledger_of(await act(service, "mark-audio-done"))
    after = ledger_of(await act(service, "mark-audio-done"))
    assert ACTION_PRICES["mark-audio-done"] == 0
    assert before["balance"] == after["balance"] == 980
    assert before["tx"] == after["tx"] or len(before["tx"]) == len(after["tx"])


@pytest.fixture
def billed(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 mock 以「真实 Provider」的身份计费。

    日上限只统计真实上游消费（用户裁决：上限的存在理由是防止烧上游真钱），
    `provider.name == "mock"` 的本地模拟不占额也不被额拦。而本文件里考日上限的
    用例关心的是「拦不拦得住」这条口径本身，与 Provider 真假无关，所以只把身份
    改成非 mock，不改任何断言、也不调大 daily_cap。
    """
    monkeypatch.setattr(MockDramaProvider, "name", "remote")


async def test_daily_cap_blocks_before_calling_the_provider(tmp_path, billed):
    """单日上限在调 Provider 之前就要拦住：上游已经烧了钱再报余额不足没有意义。

    用 `billed` 把 mock 的身份改成真实 Provider：本用例验的是「上限拦得住」，
    而离线 mock 按裁决不占额，直接跑会永远不触发这道闸门。
    """
    ref: list[DramaService] = []
    provider = EchoProvider(ref)
    service = DramaService(
        tmp_path / "drama.sqlite", provider, admins=frozenset({"local"}), daily_cap=4
    )
    service.store.install()
    ref.append(service)

    ok = await act(service, "script-doctor")  # 预占 3，用掉 2
    assert ledger_of(ok)["balance"] == 978
    with pytest.raises(AppError) as exc:
        await act(service, "script-doctor")  # 3 + 3 > 4
    assert exc.value.code == "daily_cap_exceeded"
    assert provider.calls == ["script-doctor"]  # 第二次没有真的调


async def test_project_budget_blocks_generation(made):
    """项目预算不是一个装饰字段：预算 4、已花 2、本次需预占 3 时必须被拒。"""
    service, _ = made
    body = await act(service, "saveSettings", {"budget": 4})
    pid = body["state"]["activeProjectId"]
    await act(service, "script-doctor", project_id=pid)
    with pytest.raises(AppError) as exc:
        await act(service, "script-doctor", project_id=pid)
    assert exc.value.code == "budget_exceeded"


async def test_project_budget_blocks_keyframe_submission(made):
    """关键帧批量提交同样受预算约束（旧实现 budget=1 仍能提交成功）。"""
    service, _ = made
    body = await act(service, "saveSettings", {"budget": 1})
    pid = body["state"]["activeProjectId"]
    with pytest.raises(AppError) as exc:
        await act(service, "submitTasks", None, project_id=pid)
    assert exc.value.code == "budget_exceeded"


async def test_insufficient_balance_is_reported_not_crashed(tmp_path):
    """余额不足要返回可读 409，而不是 500 或静默把余额做成负数。

    单日上限优先级高于余额（先把「今天不许再花」这件事说清楚），
    所以这里把 daily_cap 关掉，单独验证余额闸门。
    """
    ref: list[DramaService] = []
    service = DramaService(
        tmp_path / "drama.sqlite", EchoProvider(ref), admins=frozenset({"local"}), daily_cap=0
    )
    service.store.install()
    ref.append(service)

    await act(service, "saveSettings", {"budget": 100000})
    # cap 现在必须 >= ceil(t2i × 1.2)：900 的下限是 1080，否则预占不足以覆盖结算。
    await act(service, "savePricing", {"t2i": 900, "cap": 1080})
    with pytest.raises(AppError) as exc:
        await act(service, "submitTasks", None)
    assert exc.value.code == "insufficient_credits"


# ---------------------------------------------------------------- 幂等
async def test_same_request_id_is_executed_and_charged_once(made):
    """生成动作的幂等键：同 key 第二次只回放结果，不重复执行、不重复扣费。"""
    service, provider = made
    first = await act(service, "script-doctor", request_id="rq-fixed-1")
    assert ledger_of(first)["balance"] == 978

    again = await act(service, "script-doctor", request_id="rq-fixed-1")
    assert ledger_of(again)["balance"] == 978
    assert "幂等命中" in again["message"]
    assert len(again["state"]["charges"]) == 1
    assert provider.calls == ["script-doctor"]  # 只真调了一次
    assert again["result"]["kind"] == "script-doctor"  # 结果被回放


async def test_different_request_ids_each_charge(made):
    service, provider = made
    await act(service, "script-doctor", request_id="rq-a")
    body = await act(service, "script-doctor", request_id="rq-b")
    assert ledger_of(body)["balance"] == 976
    assert len(body["state"]["charges"]) == 2
    assert len(provider.calls) == 2


# ---------------------------------------------------------------- 资金与数据缺陷
async def lock_one_shot(service: DramaService, pid: str) -> str:
    """在指定项目里造一个「可提交」的镜头：新增 + 锁定。"""
    added = await act(service, "addShot", {"desc": "测试镜头", "dur": 3}, project_id=pid)
    shot_id = added["result"]["id"]
    await act(service, "toggleShotLock", shot_id, project_id=pid)
    return shot_id


async def test_deleting_a_project_keeps_the_ledger_invariant(made):
    """反例：删项目曾连带删掉已结算任务，导致守恒永久 409。"""
    service, _ = made
    created = await act(service, "createProject", {"name": "待删除"})
    doomed = created["state"]["activeProjectId"]
    await lock_one_shot(service, doomed)
    await act(service, "submitTasks", None, project_id=doomed)
    for _ in range(6):
        settled = await act(service, "tick", project_id=doomed)
    assert any(t["settled"] for t in settled["state"]["tasks"]), settled["state"]["tasks"]
    assert ledger_of(settled)["retiredCost"] == 0

    after = await act(service, "deleteProject", doomed, project_id=doomed)
    keep = await act(service, "script-doctor", project_id=after["state"]["activeProjectId"])
    # 删项目把已结算成本转成 retiredCost，守恒继续成立，后续动作不再撞 409
    assert ledger_of(keep)["retiredCost"] > 0
    assert keep["state"]["ledger"]["balance"] >= 0


async def test_deleting_a_project_releases_unsettled_reserves(made):
    """未结算任务的预占必须退回余额，否则那部分积分既不在余额也没有承载对象。"""
    service, _ = made
    created = await act(service, "createProject", {"name": "半途删除"})
    doomed = created["state"]["activeProjectId"]
    await lock_one_shot(service, doomed)
    submitted = await act(service, "submitTasks", None, project_id=doomed)
    assert ledger_of(submitted)["frozen"] > 0

    after = await act(service, "deleteProject", doomed, project_id=doomed)
    assert ledger_of(after)["frozen"] == 0
    assert ledger_of(after)["balance"] == 980
    kinds = [t["kind"] for t in ledger_of(after)["tx"]]
    assert "release" in kinds


async def test_admin_credit_idempotency_is_exact_not_substring(made):
    """review-r10 之后 review-r1 不能被当成重复请求（旧实现是子串匹配）。"""
    service, _ = made
    await act(service, "adminCredit", 10, "第一批", "review-r10")
    body = await act(service, "adminCredit", 10, "第二批", "review-r1")
    assert ledger_of(body)["balance"] == 1000

    with pytest.raises(AppError) as exc:
        await act(service, "adminCredit", 10, "第一批", "review-r10")
    assert exc.value.code == "duplicate_request"


async def test_import_rejects_an_inconsistent_ledger(made, tmp_path):
    """导入一份余额随便写的文档必须被拒，否则导完就永久 409 卡死。"""
    service, _ = made
    doc = {
        "version": 1,
        "activeProjectId": "p-x",
        "projects": [{"id": "p-x", "name": "导入", "shots": []}],
        "ledger": {"balance": 9999999, "frozen": 0, "tx": []},
        "tasks": [],
    }
    with pytest.raises(AppError) as exc:
        await service.import_doc("local", doc)
    assert exc.value.code == "invalid_import"
    assert "账本不自洽" in str(exc.value)


async def test_import_round_trip_keeps_the_invariant(made):
    """导出再导入必须自洽：charges 与 retiredCost 都要跟着走。"""
    service, _ = made
    await act(service, "script-doctor")
    doc = await service.export_doc("local")
    payload = json.loads(json.dumps(doc))
    payload.pop("datasource", None)
    restored = await service.import_doc("local", payload)
    assert restored["ledger"]["balance"] == 978
    # 导入后再跑一个动作，守恒仍成立（否则这里会抛 ledger_invariant）
    again = await act(service, "script-doctor")
    assert ledger_of(again)["balance"] == 976


@pytest.mark.parametrize(
    ("name", "args", "code"),
    [
        ("saveSettings", [{"eps": "abc"}], "invalid_field"),
        ("saveSettings", [{"budget": "x"}], "invalid_field"),
        ("addAsset", ["not_a_type", {"name": "甲"}], "invalid_asset_type"),
        ("updateAsset", ["not_a_type", "a1", {"name": "乙"}], "invalid_asset_type"),
        ("savePricing", [{"t2i": "x", "cap": 12}], "invalid_field"),
        ("adminCredit", ["x", "原因", "rq"], "invalid_field"),
    ],
)
async def test_bad_input_returns_readable_400(made, name, args, code):
    """旧实现这些输入会抛 ValueError / KeyError -> 500。"""
    service, _ = made
    with pytest.raises(AppError) as exc:
        await act(service, name, *args)
    assert exc.value.code == code
    assert exc.value.status == 400


async def test_export_film_rejects_an_empty_timeline(made):
    """0 镜头导出会产出 shots=0 的「成片」记录，看起来成功、实际没有内容。"""
    service, _ = made
    created = await act(service, "createProject", {"name": "空项目"})
    pid = created["state"]["activeProjectId"]
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)
    with pytest.raises(AppError) as exc:
        await act(service, "exportFilm", project_id=pid)
    assert exc.value.code == "empty_film"


async def test_export_film_requires_a_real_film_once_provider_is_remote(made_big):
    """接真实 Provider 后，没有合成成片就不允许产出「导出成功」记录。"""
    service, _ = made_big
    remote = RemoteEcho([service])
    # 必须走装配入口：离线 Provider 要挂上服务级的 owner 隔离素材目录。
    service.set_provider(remote)
    state = await service.state("local")
    pid = state["activeProjectId"]
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)

    with pytest.raises(AppError) as exc:
        await act(service, "exportFilm", project_id=pid)
    assert exc.value.code == "film_missing"
    assert exc.value.status == 409

    # 合成之后导出才成立，并且记录指向真实视频文件。
    # 每个镜头都要先有视频轨：缺镜头的成片不完整，导出会被拒（film_incomplete）。
    for item in project_of({"state": state})["shots"]:
        await act(service, "image-to-video", item["id"], "", project_id=pid)
    await act(service, "preview-film", project_id=pid)
    body = await act(service, "exportFilm", project_id=pid)
    record = body["result"]
    assert record["filmUrl"].startswith("/api/v1/drama/assets/")
    assert record["artifact"].startswith("成片")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="离线示例成片依赖本机 FFmpeg")
async def test_export_film_points_at_a_playable_offline_sample(made_big):
    """第五项：离线导出不再只是留痕，必须指向本机合成的真实示例成片文件。"""
    service, _ = made_big
    # 换成纯离线 Provider：EchoProvider 注入的是假 MP4 字节，FFmpeg concat 会失败。
    service.set_provider(MockDramaProvider())
    state = await service.state("local")
    pid = state["activeProjectId"]
    for item in project_of({"state": state})["shots"]:
        await act(service, "image-to-video", item["id"], "", project_id=pid)
    preview = await act(service, "preview-film", project_id=pid)
    assert preview["result"]["payload"]["sample"] is True
    assert preview["result"]["payload"]["video_url"].startswith("/api/v1/drama/assets/")
    for index in range(4):
        await act(service, "toggleQA", index, project_id=pid)
    record = (await act(service, "exportFilm", project_id=pid))["result"]
    assert record["filmUrl"].startswith("/api/v1/drama/assets/")
    assert "示例成片" in record["artifact"]
    assert record["sampleLabel"] == "示例，非模型生成"
    film_path = service.media_store.path_for_url("local", record["filmUrl"])
    assert film_path is not None and film_path.is_file() and film_path.stat().st_size > 0


async def test_mark_audio_done_counts_only_this_project(made):
    """audio_files 曾按 owner 目录全量计数，会把别的项目的音频算进来。"""
    service, _ = made
    seed_pid = (await service.state("local"))["activeProjectId"]
    voice = (await service.state("local"))["projects"][0]["audio"]["voices"][0]["id"]
    await act(service, "tts-preview", voice, project_id=seed_pid)

    other = await act(service, "createProject", {"name": "别的项目"})
    other_pid = other["state"]["activeProjectId"]
    # 新项目里没有音频；同一个 owner 目录里其实已经有一个文件了
    done = await act(service, "mark-audio-done", project_id=other_pid)
    payload = done["result"]["payload"]
    assert payload["audio_files"] == 0
    assert payload["audio_files_scope"] == "project"

    # 有音频的那个项目才应该数到 1
    mine = await act(service, "mark-audio-done", project_id=seed_pid)
    assert mine["result"]["payload"]["audio_files"] == 1


# ---------------------------------------------------------------- 授权
async def test_admin_actions_require_the_admin_allowlist(made):
    service, _ = made
    with pytest.raises(AppError) as exc:
        await act(service, "adminCredit", 100, "给自己加钱", "rq-1", owner="mallory")
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403
    # 管理员仍然可以
    body = await act(service, "adminCredit", 100, "联调充值", "rq-2")
    assert ledger_of(body)["balance"] == 1080


async def test_release_held_requires_admin(made):
    service, _ = made
    await act(service, "submitTasks", None)
    task_id = (await service.state("local"))["tasks"][0]["id"]
    await act(service, "injectFault", task_id, "lost")
    with pytest.raises(AppError) as exc:
        await act(service, "releaseHeld", task_id, "放行", owner="mallory")
    assert exc.value.code == "forbidden"


# ---------------------------------------------------------------- 分锁
async def test_different_owners_do_not_block_each_other(tmp_path):
    """反例：一把全局锁会把所有用户的所有动作串行化。"""

    class SlowProvider(MockDramaProvider):
        def script_doctor(self, layers, *, owner):
            import time as _t

            _t.sleep(0.35)
            return super().script_doctor(layers, owner=owner)

    service = DramaService(tmp_path / "drama.sqlite", SlowProvider(), admins=frozenset({"local"}))
    service.store.install()

    started = asyncio.Event()

    async def slow_call():
        started.set()
        return await act(service, "script-doctor", owner="alice")

    slow = asyncio.create_task(slow_call())
    await started.wait()
    await asyncio.sleep(0.02)  # 让慢动作真正进入 Provider

    began = asyncio.get_running_loop().time()
    await act(service, "saveScript", {"body": "x"}, owner="bob")
    elapsed = asyncio.get_running_loop().time() - began
    await slow

    assert elapsed < 0.3, f"bob 被 alice 的慢动作阻塞了 {elapsed:.2f}s"


# ---------------------------------------------------------------- 回写
async def test_candidate_image_is_written_back_to_the_asset(made):
    """反例：候选图只进 media 列表，资产卡永远是 SVG 占位。"""
    service, _ = made
    project = (await service.state("local"))["projects"][0]
    asset_id = project["assets"]["characters"][0]["id"]
    body = await act(service, "generateAssetCandidates", "characters", asset_id)
    asset = next(a for a in project_of(body)["assets"]["characters"] if a["id"] == asset_id)
    assert asset["imageUrl"].startswith("/api/v1/drama/assets/")


async def test_video_is_written_back_to_the_shot(made):
    """反例：image-to-video 的 video_url 写进 shot 后读不回来（列不存在）。"""
    service, provider = made
    project = (await service.state("local"))["projects"][0]
    shot_id = project["shots"][0]["id"]
    body = await act(service, "image-to-video", shot_id)
    shot = next(s for s in project_of(body)["shots"] if s["id"] == shot_id)
    assert shot["videoUrl"].startswith("/api/v1/drama/assets/")
    # 重新读一次 state，确认落库而不是只在内存里活着
    reloaded = (await service.state("local"))["projects"][0]
    assert next(s for s in reloaded["shots"] if s["id"] == shot_id)["videoUrl"] == shot["videoUrl"]


async def test_first_frame_bytes_reach_the_provider(made):
    """首帧图要真的读到字节，而不是只传一个 id。"""
    service, provider = made
    project = (await service.state("local"))["projects"][0]
    asset_id = project["assets"]["characters"][0]["id"]
    await act(service, "generateAssetCandidates", "characters", asset_id)
    shot_id = project["shots"][0]["id"]
    body = await act(service, "image-to-video", shot_id, asset_id)
    assert body["result"]["payload"]["first_frame_bytes"] > 0


async def test_script_doctor_result_is_persisted_on_the_project(made):
    """反例：诊断结论只弹 toast，页面上看不到。"""
    service, _ = made
    body = await act(service, "script-doctor")
    last = project_of(body)["lastResult"]
    assert last["action"] == "script-doctor"
    assert last["payload"]["findings"]
    # 再读一次，确认进了 drama_projects.last_result
    assert (await service.state("local"))["projects"][0]["lastResult"]["action"] == "script-doctor"


# ---------------------------------------------------------------- 事实分级
async def test_config_exposes_prices_and_daily_cap(made):
    service, _ = made
    cfg = await service.config("local")
    assert cfg["prices"]["image-to-video"] == ACTION_PRICES["image-to-video"]
    assert cfg["daily_cap"] == DEFAULT_DAILY_CAP
    assert cfg["daily_used"] == 0
    assert cfg["admin"] is True
    # 能力清单不能再说自己切到 6 个不存在的类
    switches = {c["switch"] for c in cfg["capabilities"]}
    assert all("RemoteDramaProvider" in s or "服务端" in s for s in switches), switches
    voice = next(c for c in cfg["capabilities"] if c["action"] == "change-voice")
    assert "TTS 音色库" not in voice["model"]


# ---------------------------------------------------------------- 取消与服务端推进
async def test_cancel_task_refunds_the_reserve(made):
    """取消是「本地队列里还没结算」的退出通道：预占必须全额退回。"""
    service, _ = made
    seed = (await service.state("local"))["activeProjectId"]
    shot = await lock_one_shot(service, seed)
    before = (await service.state("local"))["ledger"]
    submitted = await act(service, "submitTasks", [shot], project_id=seed)
    task = submitted["state"]["tasks"][-1]
    held = submitted["state"]["ledger"]["frozen"]
    assert held == task["reserve"] > 0

    cancelled = await act(service, "cancelTask", task["id"], "改主意了", project_id=seed)
    after = cancelled["state"]["ledger"]
    assert next(t for t in cancelled["state"]["tasks"] if t["id"] == task["id"])["state"] == (
        "cancelled"
    )
    assert after["frozen"] == 0
    assert after["balance"] == before["balance"]  # 预占原路退回，没扣钱
    # 取消写的是 release 而不是 refund：语义是「预占释放」而非「结算退差」
    assert [t["kind"] for t in after["tx"]][-1] == "release"


async def test_cancel_rejects_a_settled_task(made):
    """已结算的任务取消会把已经花掉的钱退回来，必须拒绝。"""
    service, _ = made
    seed = (await service.state("local"))["activeProjectId"]
    shot = await lock_one_shot(service, seed)
    submitted = await act(service, "submitTasks", [shot], project_id=seed)
    task_id = submitted["state"]["tasks"][-1]["id"]
    for _ in range(6):
        await act(service, "tick", project_id=seed)
    settled = next(t for t in (await service.state("local"))["tasks"] if t["id"] == task_id)
    assert settled["settled"] is True

    with pytest.raises(AppError) as exc:
        await act(service, "cancelTask", task_id, project_id=seed)
    assert exc.value.code == "task_settled"
    assert exc.value.status == 409


async def test_cancelled_shot_can_be_submitted_again(made):
    """取消后同一镜头必须能重新提交，否则用户点错一次就再也生成不了。"""
    service, _ = made
    seed = (await service.state("local"))["activeProjectId"]
    shot = await lock_one_shot(service, seed)
    submitted = await act(service, "submitTasks", [shot], project_id=seed)
    task_id = submitted["state"]["tasks"][-1]["id"]
    await act(service, "cancelTask", task_id, project_id=seed)

    again = await act(service, "submitTasks", [shot], project_id=seed)
    assert again["state"]["tasks"][-1]["shotId"] == shot
    assert again["state"]["tasks"][-1]["state"] == "queued"


async def test_autopump_advances_tasks_without_any_tick(tmp_path):
    """P2-13 核心：任务推进不再依赖客户端反复调 tick。"""
    service = DramaService(
        tmp_path / "drama.sqlite", autopump=True, pump_seconds=0.2, daily_cap=100000
    )
    await service.start()
    try:
        seed = (await service.state("local"))["activeProjectId"]
        shot = await lock_one_shot(service, seed)
        submitted = await act(service, "submitTasks", [shot], project_id=seed)
        task_id = submitted["state"]["tasks"][-1]["id"]

        # 一次 tick 都不调，只等服务端自己推
        settled = None
        for _ in range(40):
            await asyncio.sleep(0.15)
            row = next(t for t in (await service.state("local"))["tasks"] if t["id"] == task_id)
            if row["state"] in ("succeeded", "failed", "held"):
                settled = row
                break
        assert settled is not None, "服务端 pump 没有推进任务"
        assert settled["state"] == "succeeded" and settled["settled"] is True
        assert (await service.config("local"))["pump_ticks"] > 0
        assert (await service.config("local"))["autopump"] is True
    finally:
        await service.close()


async def test_autopump_can_be_disabled_for_deterministic_stepping(tmp_path):
    """关掉自走后必须仍是「只有 tick 才动」，测试与排障依赖这个确定性。"""
    service = DramaService(
        tmp_path / "drama.sqlite", autopump=False, pump_seconds=0.2, daily_cap=100000
    )
    await service.start()
    try:
        seed = (await service.state("local"))["activeProjectId"]
        shot = await lock_one_shot(service, seed)
        submitted = await act(service, "submitTasks", [shot], project_id=seed)
        task_id = submitted["state"]["tasks"][-1]["id"]
        await asyncio.sleep(0.6)
        row = next(t for t in (await service.state("local"))["tasks"] if t["id"] == task_id)
        assert row["state"] == "queued"
        assert (await service.config("local"))["autopump"] is False
    finally:
        await service.close()


# ---------------------------------------------------------------- 素材配额与清理
async def test_media_quota_blocks_writes_before_touching_disk(tmp_path):
    """配额必须在写盘前拦住，否则会留下半截文件。"""
    from comicdrama import media_store as media_module

    store = media_module.DramaMediaStore(tmp_path / "media", quota_bytes=64)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    store.store("local", "image", png[:32])
    with pytest.raises(AppError) as exc:
        store.store("local", "image", png + b"\x00" * 64)
    assert exc.value.code == "quota_exceeded"
    assert exc.value.status == 409
    # 被拒的那次没有留下文件
    assert store.stats("local")["files"] == 1


async def test_purge_media_previews_then_deletes(tmp_path):
    """清理默认只预览：删除不可逆，先看会删多少再决定。"""
    import os

    service = DramaService(tmp_path / "drama.sqlite", daily_cap=100000)
    await service.start()
    try:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
        first = service.media_store.store("local", "image", png)
        second = service.media_store.store("local", "image", png)
        old = time.time() - 10 * 86400
        for asset in (first, second):
            path = service.media_store.resolve("local", asset["asset_id"])
            os.utime(path, (old, old))

        preview = (await act(service, "purgeMedia", 7))["result"]
        assert preview["dry_run"] is True
        assert preview["files"] == 2
        assert service.media_store.stats("local")["files"] == 2  # 预览不动手

        real = (await act(service, "purgeMedia", 7, False))["result"]
        assert real["dry_run"] is False
        assert real["files"] == 2
        assert real["freed_bytes"] > 0
        assert service.media_store.stats("local")["files"] == 0
    finally:
        await service.close()


async def test_purge_media_requires_admin(made):
    """清理会删真实文件，必须比普通动作更严：管理员才准动。"""
    service, _ = made
    with pytest.raises(AppError) as exc:
        await act(service, "purgeMedia", 7, owner="stranger")
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403


# ---------------------------------------------------------------- AI 生成标识
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="导出前置需要本机合成的成片")
async def test_export_record_carries_ai_disclosure(made_big):
    """成片离开系统后，导出记录是唯一能证明「这是 AI 生成」的凭据。"""
    service, _ = made_big
    service.set_provider(MockDramaProvider())
    state = await service.state("local")
    seed = state["activeProjectId"]
    for item in project_of({"state": state})["shots"]:
        await act(service, "image-to-video", item["id"], "", project_id=seed)
    await act(service, "preview-film", project_id=seed)
    for index in range(4):
        await act(service, "toggleQA", index, project_id=seed)
    record = (await act(service, "exportFilm", project_id=seed))["result"]
    assert record["aiLabel"] == "AI 生成"
    assert "AI 生成" in record["aiDisclosure"]
    assert (await service.config("local"))["ai_disclosure"] == record["aiDisclosure"]


# ---------------------------------------------------------------- 账本只读概览
def test_reconcile_summarises_the_ledger():
    from comicdrama.reconcile import POINTS_PER_CNY, reconcile_doc

    doc = reconcile_doc(
        "local",
        {"ledger": {"balance": 1017, "frozen": 12}, "charges": []},
        daily_cap=500,
        daily_used=53,
    )
    assert doc["drama"]["balance"] == 1017
    assert doc["drama"]["totalBalance"] == 1029
    assert doc["drama"]["dailyUsed"] == 53
    assert doc["pointsPerCny"] == POINTS_PER_CNY
