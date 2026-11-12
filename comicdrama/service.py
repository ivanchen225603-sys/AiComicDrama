"""漫剧业务服务：服务端执行全部动作，SQLite 为真源。

资金守恒口径与前端一致（不要改写）：
- 流水 amount = 余额变动；reserve 把余额转冻结；settle 在冻结内核销（变动 0）；
  只有退差 / 释放 / 加余额才改变账面总额。
- held 保留冻结，释放写 kind=release（不是 refund）；已结算任务禁止再注入故障。
- 自检：980 + 非 seed 的 credit 流水 - 已结算任务 cost == balance + frozen。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from .compose import parse_resolution, parse_subtitle_mode
from .credentials import CredentialStore
from .errors import AppError
from .media_store import _LOCAL_URL, DramaMediaStore
from .mock_media import CAPABILITIES, LAST_MILE
from .moderation import (
    AUDIT_REJECT_MARK,
    LocalKeywordModeration,
    ModerationPort,
    NullModeration,
    audit_outcome,
    parse_label_positions,
    parse_reject_action,
)
from .pending_resolver import (
    OfflinePendingChargeResolver,
    PendingChargeResolver,
)
from .pricing import (
    ACTION_PRICES,
    CREDIT_LIMIT_MAX,
    DEFAULT_DAILY_CAP,
    PRICE_T2I,
    PRICE_VIDEO,
    VIDEO_PRICE_KEY,
    cap_of,
    effective_price,
    normalize_video,
    pricing_stale,
)
from .providers import DramaProvider, MockDramaProvider
from .providers.base import AI_DISCLOSURE, DEFAULT_AI_LABEL
from .reports import daily_report
from .sample_media import SAMPLE_HINT, SAMPLE_LABEL
from .script_diff import diff_scripts
from .store import DEFAULT_SETTINGS, MEDIA_HISTORY_MAX, DramaStore

logger = logging.getLogger(__name__)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# next(..., _MISSING) 的 Any 哨兵：运行期仍是 None，但让 mypy 不再把泛型元素类型
# 推断成 default 参数的类型，从而避免大量误报。
_MISSING: Any = None

# 生成动作定价（**本地口径**，不是上游真实单价；官方单次计费字段仍未实测）。
# 单一源在 `comicdrama/pricing.py`：本模块只是**同名再导出**，保持
# `service.ACTION_PRICES is pricing.ACTION_PRICES`，因为既有调用方从这里 import。
# 表里没有的动作 = 不调上游 = 不扣费（`_dispatch` 依赖这条语义）。
#
# 单次硬上限 = 定价 × 1.2（整数向上取整）：20→24、300→360、2→3、3→4。
# `CAP_RATIO_*` 与 `cap_of()` 同样只在 pricing.py 定义一次，这里只做别名导入。
# 单日上限 500 **只对真实 Provider 计数**：mock 是本地模拟，不花上游真钱，既不占额
# 也不被额拦（见 `_afford(simulated=...)` 与 `store.daily_spend`），所以整条离线演示
# （7 镜 × 300 = 2100 名义消费）在默认上限下能跑完；真实模式下图生视频一次冻结 360，
# 500 约够 1.6 次。

# 上传限制：本期只开 BGM 音频与角色/场景/道具参考图两类。
# 上限一律小于 media_store 的通用留存上限，给出更贴合用途的中文报错。
# 文件真实格式由 media_store 的魔数判定决定，不信任 Content-Type 与文件名后缀。
UPLOAD_LIMITS: dict[str, dict[str, Any]] = {
    "bgm": {
        "max_bytes": 20 * 1024 * 1024,
        "max_count": 5,
        "store_kind": "audio",
        "label": "BGM 音频",
    },
    "reference": {
        "max_bytes": 8 * 1024 * 1024,
        "max_count": 24,
        "store_kind": "image",
        "label": "角色/场景/道具参考图",
    },
}
UPLOAD_FILE_NAME_MAX = 80

# 上游判违规后「产物清理未完成」时追加到 charge.note 的说明。
# 不抛异常（钱已经退了），但必须留痕：否则用户看到凭据写着「已退款」却还能看到
# 被判定不合规的产物，而运维完全不知道清理漏了。
_CLEANUP_INCOMPLETE = "；未定位到生成目标，产物清理未完成，请人工检查"

# 上游 `auditStatus=4` 且收口策略是 `hold`（默认）时挂在凭据上的文案。
# 刻意写明「语义未实测」：值班看到这句话应当去人工确认，而不是当成已判违规。
_AUDIT_PENDING_NOTE = "上游审核态待确认（auditStatus=4，语义未实测），确认后自动结算或退款"


def _cap_of(price: int) -> int:
    """兼容旧调用方（含既有测试）的名字，实现只在 pricing.cap_of 一处。"""
    return cap_of(price)


# 上游「结果未知」的错误码：钱可能已经花出去，但我们拿不到结果。
# 这几种情况必须留痕（待复核）、保留冻结、禁止自动重提 —— 与关键帧任务里的
# held 状态同一口径。实测的就是这条：原来 10 个生成动作遇到超时会静默回滚，
# 用户再点一次，上游就被调了第二次，钱花两遍而本地账本一无所知。
UNKNOWN_OUTCOME_CODES: frozenset[str] = frozenset(
    {
        "drama_generation_timeout",
        "drama_generation_held",
        "drama_llm_unavailable",
        "drama_tts_unavailable",
        "drama_upstream_timeout",
    }
)

# 「肯定没调到上游」的本地失败：配置缺失、凭据没配、本机工具缺失。
# 这几个码也是 5xx，但都是**我们自己**在出网之前抛的，上游一个字节都没收到。
# 复测指出按状态码一刀切会把它们判成「结果未知」：首单漏配一项，对应动作就被
# 冻结 + 锁住，要管理员逐条复核才能再试 —— 资金安全，但首单要多几次手工操作。
# 所以这些码先于状态码判定，一律整笔回滚、不扣费、不留待复核。
LOCAL_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "drama_config",  # 模板 uuid / prompt 节点没配
        "drama_tts_config",  # TTS 地址没配
        "drama_credentials",  # 密钥环境变量没设
        "drama_ffmpeg",  # 本机 ffmpeg 缺失或执行失败（纯本地，无上游计费）
    }
)


class _PendingReview(Exception):
    """内部信号：这次动作要「落一条待复核凭据」并且仍以错误返回给调用方。

    不能直接 raise AppError —— 那样 `_action_sync` 会把内存里的状态一起丢掉，
    「待复核」记录等于没写。所以先抛这个，由 `_action_sync` 落库后再转成 504。
    """

    def __init__(self, code: str, message: str, status: int = 504) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _AuditPending(Exception):
    """把上游审核态带进 `_hold_for_review` 的轻量载体。

    没有异常语义、也不会被抛给调用方：`_hold_for_review` 只从 `exc.usage` 里读
    上游真值（`upstream_points` / `account_balance` / `upstream_task_id` /
    `audit_status`），所以直接把 Provider 的 payload 挂上去即可，不需要伪造一次失败。
    """

    def __init__(self, payload: Any) -> None:
        super().__init__("audit_pending")
        self.code = "audit_pending"
        self.usage = payload if isinstance(payload, dict) else {}


class _DeferredCall(Exception):
    """内部信号：动作要出网了，这一步必须挪到 owner 锁外面做。

    为什么需要它：`_action_sync` 是「读快照 -> 内存状态机（含出网）-> 一次写库」，
    整段挂在同一个 owner 的 asyncio 锁上。上游一次真实生成要 2 秒到 900 秒，
    这段时间里同一用户连保存剧本、切项目、推进任务进度都要排队（
    2 秒的生成里保存剧本等了 1.8 秒）。

    真正的约束不是「不能并发」，而是「不能有两个线程同时拿着同一份快照写回」。
    出网那一下既不读也不写状态，完全可以放出去。所以：状态机跑到第一个
    Provider 调用就抛这个信号 → 上层把**已经预占好的状态**落库 → 放开锁出网
    → 拿到结果（或异常）后重新进锁、重新读快照、把结果续跑完。
    """

    def __init__(self, thunk: Callable[[], Any], charge_id: str | None = None) -> None:
        super().__init__("provider call deferred out of the owner lock")
        # 锁外要执行的那一下（真正打上游）。
        self.thunk = thunk
        # 这次出网已经预占的凭据 id。空表示该动作不扣费（定价 0），无需落库。
        self.charge_id = charge_id


class _DeferredProvider:
    """放在 ctx["provider"] 里的 Provider 代理，负责「出网挪到锁外」。

    第 1 趟：任何方法都不真调，直接抛 `_DeferredCall`，把「真调用」装进 thunk。
    第 2 趟起：按顺序回放已经拿到的结果；若动作内部还要再调一次上游（理论上
    不该有，这里留余量），超出回放长度就再次抛 `_DeferredCall` 继续下一轮。

    回放里如果装的是异常，就原样抛出去 —— 这样 `_charged` 的
    「明确失败 / 结果未知」分支完全不用改，两条路径看到的东西一模一样。
    """

    def __init__(
        self,
        inner: DramaProvider,
        replay: list[Any] | None = None,
        charge_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._replay = list(replay or [])
        self._index = 0
        self.charge_id = charge_id

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            # name / 其它属性直通，`config()` 与 `_dispatch_inner` 里还在用。
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self._index < len(self._replay):
                outcome = self._replay[self._index]
                self._index += 1
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            raise _DeferredCall(lambda: attr(*args, **kwargs), self.charge_id)

        return call


# 用户自设的积分使用上限。single = 单次生成最多花多少；total = 从开启起累计最多花多少。
# 两者为 None 表示「不限」，此时可用额度回落为账户全部可用余额（只受余额本身约束）。
# 上限只约束「花钱」，不约束充值、退款与免费动作（成片合成、保存配音）。
# 合法范围的上界 CREDIT_LIMIT_MAX 由 .pricing 单一源导入（见文件头 import）。


def _limit_value(value: Any, field: str) -> int | None:
    """空串 / None = 不限；否则必须是 0 ~ CREDIT_LIMIT_MAX 的整数。

    数字语义与前端 `store.js` 的 `val()` 保持一致（允许 `"30.0"` / `1e2` 这类
    数值写法），但必须额外挡住 NaN / ±Inf：它们会让 `int()` 抛 ValueError /
    OverflowError 逃出 action 的参数包装，变成 500 且错误消息里带 Python 内部
    异常文本。所有非法值一律收敛成 `invalid_credit_limit`（400），前端
    `ERROR_TEXT` 已有对应文案。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        _fail("invalid_credit_limit", f"{field} 必须是整数")
    try:
        number = float(str(value).strip())
    except (ValueError, OverflowError):
        _fail("invalid_credit_limit", f"{field} 必须是整数")
    if (
        not math.isfinite(number)
        or number != int(number)
        or number < 0
        or number > CREDIT_LIMIT_MAX
    ):
        _fail("invalid_credit_limit", f"{field} 必须是 0 到 {CREDIT_LIMIT_MAX} 之间的整数")
    return int(number)


# 需要跨动作保留、在页面上长期展示的生成结果（见 DramaService._remember）。
KEEP_RESULTS = ("script-doctor",)


def _fail(code: str, message: str, status: int = 400) -> NoReturn:
    raise AppError(code, message, status)


# 资产分类白名单。前端只发这三个键；服务端必须自己校验，
# 否则 assets[typ] 会 KeyError 抛 500。
ASSET_TYPES: tuple[str, ...] = ("characters", "scenes", "props")


def _as_int(value: Any, field: str, minimum: int | None = None) -> int:
    """把请求体里的值转成整数，失败时抛可读的 400 而不是 500。"""
    if isinstance(value, bool) or value is None:
        raise AppError("invalid_field", f"{field} 必须是整数，收到 {value!r}", 400)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AppError("invalid_field", f"{field} 必须是整数，收到 {value!r}", 400) from None
    if minimum is not None and number < minimum:
        raise AppError("invalid_field", f"{field} 不能小于 {minimum}，收到 {number}", 400)
    return number


def _as_float(value: Any, field: str, minimum: float | None = None) -> float:
    """同 _as_int，但用于 qps 这类浮点字段。

    实测里 savePricing 传 qps="abc" 仍然 500 —— 因为原实现直接 float()。
    这里统一转成可读 400。
    """
    if isinstance(value, bool) or value is None:
        raise AppError("invalid_field", f"{field} 必须是数字，收到 {value!r}", 400)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AppError("invalid_field", f"{field} 必须是数字，收到 {value!r}", 400) from None
    if minimum is not None and number < minimum:
        raise AppError("invalid_field", f"{field} 不能小于 {minimum}，收到 {number}", 400)
    return number


# 关键帧候选张数：1 张是既有行为，4 张是上游单价可接受的上限。
# 超过 4 张一次出网的成本会显著放大，而「多候选」的目的是挑一张，
# 不是批量囤图；夹取而不是报错，是为了让旧客户端传 0 / 空串也能继续工作。
CANDIDATES_MIN = 1
CANDIDATES_MAX = 4


def _candidate_count(value: Any) -> int:
    """把候选张数夹到 1..4：**永不抛异常**。

    为什么夹取而不是 400：这个值只是「多要几张」的可选参数，不是身份也不是金额。
    旧客户端根本不传它，手滑传个空串 / 0 也很常见——为它把「出关键帧」这条主流程
    打成 400，等于用一个次要参数废掉主功能。规则：空串 / None / 非数字 → 1（最小
    合法值）；0 与负数 → 1；小数向下取整；大于 4 → 4。
    """
    if value is None or isinstance(value, bool):
        return CANDIDATES_MIN
    try:
        number = int(float(str(value).strip() or CANDIDATES_MIN))
    except (TypeError, ValueError, OverflowError):
        return CANDIDATES_MIN
    return max(CANDIDATES_MIN, min(CANDIDATES_MAX, number))


def _as_dict(value: Any, field: str) -> dict[str, Any]:
    """把「必须是对象」的入参挡成可读 400，而不是让 .get() 抛 AttributeError。

    复测：`createProject("字符串")` / `saveScript("字符串")` 仍是 500 ——
    因为 `arg(0, {}) or {}` 会把字符串**原样返回**（非空即真），随后 `f.get(...)`
    抛 AttributeError；当时 `_action_sync` 的兜底只接了 4 种异常，漏了它。
    这里显式判类型，`None` 仍按空对象处理（前端省略参数是合法的）。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AppError(
            "invalid_field",
            f"{field} 必须是 JSON 对象，收到 {type(value).__name__}",
            400,
        )
    return value


def _request_of(note: str) -> str:
    """从流水备注里精确取出 requestId。

    旧实现用 `request_id in note` 做子串匹配：先加 `review-r10`，再加
    `review-r1` 就会被误判成重复请求（`review-r1` 是 `review-r10` 的子串）。
    这里取最后一个标记之后的整段做**相等**比较，前缀重叠不再互相误伤。
    """
    marker = "requestId="
    index = note.rfind(marker)
    return note[index + len(marker) :].strip() if index >= 0 else ""


def _reid_project(proj: dict[str, Any], new_pid: str) -> dict[str, Any]:
    """复制项目时重生成所有子对象 id。

    drama_script_versions / drama_assets / drama_shots / drama_exports 的 id 是
    全局主键，直接 deepcopy 会和源项目撞主键；同时要同步 frozenVersionId、
    timeline.order 与 frames 的 shot-id 键，避免副本内部引用指向源项目。
    """
    import copy

    clone = copy.deepcopy(proj)
    clone["id"] = new_pid
    clone["name"] = str(proj.get("name", "")) + " · 副本"
    clone["archived"] = False
    clone["createdAt"] = time.strftime("%m-%d %H:%M", time.localtime())

    script = clone.get("script") or {}
    vmap: dict[str, str] = {}
    for version in script.get("versions", []):
        old = str(version.get("id"))
        new = _uid("v")
        vmap[old] = new
        version["id"] = new
    frozen = script.get("frozenVersionId")
    if frozen:
        script["frozenVersionId"] = vmap.get(str(frozen), frozen)

    smap: dict[str, str] = {}
    for shot in clone.get("shots", []):
        old = str(shot.get("id"))
        new = _uid("S")
        smap[old] = new
        shot["id"] = new
    timeline = clone.get("timeline") or {}
    timeline["order"] = [smap.get(str(x), x) for x in timeline.get("order", [])]
    for note in timeline.get("notes", []):
        if isinstance(note, dict) and "id" in note:
            note["id"] = _uid("n")
    frames = clone.get("frames") or {}
    clone["frames"] = {smap.get(str(k), k): v for k, v in frames.items()}

    for items in (clone.get("assets") or {}).values():
        for asset in items:
            asset["id"] = _uid("a")
    for export in clone.get("exports", []):
        export["id"] = _uid("ex")
    audio = clone.get("audio") or {}
    for key in ("voices", "subtitles"):
        for item in audio.get(key, []):
            if isinstance(item, dict) and "id" in item:
                item["id"] = _uid("t")
    return clone


class DramaService:
    def __init__(
        self,
        path: Path,
        provider: DramaProvider | None = None,
        *,
        admins: frozenset[str] = frozenset({"local"}),
        daily_cap: int = DEFAULT_DAILY_CAP,
        autopump: bool = True,
        pump_seconds: float = 2.0,
        media_quota: int = 0,
        ai_label: str = DEFAULT_AI_LABEL,
        ai_disclosure: str = "",
        audit_provider: str = "local",
        audit_blocklist: str = "",
        audit_reject_action: str = "hold",
        ai_label_positions: str = "container_metadata",
        retention_days: int = 0,
    ) -> None:
        self.store = DramaStore(path)
        # 真实素材落在这里：上游 7 天 URL 一律由 Provider 立即转存成本地文件。
        self.media_store = DramaMediaStore(path.parent / "drama_media", quota_bytes=media_quota)
        # 生成能力一律经 Provider；默认离线 mock，显式配置才换成真实适配器。
        self.provider: DramaProvider = provider or MockDramaProvider()
        self._attach_media_store()
        # 用户自己填写的模型密钥（按账号）。填了 Liblib 密钥的账号走按能力组合的
        # 真实 Provider，其余账号沿用部署默认的 self.provider。
        self.credentials = CredentialStore(self.store)
        self.remote_template: Any = None
        self._owner_providers: dict[str, tuple[str, Any]] = {}
        # 待确认凭据的解析器。本期唯一实现是本地模拟：查不到就 unknown，
        # 保持 pending_review，不无声冻结。真实实现待付费 / 待公网部署。
        self.pending_resolver: PendingChargeResolver = OfflinePendingChargeResolver()
        # 管理类动作（加余额 / 释放 held）只对管理员白名单开放：
        # 默认本地演示态是 {"local"}，bearer 模式必须显式配置，否则零管理员（fail-closed）。
        self.admins = frozenset(admins)
        # 单日生成消耗上限（积分）。防止一次会话把 980 种子余额烧穿，也让「花费上限」
        # 有一条可实测的红线，而不是只写在文档里。
        self.daily_cap = max(0, int(daily_cap))
        # 服务端自动推进：任务不再依赖客户端反复调 tick。关掉它只影响「自动推」，
        # tick 动作仍然保留（测试与排障需要确定性推进）。
        self.autopump = bool(autopump)
        self.pump_seconds = max(0.2, float(pump_seconds))
        # AI 生成标识文本。渲染进导出记录、混流容器元数据与前端徽章。
        self.ai_label = str(ai_label or DEFAULT_AI_LABEL).strip() or DEFAULT_AI_LABEL
        # AI 生成披露文案：`/config` 下发、导出记录、成片容器元数据共用**同一个**
        # 字符串（由 api.py 从 settings.drama_ai_disclosure 传入）。
        self.ai_disclosure = str(ai_disclosure or "").strip() or AI_DISCLOSURE
        # AI 标识位置清单：非法值丢弃并退回默认；默认绝不含 in_frame_drawtext。
        self.ai_label_positions = parse_label_positions(ai_label_positions)
        # 上游报出 `auditStatus=4` 后怎么收口：hold（默认，冻结待确认）/ refund / settle。
        # **默认不是退款**：`4` 的语义从未实测（成功样本是 3，同协议里 `generateStatus`
        # 的 4 是「审核中」），按拒审直接退款可能白送钱；先冻结最坏只是晚点收口。
        # 非法值退回 hold（见 `parse_reject_action`），不抛异常炸启动。
        self.audit_reject_action = parse_reject_action(audit_reject_action)
        # 素材保留天数。0 = 不设策略（清理时仍用 7 天默认值）。
        # **刻意不做「到点自动删」的后台任务**：删除不可逆，而「哪些文件还被引用」
        # 这份清单的完整性只能靠 `_live_asset_ids` 逐个落点枚举来保证——漏一个落点，
        # 用户就会在毫不知情的时候丢掉正在用的成片。所以保留策略是「部署方声明期望 +
        # 管理员一键按策略清理（默认先预览）」，执行者始终是人。
        self.retention_days = max(0, int(retention_days))
        # 审核端口：默认本地关键词规则（**只审文本，审不了像素**）。
        # 词表从配置一次性解析成元组，不在每次审核时重新 split。
        self.moderator: ModerationPort = self._make_moderator(audit_provider, audit_blocklist)
        # 按 owner 分锁。原实现是一把全局锁，会把所有用户的所有动作串行化：
        # 一个真实生成动作（上游最长 900s）期间，别人连保存剧本、推进任务进度
        # 都要排队。分锁后同一 owner 仍然串行（保证读-算-写无并发窗口），
        # 不同 owner 互不阻塞。
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._pump: asyncio.Task[None] | None = None
        # 每次 pump 推进了几个 owner，供 /config 与排障观察。
        self._pump_ticks = 0

    @staticmethod
    def _make_moderator(provider: str, blocklist: str) -> ModerationPort:
        """按配置构造审核端口。默认 local；只有显式配 null 才关闭审核。"""
        words = [item.strip() for item in str(blocklist or "").split(",") if item.strip()]
        if str(provider or "").strip().lower() == "null":
            return NullModeration()
        return LocalKeywordModeration(words)

    def set_moderator(self, moderator: ModerationPort) -> None:
        """替换审核端口（测试注入确定结果 / 未来接真实审核服务）。

        与 `set_provider` / `set_pending_resolver` 同一风格：审核可以被替换，
        但默认实现只能经 `__init__` 从配置装配，别处不许悄悄改。
        """
        self.moderator = moderator

    def _attach_media_store(self) -> None:
        """离线示例素材必须和真实素材落在同一个 owner 隔离目录里。

        MockDramaProvider 单独构造时用临时目录兜底（测试可独立运行）；装配进
        服务后一律改挂 self.media_store，否则示例文件的 URL 在 HTTP 层取不到。
        """
        attach = getattr(self.provider, "attach", None)
        if callable(attach):
            attach(self.media_store)

    def set_provider(self, provider: DramaProvider) -> None:
        """运行期切换 Provider 的唯一入口（api.py 按配置装配时调用）。

        直接改实例属性会漏掉「离线 Provider 要挂隔离素材目录」这一步；测试里
        切换模式也必须走这里，保证与真实部署同一条路径。
        """
        self.provider = provider
        self._attach_media_store()

    def set_remote_template(self, template: Any) -> None:
        """部署方的真实模式公共配置（公网地址、超时、FFmpeg、标识文案、配音端点）。"""
        self.remote_template = template
        self._owner_providers.clear()

    def provider_for(self, owner: str) -> Any:
        """这个账号本次该用哪个 Provider。

        没填 Liblib 密钥 → 部署默认（通常是离线示例）；填了 → 图片与视频走 Liblib，
        文本按是否填了文本模型决定走 LLM 还是内置规则，配音按部署是否配置 TTS 决定。
        结果按「账号 + 密钥指纹」缓存，改了密钥下一次请求立即生效。
        """
        creds = self.credentials.get(owner)
        if not (creds.get("liblib_access_key") and creds.get("liblib_secret_key")):
            self._owner_providers.pop(owner, None)
            return self.provider
        fingerprint = json.dumps(creds, sort_keys=True)
        cached = self._owner_providers.get(owner)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        from dataclasses import replace

        from .providers.hybrid import HybridDramaProvider
        from .providers.remote import RemoteDramaConfig, RemoteDramaProvider

        base = self.remote_template or RemoteDramaConfig(llm_base_url="", llm_model="")
        secrets = {
            base.access_key_env: creds["liblib_access_key"],
            base.secret_key_env: creds["liblib_secret_key"],
        }
        has_llm = bool(
            creds.get("llm_base_url") and creds.get("llm_model") and creds.get("llm_api_key")
        )
        if has_llm:
            secrets[base.llm_key_env] = creds["llm_api_key"]
        has_tts = all(
            creds.get(k) for k in ("tts_base_url", "tts_model", "tts_api_key", "tts_voice")
        )
        if has_tts:
            secrets[base.tts_key_env] = creds["tts_api_key"]
        cfg = replace(
            base,
            secrets=secrets,
            require_llm=False,
            llm_base_url=creds.get("llm_base_url", "") if has_llm else "",
            llm_model=creds.get("llm_model", "") if has_llm else "",
            # 用户自填密钥一律走官方通道：不需要部署方配置任何工作流模板。
            t2i_channel="official",
            i2v_channel="official",
            **(
                {
                    "tts_url": creds["tts_base_url"],
                    "tts_mode": "openai",
                    "tts_model": creds["tts_model"],
                    "tts_voices": tuple(
                        v.strip()
                        for v in creds["tts_voice"].replace("，", ",").split(",")
                        if v.strip()
                    ),
                }
                if has_tts
                else {}
            ),
        )
        real = RemoteDramaProvider(cfg, self.media_store)
        fallback = (
            self.provider if getattr(self.provider, "name", "") == "mock" else MockDramaProvider()
        )
        attach = getattr(fallback, "attach", None)
        if callable(attach):
            attach(self.media_store)
        hybrid = HybridDramaProvider(
            real,
            fallback,
            text=real if has_llm else None,
            tts=real if cfg.tts_url else None,
        )
        self._owner_providers[owner] = (fingerprint, hybrid)
        return hybrid

    def set_pending_resolver(self, resolver: PendingChargeResolver) -> None:
        """替换待确认凭据解析器（测试注入确定结果 / 未来接真实实现）。"""
        self.pending_resolver = resolver

    async def _lock_for(self, owner: str) -> asyncio.Lock:
        """取（必要时创建）某个 owner 的串行锁。

        创建过程本身要加锁，否则两个并发首请求可能各自 new 一个 Lock
        并同时进入临界区，分锁就白做了。
        """
        async with self._locks_guard:
            lock = self._locks.get(owner)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[owner] = lock
            return lock

    async def start(self) -> None:
        await asyncio.to_thread(self.store.install)
        # 上次进程如果在「出网中」被杀，会留下 in_flight 凭据：钱冻着、结果没落地。
        # 必须先转成待复核，否则那笔冻结在管理页里根本看不到（也没人能复核）。
        await asyncio.to_thread(self._sweep_in_flight)
        await asyncio.to_thread(self._sweep_in_flight_tasks)
        # 切换 Provider 后打开同一个库：把上次模式遗留的、还没结算的关键帧任务
        # 自动退还预占并标注去向，不让它们无声冻结在队列里。
        for owner in await asyncio.to_thread(self.store.owners_with_active_tasks):
            try:
                await self.action(owner, "reconcileMode", [], None, None)
            except Exception:
                logger.exception("drama: reconcileMode failed for %s on startup", owner)
        # 崩溃恢复：上次退出时留下的带编号待确认凭据，重启后立刻再问一次。
        # 解析器没结论时是纯读，不会写库、不会重复入账。
        await self.sweep_pending_charges()
        if self.autopump and self._pump is None:
            self._pump = asyncio.create_task(self._pump_loop(), name="drama-pump")

    def _sweep_in_flight(self) -> None:
        swept = 0
        for owner in self.store.owners_with_in_flight():
            swept += self.store.promote_in_flight(owner)
        if swept:
            logger.warning(
                "drama: %d 条 in_flight 凭据在上次退出时没有结果，已转为待复核（需管理员确认）",
                swept,
            )

    def _sweep_in_flight_tasks(self) -> None:
        """关键帧任务也有「出网中」瞬态：进程被杀时按结果未知转 held。"""
        swept = 0
        for owner in self.store.owners_with_in_flight_tasks():
            swept += self.store.promote_in_flight_tasks(owner)
        if swept:
            logger.warning(
                "drama: %d 个关键帧任务在上次退出时停在 in_flight，"
                "已转为 held（冻结保留，需管理员带原因释放）",
                swept,
            )

    async def close(self) -> None:
        if self._pump is not None:
            # 先取消再等，避免关闭时 pump 正持着锁去写一个已关闭的连接。
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        await asyncio.to_thread(self.store.close)

    async def _pump_loop(self) -> None:
        """每 pump_seconds 推进一次有活动任务的项目。

        走的是与 HTTP 完全相同的 `action("tick")` 通道：同一把 owner 锁、
        同一次读-算-写、同一条守恒自检。pump 只是替客户端把那一下点了。
        """
        while True:
            await asyncio.sleep(self.pump_seconds)
            try:
                owners = await asyncio.to_thread(self.store.owners_with_active_tasks)
                for owner in owners:
                    await self.action(owner, "tick", [], None, None)
                    self._pump_ticks += 1
                # 即使没有活动任务，也要按上游编号继续查待确认凭据。
                # sweep 只在真有结论时才写库，unknown / still_running 是纯读。
                await self.sweep_pending_charges()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单轮失败不能让 pump 死掉，否则任务会永久停住且没有任何提示。
                logger.exception("drama pump failed; will retry next tick")

    async def state(self, owner: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.state, owner)

    async def daily_report(self, owner: str, *, days: int = 30) -> dict[str, Any]:
        """按日对账报表（管理员）。纯读：不写库、不改价。

        报表本身是纯函数（见 `reports.daily_report`），这里只负责取快照与闸门。
        闸门放在服务层而不是路由层，理由与 `savePricing` 一致：同一个能力被
        第二个入口复用时，权限判断不能只长在某一条路由上。
        """
        self._require_admin(owner, "dailyReport")
        state = await asyncio.to_thread(self.store.state, owner)
        return daily_report(state, days=days)

    async def config(self, owner: str) -> dict[str, Any]:
        state = await self.state(owner)
        day = time.strftime("%Y-%m-%d", time.localtime())
        # 下发**生效价**而不是目录价：管理员把图生视频改成 200 后，按钮上写 300
        # 就是违约（ui.js 承诺「用户在点之前就知道扣多少」）。
        prices = {
            name: price
            for name in ACTION_PRICES
            if (price := self._effective_price(name, state)) is not None
        }
        keyframe_price = int(state["pricing"]["t2i"])
        keyframe_cap = int(state["pricing"]["cap"])
        provider = self.provider_for(owner)
        template = self.remote_template
        byok = self.credentials.view(owner)["liblib"]["configured"]
        if byok:
            prices = {name: 0 for name in prices}
        return {
            "provider": provider.name,
            "offline": provider.name == "mock",
            # 用户自填密钥的打码视图与部署前提（视频首帧需要公网 https 地址）。
            "credentials": self.credentials.view(owner),
            "public_base_url_set": bool(template is not None and template.public_base_url),
            "tts_configured": bool(template is not None and template.tts_url)
            or bool(self.credentials.view(owner)["tts"]["configured"]),
            "owner": owner,
            "points_per_cny": 100,
            "capabilities": list(CAPABILITIES),
            "last_mile": list(LAST_MILE),
            # 生成动作的真实定价与上限：前端据此在按钮上标价，避免「点了才知道扣费」。
            "prices": prices,
            # 关键帧走的是 `pricing.t2i` / `pricing.cap`（逐镜批量预占），
            # 不在 ACTION_PRICES 里，必须单独下发，否则报价预览只能靠前端翻 state。
            "keyframe_price": 0 if byok else keyframe_price,
            "keyframe_cap": 0 if byok else keyframe_cap,
            # 用户自带 Liblib 密钥：费用记在用户自己的 Liblib 账户上，平台不扣积分。
            "byok": byok,
            "pricing_stale": pricing_stale(state.get("pricing")),
            # 真实模式下如实下发「现在用的是哪条通道」，供管理页显示；mock 没有
            # channels 属性，用 getattr 兜成空 dict，不因为 mock 而报错。
            "channels": getattr(provider, "channels", {}) or {},
            "daily_cap": self.daily_cap,
            "daily_used": await asyncio.to_thread(self.store.daily_spend, owner, day),
            "daily_day": day,
            "admin": owner in self.admins,
            # 服务端自动推进：前端据此说明「不必手动点推进」，也便于排障。
            "autopump": self.autopump,
            "pump_seconds": self.pump_seconds,
            "pump_ticks": self._pump_ticks,
            # AI 生成标识与技术披露口径（不等于备案结论，法务仍需确认）。
            "ai_label": self.ai_label,
            "ai_disclosure": self.ai_disclosure,
            # 标识位置清单：默认只有容器元数据；in_frame_drawtext（烧进画面）必须由
            # 部署方显式打开——烧进像素不可撤回，本期不实现。
            "ai_label_positions": list(self.ai_label_positions),
            # 素材保留策略：前端据此把清理弹窗的默认天数与服务端口径对齐，
            # 并在页面上说明「到期的只有没人引用的文件」。
            "media_retention_days": self.retention_days,
            # 离线示例素材的三处统一标识：画面内、页面上、结果 provider 标记。
            "sample_label": SAMPLE_LABEL,
            "sample_hint": SAMPLE_HINT,
            "offline_demo": provider.name == "mock",
            # 上传限制：前端据此显示格式/大小/数量，服务端仍独立校验。
            "upload_limits": {
                key: {
                    "max_bytes": value["max_bytes"],
                    "max_count": value["max_count"],
                    "label": value["label"],
                    "store_kind": value["store_kind"],
                }
                for key, value in UPLOAD_LIMITS.items()
            },
            # 素材占用：配额为 0 表示不限。管理页据此显示用量条。
            "media": self.media_store.stats(owner),
            "endpoints": [
                "GET /api/v1/drama/config",
                "GET /api/v1/drama/state",
                "POST /api/v1/drama/action",
                "POST /api/v1/drama/reset",
                "GET /api/v1/drama/export",
                "POST /api/v1/drama/import",
                "GET /api/v1/drama/assets/{asset_id}",
                "POST /api/v1/drama/uploads",
                "DELETE /api/v1/drama/uploads/{upload_id}",
                "GET /api/v1/drama/reconcile",
            ],
            "projects": len(state["projects"]),
            "storage": "drama.sqlite",
        }

    # 一个动作最多跑几趟「出网」。正常动作只调一次上游，留余量是为了万一
    # 某个动作按资产/镜头循环调用也要能跑完；真的超了就报错，不死循环。
    MAX_PROVIDER_ROUNDS = 8

    async def action(
        self,
        owner: str,
        action_type: str,
        args: list[Any],
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """执行一个动作。锁只在「读写状态」的那两小段上，出网整段在锁外。

        原来整段（含 2 秒的 Provider 调用）都在 `async with lock` 里，
        同一用户保存剧本要等生成跑完。现在改成：
          1) 锁内跑到第一个 Provider 调用 → 预占状态落库 → 抛 _DeferredCall 解锁；
          2) 锁外真正打上游（这 2 秒里别人可以随意保存剧本 / 推进进度）；
          3) 重新进锁、重新读快照、把结果续跑完并结算。
        写快照仍然是「锁内读 + 锁内写」，不会出现两个线程各拿一份旧快照写回。
        """
        lock = await self._lock_for(owner)
        rid = (request_id or "").strip()
        replay: list[Any] = []
        resume: str | None = None
        first = True
        for _ in range(self.MAX_PROVIDER_ROUNDS):
            proxy = _DeferredProvider(self.provider_for(owner), replay=replay, charge_id=resume)
            deferred: _DeferredCall | None = None
            try:
                async with lock:
                    if first:
                        hit = await self._idempotent_replay(owner, action_type, rid)
                        if hit is not None:
                            return hit
                    return await asyncio.to_thread(
                        self._action_sync,
                        owner,
                        action_type,
                        args,
                        project_id,
                        rid,
                        proxy,
                        resume,
                    )
            except _DeferredCall as exc:
                # 预占凭据 id 由 _charged 补上；定价 0 的动作没有凭据，保持空。
                deferred = exc
                resume = exc.charge_id or resume
                first = False
            # 走到这里锁已经放开：真正的出网在这一行。
            try:
                outcome: Any = await asyncio.to_thread(deferred.thunk)
            except Exception as exc:  # noqa: BLE001
                # 把失败也当成「结果」回放：_charged 用同一套 _unknown_outcome 判定
                # 「明确没执行（整笔退回）」还是「结果未知（留待复核）」。
                outcome = exc
            replay.append(outcome)
        _fail(
            "action_too_many_rounds",
            f"「{action_type}」一次请求内反复出网超过 {self.MAX_PROVIDER_ROUNDS} 轮，已中止",
            502,
        )

    async def _idempotent_replay(
        self, owner: str, action_type: str, rid: str
    ) -> dict[str, Any] | None:
        """幂等检查：同一个 requestId 只允许真正执行一次。调用方必须持 owner 锁。"""
        if not rid:
            return None
        hit = await asyncio.to_thread(self.store.charge_by_request, owner, rid)
        if hit is None:
            return None
        if hit.get("action") != action_type:
            # 同一个 requestId 换了动作：绝不能回放上一个动作的结果。
            # 实测「同键不同动作返回了上一个动作的结果」，那是拿错了凭据。
            # 语义上这就是客户端串了键，直接 409 让它换个键，不做任何回放。
            _fail(
                "idempotency_conflict",
                f"requestId={rid} 已用于动作「{hit.get('action')}」，"
                f"不能再用它执行「{action_type}」；请换一个新的 requestId",
                409,
            )
        if hit.get("state") == "in_flight":
            # 上一趟还在锁外出网，结果没落地。此时既不能回放（没结果），
            # 也不能再执行一次（上游会被调两遍），只能让客户端等。
            _fail(
                "action_in_flight",
                f"requestId={rid} 对应的「{hit['action']}」正在执行中"
                f"（已预占 {hit['cap']} 积分），结果尚未落地；"
                "请等待当前请求返回，不要重复提交",
                409,
            )
        # 凭据还悬着（上游结果未知）时不能当「幂等命中」回放一个空结果：
        # 第一次请求返回的是 504，用同一个键再请求必须还是 409/504 语义，
        # 否则客户端会以为「这次成功了，只是没结果」。
        if hit.get("state") == "pending_review":
            _fail(
                "pending_review",
                f"requestId={rid} 对应的「{hit['action']}」上游结果未知"
                f"（凭据 {hit['id']}，冻结 {hit['cap']} 积分），"
                "需管理员复核后才能重新提交",
                409,
            )
        # 幂等命中：不重复执行、不重复扣费，回放上次结果。
        # 这是「生成按钮被连点 / 客户端超时重试」唯一安全的处理方式——
        # 真实 Provider 重放一次就是真金白银。
        state = await self.state(owner)
        return {
            "state": state,
            "result": json.loads(hit["result"]) if hit["result"] else None,
            "message": f"幂等命中：requestId={rid} 已处理，未重复执行、未重复扣费",
        }

    def _action_sync(
        self,
        owner: str,
        action_type: str,
        args: list[Any],
        project_id: str | None,
        request_id: str = "",
        provider: Any = None,
        resume_charge: str | None = None,
    ) -> dict[str, Any]:
        # 生成动作可能出网数分钟，绝不能放在 BEGIN IMMEDIATE 里执行：那会把
        # SQLite 写锁一直占住。现在改成「只读快照 -> 内存状态机（含 Provider 调用）
        # -> 一次写事务落库」。action() 负责串行：同一进程内这段读-算-写没有并发
        # 窗口。Provider 调用经 ctx["provider"]（_DeferredProvider）抛出、在锁外执行，
        # 所以「跑得久」不再等于「把别人挡住」。
        # 与旧实现的差别：新 owner 首次动作失败时，开户记录仍会提交
        # （旧实现会随事务回滚）。失败时不写任何业务状态，这一点不变。
        self.store.ensure_owner(owner)
        state = self.store.read(lambda db: self.store._state_in(db, owner))
        creds = self.credentials.get(owner)
        state["_byok"] = bool(creds.get("liblib_access_key") and creds.get("liblib_secret_key"))
        pid = project_id or state["activeProjectId"]
        project = next((p for p in state["projects"] if p["id"] == pid), _MISSING)
        ctx: dict[str, Any] = {
            "owner": owner,
            "state": state,
            "project": project,
            "args": args,
            "messages": [],
            "media_pending": [],
            "request_id": request_id,
            # Provider 一律从 ctx 取，不再读 self.provider：同一进程里不同 owner
            # 的动作会并发跑在各自线程上，改实例属性会串。
            "provider": provider if provider is not None else self.provider,
            # 非空表示「这是出网回来后的续跑」，预占已经落库，不要再预占一次。
            "resume_charge": resume_charge,
        }
        initial_active = state["activeProjectId"]
        pending: _PendingReview | None = None
        deferred: _DeferredCall | None = None

        def commit(result: Any) -> dict[str, Any]:
            """把内存状态一次写库（含审计与待插入素材）。"""
            if state["activeProjectId"] == initial_active and any(
                p["id"] == pid for p in state["projects"]
            ):
                # 动作没有显式切换项目：保留请求上下文的当前项目，
                # 否则服务端会把客户端强行拉回「第一个未归档项目」。
                # 但项目已被删掉时不能再指回去（续跑期间被删就是这条路）。
                state["activeProjectId"] = pid
            state.pop("_byok", None)
            self._refresh_film_stale(state)
            self._assert_conservation(state)

            def op(db: Any) -> dict[str, Any]:
                for media in ctx["media_pending"]:
                    db.execute("INSERT INTO drama_media VALUES(?,?,?,?,?,?,?,?)", media)
                self.store.replace_in(db, owner, state)
                db.execute(
                    "INSERT INTO drama_audit VALUES(?,?,?,?,?)",
                    (_uid("a"), owner, action_type, str(args)[:500], time.time()),
                )
                return {"state": self.store._state_in(db, owner), "result": result, "message": ""}

            return self.store.run(op)

        def rollback_if_needed() -> None:
            """异常出口：该退的钱一定要写回库，不能只存在于即将丢弃的字典里。

            两种情况：
            - `persist`：`_release_charge` 已经把退款写进内存状态，只差落盘；
            - 续跑（resume_charge）时在 `_charged` 之外抛的失败（项目被删、
              凭据被重置 / 被导入覆盖）：这条路径不经过 `_charged` 的回滚分支，
              预占会永远挂在 in_flight，同一进程内没有任何出口。这里兜底退掉。
            """
            if not ctx.get("persist") and ctx.get("resume_charge"):
                charge = next(
                    (
                        ch
                        for ch in state.get("charges", [])
                        if ch.get("id") == ctx["resume_charge"] and ch.get("state") == "in_flight"
                    ),
                    None,
                )
                if charge is not None:
                    self._release_charge(
                        ctx,
                        charge,
                        str(charge.get("action") or ""),
                        str(charge.get("projectId") or ""),
                    )
            if ctx.get("persist"):
                commit(None)

        try:
            result = self._dispatch(action_type, ctx)
        except _DeferredCall as exc:
            # 状态机跑到「要出网」那一步就停下了，但预占已经在内存状态里。
            # 必须在放开锁之前把这笔预占落库，否则出网期间进程挂掉，这笔钱
            # 在库里没有任何痕迹（旧实现就是这样：上游可能已扣费，本地查无此事）。
            deferred, result = exc, None
        except _PendingReview as exc:
            # 「上游结果未知」必须落库再报错：不落库就等于没有待复核记录，
            # 用户再点一次，上游就被调第二次（实测路径）。
            pending, result = exc, None
        except (TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
            # 兜底：任何漏掉的入参形态问题都转成可读 400，不再以 500 暴露。
            # 具体的字段级校验在 _dispatch_inner 里做，这里只保证「不漏 5xx」。
            # AttributeError 是复测抓到的漏网：`saveScript("字符串")` 会走到
            # `f.get(...)`；`_as_dict` 已在入口挡住，这层兜底是最后一道。
            rollback_if_needed()
            logger.warning("drama action %s rejected as invalid args: %r", action_type, exc)
            raise AppError(
                "invalid_args",
                f"{action_type} 的参数不合法：{type(exc).__name__}: {exc}",
                400,
            ) from exc
        except Exception:
            # 上游明确失败（或任何其它异常）：两段式下预占已落库，_release_charge
            # 把退款写进内存状态并打了 persist 标记，这里负责把它落盘再抛。
            rollback_if_needed()
            raise
        if deferred is not None and not deferred.charge_id:
            # 定价 0 的动作（preview-film / mark-audio-done）没有预占，没有什么
            # 必须落库的东西：直接放行到锁外执行，别多写一条空审计。
            raise deferred
        written = commit(result)
        if deferred is not None:
            raise deferred
        if pending is not None:
            raise AppError(pending.code, pending.message, pending.status)
        return written

    async def reset(self, owner: str) -> dict[str, Any]:
        lock = await self._lock_for(owner)
        async with lock:
            await asyncio.to_thread(self.store.reset, owner)
        return await self.state(owner)

    # ---------------------------------------------------------------- 上传（第四项）
    async def upload(
        self,
        owner: str,
        kind: str,
        name: str,
        data: bytes,
        *,
        asset_type: str = "",
        asset_name: str = "",
    ) -> dict[str, Any]:
        """上传 BGM / 参考图。全程本地、不扣积分、不经过生成动作的账本。

        与 action 同一把 owner 锁、同一份「读快照 -> 改内存 -> 一次写库」口径，
        所以离线与真实模式共用同一条上传链路，不存在第二套流程。
        """
        lock = await self._lock_for(owner)
        async with lock:
            return await asyncio.to_thread(
                self._upload_sync, owner, kind, name, data, asset_type, asset_name
            )

    def _upload_sync(
        self,
        owner: str,
        kind: str,
        name: str,
        data: bytes,
        asset_type: str,
        asset_name: str,
    ) -> dict[str, Any]:
        limit = UPLOAD_LIMITS.get(str(kind or ""))
        if limit is None:
            _fail(
                "upload_kind",
                "本期只开放两类上传：bgm（BGM 音频）与 reference（角色/场景/道具参考图）",
                400,
            )
        clean_name = str(name or "").strip()[:UPLOAD_FILE_NAME_MAX]
        if not clean_name:
            _fail("upload_name", "文件名不能为空", 400)
        if not data:
            _fail("upload_empty", "上传文件为空", 400)
        if len(data) > int(limit["max_bytes"]):
            _fail(
                "upload_too_large",
                f"{limit['label']} 单个文件不能超过 "
                f"{int(limit['max_bytes']) // (1024 * 1024)} MB，实际 {len(data)} 字节",
                409,
            )
        # 审核必须落在**落盘之前**：被拒的上传不写文件、不占配额、不进 uploads。
        # 本地实现只能查空值（审不了像素），真实像素审核要接审核服务（本期只有端口）。
        media_verdict = self.moderator.review_media(data, kind=kind, owner=owner)
        if media_verdict.allowed is False:
            _fail("content_blocked", "内容未通过安全检查，文件未保存", 400)
        # 文件名也是用户输入、也会进页面与导出记录，按文本一起审。
        name_verdict = self.moderator.review_text(clean_name, owner=owner, context=f"upload-{kind}")
        if name_verdict.allowed is False:
            _fail("content_blocked", "内容未通过安全检查，文件未保存", 400)
        self.store.ensure_owner(owner)
        state = self.store.read(lambda db: self.store._state_in(db, owner))
        same = [x for x in state.get("uploads", []) if x.get("kind") == kind]
        if len(same) >= int(limit["max_count"]):
            _fail(
                "upload_limit",
                f"{limit['label']} 最多保留 {limit['max_count']} 个，请先删除旧文件",
                409,
            )
        try:
            saved = self.media_store.store(owner, str(limit["store_kind"]), data)
        except AppError as exc:
            if exc.code in ("asset_format", "asset_too_large", "asset_kind", "asset_empty"):
                _fail(
                    "upload_format",
                    f"{limit['label']} 格式不被接受：只认按文件魔数判定的"
                    f"{'PNG / JPEG / WebP 图片' if kind == 'reference' else 'MP3 / WAV / OGG / M4A 音频'}。"
                    f" 原始错误：{exc.message}",
                    400,
                )
            raise
        upload_id = _uid("up")
        asset_type = str(asset_type or "").strip()
        bound_asset_id = ""
        pid = state.get("activeProjectId")
        if kind == "reference" and asset_type in ASSET_TYPES:
            proj = next((x for x in state["projects"] if x["id"] == pid), None)
            if proj is not None:
                label = str(asset_name or "").strip() or clean_name.rsplit(".", 1)[0]
                bucket = proj["assets"].setdefault(asset_type, [])
                item = next((a for a in bucket if a.get("name") == label), None)
                if item is None:
                    item = {
                        "id": _uid("a"),
                        "name": label,
                        "sub": "上传参考图",
                        "voice": "—",
                        "note": "用户上传的参考图（非模型生成）",
                        "prompt": "",
                        "seed": 1,
                        "locked": False,
                        "imageUrl": "",
                    }
                    bucket.append(item)
                # 写回资产后，`@角色:名字` 解析（_anchor_asset）与首帧（_first_frame）
                # 会自动选中这张上传图，无需另开一条引用链路。
                item["imageUrl"] = saved["url"]
                item["sub"] = "上传参考图"
                bound_asset_id = str(item["id"])
        row = {
            "id": upload_id,
            "kind": kind,
            "name": clean_name,
            "url": saved["url"],
            "bytes": len(data),
            "assetType": asset_type,
            "assetId": bound_asset_id,
            "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
        }
        state.setdefault("uploads", []).append(row)
        # 三个落库点（本方法、_delete_upload_sync、_action_sync）口径一致：
        # 上传会改资产图与 BGM 绑定，同样可能让已合成的成片过期。
        self._refresh_film_stale(state)
        self._assert_conservation(state)
        media_row = (
            _uid("m"),
            owner,
            pid,
            f"upload-{kind}",
            clean_name,
            json.dumps(
                {"url": saved["url"], "bytes": len(data), "sample": False}, ensure_ascii=False
            ),
            "upload",
            time.strftime("%m-%d %H:%M", time.localtime()),
        )

        def op(db: Any) -> dict[str, Any]:
            db.execute("INSERT INTO drama_media VALUES(?,?,?,?,?,?,?,?)", media_row)
            self.store.replace_in(db, owner, state)
            db.execute(
                "INSERT INTO drama_audit VALUES(?,?,?,?,?)",
                (_uid("a"), owner, f"upload-{kind}", clean_name, time.time()),
            )
            return self.store._state_in(db, owner)

        return self.store.run(op)

    async def delete_upload(self, owner: str, upload_id: str) -> dict[str, Any]:
        lock = await self._lock_for(owner)
        async with lock:
            return await asyncio.to_thread(self._delete_upload_sync, owner, upload_id)

    def _delete_upload_sync(self, owner: str, upload_id: str) -> dict[str, Any]:
        state = self.store.read(lambda db: self.store._state_in(db, owner))
        item = next((x for x in state.get("uploads", []) if x["id"] == upload_id), None)
        if item is None:
            _fail("upload_missing", "上传记录不存在或不属于当前账号", 404)
        url = str(item["url"])
        for proj in state["projects"]:
            bgm = (proj.get("audio") or {}).get("bgm") or {}
            if bgm.get("url") == url:
                bgm["url"], bgm["uploadId"] = "", ""
            for bucket in (proj.get("assets") or {}).values():
                for asset in bucket:
                    if asset.get("imageUrl") == url:
                        asset["imageUrl"] = ""
                        asset["sub"] = "草稿"
        state["uploads"] = [x for x in state.get("uploads", []) if x["id"] != upload_id]
        self._refresh_film_stale(state)
        self.media_store.remove(owner, url)
        return self.store.run(lambda db: self._write_state(db, owner, state))

    def _write_state(self, db: Any, owner: str, state: dict[str, Any]) -> dict[str, Any]:
        self.store.replace_in(db, owner, state)
        return self.store._state_in(db, owner)

    async def export_doc(self, owner: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.export_doc, owner)

    async def import_doc(self, owner: str, doc: dict[str, Any]) -> dict[str, Any]:
        # 整份状态导入是重置/迁移级别的操作：一旦允许普通用户导入，就能自带一条
        # 「加余额 100 万」的 credit 流水，而导入校验只看账本自洽 —— 流水本身可伪造。
        # 实测非管理员导入后余额 1,000,840。这里只留管理员入口。
        self._require_admin(owner, "import_doc")
        try:
            lock = await self._lock_for(owner)
            async with lock:
                await asyncio.to_thread(self.store.import_doc, owner, doc)
        except ValueError as exc:
            # 字段级失败（产物历史）自带 code=invalid_field，让调用方知道该改哪个字段；
            # 其余整档结构问题仍是 invalid_import（既有契约，不动）。
            code = str(getattr(exc, "code", "") or "invalid_import")
            _fail(code, str(exc), 400)
        return await self.state(owner)

    # ---------- 账本辅助 ----------
    def _require_admin(self, owner: str, action: str) -> None:
        """管理类动作的身份闸门：白名单外一律 403。

        bearer 模式下 credit_admins 没配就是空集，即 fail-closed——宁可管理动作
        不可用，也不能让任意登录用户给自己加余额。
        """
        if owner not in self.admins:
            _fail("forbidden", f"{action} 仅管理员可执行（当前身份 {owner}）", 403)

    @staticmethod
    def _tx(
        state: dict[str, Any], kind: str, label: str, amount: int, note: str = "", **meta: Any
    ) -> None:
        state["ledger"]["tx"].append(
            {
                "id": _uid("tx"),
                "seed": False,
                "time": time.strftime("%m-%d %H:%M", time.localtime()),
                "kind": kind,
                "label": label,
                "amount": amount,
                "note": note,
                "projectId": meta.get("projectId"),
                "taskId": meta.get("taskId"),
            }
        )

    def _first_frame(self, owner: str, proj: dict[str, Any], asset_id: Any) -> bytes | None:
        # 新口径：直接给本地素材地址（镜头关键帧 / 资产形象图）。
        if isinstance(asset_id, str) and asset_id.startswith("/api/v1/drama/assets/"):
            path = self.media_store.path_for_url(owner, asset_id)
            try:
                return path.read_bytes() if path is not None else None
            except OSError:
                return None
        return self._asset_first_frame(owner, proj, asset_id)

    def _asset_first_frame(self, owner: str, proj: dict[str, Any], asset_id: Any) -> bytes | None:
        """给图生视频取首帧图字节。

        来源：显式传入的资产 id（用户在镜头里选「用角色/场景锚点图作首帧」）。
        锚点图是 generateAssetCandidates 真实产出的本地文件，因此这条链在
        remote Provider 下是真图、真字节，不是占位。

        只接受本服务签发的本地素材 URL；外来 URL 一律当没有，避免把带签名的
        上游地址当成本地文件去读。
        """
        if not asset_id:
            return None
        for items in (proj.get("assets") or {}).values():
            found = next((a for a in items if a.get("id") == asset_id), None)
            if found is None or not found.get("imageUrl"):
                continue
            path = self.media_store.path_for_url(owner, str(found["imageUrl"]))
            if path is None:
                return None
            try:
                return path.read_bytes()
            except OSError:
                return None
        return None

    # ------------------------------------------------------- 生成请求参数 / 目标键
    # 这一组只做「把项目设定与 @角色 引用解析成 Provider 能消费的请求参数」：
    # 不出网、不读凭据、不改状态机。离线可测 —— mock 会把收到的参数回显到
    # payload，测试据此断言参数确实带上了，而不是只存进项目设定页当摆设。
    @staticmethod
    def _film_signature(proj: dict[str, Any]) -> str:
        """成片是由哪些输入合成出来的：镜头顺序/时长/视频轨 + 字幕与逐句配音 + BGM 混音。

        取数口径与 `compose.build_plan()` 一致。合成时记下这串指纹，之后任何一项
        变了（换一版分镜、删改镜头、重排时间线、重新生成视频、改字幕或配音、换 BGM），
        成片就过期：导出必须拒绝，否则导出记录写的是新分镜，指向的却是旧文件
        （实测：换分镜后导出记录 7 镜 20 秒，文件仍是 8 镜 22 秒）。
        """
        shots = {str(x.get("id")): x for x in proj.get("shots") or []}
        order = [str(x) for x in (proj.get("timeline") or {}).get("order") or []]
        ordered = [sid for sid in order if sid in shots]
        ordered += [sid for sid in shots if sid not in set(ordered)]
        audio = proj.get("audio") or {}
        bgm = audio.get("bgm") or {}
        doc = {
            "shots": [
                [sid, int(shots[sid].get("dur") or 0), str(shots[sid].get("videoUrl") or "")]
                for sid in ordered
            ],
            "subtitles": [
                [
                    str(x.get("in")),
                    str(x.get("out")),
                    str(x.get("text")),
                    str(x.get("audio_url") or ""),
                ]
                for x in audio.get("subtitles") or []
                if isinstance(x, dict) and str(x.get("text") or "").strip()
            ],
            "voices": [str(x.get("audio_url") or "") for x in audio.get("voices") or []],
            "bgm": [
                str(bgm.get("url") or ""),
                int(bgm.get("voiceLevel", 100) or 100),
                int(bgm.get("bgmLevel", 25) or 25),
            ],
        }
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _refresh_film_stale(cls, state: dict[str, Any]) -> None:
        """每次落库前重算「成片是否过期」，前端据此提示重新合成。

        没有 signature 的是本功能上线前合成的旧成片：无从判断，保持不标记。
        """
        for proj in state.get("projects") or []:
            film = proj.get("film")
            if isinstance(film, dict) and film.get("signature"):
                film["stale"] = film["signature"] != cls._film_signature(proj)

    @staticmethod
    def _project_settings(proj: dict[str, Any]) -> dict[str, str]:
        return {
            "ratio": str(proj.get("ratio") or ""),
            "style": str(proj.get("style") or ""),
            "res": str(proj.get("res") or ""),
        }

    @staticmethod
    def _anchor_asset(proj: dict[str, Any], refs: Any) -> str:
        """把镜头里的 `@角色:名字` 解析成已生成锚点图的资产 id。

        只认有 imageUrl 的资产（generateAssetCandidates 真的产出过图）；
        引用了但还没出图的不算锚点，避免把占位 SVG 当首帧传出去。
        """
        if not isinstance(refs, (list, tuple)):
            return ""
        buckets = {"@角色": "characters", "@场景": "scenes", "@道具": "props"}
        assets = proj.get("assets") or {}
        for ref in refs:
            label, sep, name = str(ref).partition(":")
            if not sep or label not in buckets:
                continue
            for item in assets.get(buckets[label]) or []:
                if str(item.get("name") or "") == name.strip() and item.get("imageUrl"):
                    return str(item["id"])
        return ""

    @classmethod
    def _asset_request(cls, item: dict[str, Any], proj: dict[str, Any]) -> dict[str, Any]:
        req = dict(item)
        req["project_settings"] = cls._project_settings(proj)
        return req

    @classmethod
    def _shot_request(
        cls, shot: dict[str, Any], proj: dict[str, Any], explicit: Any
    ) -> tuple[dict[str, Any], str | None]:
        req = dict(shot)
        req["project_settings"] = cls._project_settings(proj)
        req["anchor_refs"] = [str(x) for x in (shot.get("refs") or [])]
        anchor = cls._anchor_asset(proj, req["anchor_refs"])
        req["anchor_asset_id"] = anchor
        anchor_url = cls._asset_image(proj, anchor)
        req["anchor_media_id"] = cls._media_id(anchor_url)
        # 首帧优先级：用户显式选的形象图 > 这个镜头自己的关键帧 > @角色 解析出的锚点图。
        # 交给上游的必须是**文件编号**（素材地址里的 16 位 id），不是资产编号。
        explicit_url = (
            cls._asset_image(proj, str(explicit).strip()) if explicit not in (None, "") else ""
        )
        first_url = explicit_url or str(shot.get("imageUrl") or "") or anchor_url
        if not cls._media_id(first_url):
            first_url = ""
        req["first_frame_url"] = first_url
        req["first_frame_asset_id"] = cls._media_id(first_url)
        return req, first_url or None

    @staticmethod
    def _media_id(url: str) -> str:
        match = _LOCAL_URL.fullmatch(str(url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _asset_image(proj: dict[str, Any], asset_id: str) -> str:
        if not asset_id:
            return ""
        for items in (proj.get("assets") or {}).values():
            for item in items:
                if item.get("id") == asset_id:
                    return str(item.get("imageUrl") or "")
        return ""

    @staticmethod
    def _target_of(name: str, c: dict[str, Any]) -> str:
        """这次生成「锁」到哪个对象；空串表示项目级。

        刷新页面会换 requestId，幂等键认不出来，只能靠 target 挡住同一目标重复
        出网。前端也用它把 in_flight 凭据映射回镜头卡上的「生成中」。
        """
        args = c.get("args")
        if not isinstance(args, (list, tuple)):
            return ""

        def at(i: int) -> str:
            value = args[i] if len(args) > i else None
            if value is None or isinstance(value, (dict, list, tuple)):
                return ""
            return str(value).strip()

        if name == "image-to-video":
            return f"shot:{at(0)}" if at(0) else ""
        if name in ("generateAssetCandidates", "asset-candidates"):
            return f"asset:{at(1)}" if at(1) else ""
        return ""

    @staticmethod
    def _remember(proj: dict[str, Any], action: str, item: dict[str, Any]) -> None:
        """把「没有天然宿主对象」的生成结果挂到项目上。

        诊断结论、音色候选、成片预览这类结果不属于某个资产/镜头，但前端必须
        有地方展示，否则用户点完按钮只看到一条 toast。
        """
        at = time.strftime("%m-%d %H:%M", time.localtime())
        # lastResult 是单槽：诊断之后再做任何生成动作，诊断结论就被覆盖、剧本页看不到了。
        # 需要长期展示的结果按动作名另存一份（同一列 JSON，不改表结构）。
        kept = dict((proj.get("lastResult") or {}).get("byAction") or {})
        if action in KEEP_RESULTS:
            kept[action] = {
                "payload": item.get("payload") or {},
                "label": item.get("label"),
                "at": at,
            }
        proj["lastResult"] = {
            "action": action,
            "provider": item.get("provider"),
            "label": item.get("label"),
            "payload": item.get("payload") or {},
            "note": item.get("note") or "",
            "at": at,
            "byAction": kept,
        }

    @staticmethod
    def _media(ctx: dict[str, Any], item: dict[str, Any], project_id: str | None) -> None:
        # 只排队不写库：生成阶段在写事务之外，最终由 _action_sync 一次性插入。
        ctx["media_pending"].append(
            (
                _uid("m"),
                ctx["owner"],
                project_id,
                item["kind"],
                item["label"],
                json.dumps(item["payload"], ensure_ascii=False),
                item["provider"],
                time.strftime("%m-%d %H:%M", time.localtime()),
            )
        )

    @staticmethod
    def _assert_conservation(state: dict[str, Any]) -> None:
        ledger = state["ledger"]
        cost = sum(int(t["cost"]) for t in state["tasks"] if t["settled"])
        # 删除项目会连带删掉该项目下的已结算任务，cost 随即变小、expected 变大。
        # 如果不把「随项目一起退役的成本」单独累计，删一次项目就会永久 409。
        cost += int(ledger.get("retiredCost") or 0)
        # 生成动作走 drama_charge 直接结算，没有 task 行；其 settle 流水 amount 恒为 0，
        # 所以成本只能从 charge 凭据里取。
        cost += sum(int(c["cost"]) for c in state.get("charges", []) if c["state"] == "settled")
        credits = sum(
            int(t["amount"]) for t in ledger["tx"] if t["kind"] == "credit" and not t["seed"]
        )
        expected = 980 + credits - cost
        actual = int(ledger["balance"]) + int(ledger["frozen"])
        if expected != actual:
            _fail("ledger_invariant", f"资金守恒被破坏：期望 {expected}，实际 {actual}", 409)

    # ---------- 动作分发 ----------
    def _effective_price(self, name: str, state: dict[str, Any]) -> int | None:
        """动作的生效价 = 目录价 + 管理员对该动作的覆盖（见 pricing.effective_price）。

        为什么不能直接用 ACTION_PRICES：`pricing.video` 是可维护字段，管理员把图生视频
        从 300 改成 200 后，扣费与 `/config` 下发的「按钮价」必须同时是 200。只改一张
        静态表做不到，只改 `/config` 则等于对用户虚报价格。
        """
        return effective_price(name, state.get("pricing"))

    def _dispatch(self, name: str, c: dict[str, Any]) -> Any:
        """所有动作的统一入口：生成类动作先记账，其余直通。

        定价表 ACTION_PRICES 是唯一开关——表里没有的动作视为「不调上游」，不扣费。
        这样新增动作时不会出现「忘了接账本」的静默漏洞：要么显式登记定价，
        要么显式登记为 0。管理员对 `image-to-video` 的改价经 `_effective_price` 生效，
        单次硬上限一律由 `cap_of(生效价)` 派生，不另存一份 cap，避免两者漂移。

        **内容审核也在这里，而且必须在 `_charged` 之前**：`_charged` 的第一件事就是
        预占冻结（落一条 in_flight 凭据），审核放在它后面就等于「先冻钱、再告诉用户
        内容不合规」。这里审不过直接 400，charge 行、冻结积分、上游调用一概不发生。
        审核是纯函数、幂等，出网回来后 `_dispatch` 会被再跑一遍，重复审核无副作用。
        """
        self._moderate(name, c)
        price = self._effective_price(name, c["state"])
        if price is None:
            return self._dispatch_inner(name, c)
        if price > 0 and c["state"].get("_byok"):
            # 用户用自己的 Liblib 密钥：上游费用由用户的 Liblib 账户承担，平台不再
            # 重复扣积分。仍走 _charged（单价 0），保留「生成中」凭据、防重复提交与审计。
            return self._charged(name, c, 0)
        if price <= 0:
            # 纯本地动作（标记完成 / 本地编排预览）：不预占、不结算，但仍登记一条
            # 0 成本凭据，便于账单页按动作对账。
            return self._dispatch_inner(name, c)
        return self._charged(name, c, price)

    #: 这些动作把**剧本正文**交给上游（或据此重写剧本），剧本正文必须和参数一起审。
    #: 只列真正会发送 / 改写剧本的动作，不做「扫全量项目文本」——那既慢又会把
    #: 与本次请求无关的历史内容算进来。
    SCRIPT_ACTIONS: frozenset[str] = frozenset(
        {"script-doctor", "creative-start", "extract-assets", "resplit-shots-v2"}
    )

    @classmethod
    def _moderation_texts(cls, name: str, args: list[Any], s: dict[str, Any]) -> list[str]:
        """收集「这次真的要送给上游 / 真的会落库」的文本。

        为什么不是全文扫描：审核的对象应该是本次请求的内容。把整个项目的所有字段
        都拉进来，会让一次保存动作因为别处的历史内容被拒，也会让审核耗时随项目
        规模增长。

        覆盖范围：
        - 所有 `str` 参数，以及 dict / list 里**嵌一层**的字符串（`addShot` /
          `updateAsset` 的 prompt、台词都藏在 dict 里，只取顶层 str 会全漏）；
        - 剧本类动作额外带上当前项目剧本的 logline / outline / body。
        """
        texts: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str):
                if value.strip():
                    texts.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    add(item)
            elif isinstance(value, list | tuple):
                for item in value:
                    add(item)

        for item in args:
            add(item)

        if name in cls.SCRIPT_ACTIONS:
            pid = s.get("activeProjectId")
            proj = next((p for p in s.get("projects", []) if p.get("id") == pid), None)
            layers = ((proj or {}).get("script") or {}).get("layers") or {}
            for key in ("logline", "outline", "body"):
                add(layers.get(key))
        return texts

    def _moderate(self, name: str, c: dict[str, Any]) -> None:
        """生成前文本审核。任一文本被拒 -> 400 `content_blocked`，不扣费、不出网。"""
        owner = str(c.get("owner") or "")
        for text in self._moderation_texts(name, c.get("args") or [], c["state"]):
            verdict = self.moderator.review_text(text, owner=owner, context=name)
            if verdict.allowed is False:
                # 固定文案：不回写命中的词，也不泄露是词表里的哪一条。
                _fail(
                    "content_blocked",
                    "内容未通过安全检查，本次不会扣费，请修改后重试",
                    400,
                )

    @staticmethod
    def _simulated_provider(c: dict[str, Any]) -> bool:
        """本次动作是否提交给本地模拟 Provider（不花上游真钱）。

        判据只有一个不变式：`Provider.name == "mock"`（见 providers/base.py）。
        必须从 ctx 取 Provider，不能读 `self.provider`：同进程多 owner 并发会串。
        """
        return str(getattr(c.get("provider"), "name", "") or "") == "mock"

    def _afford(
        self,
        owner: str,
        proj: dict[str, Any] | None,
        cap: int,
        *,
        what: str,
        simulated: bool = False,
    ) -> None:
        """三道闸门：单日上限 -> 项目预算 -> 余额。不过就拒，且必须在调上游之前拒。

        抽成一个方法，是因为实测发现**重抽 / 重试绕过了这道闸门**：
        `rerollTask` / `retryTask` 各自只看了余额，于是预算 40 的项目重抽 6 次花到 70，
        单日上限 60 时重抽 10 次花到 140，而「今日已用」还显示 0。
        现在所有「要预占 cap」的入口都走这里，只留一份口径。

        `simulated=True`（本次动作提交给的是本地模拟 Provider）**只跳过单日上限**：
        单日上限的存在理由是「防止烧上游真钱」，而 mock 一分钱都不花。跳过的**只有**
        这一道——项目预算（用户自己设的约束）和余额（真钱账本）照旧生效，否则
        「离线不占额」会变成「离线可以无限刷穿账本」。

        为什么用布尔开关而不是在这里读 `self.provider`：同一进程里不同 owner 的
        动作并发跑在各自线程上，读实例属性会串；Provider 必须来自本次请求的 ctx。
        """
        if not simulated:
            # 只有真要算钱时才查库：这条查询的唯一用途是那条拒绝消息。
            day = time.strftime("%Y-%m-%d", time.localtime())
            used_today = self.store.daily_spend(owner, day)
            if self.daily_cap and used_today + cap > self.daily_cap:
                _fail(
                    "daily_cap_exceeded",
                    f"今日生成消耗 {used_today} 积分（含关键帧与生成动作），"
                    f"已达单日上限 {self.daily_cap}（{what} 需预占 {cap}）；"
                    "请明天再试或由管理员上调上限",
                    409,
                )
        if proj is not None:
            budget = int(proj.get("budget") or 0)
            spent = self.store.project_spend(owner, str(proj["id"]))
            if budget > 0 and spent + cap > budget:
                _fail(
                    "budget_exceeded",
                    f"项目预算 {budget} 积分，已花 {spent}，{what} 需预占 {cap}；"
                    "请在项目设定里上调预算",
                    409,
                )

    @staticmethod
    def _pending_spend(s: dict[str, Any]) -> int:
        """已提交、尚未结算的生成按**单价**计（不按 1.2 倍预占），用于总量上限的剩余额度。"""
        charges = sum(
            int(c.get("price") or 0)
            for c in s.get("charges", [])
            if c.get("state") in ("in_flight", "pending_review")
        )
        tasks = sum(
            int(t.get("cost") or 0)
            for t in s.get("tasks", [])
            if not t.get("settled") and t.get("state") in ("queued", "running", "in_flight", "held")
        )
        return charges + tasks

    @classmethod
    def credit_limit_view(cls, s: dict[str, Any]) -> dict[str, Any]:
        """前端展示与校验共用的一份口径。"""
        lim = s.get("creditLimits") or {}
        balance = int((s.get("ledger") or {}).get("balance") or 0)
        total = lim.get("total")
        used = int(lim.get("used") or 0)
        pending = cls._pending_spend(s)
        remaining = None if total is None else max(0, int(total) - used - pending)
        usable = balance if remaining is None else min(balance, remaining)
        return {
            "single": lim.get("single"),
            "total": total,
            "used": used,
            "pending": pending,
            "remaining": remaining,
            "usable": usable,
        }

    def _check_credit_limits(self, s: dict[str, Any], unit: int, total_need: int) -> None:
        """用户自设上限：每次要花钱之前、在动账本与调上游之前检查。

        读的是本次请求刚从库里读出的 state，所以设置一保存，下一次请求就按新值判断；
        已经提交、正在生成的任务不受影响（它们的积分在提交时已经过了这道检查）。
        """
        view = self.credit_limit_view(s)
        single = view["single"]
        if single is not None and unit > int(single):
            _fail(
                "credit_single_limit",
                f"本次需要 {unit} 积分，超过你设置的单次上限 {single} 积分",
                409,
            )
        if view["remaining"] is not None and total_need > int(view["remaining"]):
            _fail(
                "credit_total_limit",
                f"本次需要 {total_need} 积分，超过总量上限剩余的 {view['remaining']} 积分",
                409,
            )

    @staticmethod
    def _count_spend(s: dict[str, Any], cost: int) -> None:
        if cost <= 0:
            return
        lim = s.setdefault("creditLimits", {})
        lim["used"] = int(lim.get("used") or 0) + int(cost)

    def _require_balance(self, ledger: dict[str, Any], cap: int, what: str) -> None:
        if int(ledger["balance"]) < cap:
            _fail(
                "insufficient_credits",
                f"可用积分不足：{what} 需预占 {cap}，当前余额 {ledger['balance']}",
                409,
            )

    @staticmethod
    def _unknown_outcome(exc: BaseException) -> bool:
        """这次失败能不能断定「上游没执行」？

        能断定的（4xx、参数错、上游明确拒绝）返回 False：整笔回滚、不扣费，符合原语义。
        不能断定的（超时、连不上、上游 5xx/不可用）返回 True：钱可能已经花了，
        必须留待复核记录，否则重试就是二次扣费。
        """
        code = str(getattr(exc, "code", ""))
        # 先排除「本地就失败了、根本没出网」的那几种：它们状态码也是 5xx，
        # 但钱一定没花，判成待复核只会让首单多几次人工复核。
        if code in LOCAL_FAILURE_CODES:
            return False
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
        if code in UNKNOWN_OUTCOME_CODES:
            return True
        # 502/503/504 一律视为「结果不明」：这三个状态码在语义上都不保证对端未执行。
        return int(getattr(exc, "status", 0) or 0) in (502, 503, 504)

    @staticmethod
    def _upstream_of(source: Any) -> tuple[int | None, int | None]:
        """从 Provider 结果（或挂在异常上的 usage）读上游真值：(积分, 余额)。

        **未知一律返回 None，绝不返回 0。** 0 表示「上游免费」，None 表示「不知道」；
        对账时两者必须分开，否则「老库/上游没回传」会被算成「这一单零成本」，
        毛利凭空变高——这是对账表最容易骗自己的地方。

        **也绝不回填 `UPSTREAM_COST_T2I=25` / `UPSTREAM_COST_VIDEO=250`。** 那两个是
        `pricing.py` 里的**预算常量**（用来算毛利是否跌破成本、要不要告警），是「估」
        不是「实」。拿它们填这一列，等于把「未知」伪装成「已知」——正是对账表最危险的
        那种谎。上游没在响应里回传 `pointsCost` / `accountBalance`，这里就必须是 None，
        报表里如实显示「未知 N 笔」。
        """
        if not isinstance(source, dict):
            return None, None

        def one(key: str) -> int | None:
            value = source.get(key)
            # bool 是 int 的子类，`True` 会被当成 1 混进来，必须显式排掉。
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        return one("upstream_points"), one("account_balance")

    @staticmethod
    def _upstream_task_id_of(source: Any) -> str | None:
        """从 Provider 结果 payload / 异常上挂的 usage 读上游任务编号。

        读不到（mock 路径没有真实 generateUuid、上游没回传、老库）一律返回 None，
        **绝不伪造**编号：没有编号的凭据只能保持待复核，不能触发自动结算。
        编号只做等值查询用，页面上从不展示这个字段。
        """
        if not isinstance(source, dict):
            return None
        value = source.get("upstream_task_id")
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned if cleaned and len(cleaned) <= 256 else None

    @staticmethod
    def _audit_status_of(source: Any) -> str | None:
        """从 Provider 结果 payload / 异常 usage 读上游审核态 `auditStatus`。

        读不到（mock 路径、上游没回传、老库）一律返回 None = **未知**。
        绝不写 `"0"` / `""` 之类"看起来已通过"的默认值顶替：那会把未知伪装成已知。
        上游给的是字符串，所以非字符串一律当未知。
        """
        if not isinstance(source, dict):
            return None
        value = source.get("audit_status")
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    def _hold_for_review(
        self,
        name: str,
        c: dict[str, Any],
        exc: BaseException,
        cap: int,
        price: int,
        day: str,
        pid: str,
        charge: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        """把「结果未知」的那次调用落成一条待复核凭据，冻结保留、余额不退回。

        为什么保留冻结而不是退款：钱可能已经付给上游了。先退款再让用户重试，
        最坏结果是「上游跑了两次、本地只扣一次」——账本看着守恒，实际在亏钱。

        `charge` 非空表示出网前已经落过一条 `in_flight` 凭据（两段式的第一段），
        这里就地把它改成 `pending_review`，不再新增一行。

        `note` 只改**文案**，不改任何资金语义：上游 `auditStatus=4` 的待确认也走这里，
        因为两者要的是同一件事——冻结留着、凭据悬着、等管理员（或自动结算）收口。
        """
        state = c["state"]
        code = str(getattr(exc, "code", type(exc).__name__))
        custom = str(note or "").strip()
        text = custom or f"上游结果未知（{code}）：预占 {cap} 保留，禁止自动重提，需管理员复核"
        tx_text = custom or f"上游结果未知（{code}），预占 {cap} 保留，等待管理员复核"
        # 上游可能已经扣了钱但本地确实不知道结果（例如「成功但素材下载失败」）。
        # 这种情况 `cost` 仍是 0，而 `upstream_points` 已经有值——**不要为了对齐
        # 去抹平它**：管理员正是靠「待复核 + 上游已扣 N 积分」判断该退款还是该结算。
        points, balance = self._upstream_of(getattr(exc, "usage", None))
        upstream_task_id = self._upstream_task_id_of(getattr(exc, "usage", None))
        audit_status = self._audit_status_of(getattr(exc, "usage", None))
        base = {
            "id": _uid("CH"),
            "projectId": pid,
            "action": name,
            "requestId": c.get("request_id") or "",
            "price": price,
            "cap": cap,
            "cost": 0,
            "state": "pending_review",
            "result": None,
            "note": text,
            "createdAt": time.time(),
            "day": day,
            "upstreamTaskId": upstream_task_id,
            "upstreamPoints": points,
            "upstreamBalance": balance,
        }
        if audit_status is not None:
            base["auditStatus"] = audit_status
        if charge is None:
            state.setdefault("charges", []).append(base)
        else:
            charge["state"], charge["cost"], charge["result"] = "pending_review", 0, None
            charge["note"] = text
            # 就地改写 in_flight 凭据时把编号带上：这是自动结算唯一能认的键。
            # 只在拿到了真值时覆盖，别把已有的编号抹回 None。
            if upstream_task_id is not None:
                charge["upstreamTaskId"] = upstream_task_id
            # 只覆盖「这次拿到了真值」的情形，别把已有的值抹回 None。
            if points is not None:
                charge["upstreamPoints"] = points
            if balance is not None:
                charge["upstreamBalance"] = balance
            if audit_status is not None:
                charge["auditStatus"] = audit_status
        # 流水留痕（金额 0）：预占那笔已经在冻结里，这里只标记「这笔钱悬着」。
        self._tx(
            state,
            "hold",
            f"{name} 待复核",
            0,
            tx_text,
            projectId=pid,
        )

    def _charged(self, name: str, c: dict[str, Any], price: int) -> Any:
        """预占 -> 执行 -> 结算退差。口径与关键帧 t2i 完全一致。

        - 预占 cap = 定价 × 1.2 取整；结算按定价扣，差额退回余额。
        - 三道闸门：单日上限、项目预算、余额。任何一道不过就直接拒，
          并且**在调用 Provider 之前**拒——不能等上游已经烧了钱才报余额不足。
        - 上游**明确失败**：整笔回滚，不写库、不扣费。
        - 上游**结果未知**（超时 / 连不上 / 上游不可用）：保留冻结，落一条
          `pending_review` 凭据，返回 504，并且在该项目同动作上禁止再次提交，
          直到管理员复核。旧实现这里静默回滚，用户再点一次就让上游跑了两遍。
        - 两段式：预占先落成一条 `in_flight` 凭据，再出网（出网在锁外）。续跑时
          `c["resume_charge"]` 指向那条凭据，跳过预占直接落结果，绝不重复扣费。
        """
        s, proj = c["state"], c["project"]
        if proj is None:
            _fail("project_missing", "当前项目不存在", 404)
        ledger = s["ledger"]
        owner = c["owner"]
        pid = str(proj["id"])
        resume = str(c.get("resume_charge") or "")

        if resume:
            # 续跑：预占是上一趟锁内做的，并且已经落库，这里只能认领、不能再扣。
            charge = next((ch for ch in s.get("charges", []) if ch.get("id") == resume), _MISSING)
            if charge is None:
                _fail(
                    "charge_missing",
                    f"预占凭据 {resume} 已不存在（期间可能被重置或导入覆盖），请重新提交",
                    409,
                )
            if charge.get("state") != "in_flight":
                _fail(
                    "charge_conflict",
                    f"预占凭据 {resume} 当前状态是「{charge.get('state')}」，不能继续结算",
                    409,
                )
            cap = int(charge["cap"])
            price = int(charge["price"])
            day = str(charge.get("day") or "")
            pid = str(charge.get("projectId") or pid)
        else:
            cap = _cap_of(price)
            day = time.strftime("%Y-%m-%d", time.localtime())

            # 先看有没有「上一次同动作结果未知」：有就不许再提交。
            # 实测路径：模拟超时 -> 504 -> 用户再点一次 -> 上游被调了两次。
            pending = next(
                (
                    ch
                    for ch in s.get("charges", [])
                    if ch.get("state") == "pending_review"
                    and ch.get("action") == name
                    and ch.get("projectId") == pid
                ),
                None,
            )
            if pending is not None:
                _fail(
                    "pending_review",
                    f"该项目上一次「{name}」上游结果未知（凭据 {pending['id']}，"
                    f"已冻结 {pending['cap']} 积分）。为避免重复扣上游的钱，"
                    "在管理员复核（resolveCharge）之前不允许再次提交同一动作。",
                    409,
                )

            # 第 7 项：同一次生成的「在飞」窗口。刷新页面会换 requestId，
            # `_idempotent_replay` 只按同一个 requestId 查，认不出来；前端
            # `pending[name]` 又是刷新即丢的内存态，于是换号重复提交会真调上游
            # 两次、各记一次账（真实 socket 实测 980 -> 976）。这里按
            # (owner 隔离的 state, action, projectId, target) 挡住第二次。
            # 只在首趟检查：续跑（resume 非空）是同一个请求回来结算，不能拦自己。
            target = self._target_of(name, c)
            flying = next(
                (
                    ch
                    for ch in s.get("charges", [])
                    if ch.get("state") == "in_flight"
                    and ch.get("action") == name
                    and ch.get("projectId") == pid
                    and str(ch.get("target") or "") == target
                ),
                None,
            )
            if flying is not None:
                _fail(
                    "action_in_flight",
                    f"「{name}」正在生成中（凭据 {flying['id']}，已预占 {flying['cap']} 积分，"
                    f"目标 {target or '当前项目'}）。换 requestId 或刷新页面都不会绕过："
                    "请等待当前请求返回，不要重复提交。",
                    409,
                )

            # 本次动作实际提交给哪个 Provider：判「是否真实消费」只用这一个不变式，
            # `"mock"` 是唯一表示本地模拟的名字（见 providers/base.py）。
            # 存 Provider.name 原值，不写厂商名/通道名：`_settle_legacy_tasks` 拿它做
            # 等值比较，记通道名会把同模式下正当在跑的任务误判成「切换模式遗留」退钱。
            provider_name = str(getattr(c.get("provider"), "name", "") or "")
            simulated = self._simulated_provider(c)

            self._afford(owner, proj, cap, what=name, simulated=simulated)
            self._check_credit_limits(s, price, price)
            self._require_balance(ledger, cap, f"{name}（定价 {price} × 1.2）")

            ledger["balance"] -= cap
            ledger["frozen"] += cap
            self._tx(
                s,
                "reserve",
                f"{name} 预占",
                -cap,
                f"定价 {price} / 单次上限 {cap}",
                projectId=pid,
            )
            # 出网之前先把凭据落成 in_flight：出网期间进程被杀，下次启动会把它
            # 扫成 pending_review（钱可能已经花了），而不是无声消失。
            charge = {
                "id": _uid("CH"),
                "projectId": pid,
                "action": name,
                "requestId": c.get("request_id") or "",
                # 目标键跟着凭据一起落库：出网期间刷新页面，前端才能从
                # state.charges 把这条「生成中」映射回具体镜头/资产。
                "target": target,
                "price": price,
                "cap": cap,
                "cost": 0,
                "state": "in_flight",
                "result": None,
                "note": f"已预占 {cap}，等待上游返回",
                "createdAt": time.time(),
                "day": day,
                # 身份随凭据落库：日上限按它区分「真钱」与「本地模拟」，
                # 事后无法补救（同一 owner 当天可能先离线再切真实）。
                "sourceProvider": provider_name,
                # 上游任务编号：出网之前拿不到，先留 NULL。结算 / 待复核时由
                # provider 结果的 payload / usage 补上；mock 没有真实编号就保持 NULL。
                "upstreamTaskId": None,
                # 上游成本未知时先留 NULL（不是 0），结算时按上游回报的真值覆盖。
                "upstreamPoints": None,
                "upstreamBalance": None,
            }
            s.setdefault("charges", []).append(charge)

        try:
            result = self._dispatch_inner(name, c)
        except _DeferredCall as exc:
            # 出网要挪到锁外：把凭据 id 交给上层，它执行完会带着 id 回来续跑。
            if exc.charge_id is None:
                exc.charge_id = str(charge["id"])
            raise
        except Exception as exc:
            if not self._unknown_outcome(exc):
                # 预占已经落库了（两段式），「回滚」必须显式退钱，不能靠丢内存。
                self._release_charge(c, charge, name, pid)
                raise
            self._hold_for_review(name, c, exc, cap, price, day, pid, charge=charge)
            raise _PendingReview(
                "upstream_unknown",
                f"上游结果未知（{getattr(exc, 'code', type(exc).__name__)}）："
                f"预占 {cap} 积分已保留并登记待复核，禁止直接重试；"
                "请管理员在「管理」页复核为「未执行（退款）」或「已执行（结算）」。",
            ) from exc

        # 上游真值（points / balance）与审核态先读出来：两者都是**只覆盖已知值**，
        # 绝不用 0 / None 顶替。读在结算之前，是因为「审核态相关收口（退款 / 冻结）」
        # 必须在冻结被核销之前决定，否则金额会被结算一次再退一次，必然破坏守恒。
        payload = (result or {}).get("payload")
        points, balance = self._upstream_of(payload)
        audit_status = self._audit_status_of(payload)
        if audit_status is not None:
            charge["auditStatus"] = audit_status
        rejected = audit_outcome(audit_status) == "rejected"
        if rejected and self.audit_reject_action == "refund":
            # 部署方显式选了 refund：当拒审处理——退款，并把产物从项目里摘掉。
            # 钱和货必须同时收口，否则「钱退了却还看得见、导得出去」自相矛盾。
            cleaned = self._discard_rejected_media(c, charge)
            self._refund_audit_reject(
                c, charge, name, str(proj["id"]), points, balance, cleaned=cleaned
            )
            return result
        if rejected and self.audit_reject_action == "hold":
            # 默认：**先冻结待确认**，不退款也不结算。`auditStatus=4` 的语义从未实测
            # （成功样本是 3，同协议里 `generateStatus` 的 4 是「审核中」），当成已确认
            # 的拒审去退款，可能给一张随后会通过的单子白送钱。
            # 资金语义直接复用 `_hold_for_review`：冻结保留、凭据进 pending_review、
            # cost=0，后续由管理员复核或自动结算接口收口；这里只换文案。
            # 产物**不清理**、也不标 `auditRejected`：还没确认它违规，删了就没法交付。
            self._hold_for_review(
                name,
                c,
                _AuditPending(payload),
                cap,
                price,
                day,
                str(proj["id"]),
                charge=charge,
                note=_AUDIT_PENDING_NOTE,
            )
            self._assert_conservation(s)
            return result

        # 结算：冻结内核销 price，cap - price 退回余额。
        # （`settle` 与「没有审核态」走同一条路；settle 只在 note / 标记上体现。）
        ledger["frozen"] = max(0, int(ledger["frozen"]) - cap)
        self._count_spend(s, price)
        self._tx(
            s,
            "settle",
            f"{name} 结算",
            0,
            f"冻结内核销 {price} 积分，余额不变",
            projectId=str(proj["id"]),
        )
        diff = cap - price
        if diff > 0:
            ledger["balance"] += diff
            self._tx(
                s,
                "refund",
                f"{name} 结算退差",
                diff,
                f"预占 {cap} - 实际 {price} 退回余额",
                projectId=str(proj["id"]),
            )
        # 就地改写那条 in_flight 凭据：result 也进快照，幂等命中要回放它，
        # 导出/导入也要带着它才自洽。
        # 上游真值单独落库：`cost` 是**售价**，两者不能混。上游没回传时保持 NULL。
        charge.update(
            {
                "cost": price,
                "state": "settled",
                "result": result,
                "note": (
                    f"{AUDIT_REJECT_MARK}，按配置仍结算；定价 {price} / 预占 {cap}"
                    if rejected
                    else f"定价 {price} / 预占 {cap}"
                ),
                "upstreamTaskId": self._upstream_task_id_of(payload),
                "upstreamPoints": points,
                "upstreamBalance": balance,
            }
        )
        if rejected:
            # 部署方显式选择 `settle`：钱照扣，但凭据必须标出来，别让这笔消费在
            # 账单页看起来像一次正常成功。
            charge["auditRejected"] = True
        self._assert_conservation(s)
        return result

    def _refund_audit_reject(
        self,
        c: dict[str, Any],
        charge: dict[str, Any],
        name: str,
        pid: str,
        points: int | None,
        balance: int | None,
        *,
        cleaned: bool = True,
    ) -> None:
        """上游判定内容不合规：预占全额退回，凭据改 released、cost 归 0。

        **上游已扣的成本（`upstreamPoints`）原样保留**：钱确实付给上游了，那是
        对账要用的真值。为了「账面好看」把它清成 0 / None，等于伪造成本，管理页
        再也看不出这一单其实花了上游的钱。

        `cleaned` 来自 `_discard_rejected_media`：清理失败不能回滚交易（钱已经退了），
        只在 note 里留一句可排查的说明。
        """
        state = c["state"]
        ledger = state["ledger"]
        cap = int(charge["cap"])
        ledger["frozen"] = max(0, int(ledger["frozen"]) - cap)
        ledger["balance"] += cap
        charge["state"], charge["cost"], charge["result"] = "released", 0, None
        charge["note"] = f"{AUDIT_REJECT_MARK}，已全额退款" + (
            "" if cleaned else _CLEANUP_INCOMPLETE
        )
        charge["auditRejected"] = True
        # 只覆盖真值：上游没回传就保持原样（None = 未知），不用 0 顶替。
        if points is not None:
            charge["upstreamPoints"] = points
        if balance is not None:
            charge["upstreamBalance"] = balance
        self._tx(
            state,
            "release",
            f"{name} 内容不合规退款",
            cap,
            f"上游判定内容不合规，预占 {cap} 全额退回余额",
            projectId=pid,
        )
        # 退款已经写进内存状态：异常路径 / 正常返回都要落盘，不能只活在字典里。
        c["persist"] = True
        self._assert_conservation(state)

    def _discard_rejected_media(self, c: dict[str, Any], charge: dict[str, Any]) -> bool:
        """把「已被上游判违规」的产物从项目里摘掉：清 URL、删本地文件、打标记。

        只认 `_target_of` 真正产出的两种前缀：

        - `shot:<shotId>` → 清 `videoUrl`（`image-to-video` 的唯一写回字段）；
        - `asset:<assetId>` → 清 `imageUrl`（`generateAssetCandidates` 的唯一写回字段）。

        其它形态（空串 = 项目级动作、`shot:` 但镜头已被删、未知前缀）一律跳过并
        `logger.warning`，**绝不抛异常**：钱已经退了，清理失败不能把整笔交易变成 500。
        返回值只用于在 charge.note 里留一句「清理未完成」，供事后排查。

        URL 字段清成 `""` 而不是删键：前端与导出都按「键存在性」读，删键会在渲染
        时抛 undefined。是否删文件只按本地素材 URL 判断，外部 URL 绝不为了删文件
        去出网。
        """
        state = c["state"]
        owner = str(c.get("owner") or "")
        target = str(charge.get("target") or "").strip()
        pid = str(charge.get("projectId") or state.get("activeProjectId") or "")
        proj = next((p for p in state.get("projects", []) if p.get("id") == pid), None)
        if proj is None:
            logger.warning("drama: audit reject cleanup skipped (project %r missing)", pid)
            return False

        holder: dict[str, Any] | None = None
        url = ""
        if target.startswith("shot:"):
            shot_id = target[len("shot:") :]
            holder = next((x for x in proj.get("shots", []) if x.get("id") == shot_id), None)
            if holder is not None:
                url = str(holder.get("videoUrl") or "")
                holder["videoUrl"] = ""
        elif target.startswith("asset:"):
            asset_id = target[len("asset:") :]
            for bucket in (proj.get("assets") or {}).values():
                holder = next((x for x in bucket if x.get("id") == asset_id), None)
                if holder is not None:
                    break
            if holder is not None:
                url = str(holder.get("imageUrl") or "")
                holder["imageUrl"] = ""
        else:
            logger.warning("drama: audit reject cleanup skipped (unknown target %r)", target)
            return False

        if holder is None:
            logger.warning("drama: audit reject cleanup skipped (target %r not found)", target)
            return False

        # 可读标记：页面据此说明「这次生成未通过审核，已退款」，而不是一个凭空的空镜头。
        holder["auditRejected"] = True
        if url:
            # 只删本服务签发的本地素材；外来 URL / 越权路径由 media_store 自己拒绝。
            removed = self.media_store.remove(owner, url) if owner else False
            if removed:
                # 文件已经删掉：产物历史里指向它的那条必须一起去掉，否则
                # `restoreShotMedia` 会把它恢复回来，页面上就是一个永远 404 的产物。
                history = holder.get("mediaHistory")
                if isinstance(history, list):
                    holder["mediaHistory"] = [
                        h
                        for h in history
                        if not (isinstance(h, dict) and str(h.get("url") or "") == url)
                    ]
            if not removed and url.startswith("/api/v1/drama/assets/"):
                logger.warning("drama: audit reject cleanup kept local media %r", url)
        return True

    def _release_charge(
        self, c: dict[str, Any], charge: dict[str, Any], name: str, pid: str
    ) -> None:
        """上游**明确没执行**：预占原路退回，凭据改 released（留痕，不删行）。

        两段式之后预占是先落库再出网的，所以「回滚」不能再靠丢内存状态实现，
        必须显式退钱。金额上与旧行为等价，只多一条 release 流水。
        """
        state = c["state"]
        cap = int(charge["cap"])
        ledger = state["ledger"]
        ledger["frozen"] = max(0, int(ledger["frozen"]) - cap)
        ledger["balance"] += cap
        charge["state"], charge["cost"], charge["result"] = "released", 0, None
        charge["note"] = f"上游明确未执行，预占 {cap} 全额退回"
        self._tx(
            state,
            "release",
            f"{name} 未执行退回",
            cap,
            f"预占 {cap} 退回余额",
            projectId=pid,
        )
        # 退钱这件事已经写进内存状态了，异常还会往上抛：_action_sync 看到这个
        # 标记必须把状态落库，否则这次退款只存在于即将被丢弃的字典里。
        c["persist"] = True

    # ------------------------------------------------------- 待确认凭据的自动结算
    def _settle_pending_charge(
        self,
        s: dict[str, Any],
        charge: dict[str, Any],
        outcome: str,
        label: str = "复核",
    ) -> bool:
        """把一条 pending_review 凭据按 succeeded / failed 结算。

        这是**唯一**的资金路径：管理员手工 `resolveCharge` 与按上游编号自动确认
        都走这里，因此两者不可能各扣一次。返回 False 表示凭据已经不是待复核
        （幂等：重复调用不会重复入账 / 重复退款）。

        - succeeded：frozen -= cap、state=settled、cost=price、_count_spend、
          balance += (cap-price)、写 refund 流水、_assert_conservation。
        - failed：frozen -= cap、balance += cap、state=released、cost=0、
          写 release 流水、**不**调 _count_spend（自动退款不多计已用额度）。
        """
        if charge.get("state") != "pending_review":
            return False
        ledger = s["ledger"]
        cap, price = int(charge["cap"]), int(charge["price"])
        if outcome == "failed":
            ledger["frozen"] = max(0, int(ledger["frozen"]) - cap)
            ledger["balance"] += cap
            charge["state"], charge["cost"] = "released", 0
            self._tx(
                s,
                "release",
                f"{charge['action']} {label}：未执行",
                cap,
                f"凭据 {charge['id']} {label}为未执行，预占 {cap} 退回余额",
                projectId=charge.get("projectId"),
            )
        elif outcome == "succeeded":
            ledger["frozen"] = max(0, int(ledger["frozen"]) - cap)
            charge["state"], charge["cost"] = "settled", price
            self._count_spend(s, price)
            self._tx(
                s,
                "settle",
                f"{charge['action']} {label}：已执行",
                0,
                f"凭据 {charge['id']} {label}为已执行，冻结内核销 {price}",
                projectId=charge.get("projectId"),
            )
            diff = cap - price
            if diff > 0:
                ledger["balance"] += diff
                self._tx(
                    s,
                    "refund",
                    f"{charge['action']} {label}退差",
                    diff,
                    f"预占 {cap} - 实际 {price} 退回余额",
                    projectId=charge.get("projectId"),
                )
        else:
            _fail(
                "invalid_outcome",
                "复核结论只能是 failed（上游未执行，退款）或 succeeded（上游已执行，结算）",
                400,
            )
        self._assert_conservation(s)
        return True

    def _auto_settle_pending_charges(self, s: dict[str, Any]) -> int:
        """扫描带上游任务编号的待确认凭据，问一次解析器并就地结算。

        still_running / unknown 一律保持 pending_review（冻结继续保留，页面如实
        说明「正在核实、确认后自动结算或原路退回」），绝不静默扣掉或悄悄放着不管。
        没有编号的凭据（mock / 老库）同样保持待复核：没有查询对象就不能凭猜测结算。
        每条凭据只处理一次：结算后状态不再是 pending_review，下一轮自动跳过。
        """
        settled = 0
        for charge in s.get("charges", []):
            if charge.get("state") != "pending_review":
                continue
            upstream_task_id = str(charge.get("upstreamTaskId") or "").strip()
            if not upstream_task_id:
                continue
            outcome = self.pending_resolver.resolve(upstream_task_id)
            if outcome.outcome in ("still_running", "unknown"):
                continue
            # 上游真值可为 None（未知）。**只覆盖真值**：原来记着 250 就还是 250，
            # 不能被一次「这次没读到」的查询抹成 None——那等于把已知改回未知，
            # 对账表会凭空多出「缺数」并让毛利失真。与 `_hold_for_review` 同口径。
            if outcome.upstream_points is not None:
                charge["upstreamPoints"] = outcome.upstream_points
            if outcome.account_balance is not None:
                charge["upstreamBalance"] = outcome.account_balance
            if self._settle_pending_charge(s, charge, outcome.outcome, label="自动确认"):
                charge["note"] = f"{charge.get('note', '')} / 自动确认：{outcome.outcome}"
                charge["resolvedAt"] = time.strftime("%m-%d %H:%M", time.localtime())
                settled += 1
        return settled

    async def sweep_pending_charges(self) -> int:
        """后台轻量扫描：只在**真的有结论**时才写库。

        与 `tick` 共用 `_auto_settle_pending_charges`，但不用 action() 的全量提交
        （那样每 2 秒都会写一遍 state 和一条审计，解析器返回 unknown 时纯属空转）。
        崩溃恢复也走这里：进程重启后待确认凭据仍在库里，下一轮 pump 会继续问。
        """
        owners = await asyncio.to_thread(self.store.owners_with_pending_upstream)
        total = 0
        for owner in owners:
            lock = await self._lock_for(owner)
            async with lock:
                total += await asyncio.to_thread(self._sweep_owner_pending, owner)
        return total

    def _sweep_owner_pending(self, owner: str) -> int:
        self.store.ensure_owner(owner)
        state = self.store.read(lambda db: self.store._state_in(db, owner))
        changed = self._auto_settle_pending_charges(state)
        if not changed:
            return 0
        self._assert_conservation(state)
        self.store.run(lambda db: self.store.replace_in(db, owner, state))
        return changed

    # ------------------------------------------------------- 充值到账（唯一资金入口）
    async def credit_order(
        self,
        owner: str,
        amount: int,
        request_id: str,
        on_commit: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any]:
        """充值到账：余额、credit 流水、守恒自检与订单状态在**同一个事务**里提交。

        绝不直接 UPDATE drama_ledger 绕过守恒。充值**不受**用户自设积分上限约束
        （上限只管花钱）：不调 _check_credit_limits、不调 _count_spend，也不进
        daily_spend 的两个 SUM（那两个只统计 drama_charge / drama_tasks 的 cost）。
        """
        lock = await self._lock_for(owner)
        async with lock:
            return await asyncio.to_thread(
                self._credit_order_sync, owner, amount, request_id, on_commit
            )

    def _credit_order_sync(
        self,
        owner: str,
        amount: int,
        request_id: str,
        on_commit: Callable[[sqlite3.Connection], None] | None,
    ) -> dict[str, Any]:
        if type(amount) is not int or amount <= 0:
            _fail("payment_amount_invalid", "充值积分必须是正整数", 400)
        self.store.ensure_owner(owner)
        state = self.store.read(lambda db: self.store._state_in(db, owner))
        rid = str(request_id or "").strip()
        # 幂等：同一 requestId 只允许入账一次（与 adminCredit 的判重口径一致）。
        already = any(
            t["kind"] == "credit" and _request_of(str(t.get("note") or "")) == rid
            for t in state["ledger"]["tx"]
        )
        if not already:
            state["ledger"]["balance"] += amount
            self._tx(state, "credit", "充值到账", amount, f"充值到账 / requestId={rid}")
            self._assert_conservation(state)

        def op(db: sqlite3.Connection) -> None:
            if not already:
                self.store.replace_in(db, owner, state)
            # 订单状态更新与账本写入同一事务：任一步抛错都整笔 ROLLBACK，
            # 绝不会出现「钱到了但订单还是待支付」或反过来。
            if on_commit is not None:
                on_commit(db)

        self.store.run(op)
        return {
            "balance": int(state["ledger"]["balance"]),
            "amount": amount,
            "credited": not already,
            "request_id": rid,
        }

    # ------------------------------------------------------- 关键帧 / 视频任务
    @staticmethod
    def _task_kind(task: dict[str, Any]) -> str:
        """任务产物类型：`keyframe`（关键帧）或 `video`（图生视频）。

        读不到 / 老数据一律按关键帧：本功能上线前只有关键帧任务这一种，
        这不是兜底猜测，是那些行的事实。
        """
        return "video" if str(task.get("kind") or "") == "video" else "keyframe"

    @classmethod
    def _kind_label(cls, task: dict[str, Any]) -> str:
        """资金流水 / 任务备注里给用户看的中文名。"""
        return "视频" if cls._task_kind(task) == "video" else "关键帧"

    @classmethod
    def _task_unit_pricing(cls, s: dict[str, Any], task: dict[str, Any]) -> tuple[int, int]:
        """一条任务的 (售价, 单次硬上限)，按产物类型分流。

        关键帧读 `pricing.t2i/cap`（管理员可改的那一对），视频读图生视频的生效价
        再 `cap_of()` 派生 cap —— 与 `_dispatch` 同口径，绝不把 t2i 的 cap 用到视频上。
        """
        if s.get("_byok"):
            return 0, 0
        if cls._task_kind(task) == "video":
            price = effective_price(VIDEO_PRICE_KEY, s.get("pricing"))
            price = PRICE_VIDEO if price is None else price
            return price, cap_of(price)
        return int(s["pricing"]["t2i"]), int(s["pricing"]["cap"])

    @staticmethod
    def _kf_pricing(s: dict[str, Any]) -> tuple[int, int]:
        """关键帧的 (单价, 冻结上限)。自带密钥时为 0：费用由用户的 Liblib 账户承担。"""
        if s.get("_byok"):
            return 0, 0
        return int(s["pricing"]["t2i"]), int(s["pricing"]["cap"])

    @staticmethod
    def _invoke_task(
        c: dict[str, Any],
        prov: Any,
        task: dict[str, Any],
        req: dict[str, Any],
        first_frame: bytes | None,
        proj: dict[str, Any] | None,
    ) -> Any:
        """按任务类型调 Provider：关键帧走 `keyframe()`，视频走 `image_to_video()`。

        参数取法沿用 `image-to-video` 动作那一套（同一个 `_shot_request` 产出 req、
        同一个 `_first_frame` 取首帧），尺寸同样取项目比例，不另写一套——
        否则「任务批量出视频」与「单镜点生成视频」会拿不同的首帧。
        """
        if DramaService._task_kind(task) == "video":
            size = str((proj or {}).get("ratio") or "")
            return prov.image_to_video(req, size, owner=c["owner"], first_frame=first_frame)
        return prov.keyframe(req, owner=c["owner"], first_frame=first_frame)

    def _release_task_reserve(self, s: dict[str, Any], task: dict[str, Any], note: str) -> int:
        """把一条未结算任务的预占全额退回，任务标 cancelled 并写清去向。

        关键帧与视频共用这一条退款路径（金额规则相同：预占全额回到余额）。
        只有文案按 `kind` 分流，避免出现「视频任务退了钱却写着关键帧」。
        """
        label = self._kind_label(task)
        reserve = int(task.get("reserve") or 0)
        ledger = s["ledger"]
        if reserve > 0:
            ledger["frozen"] = max(0, int(ledger["frozen"]) - reserve)
            ledger["balance"] += reserve
            self._tx(
                s,
                "release",
                f"{label}预占退还 · {task['id'][-6:]}",
                reserve,
                note,
                projectId=task.get("projectId"),
                taskId=task.get("id"),
            )
        task["state"], task["percent"], task["reserve"] = "cancelled", 0, 0
        task["note"] = note
        return reserve

    def _settle_legacy_tasks(self, s: dict[str, Any], current_provider: str) -> int:
        """切换 Provider 后，给上次模式遗留的未结算关键帧任务一个明确去向。

        只处理 queued / running：in_flight 是「正在出网」，由启动兜底转 held，
        不能在这里退钱（上游可能已经执行）。空 sourceProvider 是老数据，
        视为当前模式，不误伤。
        """
        released = 0
        for task in s["tasks"]:
            origin = str(task.get("sourceProvider") or "")
            if (
                origin
                and origin != current_provider
                and task["state"] in ("queued", "running")
                and not task["settled"]
            ):
                self._release_task_reserve(
                    s,
                    task,
                    f"切换 Provider（{origin} → {current_provider}）后遗留的未结算"
                    f"{self._kind_label(task)}任务：预占已自动全额退还，任务已取消；"
                    "不会无声冻结。",
                )
                released += 1
        if released:
            self._assert_conservation(s)
        return released

    @staticmethod
    def _keyframe_request(
        service: DramaService, c: dict[str, Any], s: dict[str, Any], task: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None, dict[str, Any] | None]:
        """按镜头构造出网请求：项目设定 + @角色 锚点 + 首帧字节。

        关键帧与视频**共用**这一个函数：两者交给 Provider 的都是「同一个镜头的
        同一套参数」（`image-to-video` 动作也走 `_shot_request` + `_first_frame`）。
        另写一套就会出现「动作入口按 A 组参数出网、任务入口按 B 组」的漂移。

        `count` 是本次要出的候选张数，来自任务行（提交时已落库）：上游按张计费，
        所以这里既不能写死 1，也不能在 tick 时从 cost 反推（管理员期间改价会算错）。
        """
        proj = next((x for x in s["projects"] if x["id"] == task["projectId"]), None)
        shot = next((x for x in (proj or {}).get("shots", []) if x["id"] == task["shotId"]), None)
        if proj is None or shot is None:
            return {}, None, None, None
        req, asset_id = service._shot_request(shot, proj, None)
        req["count"] = max(1, int(task.get("candidates") or 1))
        first_frame = service._first_frame(c["owner"], proj, asset_id)
        return req, first_frame, proj, shot

    @staticmethod
    def _remember_media(shot: dict[str, Any], url: str, kind: str) -> None:
        """把一次**成功写回镜头**的产物地址记进历史（最新在前，最多 10 条）。

        为什么每次都记、且不去重：历史是「镜头先后用过哪些产物」的流水，恢复本身
        也记一条，所以恢复也可以再撤销。去重会让「恢复回上一版」在列表里看不出痕迹。
        """
        url = str(url or "").strip()
        if not url:
            return
        history = shot.get("mediaHistory")
        if not isinstance(history, list):
            history = []
        history.insert(
            0,
            {
                "url": url,
                "kind": "video" if kind == "video" else "image",
                "at": time.strftime("%m-%d %H:%M", time.localtime()),
            },
        )
        shot["mediaHistory"] = history[:MEDIA_HISTORY_MAX]

    def _finish_task(
        self,
        c: dict[str, Any],
        s: dict[str, Any],
        task: dict[str, Any],
        outcome: Any,
        shot: dict[str, Any] | None,
    ) -> None:
        """把一次 Provider 调用的结果（dict 或异常）落到 task / shot / 账本。

        关键帧与视频**共用这一条资金路径**：预占留在 frozen，成功按 cost 结算并退差，
        明确失败整笔退还，结果未知保留冻结并转 held（管理员 releaseHeld 是唯一出口）。
        只有「写回哪个字段 / 流水文案」按 `kind` 分叉——复制一份平行实现，两份迟早漂移，
        而漂移的地方是钱。

        `kind` 缺省为 keyframe：老任务行没有这一列的值时行为与从前逐字节相同。
        """
        kind = self._task_kind(task)
        label = self._kind_label(task)
        product = "视频" if kind == "video" else "图片"
        ledger = s["ledger"]
        if isinstance(outcome, BaseException) and not isinstance(outcome, dict):
            code = str(getattr(outcome, "code", type(outcome).__name__))
            if self._unknown_outcome(outcome):
                task["state"], task["percent"] = "held", 0
                # 上游可能已扣费但结果不明：把已知的真值留下来（未知仍是 NULL），
                # 管理员靠「待复核 + 上游已扣 N 积分」决定结算还是退款。
                points, balance = self._upstream_of(getattr(outcome, "usage", None))
                if points is not None:
                    task["upstreamPoints"] = points
                if balance is not None:
                    task["upstreamBalance"] = balance
                task["note"] = (
                    f"{label}生成结果未知（{code}）：预占 {task['reserve']} 保留，"
                    "禁止自动重提；请管理员在任务中心「带原因释放」"
                )
                return
            detail = str(getattr(outcome, "message", outcome) or "")[:220]
            self._release_task_reserve(
                s, task, f"{label}生成明确失败（{code}）：未产出{product}，预占全额退还"
            )
            task["state"] = "failed"
            task["note"] += f"；原因：{detail}；可用「重试同一任务」重新执行"
            return
        payload = (outcome or {}).get("payload") or {}
        provider = str((outcome or {}).get("provider") or "")
        sample = bool(payload.get("sample"))
        if kind == "video":
            video_url = str(payload.get("video_url") or "")
            task["frames"] = [video_url] if video_url else []
            task["picked"] = 0 if video_url else None
            if shot is not None and video_url:
                shot["videoUrl"] = video_url
                self._remember_media(shot, video_url, "video")
            task["upstreamTaskId"] = "vd-" + task["id"][-6:]
            task["note"] = f"视频由 {provider} 产出并写回镜头；" + (
                "示例，非模型生成" if sample else "真实 Provider 图生视频"
            )
        else:
            # 多候选：`image_urls` 是本次真正产出的全部候选；老 Provider / 老数据
            # 只回 `image_url`，这里退回单张，保持向后兼容。
            raw_urls = payload.get("image_urls")
            urls = (
                [str(u) for u in raw_urls if isinstance(u, str) and u]
                if isinstance(raw_urls, list)
                else []
            )
            first = str(payload.get("image_url") or "")
            if not urls and first:
                urls = [first]
            task["frames"] = urls
            task["picked"] = 0 if urls else None
            if shot is not None and urls:
                shot["imageUrl"] = urls[0]
                self._remember_media(shot, urls[0], "image")
            task["upstreamTaskId"] = "kf-" + task["id"][-6:]
            task["note"] = f"关键帧由 {provider} 产出真图并写回镜头；" + (
                "示例，非模型生成" if sample else "真实 Provider 文生图"
            )
        task["state"], task["settled"], task["percent"] = "succeeded", True, 100
        task["settledDay"] = time.strftime("%Y-%m-%d", time.localtime())
        # 上游真值与售价分开记：`cost` 是售价，这里记上游实际扣的积分。
        # 上游响应里没有 pointsCost / accountBalance 时保持 NULL = 未知；
        # **绝不**用 UPSTREAM_COST_T2I / UPSTREAM_COST_VIDEO 这类预算常量回填（那是估，不是实）。
        points, balance = self._upstream_of(payload)
        task["upstreamPoints"] = points
        task["upstreamBalance"] = balance
        reserve, cost = int(task["reserve"]), int(task["cost"])
        ledger["frozen"] = max(0, int(ledger["frozen"]) - reserve)
        self._count_spend(s, cost)
        self._tx(
            s,
            "settle",
            f"{label}结算 · {task['id'][-6:]}",
            0,
            f"冻结内核销 {cost} 积分，余额不变",
            projectId=task["projectId"],
            taskId=task["id"],
        )
        diff = reserve - cost
        if diff > 0:
            ledger["balance"] += diff
            self._tx(
                s,
                "refund",
                f"{label}结算退差 · {task['id'][-6:]}",
                diff,
                f"预占 {reserve} - 实际 {cost} 退回余额",
                projectId=task["projectId"],
                taskId=task["id"],
            )
        self._assert_conservation(s)
        self._media(c, outcome, task["projectId"])

    def _live_asset_ids(self, state: dict[str, Any]) -> frozenset[str]:
        """当前仍被业务引用的 asset_id：清理素材时这些文件一律保留。

        为什么必须由服务层算：`media_store` 看不到 state，它只知道磁盘上有什么。
        `purge_older_than` 原本只按 mtime 删，于是一个用了一个月的老镜头，它的视频
        mtime 同样很老，点一次「清理历史素材」就把它删了——而 `shots[].videoUrl`
        还指着它。后果不是「少一个文件」而是静默错账：`build_plan()` 对拿不到路径
        的镜头**静默跳过**，成片悄悄少一段；`exportFilm` 才报 `film_missing`，
        那时已经查不出是哪一次清理干的。

        清单要覆盖**所有会被合成或播放读到的 URL 落点**，包括产物历史：历史里的
        旧图旧片仍要能一键回退，删了就永远 404。用户主动上传的素材也算引用
        （清理的目标是生成出来的大文件，不是把人自己传的东西删掉）。

        宁可多留不可错删：这里多算一个 id，最坏是少回收一点磁盘；漏算一个，
        就是用户点一下按钮丢掉正在用的成片。
        """
        urls: set[str] = set()
        for item in state.get("uploads", []):
            urls.add(str((item or {}).get("url") or ""))
        for proj in state.get("projects", []):
            for shot in proj.get("shots", []):
                urls.add(str(shot.get("imageUrl") or ""))
                urls.add(str(shot.get("videoUrl") or ""))
                # 产物历史里的是可回退的旧版本，同样不能删。
                for entry in shot.get("mediaHistory") or []:
                    urls.add(str((entry or {}).get("url") or ""))
            # 候选帧映射：shotId -> [url]，重抽出来的候选也在这里。
            for values in (proj.get("frames") or {}).values():
                for url in values or []:
                    urls.add(str(url))
            for bucket in (proj.get("assets") or {}).values():
                for asset in bucket:
                    urls.add(str(asset.get("imageUrl") or ""))
            audio = proj.get("audio") or {}
            urls.add(str((audio.get("bgm") or {}).get("url") or ""))
            for key in ("voices", "subtitles"):
                for row in audio.get(key) or []:
                    urls.add(str((row or {}).get("audio_url") or ""))
            urls.add(str((proj.get("film") or {}).get("videoUrl") or ""))
            for exp in proj.get("exports") or []:
                urls.add(str((exp or {}).get("filmUrl") or ""))
        return frozenset(
            asset_id for asset_id in (self.media_store.asset_id_of_url(u) for u in urls) if asset_id
        )

    def _dispatch_inner(self, name: str, c: dict[str, Any]) -> Any:
        s, p, a = c["state"], c["project"], c["args"]
        ledger = s["ledger"]
        # 一律从 ctx 取 Provider（通常是 _DeferredProvider 代理），不读 self.provider：
        # 不同 owner 的动作会并发跑在各自线程上，读实例属性拿到的可能是别人的代理。
        prov = c["provider"]

        def arg(i: int, default: Any = None) -> Any:
            return a[i] if len(a) > i else default

        def need_project() -> dict[str, Any]:
            if p is None:
                _fail("project_missing", "当前项目不存在", 404)
            return p

        def asset_bucket(typ: Any) -> list[dict[str, Any]]:
            # 未校验的键会让 proj["assets"][typ] 抛 KeyError -> 500；
            # 这里统一变成可读的 400。
            if typ not in ASSET_TYPES:
                _fail(
                    "invalid_asset_type",
                    f"资产分类只能是 {' / '.join(ASSET_TYPES)}，收到 {typ!r}",
                )
            return need_project()["assets"][str(typ)]

        # --- 项目 ---
        if name == "createProject":
            f = _as_dict(arg(0), "createProject 的参数")
            title = str(f.get("name") or "").strip()
            if not title:
                _fail("invalid_name", "项目名称不能为空")
            proj = {
                "id": _uid("p"),
                "name": title,
                "season": _as_int(f.get("season") or 1, "season", 0),
                "episode": _as_int(f.get("episode") or 1, "episode", 0),
                "episodeTitle": f.get("episodeTitle") or "未命名",
                "genre": f.get("genre") or DEFAULT_SETTINGS["genre"],
                "style": f.get("style") or DEFAULT_SETTINGS["style"],
                "ratio": f.get("ratio") or DEFAULT_SETTINGS["ratio"],
                "eps": _as_int(f.get("eps") or 12, "eps", 1),
                "duration": _as_int(f.get("duration") or 90, "duration", 1),
                "budget": _as_int(f.get("budget") or 3000, "budget", 1),
                "res": DEFAULT_SETTINGS["res"],
                "archived": False,
                "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
                "script": {
                    "layers": {"logline": "", "outline": "", "body": ""},
                    "dirtySinceFreeze": False,
                    "frozenVersionId": None,
                    "versions": [
                        {
                            "id": _uid("v"),
                            "label": "v1 · 草稿",
                            "layers": {"logline": "", "outline": "", "body": ""},
                            "createdAt": "刚创建",
                            "note": "新建",
                        }
                    ],
                },
                "assets": {"characters": [], "scenes": [], "props": []},
                "shots": [],
                "frames": {},
                "timeline": {"order": [], "notes": []},
                "audio": {
                    "voices": [],
                    "subtitles": [],
                    "bgm": {"name": "未设置", "voiceLevel": 100, "bgmLevel": 25, "ambient": "无"},
                },
                "qa": [False] * 4,
                "exports": [],
            }
            s["projects"].append(proj)
            s["activeProjectId"] = proj["id"]
            return proj
        if name == "copyProject":
            src = next((x for x in s["projects"] if x["id"] == arg(0)), _MISSING)
            if src is None:
                _fail("project_missing", "项目不存在", 404)
            clone = _reid_project(src, _uid("p"))
            s["projects"].append(clone)
            s["activeProjectId"] = clone["id"]
            return clone
        if name == "archiveProject":
            target = next((x for x in s["projects"] if x["id"] == arg(0)), _MISSING)
            if target is None:
                return None
            if sum(1 for x in s["projects"] if not x["archived"]) <= 1:
                _fail("last_project", "至少保留一个未归档项目")
            target["archived"] = True
            # 归档当前项目后必须切到另一个未归档项目，否则 activeProjectId 悬空，
            # 后续所有项目内动作都会 404（与前端 localActs.archiveProject 保持一致）。
            if s["activeProjectId"] == target["id"]:
                nxt = next((x for x in s["projects"] if not x["archived"]), _MISSING)
                if nxt is not None:
                    s["activeProjectId"] = nxt["id"]
            return None
        if name == "unarchiveProject":
            target = next((x for x in s["projects"] if x["id"] == arg(0)), _MISSING)
            if target is None:
                _fail("project_missing", "项目不存在", 404)
            target["archived"] = False
            return None
        if name == "deleteProject":
            if len(s["projects"]) <= 1:
                _fail("last_project", "至少保留一个项目")
            gone = str(arg(0))
            survivors = [t for t in s["tasks"] if t["projectId"] != gone]
            # 分两种处理，少一种都会破坏守恒：
            # 1) 已结算任务的 cost 随行一起消失 -> 累计进 retiredCost，守恒基数才不变；
            # 2) 未结算任务的 reserve 还在 frozen 里 -> 必须先释放回余额，
            #    否则这部分积分既不在 balance 也没有对应任务，等于凭空消失。
            retired = 0
            for task in s["tasks"]:
                if task["projectId"] != gone:
                    continue
                if task["settled"]:
                    retired += int(task["cost"])
                else:
                    reserve = int(task.get("reserve") or 0)
                    if reserve > 0:
                        ledger["frozen"] = max(0, int(ledger["frozen"]) - reserve)
                        ledger["balance"] += reserve
                        self._tx(
                            s,
                            "release",
                            f"删项目释放预占 · {task['id'][-6:]}",
                            reserve,
                            "项目被删除，未结算任务的预占退回余额",
                            projectId=gone,
                            taskId=task["id"],
                        )
            if retired:
                ledger["retiredCost"] = int(ledger.get("retiredCost") or 0) + retired
            s["projects"] = [x for x in s["projects"] if x["id"] != gone]
            s["tasks"] = survivors
            # 同理：删除当前项目后切到剩余第一个项目。
            if s["activeProjectId"] == gone:
                s["activeProjectId"] = s["projects"][0]["id"]
            return None

        # --- 设定 ---
        if name in ("saveSettings", "restoreDefaults"):
            proj = need_project()
            f = _as_dict(arg(0), f"{name} 的参数")
            if name == "restoreDefaults":
                f = dict(DEFAULT_SETTINGS) | {
                    "name": proj["name"],
                    "episodeTitle": proj["episodeTitle"],
                }
            # 判「是否需要把已锁定分镜标脏」只看本次**真的带了的**两项：
            # 调用方只改字幕/分辨率时 style/ratio 缺键，按旧写法会用 None 去比，
            # 结果 changed 恒为 True，把一个纯交付格式的改动说成「画面变了」，
            # 顺手把全部分镜标成待更新。页面总是整份提交，所以这不改变既有行为。
            changed = (f.get("style") is not None and proj["style"] != f["style"]) or (
                f.get("ratio") is not None and proj["ratio"] != f["ratio"]
            )
            proj["name"] = str(f.get("name") or proj["name"]).strip() or proj["name"]
            for key in ("genre", "style", "ratio", "res", "episodeTitle"):
                if f.get(key) is not None:
                    proj[key] = f[key]
            for key in ("eps", "duration", "budget"):
                if f.get(key) is not None:
                    proj[key] = _as_int(f[key], key, 1)
            # 成片交付选项。非法值一律夹到最保守的一侧（软封装 / 不缩放），
            # 因为这两项直接改变 ffmpeg 的命令行：拼错不该让一次保存变成 500，
            # 也不该意外把一个项目切成「重编码 + 烧字幕」。
            if f.get("subtitleMode") is not None:
                proj["subtitleMode"] = parse_subtitle_mode(f.get("subtitleMode"))
            if f.get("outputResolution") is not None:
                proj["outputResolution"] = parse_resolution(f.get("outputResolution"))
            if changed:
                for shot in proj["shots"]:
                    if shot["state"] == "locked":
                        shot["state"], shot["dirty"] = "dirty", True
            return changed

        # --- 剧本 ---
        if name == "saveScript":
            proj = need_project()
            f = _as_dict(arg(0), "saveScript 的参数")
            # 只有**明确带了**这一层才覆盖：少传一层就把它清空，等于给调用方
            # 埋了一个「只想改正文、结果简介和大纲被抹掉」的坑。显式传 ""
            # 仍然清空（那是用户真的想删），缺键才是「别动它」。
            for key in ("logline", "outline", "body"):
                if key in f and f[key] is not None:
                    proj["script"]["layers"][key] = f[key]
            if proj["script"]["frozenVersionId"]:
                proj["script"]["dirtySinceFreeze"] = True
            return None
        if name == "newScriptVersion":
            proj = need_project()
            n = len(proj["script"]["versions"]) + 1
            version = {
                "id": _uid("v"),
                "label": f"v{n} · 草稿",
                "layers": dict(proj["script"]["layers"]),
                "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
                "note": "手动保存",
            }
            proj["script"]["versions"].insert(0, version)
            return version
        if name == "freezeScript":
            proj = need_project()
            if not proj["script"]["layers"]["body"].strip():
                _fail("empty_script", "剧本正文为空，不能冻结")
            vid = arg(0)
            version = next((v for v in proj["script"]["versions"] if v["id"] == vid), _MISSING)
            if version is None:
                version = self._dispatch("newScriptVersion", c)
            proj["script"]["frozenVersionId"] = version["id"]
            proj["script"]["dirtySinceFreeze"] = False
            version["label"] = str(version["label"]).replace("草稿", "制作基准")
            version["note"] = "冻结"
            return version
        if name == "rollbackScript":
            proj = need_project()
            version = next((v for v in proj["script"]["versions"] if v["id"] == arg(0)), _MISSING)
            if version is None:
                _fail("version_missing", "版本不存在", 404)
            proj["script"]["layers"] = dict(version["layers"])
            if proj["script"]["frozenVersionId"] not in (None, version["id"]):
                proj["script"]["dirtySinceFreeze"] = True
            return None
        if name == "diffScriptVersions":
            # 纯读：不写库、不改任何版本、不动冻结状态。对比算法本身在
            # `script_diff` 里，这里只负责取版本与把「空 id」解释成当前草稿。
            proj = need_project()
            versions = proj["script"]["versions"]

            def pick(raw: Any, side: str) -> dict[str, Any]:
                key = str(raw or "").strip()
                if not key:
                    # 空 id = 当前草稿。这是最常用的一次对比（「我这版改了什么」），
                    # 让前端不必先存成一个版本才能看差异。
                    return {
                        "id": "",
                        "label": "当前草稿",
                        "layers": dict(proj["script"]["layers"]),
                    }
                found = next((v for v in versions if v["id"] == key), _MISSING)
                if found is None:
                    _fail("version_missing", f"{side}版本不存在，可能已被删除，请刷新后重试", 404)
                return found

            return diff_scripts(pick(arg(0), "起始"), pick(arg(1), "对比"))
        if name == "markDirtyByScript" or name == "resplitShots":
            proj = need_project()
            if not proj["script"]["layers"]["body"].strip():
                _fail("empty_script", "剧本正文为空，无法拆分分镜")
            for shot in proj["shots"]:
                if shot["state"] == "locked":
                    shot["state"], shot["dirty"] = "dirty", True
            return None
        if name == "script-doctor":
            proj = need_project()
            item = prov.script_doctor(proj["script"]["layers"], owner=c["owner"])
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item
        if name == "creative-start":
            proj = need_project()
            item = prov.creative_start(str(arg(0, "idea")), str(arg(1, "")), owner=c["owner"])
            proj["script"]["layers"] = item["payload"]["layers"]
            if proj["script"]["frozenVersionId"]:
                proj["script"]["dirtySinceFreeze"] = True
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item

        # --- 资产 ---
        if name == "addAsset":
            typ, f = arg(0, "characters"), arg(1, {}) or {}
            items = asset_bucket(typ)
            title = str(f.get("name") or "").strip()
            if not title:
                _fail("invalid_name", "名称不能为空")
            if any(x["name"] == title for x in items):
                _fail("duplicate_asset", "同名资产已存在", 409)
            item = {
                "id": _uid("a"),
                "name": title,
                "sub": f.get("sub") or "草稿",
                "voice": f.get("voice") or "—",
                "note": f.get("note") or "",
                "prompt": f.get("prompt") or "",
                "seed": int(time.time()) % 60 + 1,
                "locked": False,
                "imageUrl": "",
            }
            items.append(item)
            return item
        if name == "updateAsset":
            typ, aid, f = arg(0), arg(1), arg(2, {}) or {}
            items = asset_bucket(typ)
            item = next((x for x in items if x["id"] == aid), _MISSING)
            if item is None:
                _fail("asset_missing", "资产不存在", 404)
            label = "@角色" if typ == "characters" else ("@场景" if typ == "scenes" else "@道具")
            new_name = str(f.get("name") or "").strip()
            if new_name and new_name != item["name"]:
                if any(x["name"] == new_name and x["id"] != aid for x in items):
                    _fail("duplicate_asset", "同名资产已存在", 409)
                old_token, new_token = f"{label}:{item['name']}", f"{label}:{new_name}"
                for shot in need_project()["shots"]:
                    shot["refs"] = [new_token if r == old_token else r for r in shot["refs"]]
                item["name"] = new_name
            for key in ("sub", "voice", "note", "prompt"):
                if f.get(key) is not None:
                    item[key] = f[key]
            token = f"{label}:{item['name']}"
            for shot in need_project()["shots"]:
                if token in shot["refs"] and shot["state"] == "locked":
                    shot["state"], shot["dirty"] = "dirty", True
            return None
        if name == "deleteAsset":
            typ, aid = arg(0), arg(1)
            items = asset_bucket(typ)
            item = next((x for x in items if x["id"] == aid), _MISSING)
            if item is None:
                return None
            label = "@角色" if typ == "characters" else ("@场景" if typ == "scenes" else "@道具")
            used = sum(
                1 for sh in need_project()["shots"] if f"{label}:{item['name']}" in sh["refs"]
            )
            if used:
                _fail("asset_in_use", f"该资产被 {used} 个镜头引用，请先移除引用", 409)
            need_project()["assets"][str(typ)] = [x for x in items if x["id"] != aid]
            return None
        if name == "toggleAssetLock":
            item = next((x for x in asset_bucket(arg(0)) if x["id"] == arg(1)), _MISSING)
            if item is None:
                return None
            if not item["locked"] and not item["prompt"].strip():
                _fail("prompt_required", "请先填写提示词再锁定锚点")
            item["locked"] = not item["locked"]
            item["sub"] = "已锁定锚点" if item["locked"] else "草稿"
            return None
        if name in ("generateAssetCandidates", "asset-candidates"):
            proj = need_project()
            item = next((x for x in asset_bucket(arg(0)) if x["id"] == arg(1)), _MISSING)
            if item is None:
                return None
            # A1：项目设定（比例 / 画风 / 分辨率）随请求一起交给 Provider，
            # 不再只躺在项目设定页。传副本，别把生成的辅助字段写回业务 state。
            result = prov.asset_candidates(self._asset_request(item, proj), owner=c["owner"])
            item["seed"] = result["payload"]["seed"]
            # 真实候选图必须回写到资产本身，否则前端资产卡只能继续画 SVG 占位
            # 。mock 的 image_url 为 None，因此离线行为不变。
            image_url = result["payload"].get("image_url")
            if image_url:
                item["imageUrl"] = str(image_url)
            self._media(c, result, proj["id"])
            return result
        if name == "extract-assets":
            proj = need_project()
            result = prov.extract_assets(proj["script"]["layers"], owner=c["owner"])
            prefix = {"characters": "@角色", "scenes": "@场景", "props": "@道具"}
            added = 0
            for typ, candidates in result["payload"]["found"].items():
                if typ not in ASSET_TYPES:
                    continue
                for candidate in candidates:
                    title = candidate["name"]
                    if any(x["name"] == title for x in proj["assets"][typ]):
                        continue
                    proj["assets"][typ].append(
                        {
                            "id": _uid("a"),
                            "name": title,
                            "sub": "剧本抽取 · 草稿",
                            "voice": "—",
                            "note": "本地正则抽取，需人工确认",
                            "prompt": candidate["prompt"],
                            "seed": added * 3 + 5,
                            "locked": False,
                            "imageUrl": "",
                        }
                    )
                    added += 1
            result["payload"]["added"] = added
            result["payload"]["prefix"] = prefix
            self._media(c, result, proj["id"])
            return result

        # --- 分镜 ---
        if name in ("addShot", "insertShotBefore"):
            proj = need_project()
            # addShot 的表单在 args[0]（前端 A.act.addShot(form)）；
            # insertShotBefore 的 args[0] 是锚点镜头 id，没有表单。
            # 旧实现把 addShot 也读成 arg(1)，于是用户填的景别/台词/运镜全部被丢掉，
            # 只能得到「待补充画面描述」的默认镜头。
            f = _as_dict(arg(0), "addShot 的参数") if name == "addShot" else {}
            shot = {
                "id": _uid("S"),
                "dur": _as_int(f.get("dur") or 3, "dur", 1),
                "size": f.get("size") or "中景",
                "desc": f.get("desc") or "待补充画面描述",
                "prompt": f.get("prompt") or "",
                "line": f.get("line") or "",
                "move": f.get("move") or "固定镜头",
                "refs": [x for x in str(f.get("refs") or "").replace(",", " ").split() if x],
                "state": "draft",
                "dirty": False,
                "videoUrl": "",
            }
            if name == "insertShotBefore":
                index = next(
                    (i for i, x in enumerate(proj["shots"]) if x["id"] == arg(0)), _MISSING
                )
                if index is None:
                    _fail("shot_missing", "镜头不存在", 404)
                proj["shots"].insert(index, shot)
            else:
                proj["shots"].append(shot)
            proj["timeline"]["order"] = [x["id"] for x in proj["shots"]]
            return shot
        if name == "updateShot":
            proj = need_project()
            shot = next((x for x in proj["shots"] if x["id"] == arg(0)), _MISSING)
            if shot is None:
                return None
            f = arg(1, {}) or {}
            shot["dur"] = _as_int(f.get("dur") or shot["dur"], "dur", 1)
            for key in ("size", "desc", "line", "move"):
                if f.get(key) is not None:
                    shot[key] = f[key]
            shot["prompt"] = f.get("prompt") or ""
            shot["refs"] = [x for x in str(f.get("refs") or "").replace(",", " ").split() if x]
            return None
        if name == "deleteShot":
            proj = need_project()
            proj["shots"] = [x for x in proj["shots"] if x["id"] != arg(0)]
            proj["timeline"]["order"] = [x["id"] for x in proj["shots"]]
            proj["frames"].pop(arg(0), None)
            return None
        if name == "moveShot":
            proj = need_project()
            # 先校验参数再查镜头：否则「镜头 id 打错 + 位移传字母」会静默无操作，
            # 前端和小程序都看不出自己传错了。允许负数 —— 前端「上移」就是 dir=-1。
            offset = _as_int(arg(1, 0), "offset")
            ids = [x["id"] for x in proj["shots"]]
            i = ids.index(arg(0)) if arg(0) in ids else -1
            j = i + offset if i >= 0 else -1
            if 0 <= j < len(proj["shots"]):
                proj["shots"][i], proj["shots"][j] = proj["shots"][j], proj["shots"][i]
                proj["timeline"]["order"] = [x["id"] for x in proj["shots"]]
            return None
        if name == "toggleShotLock":
            proj = need_project()
            shot = next((x for x in proj["shots"] if x["id"] == arg(0)), _MISSING)
            if shot is None:
                return None
            shot["state"] = "draft" if shot["state"] == "locked" else "locked"
            shot["dirty"] = False
            return None
        if name == "resplit-shots-v2":
            proj = need_project()
            drafts = [x for x in proj["shots"] if x["state"] != "locked"]
            kept = [x for x in proj["shots"] if x["state"] == "locked"]
            # 未锁定镜头会被整批替换，它们已生成的关键帧 / 视频随之丢失，而那是花过
            # 积分的（实测：4 个各 30 积分的视频被无提示丢弃）。必须由调用方
            # 显式确认；检查放在调 Provider 之前，拒绝时预占原样退回、不产生扣费。
            tasked = {str(t.get("shotId")) for t in s["tasks"] if t.get("projectId") == proj["id"]}
            produced = [x for x in drafts if x.get("videoUrl") or x.get("imageUrl")]
            opts = arg(0) if isinstance(arg(0), dict) else {}
            if produced and not opts.get("discardGenerated"):
                videos = sum(1 for x in produced if x.get("videoUrl"))
                _fail(
                    "resplit_discards_media",
                    f"换一版分镜会替换 {len(drafts)} 个未锁定镜头，其中 {len(produced)} 个已有生成素材"
                    f"（含 {videos} 段视频），替换后无法找回、已花积分不退。"
                    "想保留的镜头请先锁定；确认丢弃请带上 discardGenerated=true 再提交。",
                    409,
                )
            result = prov.resplit_shots(proj["script"]["layers"], owner=c["owner"])
            for index, draft in enumerate(result["payload"]["shots"]):
                item = dict(draft) | {
                    "id": _uid("S"),
                    "refs": [],
                    "state": "draft",
                    "dirty": False,
                    "videoUrl": "",
                }
                # 只有「干净」的旧草稿才复用 id。带着素材或关键帧任务的旧 id 一旦复用，
                # 新镜头会继承旧任务：submitTasks 认为它已提交过，再也提不了关键帧。
                if index < len(drafts):
                    old = drafts[index]
                    if not (old.get("videoUrl") or old.get("imageUrl") or old["id"] in tasked):
                        item["id"] = old["id"]
                kept.append(item)
            proj["shots"] = kept
            proj["timeline"]["order"] = [x["id"] for x in kept]
            result["payload"]["replaced"] = len(drafts)
            self._remember(proj, name, result)
            self._media(c, result, proj["id"])
            return result

        # --- 生成与账本 ---
        if name == "submitTasks":
            proj = need_project()
            # A3 漏口已闭环：关键帧不再有「本地模拟 tick」。两种模式都走 Provider 的
            # keyframe()（真实 = 文生图 / 离线 = 确定性示例图），任务由 tick 两段式
            # 真正结算，因此这里不再按 Provider 名称禁用，也不存在「收费但不交付」。
            candidates = _candidate_count(arg(1))
            if candidates > 1 and not self._simulated_provider(c):
                # 真实通道的多图节点形状**从未实测**：`RemoteDramaProvider.keyframe`
                # 会把 count 如实报给上游，但实际只拿得回一张（`image_urls` 长度为 1）。
                # 按 4 张收钱、只交付 1 张，就是「收费不交付」——与「remote 下关键帧
                # 仍为模拟时禁止扣费」是同一条底线。这里 fail-closed：宁可暂时不给
                # 多候选，也不收钱少给货。离线 mock 不受影响（它能真的产出 N 张）。
                _fail(
                    "candidates_unsupported",
                    "真实生成通道暂不支持一次多张候选（上游返回张数未实测），请按 1 张生成",
                    409,
                )
            ids = arg(0) or [x["id"] for x in proj["shots"] if x["state"] in ("locked", "dirty")]
            # 准入只按**同类任务**判重：一条已经结算的视频任务不该把这个镜头的
            # 关键帧提交永久挡死。按 kind 分流的理由与 `submitVideoTasks` 完全对称
            # （那边只认 kind=video）；不分流的话，「先出视频、再想重出关键帧」
            # 会被判成 no_pending，而提示还写着「请先在分镜故事板锁定镜头」——
            # 用户锁了也没用，因为挡住他的是另一类任务。
            pending = [
                sid
                for sid in ids
                if not any(
                    t["projectId"] == proj["id"]
                    and t["shotId"] == sid
                    and self._task_kind(t) == "keyframe"
                    and t["state"] not in ("failed", "released", "cancelled")
                    # 示例模式留下的任务不挡真实生成（反之亦然）
                    and str(t.get("sourceProvider") or prov.name) == prov.name
                    for t in s["tasks"]
                )
            ]
            if not pending:
                _fail("no_pending", "没有可提交的镜头：请先在分镜故事板锁定镜头")
            cost, cap = self._kf_pricing(s)
            # 多候选按张线性放大预占与成本：上游按张计费，收一张的钱产四张
            # 等于平台自己吃下那三张的成本。candidates=1 时 per_reserve/per_cost
            # 与改造前逐字节相同（cap / t2i），既有账本与断言不受影响。
            per_reserve = cap * candidates
            per_cost = cost * candidates
            reserve_total = len(pending) * per_reserve
            # 项目预算与单日上限必须对关键帧任务同样生效，否则「预算 1 积分」
            # 只是一个装饰字段。口径统一走 _afford，
            # 与重抽 / 重试 / 生成动作共用同一份，避免再出现「某个入口漏了」。
            owner = c["owner"]
            self._afford(
                owner,
                proj,
                reserve_total,
                what=f"{len(pending)} 个镜头",
                # 离线 mock 不占日上限（用户裁决），关键帧入口同样适用。
                simulated=self._simulated_provider(c),
            )
            self._check_credit_limits(s, per_cost, per_cost * len(pending))
            self._require_balance(ledger, reserve_total, f"{len(pending)} 个镜头的关键帧")
            ledger["balance"] -= reserve_total
            ledger["frozen"] += reserve_total
            self._tx(
                s,
                "reserve",
                "批量关键帧预占",
                -reserve_total,
                f"{len(pending)} 镜 × {cap} 积分"
                if candidates == 1
                else f"{len(pending)} 镜 × {candidates} 张 × {cap} 积分（每张上限）",
                projectId=proj["id"],
            )
            for sid in pending:
                s["tasks"].append(
                    {
                        "id": _uid("T"),
                        "projectId": proj["id"],
                        "shotId": sid,
                        "type": "关键帧 t2i",
                        "kind": "keyframe",
                        "state": "queued",
                        "percent": 0,
                        "reserve": per_reserve,
                        "cost": per_cost,
                        "candidates": candidates,
                        "settled": False,
                        "picked": None,
                        "frames": [],
                        "idem": _uid("IK"),
                        "chargeId": _uid("C"),
                        "upstreamTaskId": None,
                        "sourceProvider": prov.name,
                        "note": f"预占 {per_reserve} / 预计实际 {per_cost}；"
                        "等待 Provider 产出关键帧",
                        "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
                    }
                )
            return len(pending)
        if name == "submitVideoTasks":
            # 批量图生视频：语义与 submitTasks 严格对齐（一次预占 -> N 条 queued
            # -> tick 逐镜出网结算），只是产物是视频、且要求镜头已有关键帧。
            proj = need_project()
            price = self._effective_price("image-to-video", s)
            if s.get("_byok"):
                price = 0
            elif price is None or price <= 0:
                # 定价表是「是否调上游」的唯一开关：没有价就没有可预占的金额，
                # 这里说明白而不是静默按 0 预占（那等于免费出网）。
                _fail("pricing_missing", "图生视频没有生效价，无法批量提交", 409)
            # 单次硬上限与 _dispatch 同口径：cap_of(生效价)，不读 pricing.cap
            # （那是 t2i 的 cap，36，用它会连一次视频都覆盖不住）。
            cap = cap_of(price)
            raw = arg(0)
            if raw is None or (isinstance(raw, list) and not raw) or raw == "":
                ids = [x["id"] for x in proj["shots"]]
            elif isinstance(raw, list):
                ids = [str(x) for x in raw]
            else:
                _fail("invalid_field", "镜头 id 必须是数组", 400)
            shots = {x["id"]: x for x in proj["shots"]}
            # 显式点名的镜头不存在 = 客户端拿错了 id，直接 404；
            # 不静默跳过（否则用户以为这一镜进了队列，钱却没预占它的份）。
            missing = [sid for sid in ids if sid not in shots]
            if missing:
                _fail("shot_missing", f"镜头不存在：{missing[0]}", 404)
            skipped: list[dict[str, str]] = []
            todo: list[str] = []
            for sid in ids:
                shot = shots[sid]
                if not str(shot.get("imageUrl") or ""):
                    skipped.append({"shotId": sid, "reason": "no_keyframe"})
                    continue
                busy = any(
                    self._task_kind(t) == "video"
                    and t["projectId"] == proj["id"]
                    and t["shotId"] == sid
                    and t["state"] not in ("failed", "released", "cancelled")
                    and str(t.get("sourceProvider") or prov.name) == prov.name
                    for t in s["tasks"]
                )
                if busy:
                    # held 也算 busy：上游可能已经执行，再提一次就是二次扣费。
                    skipped.append({"shotId": sid, "reason": "busy"})
                    continue
                todo.append(sid)
            if not todo:
                # 全部跳过时什么都不动账本：没有任务、没有预占，
                # 由 skipped[] 说明每一镜为什么没进队列。
                return {"queued": 0, "reserve": 0, "skipped": skipped}
            reserve_total = len(todo) * cap
            self._afford(
                c["owner"],
                proj,
                reserve_total,
                what=f"{len(todo)} 个镜头的视频",
                simulated=self._simulated_provider(c),
            )
            self._check_credit_limits(s, price, price * len(todo))
            self._require_balance(ledger, reserve_total, f"{len(todo)} 个镜头的视频")
            ledger["balance"] -= reserve_total
            ledger["frozen"] += reserve_total
            self._tx(
                s,
                "reserve",
                "批量视频预占",
                -reserve_total,
                f"{len(todo)} 镜 × {cap} 积分",
                projectId=proj["id"],
            )
            for sid in todo:
                s["tasks"].append(
                    {
                        "id": _uid("T"),
                        "projectId": proj["id"],
                        "shotId": sid,
                        "type": "视频 i2v",
                        "kind": "video",
                        "state": "queued",
                        "percent": 0,
                        "reserve": cap,
                        "cost": price,
                        "candidates": 1,
                        "settled": False,
                        "picked": None,
                        "frames": [],
                        "idem": _uid("IK"),
                        "chargeId": _uid("C"),
                        "upstreamTaskId": None,
                        "sourceProvider": prov.name,
                        "note": f"预占 {cap} / 预计实际 {price}；等待 Provider 产出视频",
                        "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
                    }
                )
            return {"queued": len(todo), "reserve": reserve_total, "skipped": skipped}
        if name == "tick":
            # 任务推进走 Provider 驱动的两段式，关键帧与视频同一条路径：
            #   首趟：选一条 queued -> 标 in_flight 落库 -> 出网（锁外）
            #   续跑：回放结果 -> 写回镜头产物 -> 按既有预占/结算/退还规则结算
            # 不再有「percent += 22 然后写死 frames = numeric*7+i」的本地模拟。
            provider_name = str(getattr(prov, "name", "") or "")
            self._settle_legacy_tasks(s, provider_name)
            # 待确认积分自动结算：带上游任务编号的 pending_review 凭据问一次解析器，
            # succeeded / failed 走与 resolveCharge 完全相同的资金路径；没有编号
            # 或结果仍未知时保持待复核（冻结保留，页面如实说明预计处理方式）。
            self._auto_settle_pending_charges(s)
            resume = str(c.get("resume_charge") or "")
            if resume.startswith("tick:"):
                # 续跑轮：`resume_charge` 带的是本请求首趟认领的 task id。
                task_id = resume[5:]
                task = next(
                    (t for t in s["tasks"] if t["id"] == task_id and t["state"] == "in_flight"),
                    None,
                )
                if task is None:
                    return False
                req, first_frame, task_proj, task_shot = self._keyframe_request(self, c, s, task)
                try:
                    item = self._invoke_task(c, prov, task, req, first_frame, task_proj)
                except Exception as exc:  # noqa: BLE001 - 失败也要落库成明确结局
                    self._finish_task(c, s, task, exc, task_shot)
                else:
                    self._finish_task(c, s, task, item, task_shot)
                return any(t["state"] in ("queued", "running") for t in s["tasks"])

            # 首趟：已有 in_flight 说明另一个请求正在出网，绝不重复调用上游。
            if any(t["state"] == "in_flight" for t in s["tasks"]):
                return True
            task = next(
                (t for t in s["tasks"] if t["state"] in ("queued", "running") and not t["settled"]),
                None,
            )
            if task is None:
                return False
            task["state"] = "in_flight"
            task["percent"] = 0
            task["note"] = (
                f"{self._kind_label(task)}已提交给 {provider_name} Provider，等待结果（预占保留）"
            )
            req, first_frame, task_proj, task_shot = self._keyframe_request(self, c, s, task)
            try:
                item = self._invoke_task(c, prov, task, req, first_frame, task_proj)
            except _DeferredCall as exc:
                # 非空 charge_id 让 _action_sync 先把 in_flight 状态落库再放开锁；
                # 它只是「续跑令牌」，不是一条新的扣费凭据，资金路径不变。
                exc.charge_id = f"tick:{task['id']}"
                raise
            # 同步 Provider（测试直接调 _dispatch_inner 时）：立即结算。
            self._finish_task(c, s, task, item, task_shot)
            return any(t["state"] in ("queued", "running") for t in s["tasks"])

        if name == "reconcileMode":
            # 服务启动 / 切换 Provider 时由 start() 调用；也可显式触发排障。
            return self._settle_legacy_tasks(s, str(getattr(prov, "name", "") or ""))
        if name == "injectFault":
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None:
                _fail("task_missing", "任务不存在", 404)
            if task["settled"] or task["state"] in ("succeeded", "released"):
                _fail("task_settled", "已结算任务不能再注入故障", 409)
            kind = str(arg(1) or "lost")
            if kind == "reject":
                # 上游明确拒绝：任务未执行，自动退还预占（不是管理员释放 held）。
                # 这样 failed 状态在本地 mock 下可达，「重试同一 task_id」按钮才有意义。
                reserve = int(task["reserve"])
                task["state"], task["percent"] = "failed", 0
                task["note"] = "上游明确拒绝：任务未执行，预占自动退还（同一 task_id 可重试）"
                ledger["frozen"] = max(0, ledger["frozen"] - reserve)
                ledger["balance"] += reserve
                self._tx(
                    s,
                    "release",
                    f"明确拒绝退还 · {task['id'][-6:]}",
                    reserve,
                    "上游明确拒绝、未执行，自动释放预占",
                    projectId=task["projectId"],
                    taskId=task["id"],
                )
                return None
            if kind != "lost":
                _fail("invalid_fault", "故障类型只能是 lost / reject")
            task["state"], task["percent"] = "held", 0
            task["note"] = "提交后响应丢失：上游可能已执行，禁止自动重提"
            return None
        if name == "cancelTask":
            # 只有「还没结算、也还在本地队列里」的任务可以取消。
            # succeeded 已花钱不能退；held 是「上游可能已执行」，必须走管理员释放；
            # failed 已自动退过预占，再退一次会重复退款。
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None:
                _fail("task_missing", "任务不存在", 404)
            if task["settled"] or task["state"] in ("succeeded", "released", "cancelled"):
                _fail("task_settled", "已结算 / 已释放 / 已取消的任务不能再取消", 409)
            if task["state"] not in ("queued", "running", "draft", "quoted"):
                _fail("not_cancellable", f"当前状态 {task['state']} 不能取消", 409)
            reason = str(arg(1) or "").strip() or "用户主动取消"
            reserve = int(task.get("reserve") or 0)
            task["state"], task["percent"] = "cancelled", 0
            task["note"] = "已取消 / 原因：" + reason
            if reserve > 0:
                ledger["frozen"] = max(0, ledger["frozen"] - reserve)
                ledger["balance"] += reserve
            self._tx(
                s,
                "release",
                f"任务取消 · {task['id'][-6:]}",
                reserve,
                "本地队列取消，预占全额退还 / " + reason,
                projectId=task["projectId"],
                taskId=task["id"],
            )
            return None
        if name == "releaseHeld":
            self._require_admin(c["owner"], "releaseHeld")
            reason = str(arg(1) or "").strip()
            if not reason:
                _fail("reason_required", "释放必须填写原因")
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None or task["state"] != "held":
                _fail("not_held", "只有 held 任务可以释放", 409)
            task["state"] = "released"
            task["note"] = "管理员确认不重提 / 原因：" + reason
            reserve = int(task.get("reserve") or 0)
            if reserve > 0:
                ledger["frozen"] = max(0, ledger["frozen"] - reserve)
                ledger["balance"] += reserve
            self._tx(
                s,
                "release",
                f"held 释放 · {task['id'][-6:]}",
                reserve,
                "管理员带原因释放 / " + reason,
                projectId=task["projectId"],
                taskId=task["id"],
            )
            return None
        if name == "retryTask":
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None or task["state"] != "failed":
                _fail("not_failed", "只有失败任务可重试同一 task_id", 409)
            # failed 是「上游明确拒绝、未执行」，当时已自动退还预占。
            # 重试视为新一次执行：重新预占 cap，但沿用同一 upstream task_id、不重提生成。
            # 注意：重试同样要过三道闸门。实测「重试绕过了预算与单日上限」，
            # 因为原来这里只看了余额。
            #
            # 售价与 cap 按**任务类型**取：视频任务不能用 t2i 的 cap（36 覆盖不住 300 的
            # 售价，结算退差会变成负数并把守恒断言打成 409）。同时把 cost 一并对齐到
            # 当前生效价：管理员在失败与重试之间改过价时，reserve 与 cost 必须来自同一次
            # 定价计算，否则「预占 < 成本」同样是负退差。
            unit_cost, cap = self._task_unit_pricing(s, task)
            label = self._kind_label(task)
            task_proj = next((x for x in s["projects"] if x["id"] == task["projectId"]), None)
            self._afford(
                c["owner"],
                task_proj,
                cap,
                what=f"重试{label}",
                simulated=self._simulated_provider(c),
            )
            self._check_credit_limits(s, unit_cost, unit_cost)
            self._require_balance(ledger, cap, f"重试{label}")
            ledger["balance"] -= cap
            ledger["frozen"] += cap
            self._tx(
                s,
                "reserve",
                f"重试重新预占 · {task['id'][-6:]}",
                -cap,
                "明确拒绝时已退还预占，重试重新占用 cap",
                projectId=task["projectId"],
                taskId=task["id"],
            )
            task["reserve"] = cap
            task["cost"] = unit_cost
            task["state"], task["percent"] = "queued", 0
            task["sourceProvider"] = prov.name
            task["note"] = "重试同一 upstream task_id，不重新提交生成；已重新预占 cap"
            return None
        if name == "pickFrame":
            # 选帧**必须真的改变镜头用的那张图**：以前只写 task.picked，镜头卡上的
            # 画面一点没变，用户点了半天「用这张」等于没点。这是一个功能缺口。
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None:
                _fail("task_missing", "任务不存在", 404)
            if not task.get("settled"):
                _fail("task_not_settled", "任务还没结算，暂时不能选帧；请等生成完成", 409)
            frames = [str(x) for x in (task.get("frames") or [])]
            index = _as_int(arg(1) if arg(1) is not None else 0, "index", 0)
            if not frames or index >= len(frames):
                _fail(
                    "invalid_field",
                    f"候选序号超出范围：本任务只有 {len(frames)} 张可用候选",
                    400,
                )
            task["picked"] = index
            chosen = frames[index]
            owner_proj = next((x for x in s["projects"] if x["id"] == task["projectId"]), None)
            target = next(
                (x for x in (owner_proj or {}).get("shots", []) if x["id"] == task["shotId"]),
                None,
            )
            if target is not None:
                if self._task_kind(task) == "video":
                    target["videoUrl"] = chosen
                    self._remember_media(target, chosen, "video")
                else:
                    target["imageUrl"] = chosen
                    self._remember_media(target, chosen, "image")
            return None
        if name == "restoreShotMedia":
            # 产物回退：只能回到**这一镜历史里出现过**的地址。
            # 不校验就等于开了一个「把镜头换成任意 URL」的写口子（外链、别人的素材）。
            proj = need_project()
            shot = next((x for x in proj["shots"] if x["id"] == arg(0)), _MISSING)
            if shot is None:
                _fail("shot_missing", "镜头不存在", 404)
            url = str(arg(1) or "").strip()
            history = shot.get("mediaHistory")
            entry = next(
                (
                    h
                    for h in (history if isinstance(history, list) else [])
                    if isinstance(h, dict) and str(h.get("url") or "") == url
                ),
                None,
            )
            if not url or entry is None:
                _fail(
                    "invalid_field",
                    "只能恢复该镜头历史里出现过的产物地址（历史里没有这条记录）",
                    400,
                )
            kind = "video" if str((entry or {}).get("kind") or "") == "video" else "image"
            if kind == "video":
                shot["videoUrl"] = url
            else:
                shot["imageUrl"] = url
            # 恢复本身也是一次「换成这个产物」：必须记进历史，否则恢复不可撤销。
            self._remember_media(shot, url, kind)
            return {"shotId": shot["id"], "url": url, "kind": kind}
        if name == "rerollTask":
            task = next((t for t in s["tasks"] if t["id"] == arg(0)), _MISSING)
            if task is None or not task["settled"]:
                _fail("not_settled", "只有已结算任务可重抽", 409)
            if self._task_kind(task) == "video":
                # 重抽只会建一条关键帧 t2i 任务并把成本按 t2i 结算，套在视频任务上
                # 等于「拿 30 积分的价格重出一条视频」——明说（409），不静默改产物类型。
                _fail(
                    "not_supported",
                    "视频任务不支持重抽：请对该镜头重新提交视频生成",
                    409,
                )
            # 重抽也是「再花一次钱」，同样要过单日上限与项目预算。
            cap = self._kf_pricing(s)[1]
            task_proj = next((x for x in s["projects"] if x["id"] == task["projectId"]), None)
            self._afford(
                c["owner"],
                task_proj,
                cap,
                what="重抽关键帧",
                simulated=self._simulated_provider(c),
            )
            self._check_credit_limits(s, self._kf_pricing(s)[0], self._kf_pricing(s)[0])
            self._require_balance(ledger, cap, "重抽关键帧")
            ledger["balance"] -= cap
            ledger["frozen"] += cap
            self._tx(
                s,
                "reserve",
                "重抽候选预占",
                -cap,
                "新 chargeId，与旧任务无关",
                projectId=task["projectId"],
            )
            s["tasks"].append(
                {
                    "id": _uid("T"),
                    "projectId": task["projectId"],
                    "shotId": task["shotId"],
                    "type": "关键帧 t2i（重抽）",
                    "kind": "keyframe",
                    "state": "queued",
                    "percent": 0,
                    "reserve": cap,
                    "cost": self._kf_pricing(s)[0],
                    "candidates": 1,
                    "settled": False,
                    "picked": None,
                    "frames": [],
                    "idem": _uid("IK"),
                    "chargeId": _uid("C"),
                    "upstreamTaskId": None,
                    "sourceProvider": prov.name,
                    "note": "重抽：新任务新预占，等待 Provider 产出关键帧",
                    "createdAt": time.strftime("%m-%d %H:%M", time.localtime()),
                }
            )
            return None
        if name == "purgeMedia":
            self._require_admin(c["owner"], "purgeMedia")
            # 默认 dryRun：删除是不可逆的，先看「会删多少」再决定。
            # 天数默认取部署方的保留策略（未配置时沿用 7 天）。
            default_days = self.retention_days or 7
            days = _as_float(arg(0) if arg(0) is not None else default_days, "days", 0)
            dry_run = True if arg(1) is None else bool(arg(1))
            if days < 0:
                _fail("invalid_field", "days 不能为负", 400)
            report = self.media_store.purge_older_than(
                c["owner"], days, dry_run=dry_run, keep_ids=self._live_asset_ids(s)
            )
            report["forgotten_uploads"] = 0
            if not dry_run and report["asset_ids"]:
                # 文件删了，日志行不删就会留下指向空文件的记录。
                report["forgotten_rows"] = self.store.forget_media(c["owner"], report["asset_ids"])
                # 上传元数据与业务引用也要同步清掉，否则 BGM / 锚点图会指向已删文件。
                touched = set(report["asset_ids"])
                doomed = [
                    x
                    for x in s.get("uploads", [])
                    if self.media_store.asset_id_of_url(str(x.get("url") or "")) in touched
                ]
                if doomed:
                    urls = {str(x["url"]) for x in doomed}
                    for proj in s["projects"]:
                        bgm = (proj.get("audio") or {}).get("bgm") or {}
                        if bgm.get("url") in urls:
                            bgm["url"], bgm["uploadId"] = "", ""
                        for bucket in (proj.get("assets") or {}).values():
                            for asset in bucket:
                                if asset.get("imageUrl") in urls:
                                    asset["imageUrl"] = ""
                                    asset["sub"] = "草稿"
                    s["uploads"] = [
                        x for x in s.get("uploads", []) if x["id"] not in {d["id"] for d in doomed}
                    ]
                    report["forgotten_uploads"] = len(doomed)
            else:
                report["forgotten_rows"] = 0
            report["storage"] = self.media_store.stats(c["owner"])
            return report
        if name == "dropSampleMedia":
            # 切到真实模式后调用：把离线示例产物从镜头、形象图、配音与成片上摘掉，
            # 避免真实成片里混进「示例」画面和提示音。文件本身不删，用户上传不动。
            store = self.media_store
            owner = c["owner"]
            dropped = 0

            def sample(url: Any) -> bool:
                return bool(url) and store.is_sample_url(owner, str(url))

            for proj in s["projects"]:
                for shot in proj.get("shots") or []:
                    for key in ("imageUrl", "videoUrl"):
                        if sample(shot.get(key)):
                            shot[key] = ""
                            dropped += 1
                for bucket in (proj.get("assets") or {}).values():
                    for item in bucket:
                        if sample(item.get("imageUrl")):
                            item["imageUrl"] = ""
                            dropped += 1
                audio = proj.get("audio") or {}
                for row in list(audio.get("voices") or []) + list(audio.get("subtitles") or []):
                    if sample(row.get("audio_url")):
                        row["audio_url"] = ""
                        dropped += 1
                film = proj.get("film") or {}
                if sample(film.get("videoUrl")):
                    proj["film"] = {}
                    dropped += 1
            return {"dropped": dropped}
        if name == "saveCreditLimits":
            opts = arg(0) if isinstance(arg(0), dict) else {}
            lim = dict(s.get("creditLimits") or {})
            single = _limit_value(opts.get("single"), "单次上限")
            total = _limit_value(opts.get("total"), "总量上限")
            # 从「不限」开启总量上限，或用户要求重新计数时，已用额度从 0 开始；
            # 只改数值不清零，否则调高再调低就能绕过上限。
            if total is None:
                lim["used"], lim["since"] = 0, ""
            elif lim.get("total") is None or opts.get("resetUsed"):
                lim["used"] = 0
                lim["since"] = time.strftime("%Y-%m-%d %H:%M", time.localtime())
            lim["single"], lim["total"] = single, total
            lim.setdefault("used", 0)
            s["creditLimits"] = lim
            return self.credit_limit_view(s)
        if name == "adminCredit":
            self._require_admin(c["owner"], "adminCredit")
            amount = _as_int(arg(0), "amount", 1)
            reason = str(arg(1) or "").strip()
            request_id = str(arg(2) or "").strip()
            if not reason or not request_id:
                _fail("invalid_credit", "加余额金额必须为正整数，且原因 / requestId 不能为空")
            if any(
                t["kind"] == "credit" and _request_of(t.get("note") or "") == request_id
                for t in ledger["tx"]
            ):
                _fail("duplicate_request", "requestId 已存在，重复请求被幂等拒绝", 409)
            ledger["balance"] += amount
            self._tx(s, "credit", "管理员加余额", amount, f"{reason} / requestId={request_id}")
            return None
        if name == "resolveCharge":
            # 待复核凭据的**兜底出口**。自动结算与这里共用 _settle_pending_charge，
            # 且结算后 state 不再是 pending_review，因此不可能重复入账。
            self._require_admin(c["owner"], "resolveCharge")
            charge = next((x for x in s.get("charges", []) if x["id"] == arg(0)), _MISSING)
            if charge is None:
                _fail("charge_missing", "凭据不存在", 404)
            if charge.get("state") != "pending_review":
                _fail("not_pending", "只有待复核（pending_review）的凭据可以复核", 409)
            outcome = str(arg(1) or "").strip()
            self._settle_pending_charge(s, charge, outcome)
            charge["note"] = f"{charge.get('note', '')} / 复核结论：{outcome}"
            charge["resolvedAt"] = time.strftime("%m-%d %H:%M", time.localtime())
            return dict(charge)
        if name == "savePricing":
            # 改定价等于改钱：非管理员把 t2i 设成 0，就等于免费无限生成。
            # 实测「非管理员调用 savePricing 成功」，这里补上闸门。
            self._require_admin(c["owner"], "savePricing")
            f = _as_dict(arg(0), "savePricing 的参数")
            current = dict(s.get("pricing") or {})
            # cap 未显式给出时跟随售价（cap_of(t2i)），不再默认成 1/12 这类魔法值。
            t2i = _as_int(f.get("t2i", current.get("t2i", PRICE_T2I)), "t2i", 0)
            cap = _as_int(f.get("cap", cap_of(t2i)), "cap", 1)
            # 下界从 `cap >= t2i` 收紧到 `cap >= cap_of(t2i)`：
            # 关键帧的预占读 cap、结算读 t2i，若 cap 只有 1.0 倍售价，退差 `cap - t2i`
            # 会变成 0 甚至负数——负数时 `if diff > 0` 跳过退款、frozen 只减 cap，
            # 而 cost=t2i 全额进守恒校验，结果不是「少扣钱」而是结算当场 409 卡死。
            if cap < cap_of(t2i):
                _fail(
                    "invalid_pricing",
                    f"cap 必须 ≥ 单次硬上限 {cap_of(t2i)}（售价 {t2i} × 1.2 取整），"
                    "否则预占不足以覆盖结算，退差为负会破坏资金守恒",
                    400,
                )
            qps = _as_float(f.get("qps") or current.get("qps") or 1, "qps", 0.1)
            concurrency = _as_int(
                f.get("concurrency") or current.get("concurrency") or 5, "concurrency", 1
            )
            # video 不再被硬写 None：它是管理员可维护的图生视频售价。
            # 未传 = 保持现值；显式传空/null = 清空（回落到目录价）。
            if "video" in f:
                raw_video = f.get("video")
                video: int | None = (
                    None
                    if raw_video is None or (isinstance(raw_video, str) and not raw_video.strip())
                    else _as_int(raw_video, "video", 1)
                )
            else:
                video = normalize_video(current.get("video"))
            s["pricing"] = {
                "t2i": t2i,
                "cap": cap,
                "qps": qps,
                "concurrency": concurrency,
                "video": video,
                "stale": pricing_stale({"t2i": t2i, "video": video}),
            }
            return None

        # --- 音频 / 时间线 / 质检 ---
        if name == "addVoice":
            proj = need_project()
            f = _as_dict(arg(0), "addVoice 的参数")
            if not str(f.get("character") or "").strip():
                _fail("invalid_name", "角色不能为空")
            proj["audio"]["voices"].append(
                {
                    "id": _uid("vo"),
                    "character": str(f["character"]).strip(),
                    "voice": f.get("voice") or "未绑定",
                    "lines": _as_int(f.get("lines") or 0, "lines", 0),
                    "duration": f.get("duration") or "—",
                }
            )
            return None
        if name == "deleteVoice":
            proj = need_project()
            proj["audio"]["voices"] = [x for x in proj["audio"]["voices"] if x["id"] != arg(0)]
            return None
        if name == "addSubtitle":
            proj = need_project()
            f = _as_dict(arg(0), "addSubtitle 的参数")
            if not str(f.get("text") or "").strip():
                _fail("invalid_text", "字幕文本不能为空")
            proj["audio"]["subtitles"].append(
                {
                    "id": _uid("st"),
                    "in": f.get("in") or "00:00",
                    "out": f.get("out") or "00:03",
                    "text": str(f["text"]).strip(),
                    "checked": False,
                }
            )
            return None
        if name == "toggleSubtitle":
            proj = need_project()
            for sub in proj["audio"]["subtitles"]:
                if sub["id"] == arg(0):
                    sub["checked"] = not sub["checked"]
            return None
        if name == "deleteSubtitle":
            proj = need_project()
            proj["audio"]["subtitles"] = [
                x for x in proj["audio"]["subtitles"] if x["id"] != arg(0)
            ]
            return None
        if name == "saveBgm":
            proj = need_project()
            f = _as_dict(arg(0), "saveBgm 的参数")
            previous = dict(proj["audio"].get("bgm") or {})
            upload_id = str(f.get("uploadId") or "").strip()
            url = str(f.get("url") or "").strip()
            name = f.get("name") or previous.get("name") or "未设置"
            if upload_id:
                found = next(
                    (
                        x
                        for x in s.get("uploads", [])
                        if x["id"] == upload_id and x["kind"] == "bgm"
                    ),
                    None,
                )
                if found is None:
                    _fail("upload_missing", "所选 BGM 上传不存在或不属于当前账号", 404)
                url = str(found["url"])
                name = f.get("name") or found["name"] or name
            # 只认本账号上传入口登记的本地音频：请求体里塞外链一律拒绝。
            if url and not any(
                x["kind"] == "bgm" and x["url"] == url for x in s.get("uploads", [])
            ):
                _fail("bgm_url", "BGM 必须是本账号通过上传入口登记的本地音频文件", 400)
            proj["audio"]["bgm"] = {
                "name": name,
                "voiceLevel": max(
                    0, min(100, _as_int(f.get("voiceLevel") or 100, "voiceLevel", 0))
                ),
                "bgmLevel": max(0, min(100, _as_int(f.get("bgmLevel") or 25, "bgmLevel", 0))),
                "ambient": f.get("ambient") or "无",
                "url": url or str(previous.get("url") or ""),
                "uploadId": upload_id or str(previous.get("uploadId") or ""),
            }
            return None
        if name == "moveClip":
            proj = need_project()
            order = proj["timeline"]["order"]
            i = order.index(arg(0)) if arg(0) in order else -1
            j = i + _as_int(arg(1, 0), "offset", 0) if i >= 0 else -1
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                positions = {sid: n for n, sid in enumerate(order)}
                proj["shots"].sort(key=lambda x: positions.get(x["id"], 0))
            return None
        if name == "addNote":
            proj = need_project()
            text = str(arg(0) or "").strip()
            if not text:
                _fail("invalid_text", "批注不能为空")
            proj["timeline"]["notes"].append(
                {
                    "id": _uid("n"),
                    "time": time.strftime("%m-%d %H:%M", time.localtime()),
                    "text": text,
                }
            )
            return None
        if name == "toggleQA":
            proj = need_project()
            i = _as_int(arg(0) or 0, "qa index", 0)
            if 0 <= i < len(proj["qa"]):
                proj["qa"][i] = not proj["qa"][i]
            return None
        if name == "exportFilm":
            proj = need_project()
            if not proj["shots"]:
                # 空时间线导出会产出 shots=0 / duration=0 的「成片」记录，
                # 看起来像成功、实际没有任何内容。
                _fail("empty_film", "时间线没有镜头，无法导出成片：请先拆分分镜", 409)
            if not all(proj["qa"]):
                _fail("qa_incomplete", "请先勾选全部 4 项质检")
            # 导出记录必须挂在真实产物上，否则「导出成功」只是一条留痕。
            # 两种模式都走同一条本机合成链，离线也产出可播放的示例成片，
            # 因此这里不再保留「离线演示留痕」这条降级路径。
            last = proj.get("lastResult") or {}
            # 优先读项目级 film（不会被后续动作覆盖），lastResult 仅作旧数据兼容。
            film_url = (proj.get("film") or {}).get("videoUrl") or (
                (last.get("payload") or {}).get("video_url")
                if last.get("action") == "preview-film"
                else None
            )
            film_path = self.media_store.path_for_url(c["owner"], str(film_url or ""))
            if film_path is None or not film_path.is_file():
                _fail(
                    "film_missing",
                    "尚未合成成片，或成片文件已被清理：请在时间线重新执行「预览成片（合成）」。"
                    "导出记录必须指向真实视频文件，不再产出纯留痕。",
                    409,
                )
            # 合成会跳过没有视频轨的镜头。导出记录写的是全部镜头数与总时长，
            # 成片里却只有其中一部分画面，对不上，必须先补齐（或删掉这些镜头）。
            missing = [x for x in proj["shots"] if not x.get("videoUrl")]
            if missing:
                _fail(
                    "film_incomplete",
                    f"还有 {len(missing)} / {len(proj['shots'])} 个镜头没有视频轨，成片里不包含它们："
                    "请先为这些镜头执行「图生视频」并重新合成，或删除不需要的镜头后再导出。",
                    409,
                )
            film = proj.get("film") or {}
            if film.get("signature") and film["signature"] != self._film_signature(proj):
                _fail(
                    "film_stale",
                    "成片已过期：合成之后分镜、时间线、视频轨、字幕配音或 BGM 有改动，"
                    "现有成片不再对应当前内容。请在时间线重新执行「预览成片（合成）」后再导出。",
                    409,
                )
            offline = prov.name == "mock"
            record = {
                "id": _uid("ex"),
                "time": time.strftime("%m-%d %H:%M", time.localtime()),
                "name": f"{proj['name']} · 第 {proj['episode']} 集",
                "res": proj["res"],
                "shots": len(proj["shots"]),
                "duration": sum(int(x["dur"]) for x in proj["shots"]),
                "filmUrl": str(film_url),
                "artifact": "示例成片（本机 FFmpeg 合成，非模型生成）"
                if offline
                else "成片（真实视频文件）",
                # 示例标识随记录一起留痕；离线示例只证明流程与合成链路可用，
                # 不证明模型效果。正式上线还需法务与备案口径确认。
                "sampleLabel": SAMPLE_LABEL if offline else "",
                "aiLabel": self.ai_label,
                "aiDisclosure": self.ai_disclosure,
            }
            proj["exports"].append(record)
            return record
        if name == "returnForReshoot":
            proj = need_project()
            reason = str(arg(1) or "").strip()
            if not reason:
                _fail("reason_required", "退回必须填写原因")
            shot = next((x for x in proj["shots"] if x["id"] == arg(0)), _MISSING)
            if shot is None:
                _fail("shot_missing", "镜头不存在", 404)
            if shot["state"] == "locked":
                shot["state"] = "dirty"
            shot["dirty"] = True
            proj["qa"] = [False] * 4
            proj["timeline"]["notes"].append(
                {
                    "id": _uid("n"),
                    "time": time.strftime("%m-%d %H:%M", time.localtime()),
                    "text": f"质检退回局部重拍 · 镜 {proj['shots'].index(shot) + 1} · {reason}",
                }
            )
            return None

        # --- mock 生成能力（原「待后端」按钮） ---
        if name == "tts-preview":
            proj = need_project()
            voice = next((v for v in proj["audio"]["voices"] if v["id"] == arg(0)), _MISSING)
            if voice is None:
                _fail("voice_missing", "音色不存在", 404)
            subtitles = proj["audio"]["subtitles"]
            # 第三项：配音以字幕时间轴为准。显式传 subtitleId 时绑定那一句；
            # 否则依次认领「未校对且还没有配音」的句子，逐句试听就能逐句对齐。
            wanted = str(arg(1) or "").strip()
            sub = (
                next((x for x in subtitles if x["id"] == wanted), None)
                if wanted
                else next(
                    (x for x in subtitles if not x["checked"] and not x.get("audio_url")), None
                )
            )
            if wanted and sub is None:
                _fail("subtitle_missing", "指定的字幕句不存在", 404)
            text = str(sub["text"]) if sub else "（暂无待配音字幕）"
            item = prov.tts_preview(voice["character"], voice["voice"], text, owner=c["owner"])
            audio_url = item["payload"].get("audio_url")
            # 音频产物落到音色行上，前端才能直接 <audio> 播放；
            # 同时把 URL 挂到字幕句，合成时按该句的 in 做 adelay 对齐。
            if audio_url:
                voice["audio_url"] = str(audio_url)
                if sub is not None:
                    sub["audio_url"] = str(audio_url)
                    sub["voiceId"] = voice["id"]
                    sub["audioCharacter"] = voice["character"]
            item["payload"]["subtitle_id"] = str(sub["id"]) if sub else ""
            item["payload"]["subtitle_in"] = str(sub["in"]) if sub else ""
            item["payload"]["alignment"] = "subtitle" if sub else "none"
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item
        if name == "change-voice":
            proj = need_project()
            voice = next((v for v in proj["audio"]["voices"] if v["id"] == arg(0)), _MISSING)
            if voice is None:
                _fail("voice_missing", "音色不存在", 404)
            item = prov.change_voice(voice["character"], voice["voice"], owner=c["owner"])
            voice["voice"] = item["payload"]["to"]
            # 候选清单挂到音色行上，用户能看到「还换了哪些」而不是只看到当前值。
            if item["payload"].get("candidates"):
                voice["candidates"] = [str(x) for x in item["payload"]["candidates"]]
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item
        if name == "mark-audio-done":
            proj = need_project()
            item = prov.mark_audio_done(
                proj["audio"]["voices"], proj["audio"]["subtitles"], owner=c["owner"]
            )
            # Provider 只能按 owner 目录数文件，会把别的项目的音频算进来。
            # 项目归属是服务端才知道的事实，所以这里直接按 project_id 打表统计并覆盖。
            # 口径：本项目已产出的 TTS 试听条数（当前唯一会产出音频文件的动作）。
            item["payload"]["audio_files"] = self.store.media_count(
                c["owner"], str(proj["id"]), "tts-preview"
            )
            item["payload"]["audio_files_scope"] = "project"
            proj["audio"]["done"] = True
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item
        if name == "preview-film":
            proj = need_project()
            item = prov.preview_film(proj, owner=c["owner"])
            # 成片地址必须挂到**项目**上，不能只留在 lastResult 里。
            # 实测：合成成功、再点一次剧本医生或试听，导出就报「尚未合成成片」——
            # 因为 lastResult 是单槽的，被下一次动作覆盖了。
            video_url = (item.get("payload") or {}).get("video_url")
            if video_url:
                proj["film"] = {
                    "videoUrl": str(video_url),
                    "at": time.strftime("%m-%d %H:%M", time.localtime()),
                    # 合成输入的指纹：导出前据此判断成片是否仍对应当前分镜与音轨。
                    "signature": self._film_signature(proj),
                    "stale": False,
                }
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item
        if name == "image-to-video":
            proj = need_project()
            shot = next((x for x in proj["shots"] if x["id"] == arg(0)), _MISSING)
            if shot is None:
                _fail("shot_missing", "镜头不存在", 404)
            # A1/A2：项目设定 + @角色 引用进请求参数；首帧按
            # 显式 assetId -> @角色/场景/道具 对应的已生成锚点图 -> 无 取。
            # 传副本，避免把只供 Provider 消费的字段写回业务 state。
            req, asset_id = self._shot_request(shot, proj, arg(1))
            # 传 bytes 而不是 URL：上游节点只接受图片本体，本地 URL 出不了网。
            first_frame = self._first_frame(c["owner"], proj, asset_id)
            item = prov.image_to_video(
                req, proj["ratio"], owner=c["owner"], first_frame=first_frame
            )
            # 真实 Provider 会产出本地视频 URL；写回镜头才能被「预览成片」的合成
            # 步骤引用。mock 的 video_url 为 None，因此离线路径行为不变。
            if item["payload"].get("video_url"):
                shot["videoUrl"] = str(item["payload"]["video_url"])
                # 写回即记历史：否则再生成一次就永远回不到这一版（文件还在，地址没了）。
                self._remember_media(shot, str(item["payload"]["video_url"]), "video")
            # 离线示例视频带一张同内容的封面图，镜头卡可以直接预览；
            # 真实 Provider 的 cover_url 可能是外部地址，不写进本地素材字段。
            if item["payload"].get("sample") and item["payload"].get("cover_url"):
                if not shot.get("imageUrl"):
                    shot["imageUrl"] = str(item["payload"]["cover_url"])
                    self._remember_media(shot, str(item["payload"]["cover_url"]), "image")
            self._remember(proj, name, item)
            self._media(c, item, proj["id"])
            return item

        _fail("unknown_action", f"未知动作：{name}", 404)
        return None
