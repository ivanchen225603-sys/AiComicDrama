"""定价单一源：后端扣费表、默认计价、日上限与迁移口径的唯一出处。

本模块是**纯数据 + 纯函数**，不 import 任何 `comicdrama.*` 模块。
原因：`store` 已经被 `service` 依赖（`service` 用 `DramaStore`），而定价又同时被
`store`（落库默认值）、`service`（扣费）和前端常量（离线态真实扣费）三方使用。
把定价放进 `service` 会让 `store -> service` 成环；放在独立模块里，三方都只依赖它，
依赖方向永远指向叶子。

三处必须同改的价格（少一处就会「在线扣 30、离线扣 20」，用户断网重连会看到余额跳变）：
1. 本模块的 `ACTION_PRICES` / `PRICE_T2I`（后端扣费真源）；
2. `store.DEFAULT_PRICING`（新开户的落库默认值，由 `default_pricing()` 派生）；
3. `comicdrama/web/js/store.js` 的 `A.DEFAULT_PRICING`（离线态真实扣费依据）。

事实边界：这里的数字是**本地口径**，不是上游真实账单。两个成本常量都已由真实调用
自报的 `pointsCost` 实测（图生视频 250、文生图 25），单独列在这里供 `pricing_stale`
只读标记使用——它们**不参与扣费**，也绝不能回填到「上游真值」列里：拿不到上游
自报值时那一列必须是 NULL（未知），预算常量是「估」不是「实」。
"""

from __future__ import annotations

from typing import Any

# ---------- 售价（用户被扣多少） ----------
# 关键帧 t2i 与锚点候选图（generateAssetCandidates）同价：两者都是「一张图」的上游成本，
# 之前一个是 10 一个是 10 只是巧合一致，现在把这条口径写成常量而不是两个字面量。
#
# 2026-11-11 用户裁决「成本 + 毛利」，与视频口径统一为 +20%（300/250 → 30/25）。
# 旧值 20 比实测成本 25 还低 —— 每张亏 5 积分，一集 7 镜 + 1 张锚点 = 8 张亏 40。
PRICE_T2I = 30
# 图生视频：上游按 250 积分/条计（已实测自报），售价 300 保留 20% 毛利空间；
# 与 `cap = 1.2 × 售价` 的硬上限口径无关（上限是防超支，不是毛利）。
PRICE_VIDEO = 300

# ---------- 上游成本（只用于 pricing_stale 标记，不参与扣费） ----------
# 2026-11-11 实测：seedreamV4、1440×2560（下限零余量档），status=5 时
# `pointsCost=25`、`accountBalance` 211→186。原值 13 来自同事文档的估计，
# **从未实测过，现已证伪**。
# 25 既然是「能出图的最小档」的实测价，就是产品可用文生图的**下限成本**，
# 不是「某个尺寸的价」。像素与单价是否有比例关系仍未验证（只测了这一档），
# 所以不要按尺寸推算单价，更不要拿它去回填上游真值列。
UPSTREAM_COST_T2I = 25
UPSTREAM_COST_VIDEO = 250

# 生成动作定价表（**本地口径**）：表里没有的动作 = 不调上游 = 不扣费。
# 这条语义被 `DramaService._dispatch` 依赖（`ACTION_PRICES.get(name) is None` 直通），
# 因此新增动作时必须显式登记（哪怕是 0）。
ACTION_PRICES: dict[str, int] = {
    "script-doctor": 2,
    "creative-start": 2,
    "extract-assets": 2,
    "resplit-shots-v2": 2,
    "change-voice": 2,
    "tts-preview": 3,
    "generateAssetCandidates": PRICE_T2I,
    "image-to-video": PRICE_VIDEO,
    "mark-audio-done": 0,
    "preview-film": 0,
}

# ---------- 单次硬上限 ----------
# 单次硬上限 = 售价 × 1.2 向上取整（整数运算，避免浮点误差）。
# 为什么是向上取整而不是 `price * 12 // 10`：后者会让 2→2、3→3，把现有小额动作
# （script-doctor=2、tts-preview=3）的硬上限从 3/4 降到 2/3，属于无关的行为变更，
# 且会让「预占 ≥ 实际」这条不变量在结算退差时失去余量。既有测试
# `test_single_action_cap_is_one_point_two_times_price` 明确断言 tts-preview 的 cap 为 4。
CAP_RATIO_NUM = 12
CAP_RATIO_DEN = 10

# 单日生成消耗上限（积分）。种子余额 980，500 约等于一半，防止一次会话烧穿。
# **只对真实 Provider 计数**（用户裁决）：日上限的存在理由是「防止烧上游真钱」，
# 所以 `provider.name == "mock"` 的本地模拟既不占额也不被额拦（见 store.daily_spend
# 与 service._afford 的 simulated 分支），离线演示（实测 7 镜 × 300 = 2100 名义消费）
# 在默认 500 下也能跑完。
# 真实模式下 `_afford()` 比的是**单次硬上限 cap**、不是售价：图生视频一次冻结 360，
# 500 约够 1.6 次，第 2 次会 409（daily_cap_exceeded）——这是该口径的预期行为。
DEFAULT_DAILY_CAP = 500

# 用户自设积分使用上限的合法范围上界。`service._limit_value`（保存上限时）
# 与 `store.import_doc`（导入存档时）共用这一处，避免两处各写一个常量导致
# 校验口径漂移：一边放宽、另一边仍会锁死账户或放进超限数值。
CREDIT_LIMIT_MAX = 100_000_000

# 迁移判据快照：功能阶段上线时的旧默认值。只用于「这一行是不是当年那批默认值」，
# 绝不用「当前值等于旧默认」judge 幂等（管理员事后把价改回 10 会被二次迁移）。
# 注意这里**不含 video**：旧库里 video 恒为 NULL，而 NULL 既可能是「没设置」，
# 也可能是「迁移过但管理员清空了」，靠值无法区分，故 video 的迁移也由
# `migrated_from` 一次性判据兜住。
OLD_PRICING_DEFAULTS: dict[str, int] = {"t2i": 10, "cap": 12}

# 管理员可单独维护售价的动作。目前只有图生视频：它是唯一「上游成本已实测且远高于
# 目录价」的动作，管理员需要按套餐/活动改价；其余动作的售价仍由 ACTION_PRICES 决定。
VIDEO_PRICE_KEY = "image-to-video"

# 迁移标记：新开户的行没有「迁移前原值」，用这个显式值占位，
# 避免把种子行错误地记成「从 30/36/null 迁移而来」。
MIGRATED_FROM_SEED = "seed"


def cap_of(price: int) -> int:
    """单次硬上限 = 售价 × 1.2 向上取整（纯整数运算）。"""
    return (price * CAP_RATIO_NUM + CAP_RATIO_DEN - 1) // CAP_RATIO_DEN


def default_pricing() -> dict[str, Any]:
    """新开户的落库默认计价。

    `video` 为 None 表示「未单独设置、走目录价 PRICE_VIDEO」——由 `effective_price`
    兜底成 300。它不是 0，也不是「上游免费」。
    """
    return {
        "t2i": PRICE_T2I,
        "cap": cap_of(PRICE_T2I),
        "qps": 1,
        "concurrency": 5,
        "video": None,
    }


def _positive_int(value: object) -> int | None:
    """严格的「正整数」取值：bool 与字符串都不算（True 是 int 子类但语义不是价格）。"""
    if type(value) is int and value > 0:
        return value
    return None


def normalize_video(value: object) -> int | None:
    """把 `drama_pricing.video` 归一成 `int | None`。

    为什么需要它：该列的 sqlite 声明类型是 `TEXT`，TEXT affinity 会把写进去的
    整数 300 **转成字符串 "300"** 存下来。如果读取侧要求 `type is int`，管理员把
    视频价设成 200 之后会被静默当成「未设置」而回落到 300——改价不生效，且没有任何
    报错。所以「存储形态」和「语义」必须分开：写入侧仍存整数（由 `_as_int` 校验），
    读取侧允许「纯数字字符串」，其余一律视为未设置。
    """
    as_int = _positive_int(value)
    if as_int is not None:
        return as_int
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def effective_price(name: str, pricing: dict[str, Any] | None) -> int | None:
    """动作的**生效价**：目录价 + 管理员对该动作的覆盖。

    返回 None 表示表里没有这个动作 → 调用方应「不扣费、直通」，这条既有语义不变。
    覆盖只对 `VIDEO_PRICE_KEY` 生效：`None`/空串（未设置）、`0`、`True`、非数字字符串
    一律回落目录价——宁可按目录价扣，也不让一个非法值把售价变成 0/1。
    """
    base = ACTION_PRICES.get(name)
    if base is None:
        return None
    if name != VIDEO_PRICE_KEY:
        return base
    override = normalize_video((pricing or {}).get("video"))
    return override if override is not None else base


def pricing_stale(pricing: dict[str, Any] | None) -> bool:
    """只读标记：当前生效价是否已跌破上游成本（卖了就亏）。

    用**生效价**判定而不是原始字段：`video=None` 表示走目录价 300，不是「0 积分」，
    否则每个新开户都会永久挂着 stale 标记。本函数只产出标记，**不阻断任何动作**。
    """
    t2i_price = _positive_int((pricing or {}).get("t2i"))
    if t2i_price is None:
        t2i_price = PRICE_T2I
    video_price = effective_price(VIDEO_PRICE_KEY, pricing)
    if video_price is None:
        video_price = PRICE_VIDEO
    return t2i_price < UPSTREAM_COST_T2I or video_price < UPSTREAM_COST_VIDEO


def migrate_pricing(t2i: int, cap: int, video: int | None) -> tuple[int, int, int | None, str]:
    """按字段独立迁移一行旧计价，返回 (t2i, cap, video, migrated_from)。

    为什么**逐字段**判断而不是整组判据（`WHERE t2i=10 AND cap=12 AND video IS NULL`）：
    管理员当年可能只改过其中一个字段（例如把 cap 调成 30 保留 t2i=10），整组判据会
    把这一行漏在外面，于是 t2i 停在 10、售价低于实测成本 25，卖一单亏一单。

    为什么 cap 跟着 `cap_of(PRICE_T2I)` 而不是 `cap_of(new_t2i)`：口径要求
    「旧默认 cap 一律升到新默认 cap=36」。对管理员自定义过 t2i 的行（如 15），
    36 仍然 ≥ `cap_of(15)=18`，不变量成立；末尾的 `max(...)` 是兜底，保证任何情况下
    都不会出现 `cap < cap_of(t2i)`（那种状态下退差为负，会当场触发守恒断言 409）。

    video **原样写回**（NULL 保持 NULL）。为什么不像 t2i 那样"升级"：
    `pricing.video` 的语义是「该 owner 的**覆盖价**」，NULL 表示「没有覆盖、
    跟随目录价」。把 NULL 写成 300 等于给每个存量 owner 凭空造一条「管理员设过
    300」的记录，后果有三：① 以后目录价调整（促销/降价/换更便宜的模型）这批
    owner 不会跟着变，而且没人知道为什么；② 审计记录会写「video null→300」，
    但真实语义是「没设」，回溯时误导排查；③ 新开户 `_seed()` 写 NULL、迁移写
    300，同一种情况两种表示，是典型的语义漂移。
    `effective_price()` 已经把 None 兜底成目录价，所以保持 NULL 不会让视频免费。
    """
    new_t2i = PRICE_T2I if t2i == OLD_PRICING_DEFAULTS["t2i"] else t2i
    new_cap = cap_of(PRICE_T2I) if cap == OLD_PRICING_DEFAULTS["cap"] else cap
    # 兜底：无论上面怎么算，cap 都必须覆盖自己这行的售价（含小数取整）。
    new_cap = max(new_cap, cap_of(new_t2i))
    marker = f"{t2i}/{cap}/{'null' if video is None else video}"
    return new_t2i, new_cap, video, marker
