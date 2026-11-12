"""内容安全 —— 审核端口 / 生成前拦截 / 上游审核态收口 / AI 标识位置。

三条不可动摇的口径（与 `comicdrama/moderation.py` 的 docstring 一致）：

1. **本地规则只能审文本。** `review_media` 只查空值，审不了像素；像素级审核必须接
   真实审核服务，本期只有显式 `NotImplementedError` 的端口桩。这里用静态断言锁住
   「不得偷偷出网」。
2. **被拒时一分钱都不许动。** 审核在预占（`_charged` 冻结余额、落 in_flight 凭据）
   之前跑：被拒就不建 charge 行、不冻结、不调上游。
3. **`auditStatus=4` 不等于「已确认拒审」，默认冻结待确认。** 它的语义从未实测
   （实测成功样本是 3，同协议里 `generateStatus` 的 4 是「审核中」），所以默认
   `hold`：不退款、不结算、冻结留着。`refund` / `settle` 是部署方显式选择。
   三条路都必须保留上游已扣的成本真值——为了「账面好看」清成 0 / None 就是伪造。

资金用例的期望值全部手算（种子余额 980、图生视频 300 / 预占 360），
不用「前后差值自己跟自己比」。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from comicdrama.app import create_app
from comicdrama.config import Settings
from comicdrama.labels import AI_DISCLOSURE
from comicdrama.media_store import DramaMediaStore
from comicdrama.moderation import (
    DEFAULT_BLOCKLIST,
    LocalKeywordModeration,
    ModerationVerdict,
    NullModeration,
    RemoteModeration,
    audit_outcome,
    parse_label_positions,
    parse_reject_action,
)
from comicdrama.pricing import PRICE_VIDEO
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.providers.remote import (
    RemoteDramaConfig,
    RemoteDramaProvider,
    _worst_audit_status,
)
from comicdrama.service import DramaService
from tests.conftest import ROOT

BASE = "/api/v1/drama"
ACCOUNT = f"{BASE}/account"
PASSWORD = "offline-pass-1"
SEED_BALANCE = 980
# 内置词表里的一条**类别标签**；正常创作文本里不会出现这个词。
BLOCKED = "儿童性剥削"
MODERATION_SRC = ROOT / "comicdrama" / "moderation.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20


def state_of(body: dict[str, Any]) -> dict[str, Any]:
    return body["state"] if "state" in body else body


@pytest.fixture
async def api(tmp_path: Path) -> Iterator[SimpleNamespace]:
    settings = Settings(
        data_dir=tmp_path / "api",
        drama_autopump=False,
        drama_daily_cap=100000,
    )
    app = create_app(settings)
    clients: list[httpx.AsyncClient] = []

    def new_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        )
        clients.append(client)
        return client

    async with app.router.lifespan_context(app):
        try:
            yield SimpleNamespace(app=app, new_client=new_client, tmp_path=tmp_path)
        finally:
            for client in clients:
                await client.aclose()


async def login(api: SimpleNamespace, username: str) -> httpx.AsyncClient:
    client = api.new_client()
    registered = await client.post(
        f"{ACCOUNT}/register", json={"username": username, "password": PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    logged_in = await client.post(
        f"{ACCOUNT}/login", json={"username": username, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text
    return client


async def current_state(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(f"{BASE}/state")
    assert response.status_code == 200, response.text
    return response.json()


def _remote(tmp_path: Path, store: DramaMediaStore | None = None) -> RemoteDramaProvider:
    config = RemoteDramaConfig(
        llm_base_url="http://127.0.0.1/v1",
        llm_model="m",
        t2i_template_uuid="template-t2i",
        t2i_prompt_node="node-1",
        i2v_template_uuid="template-i2v",
        i2v_prompt_node="node-2",
    )
    return RemoteDramaProvider(config, store or DramaMediaStore(tmp_path / "media"))


def _service(tmp_path: Path, **kwargs: Any) -> DramaService:
    service = DramaService(
        tmp_path / "drama.sqlite",
        admins=frozenset({"local"}),
        autopump=False,
        **kwargs,
    )
    service.store.install()
    return service


async def _act(service: DramaService, name: str, *args: Any, pid: str | None = None) -> Any:
    return await service.action("local", name, list(args), pid, None)


async def _first_shot(service: DramaService) -> tuple[str, str]:
    state = await service.state("local")
    project = state["projects"][0]
    return str(project["id"]), str(project["shots"][0]["id"])


def _settled_cost(state: dict[str, Any]) -> int:
    return sum(int(c["cost"]) for c in state["charges"] if c["state"] == "settled")


def _credits(state: dict[str, Any]) -> int:
    return sum(
        int(t["amount"]) for t in state["ledger"]["tx"] if t["kind"] == "credit" and not t["seed"]
    )


# ---------------------------------------------------------------- 1. 本地审核
def test_local_keyword_moderation_blocks_labels_without_echoing_them() -> None:
    """命中内置类别标签就拒；reason 里**绝不出现**命中的词（那等于教人绕过）。"""
    moderator = LocalKeywordModeration()
    assert moderator.name == "local-keywords"
    assert BLOCKED in DEFAULT_BLOCKLIST, "用例依赖内置词表里这条类别标签"

    hit = moderator.review_text(f"剧本里出现了{BLOCKED}的情节", owner="local", context="saveScript")
    assert isinstance(hit, ModerationVerdict)
    assert hit.allowed is False
    assert hit.reason == "内容未通过安全检查"
    assert BLOCKED not in hit.reason
    assert all(word not in hit.reason for word in DEFAULT_BLOCKLIST), "任何命中词都不许回写"
    assert hit.categories == ("keyword",)
    assert hit.provider == "local-keywords"

    clean = moderator.review_text("都市悬疑：女主在雨夜发现一封旧信", owner="local", context="x")
    assert clean.allowed is True
    assert clean.reason == ""

    # 空文本 / 纯空白不算内容：保存空剧本不能被误判成违规。
    for blank in ("", "   \n\t"):
        assert moderator.review_text(blank, owner="local", context="x").allowed is True


def test_local_keyword_moderation_honours_extra_words() -> None:
    """部署方通过 `COMIC_DRAMA_AUDIT_BLOCKLIST` 追加的词必须生效（含去空白/去重）。"""
    moderator = LocalKeywordModeration([" 自定义违规词 ", "", "自定义违规词"])
    blocked = moderator.review_text("这段包含自定义违规词", owner="local", context="x")
    assert blocked.allowed is False
    assert "自定义违规词" not in blocked.reason


def test_local_review_media_only_checks_emptiness_and_says_so() -> None:
    """本地规则看不到画面：非空就放行，但必须在 reason 里明说「没做像素级审核」。"""
    moderator = LocalKeywordModeration()
    empty = moderator.review_media(b"", kind="image", owner="local")
    assert empty.allowed is False
    assert empty.reason == "文件内容为空"
    ok = moderator.review_media(PNG, kind="image", owner="local")
    assert ok.allowed is True
    assert ok.reason == "本地规则不做像素级审核"


# ---------------------------------------------------------------- 2. null / remote
def test_null_moderation_allows_everything() -> None:
    moderator = NullModeration()
    assert moderator.name == "null"
    text = moderator.review_text(f"{BLOCKED}", owner="local", context="x")
    assert text.allowed is True and text.reason == ""
    media = moderator.review_media(b"", kind="image", owner="local")
    assert media.allowed is True and media.reason == ""


def test_remote_moderation_is_an_explicit_stub_for_both_methods() -> None:
    """真实审核服务只有端口：两个方法都必须抛，绝不假装审过。"""
    moderator = RemoteModeration()
    with pytest.raises(NotImplementedError) as text_error:
        moderator.review_text("任意文本", owner="local", context="x")
    assert "NOT_IMPLEMENTED" in str(text_error.value)
    with pytest.raises(NotImplementedError) as media_error:
        moderator.review_media(PNG, kind="image", owner="local")
    assert "NOT_IMPLEMENTED" in str(media_error.value)


# ---------------------------------------------------------------- 3. audit_outcome
def test_audit_outcome_maps_only_the_confirmed_value() -> None:
    assert audit_outcome("4") == "rejected"
    for unknown in ("3", "5", "0", "", "  ", None):
        assert audit_outcome(unknown) is None, f"{unknown!r} 的语义未确认，绝不能猜"


# ---------------------------------------------------------------- 4. _worst_audit_status
def test_worst_audit_status_picks_the_most_severe() -> None:
    def asset(value: Any) -> SimpleNamespace:
        return SimpleNamespace(meta={"auditStatus": value})

    assert _worst_audit_status([asset("4"), asset("")]) == "4"
    assert _worst_audit_status([asset("3"), asset("4")]) == "4"
    assert _worst_audit_status([asset(""), asset("3")]) == "3"
    assert _worst_audit_status([]) is None
    assert _worst_audit_status([None]) is None
    assert _worst_audit_status([asset(None), SimpleNamespace(meta={})]) is None, "未知不补默认值"


# ---------------------------------------------------------------- 5. 标识位置解析
def test_parse_reject_action_accepts_three_states_and_clamps_the_rest() -> None:
    """三态解析：合法值原样、大小写/空白宽容、其余一律回退 hold（不抛异常）。"""
    for action in ("hold", "refund", "settle"):
        assert parse_reject_action(action) == action
    assert parse_reject_action(" REFUND ") == "refund"
    for junk in ("", "   ", "nuke", "refund!", "settle_all", None):
        assert parse_reject_action(junk) == "hold", f"{junk!r} 必须回退到资金可回滚的一侧"


def test_parse_label_positions_defaults_exclude_in_frame_drawtext() -> None:
    assert parse_label_positions("") == ["container_metadata"]
    assert "in_frame_drawtext" not in parse_label_positions("")
    assert parse_label_positions("container_metadata,video_overlay") == [
        "container_metadata",
        "video_overlay",
    ]
    # 只有显式写入才允许烧进画面
    explicit = parse_label_positions("container_metadata,in_frame_drawtext")
    assert "in_frame_drawtext" in explicit
    # 全是垃圾值 -> 退回默认，不抛异常炸启动
    assert parse_label_positions("watermark, ,烧进画面") == ["container_metadata"]


# ---------------------------------------------------------------- 6. 生成前拦截不花钱
async def test_blocked_generation_creates_no_charge_and_freezes_nothing(
    api: SimpleNamespace,
) -> None:
    """含违规词的 prompt 直接 400；余额、冻结、凭据条数一个都不许变。"""
    client = await login(api, "moderation-block")
    before = await current_state(client)

    response = await client.post(
        f"{BASE}/action",
        json={"type": "creative-start", "args": [f"请写一段关于{BLOCKED}的剧本"]},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "content_blocked"
    assert "不会扣费" in response.json()["message"]

    after = await current_state(client)
    assert after["ledger"]["balance"] == before["ledger"]["balance"] == SEED_BALANCE
    assert after["ledger"]["frozen"] == before["ledger"]["frozen"] == 0
    assert len(after["charges"]) == len(before["charges"]) == 0, "被拒不得创建 charge 行"
    assert after["projects"] == before["projects"], "被拒不得改动业务状态"


async def test_normal_generation_still_runs_through_moderation(api: SimpleNamespace) -> None:
    """同一条链路下正常 prompt 照旧跑通（证明审核没有误伤）。"""
    client = await login(api, "moderation-clean")
    await current_state(client)
    response = await client.post(
        f"{BASE}/action",
        json={"type": "creative-start", "args": ["都市悬疑：雨夜的一封旧信", "短剧"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    state = state_of(body)
    assert state["charges"][-1]["state"] == "settled"
    assert state["charges"][-1]["cost"] == 2, "creative-start 定价 2"
    assert state["ledger"]["balance"] == SEED_BALANCE - 2


# ------------------------------------------- 8. auditStatus=4：按配置三选一收口
async def test_audit_reject_action_defaults_to_hold(tmp_path: Path) -> None:
    """不传配置时默认 hold：**不退款、不结算**，冻结原样留着等确认。"""
    service = _service(tmp_path)
    assert service.audit_reject_action == "hold"


@pytest.mark.parametrize("action", ["hold", "refund", "settle"])
async def test_audit_status_4_follows_the_configured_action(tmp_path: Path, action: str) -> None:
    """上游报 `auditStatus=4`：同一个输入，三种收口，资金各走一条路。

    手算基准：种子 980、图生视频定价 300、预占 cap = 300 × 1.2 = 360。

    - hold：余额 980-360=620，冻结 360，凭据 pending_review，cost 0；
    - refund：余额回到 980，冻结 0，凭据 released，cost 0；
    - settle：余额 620+60=680，冻结 0，凭据 settled，cost 300。
    三条都必须满足 980 + Σcredit - Σ结算cost == 余额 + 冻结。
    """
    service = _service(tmp_path, audit_reject_action=action)
    provider = _remote(tmp_path, service.media_store)
    service.set_provider(provider)
    # 注入空审核器：这条路径只由上游审核态驱动，与本地词表无关。
    service.set_moderator(NullModeration())
    created = _spy_store(service)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        usage.update(
            upstream_task_id="u-audit",
            upstream_points=250,
            account_balance=730,
            audit_status="4",
        )
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)
    assert created, "假上游必须真的把产物写进本地素材目录"

    state = await service.state("local")
    charge = state["charges"][-1]
    ledger = state["ledger"]
    shot_view = next(x for x in state["projects"][0]["shots"] if x["id"] == shot)

    # 三条路都必须保留上游真值：那是真花出去的钱，不是 0。
    assert charge["upstreamPoints"] == 250
    assert charge["upstreamBalance"] == 730

    if action == "hold":
        assert charge["state"] == "pending_review"
        assert charge["cost"] == 0, "待确认期间不得计费"
        assert ledger["balance"] == SEED_BALANCE - 360
        assert ledger["frozen"] == 360, "冻结原样保留，等确认后再结算或退款"
        assert "审核态待确认" in charge["note"]
        assert charge["auditRejected"] is False, "还没确认违规，不许打出「未通过审核」"
        assert shot_view["videoUrl"].startswith("/api/v1/drama/assets/"), (
            "hold 不清理产物：删了就没法交付，也证明不了它到底违规没有"
        )
        assert shot_view["auditRejected"] is False
        assert service.media_store.stats("local")["files"] == 1
        # 只有预占流水（-360）和一条 0 元的 hold 标记；不得有结算 / 释放。
        assert not [t for t in ledger["tx"] if t["kind"] == "settle" and not t["seed"]]
        assert not [t for t in ledger["tx"] if t["kind"] == "release" and not t["seed"]]
        hold = [t for t in ledger["tx"] if t["kind"] == "hold" and not t["seed"]]
        assert len(hold) == 1 and hold[0]["amount"] == 0
    elif action == "refund":
        assert charge["state"] == "released", "不能停在 in_flight / pending_review"
        assert charge["cost"] == 0, "用户没拿到合规产物，售价不计"
        assert charge["auditRejected"] is True
        assert ledger["balance"] == SEED_BALANCE
        assert ledger["frozen"] == 0
        assert shot_view["videoUrl"] == "", "退款必须连产物一起摘掉"
        assert shot_view["auditRejected"] is True
        assert service.media_store.stats("local")["files"] == 0
        release = [t for t in ledger["tx"] if t["kind"] == "release" and not t["seed"]]
        assert len(release) == 1 and release[0]["amount"] == 360
        assert "内容不合规" in release[0]["label"] + release[0]["note"]
        assert not [t for t in ledger["tx"] if t["kind"] == "settle" and not t["seed"]]
        assert not [t for t in ledger["tx"] if t["kind"] == "hold" and not t["seed"]]
    else:  # settle
        assert charge["state"] == "settled"
        assert charge["cost"] == PRICE_VIDEO
        assert charge["auditRejected"] is True
        assert "按配置仍结算" in charge["note"]
        assert ledger["balance"] == SEED_BALANCE - PRICE_VIDEO
        assert ledger["frozen"] == 0
        # 用户付了钱：产物必须留在项目里，删掉等于收钱不给货。
        assert shot_view["videoUrl"].startswith("/api/v1/drama/assets/")
        assert service.media_store.stats("local")["files"] == 1
        assert not [t for t in ledger["tx"] if t["kind"] == "release" and not t["seed"]]

    # 守恒：980 + Σcredit(0) - Σ结算cost == 余额 + 冻结。
    assert SEED_BALANCE + _credits(state) - _settled_cost(state) == (
        ledger["balance"] + ledger["frozen"]
    )


def test_illegal_reject_action_falls_back_to_hold_without_crashing(tmp_path: Path) -> None:
    """拼错的收口策略：回退 hold，既不抛异常，也不落到「已退款 / 已结算」一侧。"""
    service = _service(tmp_path, audit_reject_action="nuke")
    assert service.audit_reject_action == "hold"

    # 环境变量入口同样不能炸：Settings 里的 Literal 会拒非法值，所以在解析前夹到 hold。
    settings = Settings(data_dir=tmp_path / "bad-cfg", drama_audit_reject_action="nuke")
    assert settings.drama_audit_reject_action == "hold"
    app = create_app(settings)
    assert app is not None, "非法配置不得把服务挡在启动之外"


# ---------------------------------------------------------------- 10. 无审核态 -> 行为不变
async def test_missing_audit_status_keeps_behaviour_identical(tmp_path: Path) -> None:
    """上游没回传 auditStatus（mock / 老上游）：不退款、不标记、产物照旧留在镜头上。"""
    service = _service(tmp_path)
    provider = _remote(tmp_path, service.media_store)
    service.set_provider(provider)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)

    state = await service.state("local")
    charge = state["charges"][-1]
    assert charge["state"] == "settled"
    assert charge["cost"] == PRICE_VIDEO
    assert charge["auditRejected"] is False
    assert charge["upstreamPoints"] is None, "未知就是 None，不用 0 顶替"
    assert state["ledger"]["balance"] == SEED_BALANCE - PRICE_VIDEO
    # 没有审核态就绝不能碰产物：视频 URL 还在、文件还在、标记为 False。
    shot_view = next(x for x in state["projects"][0]["shots"] if x["id"] == shot)
    assert shot_view["videoUrl"].startswith("/api/v1/drama/assets/")
    assert shot_view["auditRejected"] is False
    assert service.media_store.stats("local")["files"] == 1, "正常产物不得被误删"


# ------------------------------------------------- 10b. 判违规后清理产物（shot / asset）
def _spy_store(service: DramaService) -> list[str]:
    """记录假上游真的往本地素材目录写了什么（不读文件内容，只看写没写）。"""
    created: list[str] = []
    real_store = service.media_store.store

    def spy(owner: str, kind: str, data: bytes) -> dict[str, Any]:
        saved = real_store(owner, kind, data)
        created.append(str(saved["url"]))
        return saved

    service.media_store.store = spy  # type: ignore[method-assign]
    return created


async def test_audit_reject_clears_the_shot_video_and_deletes_the_local_file(
    tmp_path: Path,
) -> None:
    """钱退了、产物也必须真的摘掉：URL 清空、`/state` 读回来就是空的、本地文件删掉。"""
    service = _service(tmp_path, audit_reject_action="refund")
    provider = _remote(tmp_path, service.media_store)
    service.set_provider(provider)
    created = _spy_store(service)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        usage.update(upstream_task_id="u-reject", upstream_points=250, audit_status="4")
        return MP4

    provider._generate = fake_generate  # type: ignore[method-assign]
    pid, shot = await _first_shot(service)
    await _act(service, "image-to-video", shot, "", pid=pid)

    assert created, "假上游必须真的把产物写进本地素材目录（否则删除断言是空的）"
    state = await service.state("local")
    shot_view = next(x for x in state["projects"][0]["shots"] if x["id"] == shot)
    assert shot_view["videoUrl"] == "", "判违规的产物不能还挂在镜头上"
    assert shot_view["auditRejected"] is True, "要能说明「未通过审核」而不是凭空变空"
    # 清理真正落库：重新读一次 /state（走 DB 投影）仍然是清理后的值。
    again = await service.state("local")
    shot_again = next(x for x in again["projects"][0]["shots"] if x["id"] == shot)
    assert (shot_again["videoUrl"], shot_again["auditRejected"]) == ("", True)

    charge = state["charges"][-1]
    assert charge["state"] == "released"
    assert "产物清理未完成" not in charge["note"], "能定位到目标就不该报清理失败"
    # 文件真的删了：用量/文件数对比，不读文件内容。
    assert service.media_store.stats("local")["files"] == 0
    assert service.media_store.count("local", "video") == 0
    assert service.media_store.usage("local") == 0


async def test_audit_reject_clears_the_asset_candidate_image(tmp_path: Path) -> None:
    """`asset:<id>` 前缀同样生效：候选图被判违规后清空并标记。"""
    service = _service(tmp_path, audit_reject_action="refund")
    provider = _remote(tmp_path, service.media_store)
    service.set_provider(provider)
    created = _spy_store(service)

    def fake_generate(client, prompt, *, width=1024, height=1024, usage=None):
        assert usage is not None
        usage.update(upstream_points=25, account_balance=730, audit_status="4")
        return PNG

    provider._generate = fake_generate  # type: ignore[method-assign]
    state = await service.state("local")
    pid = str(state["projects"][0]["id"])
    asset = state["projects"][0]["assets"]["characters"][0]
    await _act(service, "generateAssetCandidates", "characters", asset["id"], pid=pid)

    assert created, "候选图必须真的落过盘"
    after = await service.state("local")
    view = next(x for x in after["projects"][0]["assets"]["characters"] if x["id"] == asset["id"])
    assert view["imageUrl"] == ""
    assert view["auditRejected"] is True
    assert after["charges"][-1]["state"] == "released"
    assert service.media_store.stats("local")["files"] == 0


async def test_audit_reject_without_a_target_refunds_and_notes_the_cleanup_gap(
    tmp_path: Path,
) -> None:
    """`target` 解析不出来（项目级动作）时：不抛异常、退款照常、note 里留清理失败提示。"""
    service = _service(tmp_path, audit_reject_action="refund")
    provider = MockDramaProvider()
    real = provider.script_doctor

    def fake(layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        item = real(layers, owner=owner)
        item["payload"]["audit_status"] = "4"
        return item

    provider.script_doctor = fake  # type: ignore[method-assign]
    service.set_provider(provider)

    pid, _shot = await _first_shot(service)
    # script-doctor 的 target 恒为空串（`_target_of` 只认 shot: / asset:），正好覆盖这条路。
    await _act(service, "script-doctor", pid=pid)

    state = await service.state("local")
    charge = state["charges"][-1]
    assert charge["action"] == "script-doctor"
    assert charge["state"] == "released"
    assert charge["cost"] == 0
    assert charge["auditRejected"] is True
    assert "产物清理未完成" in charge["note"]
    # 手算：script-doctor 定价 2 / 预占 3 -> 全额退回，余额仍是 980。
    assert state["ledger"]["balance"] == SEED_BALANCE
    assert state["ledger"]["frozen"] == 0


# ---------------------------------------------------------------- 11. 上传前拦
async def test_upload_is_blocked_before_anything_is_written(api: SimpleNamespace) -> None:
    """命中违规词的文件名 -> 400 content_blocked，不落盘、不占配额、不进 uploads。"""
    client = await login(api, "moderation-upload")
    await current_state(client)
    before_config = (await client.get(f"{BASE}/config")).json()

    blocked = await client.post(
        f"{BASE}/uploads?kind=reference&name={BLOCKED}.png",
        content=PNG,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["error"] == "content_blocked"

    after = await current_state(client)
    assert after["uploads"] == [], "被拒的上传不得留下记录"
    after_config = (await client.get(f"{BASE}/config")).json()
    assert after_config["media"]["files"] == before_config["media"]["files"]

    # 同一份文件换个正常文件名照旧能传（证明拦截只看内容/文件名，不误伤格式）。
    ok = await client.post(
        f"{BASE}/uploads?kind=reference&name=lin.png",
        content=PNG,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------- 12. config 下发
async def test_config_exposes_disclosure_and_label_positions(api: SimpleNamespace) -> None:
    client = await login(api, "moderation-config")
    config = (await client.get(f"{BASE}/config")).json()
    assert config["ai_disclosure"] == AI_DISCLOSURE, "空配置回落到 labels.AI_DISCLOSURE"
    assert config["ai_label_positions"] == ["container_metadata"]
    assert "in_frame_drawtext" not in config["ai_label_positions"]


# ---------------------------------------------------------------- 13. 端口不得偷偷出网
def test_moderation_module_never_imports_a_network_client() -> None:
    source = MODERATION_SRC.read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "urllib", "aiohttp"):
        assert forbidden not in source, f"审核端口不得引入 {forbidden}（假装能审像素比不做更糟）"
