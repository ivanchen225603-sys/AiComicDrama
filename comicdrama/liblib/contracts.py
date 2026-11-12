"""L2 媒体 Provider 契约层：只描述任务与素材，不承载 Agent 规划，也不绑定上游。

刻意不兼容 Toonflow 的 GenerateMedia 契约。那份 types.d.ts 逐字要求三种生成函数
「统一返回最终媒体数组，不能返回任务 ID 或原始平台响应」；而 LibTV 任务级 API
是先返回 task_id 再轮询 comfy/status，提交与结果之间存在不可消除的时间窗。
因此本契约显式返回任务句柄，并把「提交结果未知」建模为一等状态，禁止自动重提。

事实边界：这里的 Quote 是本地预占口径，不是 LibTV 官方单次计费表（该表页面
在检查时「暂无数据」）。真实用量只能在拿到上游返回后结算，功能阶段由 mock 给出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

POINTS_MICRO = 1_000_000
CURRENCY = "LIBTV_POINTS"
# 100 积分 = 1 元只在展示层换算；账本一律用 micro_points 整数。
DEFAULT_ESTIMATE_MICRO = 100 * POINTS_MICRO
DEFAULT_CAP_MICRO = 120 * POINTS_MICRO
MAX_PROMPT_CHARS = 500

# 素材留存上限按类型分档。视频/音频是「整段一个文件」，不能沿用图片的 16MB；
# 但上限只是「拒绝明显异常」的护栏，不是业务配额。超限一律转 held 人工核对，
# 绝不截断保存——截断的视频是坏数据，比没有更糟。
MAX_ASSET_BYTES = 16 * 1024 * 1024  # 图片；保留原名，兼容既有调用方
MAX_VIDEO_BYTES = 128 * 1024 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024

AssetKind = Literal["image", "video", "audio"]

ASSET_LIMITS: dict[str, int] = {
    "image": MAX_ASSET_BYTES,
    "video": MAX_VIDEO_BYTES,
    "audio": MAX_AUDIO_BYTES,
}

# 预设尺寸样本：只服务于前端下拉/元信息展示（GET /config 的 "sizes"），
# **不再是校验闸门**。历史上一份「固定成员白名单」被拿去当 validate() 的判据，
# 结果产品默认的 1080×1920 在本地就被拒（零出网），关键帧与图生视频整条真实链路
# 直接 502。现在尺寸由下方有界规则判定，本元组仅作展示，故保持不变以免破坏
# 既有外部契约（/config 结构不变）。
ALLOWED_SIZES = ((512, 512), (768, 768), (1024, 1024), (512, 768), (768, 512))

# 有界尺寸规则。为什么不再用穷举白名单：
#   1. 产品的合法尺寸来自项目设定（`drama.sample_media.dimensions()`：res 直读、
#      ratio 等比换算），取值空间是开放的，任何有限枚举都必然误杀真实需求；
#   2. 但也不能「什么都收」——离谱输入（负数、奇数、10 万像素、50:1 极端比例）
#      打出去只会浪费一次上游调用并占用积分；
#   3. 因此改成「区间 + 偶数 + 宽高比白名单」三重护栏：既放行产品自身的所有尺寸，
#      又仍然把明显异常挡在本地（拒绝时 outcome="rejected"，可自动退还预占）。
#
# 每条规则的依据：
#   - 正整数：宽高为 0/负数无法构成画面；
#   - 偶数：上游视频编码（H.264 yuv420p）要求色度下采样后的宏块对齐，奇数边
#     普遍被拒或在编码阶段被静默裁切，本机提前挡掉；
#   - 单边 64~8192：64 是画面有意义内容的下限（再小已无可辨认画面），8192 是
#     通用显卡单轴扩散上限量级，超过必然触发上游 OOM 而白花积分；
#   - 总像素 65,536~33,554,432：下限对应 256×256（低于此无信息量），上限约
#     33.5MP（8192×4096 量级），再高只会在上游排队超时；
#   - 宽高比白名单 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3（容差 1%）：
#     覆盖产品实际会用的方图、横屏、竖屏与 3:2 照片比例；容差 1% 是为了容纳
#     「1024 长边等比换算」这类带取整误差的尺寸（例如 9:16 的 576×1024），
#     同时仍拒绝 50:1 这种上游模板根本不会接受的极端比例。
MIN_SIZE_SIDE = 64
MAX_SIZE_SIDE = 8192
MIN_TOTAL_PIXELS = 65_536
MAX_TOTAL_PIXELS = 33_554_432
ALLOWED_ASPECT_RATIOS: tuple[tuple[int, int], ...] = (
    (1, 1),
    (16, 9),
    (9, 16),
    (4, 3),
    (3, 4),
    (3, 2),
    (2, 3),
)
ASPECT_RATIO_TOLERANCE = 0.01


def _aspect_ratio_supported(width: int, height: int) -> bool:
    """宽高比是否落在白名单内（相对容差 ASPECT_RATIO_TOLERANCE）。"""
    actual = width / height
    for ratio_width, ratio_height in ALLOWED_ASPECT_RATIOS:
        target = ratio_width / ratio_height
        if abs(actual - target) <= target * ASPECT_RATIO_TOLERANCE:
            return True
    return False


def size_rejection(width: object, height: object) -> str | None:
    """按有界规则判定尺寸；通过返回 None，否则返回人类可读的拒绝原因。

    返回原因字符串而不是直接抛异常，是为了让 ProviderError 的构造留在
    `providers/base.py`：异常契约（code / outcome）只有一个出口，这里只做纯判定，
    既可被 quote/submit 复用，也能被测试直接断言而无需捕获异常。
    """
    if type(width) is not int or type(height) is not int:
        # bool 是 int 的子类但 type 不是 int，会落在这里；字符串 "1080"、浮点 1080.0 同理。
        return (
            f"width and height must be integers, got {type(width).__name__}/{type(height).__name__}"
        )
    if width <= 0 or height <= 0:
        return f"width and height must be positive, got {width}x{height}"
    if width % 2 or height % 2:
        return f"width and height must be even for upstream video encoding, got {width}x{height}"
    if not (MIN_SIZE_SIDE <= width <= MAX_SIZE_SIDE and MIN_SIZE_SIDE <= height <= MAX_SIZE_SIDE):
        return f"each side must be within {MIN_SIZE_SIDE}-{MAX_SIZE_SIDE} px, got {width}x{height}"
    total_pixels = width * height
    if not MIN_TOTAL_PIXELS <= total_pixels <= MAX_TOTAL_PIXELS:
        return (
            f"total pixels must be within {MIN_TOTAL_PIXELS}-{MAX_TOTAL_PIXELS}, "
            f"got {total_pixels} for {width}x{height}"
        )
    if not _aspect_ratio_supported(width, height):
        allowed = ", ".join(f"{w}:{h}" for w, h in ALLOWED_ASPECT_RATIOS)
        return (
            f"aspect ratio {width}x{height} is not supported; "
            f"allowed {allowed} (tolerance {ASPECT_RATIO_TOLERANCE:.0%})"
        )
    return None


def size_policy() -> dict[str, object]:
    """尺寸规则的机器可读描述，供 HTTP 元信息接口声明「sizes 不是穷举清单」。"""
    return {
        "kind": "bounded",
        "min_side": MIN_SIZE_SIDE,
        "max_side": MAX_SIZE_SIDE,
        "min_total_pixels": MIN_TOTAL_PIXELS,
        "max_total_pixels": MAX_TOTAL_PIXELS,
        "must_be_even": True,
        "aspect_ratios": [[w, h] for w, h in ALLOWED_ASPECT_RATIOS],
        "aspect_ratio_tolerance": ASPECT_RATIO_TOLERANCE,
        "note": (
            "尺寸按有界规则校验，`sizes` 只是预设样本而非穷举清单；任何满足上述规则的宽高都被接受。"
        ),
    }


MediaState = Literal["queued", "running", "succeeded", "failed", "held"]
ProviderOutcome = Literal["rejected", "unknown", "transient"]

TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "held"})


def asset_limit(kind: str) -> int | None:
    """按类型取留存上限；未知类型返回 None，由调用方显式拒绝。

    刻意不做「猜一个兜底上限」：未知 kind 意味着上游返回了契约外的媒体形态，
    静默接受只会在后面判扩展名时炸在更深的位置，错误信息也更难定位。
    """
    return ASSET_LIMITS.get(kind)


class ProviderError(Exception):
    """上游异常。outcome 决定积分预占是自动退还还是转人工复核。

    rejected  上游明确拒绝（例如 4xx 参数错误）：确认未执行，可自动 release。
    unknown   提交后响应丢失/超时：上游可能已执行，禁止自动重提，必须 held。
    transient 轮询或下载阶段的临时故障：只重试同一 task_id 或同一 URL。
    """

    def __init__(self, code: str, message: str, *, outcome: ProviderOutcome):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class Quote:
    """报价只用于预占，不扣费；cap_micro 是结算时的硬上限。"""

    estimated_micro: int
    cap_micro: int
    currency: str = CURRENCY
    note: str = ""


@dataclass(frozen=True, slots=True)
class RemoteAsset:
    """上游结果 URL。官方说明地址有时效性（7 天），必须服务端立即转存。

    kind 决定转存时的扩展名判定与体积上限，也决定 HTTP 层回什么 Content-Type；
    它必须由 Provider 显式给出，不能让下游按 URL 后缀去猜。
    """

    url: str
    kind: AssetKind = "image"
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    upstream_task_id: str
    state: MediaState = "queued"
    percent: int = 0
    upstream_state: str | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    state: MediaState
    percent: int = 0
    upstream_state: str | None = None
    assets: tuple[RemoteAsset, ...] = ()
    cost_micro: int | None = None
    error: str | None = None
    # 上游自报的积分消耗与结算后余额。LibTV 官方视频/图像状态接口实测会带
    # pointsCost 与 accountBalance，落下来才能做计费对账，不必再靠控制台人工核对。
    upstream_points: int | None = None
    account_balance: int | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@runtime_checkable
class MediaProvider(Protocol):
    """L2 契约：submit 返回句柄，poll 只读同一句柄，不提供「重新提交」入口。"""

    name: str

    def quote(self, prompt: str, *, width: int, height: int) -> Quote: ...

    async def submit(
        self, *, task_id: str, request_id: str, prompt: str, width: int, height: int
    ) -> SubmitResult: ...

    async def poll(self, upstream_task_id: str) -> PollResult: ...

    async def fetch(self, asset: RemoteAsset) -> bytes: ...
