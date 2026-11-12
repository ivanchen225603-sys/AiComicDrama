"""漫剧工作台 HTTP 边界：同源静态资源、CSP、服务端动作分发、资金守恒与事实分级。

边界（务必与产品文档保持一致）：
- 全程零付费、零公网、零真实凭据；所有「生成」结果来自 comicdrama/mock_media.py
  的确定性本地规则，绝不代表上游真实漫剧模型已接通。
- 数据落在 drama.sqlite。
- 「最后一公里」= 付费开通上游真实模型、填凭据、显式切到 remote Provider，
  并按首次实测核对上游响应形状与真实计费字段（抽象层与素材转存已实现）。
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from comicdrama.app import create_app
from comicdrama.config import Settings
from comicdrama.media_store import DramaMediaStore
from tests.conftest import ROOT

BASE = "/api/v1/drama"
WEB = ROOT / "comicdrama" / "web"
SERVICE = ROOT / "comicdrama" / "service.py"
CORE_JS = WEB / "js" / "core.js"


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "api",
        # 这些用例全部显式调 tick 推进状态机：关掉自走才能断言中间态，
        # 否则后台 pump 会在断言与断言之间把任务推过去（自走能力单独有专门用例）。
        drama_autopump=False,
        # 流水线用例要逐镜图生视频（300 积分/镜、预占 360），默认单日上限 500 下
        # 第 2 个镜头就 409。日上限本身另有专门用例（test_residuals.py），
        # 这里放开是为了让「全部动作可达」这条端到端断言能跑完。
        drama_daily_cap=100000,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        ) as session,
    ):
        yield session, tmp_path


def project(state: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


async def act(
    session: httpx.AsyncClient,
    name: str,
    *args: Any,
    project_id: str | None = None,
    expected: int = 200,
) -> dict[str, Any]:
    response = await session.post(
        f"{BASE}/action",
        json={"type": name, "args": list(args), "project_id": project_id},
    )
    assert response.status_code == expected, f"{name}: {response.status_code} {response.text[:300]}"
    return response.json()


# ---------------------------------------------------------------- 静态与配置
async def test_drama_asset_route_serves_only_locally_stored_media(client):
    """真实 Provider 转存的素材必须能被同源取回，且不采信外来/伪造的路径。"""
    session, tmp_path = client
    # 与 create_app 用的是同一个目录；不碰 httpx 的私有 transport 属性。
    store = DramaMediaStore(tmp_path / "api" / "drama_media")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    saved = store.store("local", "image", png)
    response = await session.get(saved["url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == png
    assert (await session.get(f"{BASE}/assets/../../secret")).status_code in (400, 404)
    assert (await session.get(f"{BASE}/assets/0000000000000000")).status_code == 404


async def test_drama_static_pages_and_csp(client):
    session, _ = client
    for path in (
        "/",
        "/index.html",
        "/assets/styles.css",
        "/js/core.js",
        "/js/store.js",
        "/js/datasource.js",
        "/js/ui.js",
        "/js/pages.js",
        "/js/app.js",
    ):
        response = await session.get(path)
        assert response.status_code == 200, path
    page = (await session.get("/")).text
    csp = (await session.get("/")).headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert not re.search(r"<script[^>]*>[^<]+</script>", page), "不能有内联脚本"
    assert not re.search(r"\son[a-z]+\s*=", page), "不能有内联事件处理器"
    assert not re.search(r"https?://(?!127\.0\.0\.1)", page), "不能有外链资源"


async def test_config_declares_mock_and_last_mile(client):
    session, _ = client
    config = (await session.get(f"{BASE}/config")).json()
    assert config["provider"] == "mock"
    assert config["offline"] is True
    assert config["owner"] == "local"
    # 第五项新增 BGM / 参考图上传与删除两个端点（raw body，不依赖 python-multipart）。
    assert len(config["endpoints"]) == 10
    assert any("drama/assets/{asset_id}" in e for e in config["endpoints"])
    assert any("POST /api/v1/drama/uploads" in e for e in config["endpoints"])
    assert any("DELETE /api/v1/drama/uploads/{upload_id}" in e for e in config["endpoints"])
    # 离线示例标识与上传限制由 config 下发，前端角标 / 页面文案与服务端同源。
    assert config["sample_label"] == "示例，非模型生成"
    assert config["offline_demo"] is True
    assert config["upload_limits"]["bgm"]["max_bytes"] == 20 * 1024 * 1024
    assert config["upload_limits"]["reference"]["max_count"] == 24
    assert len(config["capabilities"]) == 10
    assert len(config["last_mile"]) == 7
    joined = " ".join(config["last_mile"])
    for word in ("付费开通", "凭据", "JSON 输出", "暂无数据", "videos[]", "7 天"):
        assert word in joined, word
    # config 不得泄露任何真实凭据或暗示模型已接通
    assert not any(k in config for k in ("access_key", "secret_key", "api_key", "token"))
    assert "drama.sqlite" in config["storage"]


def test_remote_actions_match_server_branches():
    """前端声明的在线动作名必须全部有服务端分支，防止后续改动漂移。"""
    core = CORE_JS.read_text(encoding="utf-8")
    block = core.split("App.REMOTE_ACTS", 1)[1].split("];", 1)[0]
    remote = set(re.findall(r'"([^"]+)"', block))
    service = SERVICE.read_text(encoding="utf-8")
    branches: set[str] = set(re.findall(r'name\s*==\s*"([^"]+)"', service))
    for group in re.findall(r"name\s+in\s+\(([^)]*)\)", service):
        branches.update(re.findall(r'"([^"]+)"', group))
    assert len(remote) == 60
    assert remote <= branches, f"服务端缺分支：{sorted(remote - branches)}"


# ---------------------------------------------------------------- 完整业务链路
async def test_all_actions_reachable_in_one_pipeline(client):
    """按真实用户顺序走一条流水线，让 60 个动作在满足前置条件时全部返回 200。

    例外：`resolveCharge` 需要一条「上游结果未知」的待复核凭据，mock Provider 造不出来，
    由 test_residuals.py 用会超时的桩 Provider 单独覆盖（见该文件）。
    """
    session, _ = client
    state = (await session.get(f"{BASE}/state")).json()
    pid = state["activeProjectId"]
    called: list[str] = []

    async def do(name: str, *args: Any) -> dict[str, Any]:
        nonlocal state, pid
        body = await act(session, name, *args, project_id=pid)
        called.append(name)
        state = body["state"]
        pid = state["activeProjectId"]
        # 服务端每个动作后都做资金守恒断言；这里再独立算一遍。
        # 成本有两个来源，漏一个就会算成「凭空少钱」：
        #   1) 已结算的关键帧任务（drama_tasks）；
        #   2) 直接结算的生成动作（drama_charge）+ 随项目删除而退役的成本。
        cost = sum(t["cost"] for t in state["tasks"] if t["settled"])
        cost += sum(c["cost"] for c in state.get("charges", []) if c["state"] == "settled")
        cost += state["ledger"].get("retiredCost", 0)
        credits = sum(
            x["amount"] for x in state["ledger"]["tx"] if x["kind"] == "credit" and not x["seed"]
        )
        assert 980 + credits - cost == state["ledger"]["balance"] + state["ledger"]["frozen"], name
        return body

    def new_id(before: set[str], items: list[dict[str, Any]]) -> str:
        return next(x["id"] for x in items if x["id"] not in before)

    # --- 项目与设定 ---
    await do("createProject", {"name": "联调项目", "genre": "古风"})
    await do(
        "saveSettings",
        {
            "genre": "古风",
            "style": "精致国漫",
            "ratio": "9:16 竖屏",
            "eps": 8,
            "duration": 90,
            "budget": 2000,
        },
    )
    await do("restoreDefaults")
    # 新价下这条流水线要逐镜图生视频（300 积分/镜、单镜预占 360）：种子 980 和
    # 默认预算 3000 都撑不住 8 个镜头。这里先补足余额与预算——日上限/预算闸门
    # 本身另有专门用例，本用例只证明「每个动作都能通过 HTTP 走通」。
    await do("adminCredit", 20000, "流水线夹具", "req-pipeline-seed")
    await do("saveSettings", {"budget": 100000})
    await do(
        "saveScript",
        {
            "logline": "一封迟到的信在雨夜被重新送回",
            "outline": "开场：线索出现；推进：人物回到旧地；收束：留一盏灯",
            "body": "【场景 1 · 旧地 · 夜】\n甲：你还是来了。\n【场景 2 · 门廊 · 夜】\n乙：我收到信了。",
        },
    )
    version = (await do("newScriptVersion"))["result"]
    await do("freezeScript", None)
    await do("rollbackScript", version["id"])
    # 版本对比：纯读动作，不写库、不动冻结状态，所以放在回滚之后也安全。
    # 这里比的是「刚回滚的那一版 vs 当前草稿」——刚刚才把这一版盖回来，
    # 两者内容必然一致，所以 changed 必须是 False（这条同时证明确实读到了版本）。
    diff = (await do("diffScriptVersions", version["id"], ""))["result"]
    assert diff["summary"]["changed"] is False
    assert [f["field"] for f in diff["fields"]] == ["logline", "outline", "body"]
    await do("markDirtyByScript")
    await do("resplitShots")
    await do("script-doctor")
    await do("creative-start", "idea", "一封迟到的信")
    await do("extract-assets")
    await do("resplit-shots-v2")
    assert project(state)["shots"], "按场景拆分后应有分镜草稿"

    # --- 资产 ---
    before = {a["id"] for a in project(state)["assets"]["characters"]}
    await do("addAsset", "characters", {"name": "联调角色", "sub": "测试", "prompt": "冷调"})
    asset_id = new_id(before, project(state)["assets"]["characters"])
    await do("updateAsset", "characters", asset_id, {"name": "改名角色", "prompt": "暖调"})
    await do("toggleAssetLock", "characters", asset_id)
    await do("generateAssetCandidates", "characters", asset_id)
    await do("deleteAsset", "characters", asset_id)

    # --- 分镜 ---
    before = {s["id"] for s in project(state)["shots"]}
    await do("addShot", {"dur": 3, "size": "中景", "desc": "联调镜头", "prompt": "冷调"})
    shot_id = new_id(before, project(state)["shots"])
    await do("insertShotBefore", shot_id)
    inserted = new_id(before | {shot_id}, project(state)["shots"])
    await do("deleteShot", inserted)
    await do("updateShot", shot_id, {"desc": "改过的镜头"})
    await do("moveShot", shot_id, 1)
    await do("toggleShotLock", shot_id)

    # --- 配音 / 字幕 ---
    await do(
        "addVoice", {"character": "联调角色", "voice": "声线 A", "lines": 2, "duration": "00:06"}
    )
    voice_id = project(state)["audio"]["voices"][-1]["id"]
    await do("tts-preview", voice_id)
    await do("change-voice", voice_id)
    await do("addSubtitle", {"in": "00:00", "out": "00:03", "text": "联调字幕"})
    subtitle_id = project(state)["audio"]["subtitles"][-1]["id"]
    await do("toggleSubtitle", subtitle_id)
    await do("deleteSubtitle", subtitle_id)
    await do("saveBgm", {"name": "雨声", "voiceLevel": 100, "bgmLevel": 25, "ambient": "雨"})
    await do("mark-audio-done")
    await do("deleteVoice", voice_id)

    # --- 时间线 / 质检 / 导出 / 视频占位 ---
    await do("addNote", "联调批注")
    await do("moveClip", shot_id, 1)
    await do("preview-film")
    await do("image-to-video", shot_id)
    # 每个镜头都要有视频轨：缺镜头的成片不完整，导出会被拒（film_incomplete）。
    for other in [x["id"] for x in project(state)["shots"] if not x.get("videoUrl")]:
        await do("image-to-video", other)
    # 批量生成视频：此时每个镜头都有视频轨，所以会真的排任务（不是全跳过）。
    # 只点一个镜头，避免把整批 360 × N 的预占塞进这条用例。
    batch = (await do("submitVideoTasks", [shot_id]))["result"]
    assert batch["queued"] == 1 and batch["reserve"] > 0
    assert batch["skipped"] == []
    # 必须当场推进到结算：`submitTasks` 判断「镜头是否已有在途任务」时**不区分 kind**，
    # 留着一条 queued 的视频任务会让后面的关键帧提交被判成 no_pending。
    for _ in range(3):
        await do("tick")
    video_task = next(t for t in state["tasks"] if t["kind"] == "video")
    assert video_task["state"] == "succeeded" and video_task["settled"] is True
    # 产物回退：视频结算后镜头必然留有产物历史，用最新一条恢复一次。
    # 这是「历史可回退」的端到端证据——以前视频产物只有原地覆盖，没有历史。
    history = next(s for s in project(state)["shots"] if s["id"] == shot_id)["mediaHistory"]
    assert history and history[0]["kind"] == "video"
    await do("restoreShotMedia", shot_id, history[0]["url"])
    # 导出记录现在必须指向真实文件：先逐镜产出视频轨，再走一次本机 FFmpeg 合成。
    # 第一次 preview-film 发生在视频轨之前，只会返回「尚无可合成的视频轨」提示。
    await do("preview-film")
    for i in range(4):
        await do("toggleQA", i)
    await do("exportFilm")
    await do("returnForReshoot", shot_id, "联调退回原因")

    # --- 素材清理（管理员动作，默认只预览，不删文件） ---
    purge = (await do("purgeMedia", 7, True))["result"]
    assert purge["dry_run"] is True
    assert purge["files"] >= 0 and "storage" in purge

    # --- 账本与任务状态机 ---
    await do("adminCredit", 100, "联调充值", "req-1")
    await do("saveCreditLimits", {"single": "", "total": ""})
    await do("savePricing", {"t2i": 10, "cap": 12, "qps": 1, "concurrency": 5})
    # 用户主动取消：提交后立刻取消，预占必须全额回到可用余额
    await do("submitTasks", [shot_id])
    cancel_id = state["tasks"][-1]["id"]
    reserve = state["tasks"][-1]["reserve"]
    frozen_before = state["ledger"]["frozen"]
    await do("cancelTask", cancel_id, "联调取消")
    cancelled = next(t for t in state["tasks"] if t["id"] == cancel_id)
    assert cancelled["state"] == "cancelled" and cancelled["settled"] is False
    assert state["ledger"]["frozen"] == frozen_before - reserve
    # 取消过的镜头可以重新提交（不能被当成「仍有在途任务」而卡住）
    await do("submitTasks", [shot_id])
    task_id = state["tasks"][-1]["id"]
    assert task_id != cancel_id
    for _ in range(6):
        await do("tick")
    settled = next(t for t in state["tasks"] if t["id"] == task_id)
    assert settled["state"] == "succeeded" and settled["settled"] is True
    # 关键帧不再写死 4 张模拟帧：frames 里是 Provider 真正产出的关键帧图片地址。
    assert len(settled["frames"]) == 1
    assert settled["frames"][0].startswith("/api/v1/drama/assets/")
    await do("pickFrame", task_id, 0)
    await do("rerollTask", task_id)
    reroll_id = state["tasks"][-1]["id"]
    assert reroll_id != task_id
    # 明确拒绝 -> failed + 自动退还；重试 -> 重新预占；响应丢失 -> held；带原因释放
    await do("injectFault", reroll_id, "reject")
    assert next(t for t in state["tasks"] if t["id"] == reroll_id)["state"] == "failed"
    await do("retryTask", reroll_id)
    await do("injectFault", reroll_id, "lost")
    await do("releaseHeld", reroll_id, "管理员确认上游未执行")
    assert next(t for t in state["tasks"] if t["id"] == reroll_id)["state"] == "released"

    # --- 项目复制 / 归档 / 恢复 / 删除 ---
    source_id = pid
    clone = (await do("copyProject", source_id))["result"]
    assert clone["id"] != source_id
    src_ids = {v["id"] for v in project_by_id(state, source_id)["script"]["versions"]}
    clone_ids = {v["id"] for v in clone["script"]["versions"]}
    assert src_ids.isdisjoint(clone_ids), "复制项目必须重生成子对象 id"
    await do("archiveProject", clone["id"])
    await do("unarchiveProject", clone["id"])
    await do("deleteProject", clone["id"])
    assert state["activeProjectId"] in {x["id"] for x in state["projects"]}

    # 覆盖前端声明的全部在线动作；tick / toggleQA 等会重复调用，故用集合比对
    core = CORE_JS.read_text(encoding="utf-8")
    block = core.split("App.REMOTE_ACTS", 1)[1].split("];", 1)[0]
    remote = set(re.findall(r'"([^"]+)"', block))
    assert set(called) == remote - {"resolveCharge"}, (
        f"未覆盖 {sorted(remote - set(called))} / 多余 {sorted(set(called) - remote)}"
    )

    # media 日志带 provider=mock，绝不写成模型输出
    assert state["media"]
    assert all(m["provider"] == "mock" for m in state["media"])


def project_by_id(state: dict[str, Any], pid: str) -> dict[str, Any]:
    return next(p for p in state["projects"] if p["id"] == pid)


# ---------------------------------------------------------------- 边界与隔离
async def test_unknown_action_owner_isolation_and_reset(client):
    session, _ = client
    unknown = await act(session, "not_a_real_action", project_id=None, expected=404)
    assert unknown["error"] == "unknown_action"

    # 请求体里的 owner 被 StrictModel 严格拒绝，身份一律取服务端 principal
    response = await session.post(
        f"{BASE}/action",
        json={"type": "tick", "args": [], "owner": "someone-else"},
    )
    assert response.status_code == 422
    assert (await session.get(f"{BASE}/config")).json()["owner"] == "local"

    # 加余额幂等：同一 requestId 第二次必须被拒
    await act(session, "adminCredit", 50, "幂等测试", "dup-key")
    duplicate = await act(session, "adminCredit", 50, "幂等测试", "dup-key", expected=409)
    assert duplicate["error"] == "duplicate_request"

    exported = (await session.get(f"{BASE}/export")).json()
    assert exported["version"] == 1
    assert exported["ledger"]["balance"] == 1030
    imported = await session.post(f"{BASE}/import", json={"doc": exported})
    assert imported.status_code == 200
    assert imported.json()["ledger"]["balance"] == 1030
    invalid = await session.post(f"{BASE}/import", json={"doc": {"bad": 1}})
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "invalid_import"

    reset = await session.post(f"{BASE}/reset")
    assert reset.status_code == 200
    assert reset.json()["ledger"]["balance"] == 980


async def test_reconcile_view_is_read_only(client):
    """账本概览只读，不动任何一张表。"""
    session, _ = client
    before = (await session.get(f"{BASE}/state")).json()["ledger"]
    response = await session.get(f"{BASE}/reconcile")
    assert response.status_code == 200
    doc = response.json()
    assert doc["drama"]["unit"] == "整数积分"
    assert doc["drama"]["balance"] == before["balance"]

    after = (await session.get(f"{BASE}/state")).json()["ledger"]
    assert after == before, "对账视图不该改动账本"


async def test_autopump_settles_a_task_without_any_tick_call(tmp_path):
    """HTTP 层证明「服务端驱动」真的存在：一次 tick 都不调，任务自己跑完并结算。"""
    import asyncio

    settings = Settings(
        data_dir=tmp_path / "auto",
        drama_autopump=True,
        drama_pump_seconds=0.2,
        drama_daily_cap=100000,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        ) as session,
    ):
        state = (await session.get(f"{BASE}/state")).json()
        pid = state["activeProjectId"]
        shot = project(state)["shots"][0]["id"]
        await session.post(f"{BASE}/action", json={"type": "toggleShotLock", "args": [shot]})
        submitted = await session.post(
            f"{BASE}/action", json={"type": "submitTasks", "args": [shot], "project_id": pid}
        )
        task_id = submitted.json()["state"]["tasks"][-1]["id"]

        final = None
        for _ in range(40):
            await asyncio.sleep(0.15)
            row = next(
                t
                for t in (await session.get(f"{BASE}/state")).json()["tasks"]
                if t["id"] == task_id
            )
            if row["state"] in ("succeeded", "failed", "held"):
                final = row
                break
        assert final is not None, "服务端 pump 没有推进任务"
        assert final["state"] == "succeeded" and final["settled"] is True
        cfg = (await session.get(f"{BASE}/config")).json()
        assert cfg["autopump"] is True
        assert cfg["pump_ticks"] > 0


async def test_upstream_timeout_leaves_pending_review_over_http(tmp_path):
    """HTTP 层复现「超时不留记录、换 requestId 就扣两次」并证明已修好。

    用 `drama_mock_fault="script-doctor"` 让 mock Provider 第一次调用就抛 504
    （结果未知），整条链路走真实请求：504 -> 待复核凭据落库 -> 冻结保留 ->
    同动作被 409 挡住 -> 管理员 resolveCharge 复核 -> 恢复可用。
    """
    settings = Settings(
        data_dir=tmp_path / "pending",
        drama_autopump=False,
        drama_mock_fault="script-doctor",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        ) as session,
    ):
        before = (await session.get(f"{BASE}/state")).json()["ledger"]

        timeout = await session.post(
            f"{BASE}/action",
            json={"type": "script-doctor", "args": [], "request_id": "e2e-timeout-1"},
        )
        assert timeout.status_code == 504, timeout.text
        assert timeout.json()["error"] == "upstream_unknown"

        state = (await session.get(f"{BASE}/state")).json()
        pending = [c for c in state["charges"] if c["state"] == "pending_review"]
        assert len(pending) == 1, "超时必须在库里留下待复核凭据"
        charge = pending[0]
        assert charge["action"] == "script-doctor" and charge["cost"] == 0
        # 钱不退：可能已经付给上游。冻结保留、余额仍少 cap。
        assert state["ledger"]["frozen"] == before["frozen"] + charge["cap"]
        assert state["ledger"]["balance"] == before["balance"] - charge["cap"]

        # 换 requestId 再点：挡住，且不能真的再打一次上游。
        again = await session.post(
            f"{BASE}/action",
            json={"type": "script-doctor", "args": [], "request_id": "e2e-timeout-2"},
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"] == "pending_review"

        resolved = await session.post(
            f"{BASE}/action",
            json={"type": "resolveCharge", "args": [charge["id"], "failed"]},
        )
        assert resolved.status_code == 200, resolved.text
        after = (await session.get(f"{BASE}/state")).json()
        assert [c for c in after["charges"] if c["state"] == "pending_review"] == []
        # 金额回到原点（流水当然会多出预占/释放两条，这是留痕，不是钱变了）。
        assert after["ledger"]["balance"] == before["balance"]
        assert after["ledger"]["frozen"] == before["frozen"] == 0

        # 故障只注入一次，复核后同一个动作恢复可用。
        ok = await session.post(
            f"{BASE}/action",
            json={"type": "script-doctor", "args": [], "request_id": "e2e-timeout-3"},
        )
        assert ok.status_code == 200, ok.text


class _MisconfiguredProvider:
    """真实形态：密钥环境变量没配，Provider 在出网前就抛 503。

    这是 remote 适配器在缺 `DRAMA_LLM_KEY` 时的真实行为（`drama_credentials` 503）。
    它也是 5xx，但上游一个字节都没收到 —— 不能和「超时、结果未知」同一种处理。
    """

    name = "misconfigured"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        from comicdrama.errors import AppError

        raise AppError("drama_credentials", "未配置 DRAMA_LLM_KEY", 503)


@pytest.fixture
async def misconfigured_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "api-misconf",
        drama_autopump=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # 换掉 Provider 只影响本用例：模拟「首单漏配一项」的真实情形。
        app.state.drama.provider = _MisconfiguredProvider(app.state.drama.provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        ) as session:
            yield session


async def test_misconfigured_provider_does_not_freeze_the_action(misconfigured_client):
    """配置没配好：原样报 503、不落待复核、动作立刻可重试。

    复测指出按状态码一刀切（502/503/504 → 待复核）会把「密钥没配」也算成
    「上游可能已执行」：首单漏配一项，对应动作就被冻结 + 上锁，要管理员逐条
    复核才能再试。资金上是安全的，但首单会多几次没必要的操作。
    """
    session = misconfigured_client
    before = (await session.get(f"{BASE}/state")).json()["ledger"]

    first = await session.post(
        f"{BASE}/action",
        json={"type": "script-doctor", "args": [], "request_id": "misconf-1"},
    )
    assert first.status_code == 503, first.text
    assert first.json()["error"] == "drama_credentials", "必须原样透出配置错误码"

    state = (await session.get(f"{BASE}/state")).json()
    assert [c for c in state["charges"] if c["state"] == "pending_review"] == []
    assert all(c["state"] != "in_flight" for c in state["charges"]), "不能留下悬空预占"
    assert state["ledger"]["frozen"] == before["frozen"] == 0
    assert state["ledger"]["balance"] == before["balance"], "配置错误不扣费"

    # 关键：动作没有被锁住 —— 换个 requestId 仍然报同一个配置错误，而不是 409 待复核。
    second = await session.post(
        f"{BASE}/action",
        json={"type": "script-doctor", "args": [], "request_id": "misconf-2"},
    )
    assert second.status_code == 503, second.text
    assert second.json()["error"] == "drama_credentials"


# ---------------------------------------------------------------- 第四项：上传端点
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 28


async def test_upload_endpoints_accept_raw_body_without_charging(client):
    """raw-body 上传不依赖 python-multipart；上传不扣积分；非法文件有清晰错误码。"""
    session, _ = client
    before = (await session.get(f"{BASE}/state")).json()["ledger"]

    response = await session.post(
        f"{BASE}/uploads?kind=reference&name=%E6%9E%97%E7%A0%9A.png"
        "&asset_type=characters&asset_name=%E6%9E%97%E7%A0%9A",
        content=PNG,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    state = response.json()
    upload = state["uploads"][0]
    assert upload["url"].startswith("/api/v1/drama/assets/")
    assert upload["assetId"], "参考图应绑定 / 新建资产"
    assert state["ledger"] == before, "上传本身不扣积分、不动冻结"

    asset = await session.get(upload["url"])
    assert asset.status_code == 200 and asset.content == PNG

    bad_kind = await session.post(
        f"{BASE}/uploads?kind=video&name=clip.mp4",
        content=WAV,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error"] == "upload_kind"

    bad_format = await session.post(
        f"{BASE}/uploads?kind=bgm&name=fake.wav",
        content=b"definitely-not-audio" * 8,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert bad_format.status_code == 400
    assert bad_format.json()["error"] == "upload_format"

    # BGM 也能传；删除后记录与引用一起消失，重复删除 404。
    bgm = await session.post(
        f"{BASE}/uploads?kind=bgm&name=rain.wav",
        content=WAV,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert bgm.status_code == 200
    upload_id = next(x for x in bgm.json()["uploads"] if x["kind"] == "bgm")["id"]
    assert (await session.delete(f"{BASE}/uploads/{upload_id}")).status_code == 200
    assert (await session.delete(f"{BASE}/uploads/{upload_id}")).status_code == 404


async def test_upload_endpoints_isolate_bearer_owners(tmp_path):
    """跨用户访问：另一个 principal 看不到上传记录、下载不到文件、也删不掉。"""
    settings = Settings(
        data_dir=tmp_path / "bearer-api",
        auth_mode="bearer",
        bearer_principals={"a" * 24: "alice", "b" * 24: "bob"},
        drama_autopump=False,
    )
    app = create_app(settings)
    alice = {"Authorization": "Bearer " + "a" * 24, "Content-Type": "application/octet-stream"}
    bob = {"Authorization": "Bearer " + "b" * 24}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        ) as session,
    ):
        created = await session.post(
            f"{BASE}/uploads?kind=reference&name=a.png", content=PNG, headers=alice
        )
        assert created.status_code == 200, created.text
        upload = created.json()["uploads"][0]

        assert (await session.get(f"{BASE}/state", headers=bob)).json()["uploads"] == []
        assert (await session.get(upload["url"], headers=bob)).status_code == 404
        assert (
            await session.delete(f"{BASE}/uploads/{upload['id']}", headers=bob)
        ).status_code == 404
        assert (await session.get(upload["url"], headers=alice)).status_code == 200
        assert (
            await session.delete(f"{BASE}/uploads/{upload['id']}", headers=alice)
        ).status_code == 200
