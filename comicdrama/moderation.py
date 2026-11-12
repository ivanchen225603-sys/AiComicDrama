"""内容安全的可插拔审核端口 + 本地实现。

**事实边界（必须如实告知部署方与用户，不得包装成"已合规"）：**

- 本地实现 `LocalKeywordModeration` **只能审文本**：对文本做关键词子串匹配。
  `review_media` 只做「空 / 非空」检查，**审不了像素**——图片和视频里画的是什么，
  本地代码一无所知。
- 像素级审核必须接真实审核服务（如云厂商内容安全 API），本期**只留端口**
  `RemoteModeration`：两个方法都是显式 `NotImplementedError` 桩，不写任何猜测性
  的 HTTP 调用。本文件**连一个 HTTP 客户端库都不 import**（用例里有静态断言），
  第三方库名也不写进源码，免得日后有人照着补一个"看起来能用"的请求。
  假装能审像素，比不做更危险。
- 内置词表只放**少量违法类别标签**（儿童性剥削、毒品制作、血腥肢解等），
  不是违规词库：真正可用的词表必须由部署方按自己的法务口径配置
  （`COMIC_DRAMA_AUDIT_BLOCKLIST`）。代码里不预置任何政治敏感清单，也不放正常
  创作词汇——那类清单属于法务 / 合规职责，不该由代码作者凭猜测代填。
- 命中的词**绝不回写**到 `reason`：回写等于把词表告诉用户，教人绕过。
- 审核是纯函数、幂等：同一个文本审两次结果相同，失败也不会留下任何副作用。

上游审核态 `auditStatus` 的语义只认实测确认过的值（见 `audit_outcome`），而且
「不通过」不等于「已确认拒审」：默认收口是冻结待确认（见 `parse_reject_action`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

# 内置词表：**违法类别标签**，不是词表。
# 部署方按法务口径用 `COMIC_DRAMA_AUDIT_BLOCKLIST` 追加，内置这几条只覆盖
# "明显违法、任何法务口径下都必须拦"的类别，不做任何创作内容的好恶判断。
DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "儿童性剥削",
    "未成年人不当内容",
    "毒品制作",
    "制毒",
    "血腥肢解",
    "恐怖袭击教程",
)

# AI 生成标识可以出现的位置。`in_frame_drawtext`（烧进画面）故意不在默认值里：
# 一旦烧进像素就不可撤回、也不能二次编辑，必须等法务口径明确后由部署方显式打开。
#
# TODO(legal)：`in_frame_drawtext` 本期**只有配置项与位置清单，没有实现**。
# 原因有两条，缺一不可：① 法务与备案口径未定（画面上写什么、写在哪儿、字号/对比度
# 是否达标都还没有结论）；② 烧进画面不可撤回、不能二次编辑，做错了只能重渲。
# 在口径确定之前实现它，等于替业务方拍一个他们没拍的合规决定。
LABEL_POSITIONS: tuple[str, ...] = (
    "container_metadata",
    "video_overlay",
    "in_frame_drawtext",
)
DEFAULT_LABEL_POSITION = "container_metadata"

# 上游拒绝时写进 charge.note 的固定标记。
# 为什么用文本标记而不是新列：`drama_charge` 没有 audit_status 列，为这一个布尔
# 再加一列要走一次表结构迁移（老库、导入导出、对账都要跟着动）。标记随 note 落库，
# 存储投影据此派生 `auditRejected`，代价最小且不会丢失。
AUDIT_REJECT_MARK = "上游判定内容不合规"

_NOT_IMPLEMENTED = (
    "NOT_IMPLEMENTED: pixel-level moderation requires a real moderation service "
    "(image/video/violence/porn classifiers). Local keyword rules cannot inspect pixels; "
    "this port is intentionally a stub so nothing pretends to have reviewed the media."
)


@dataclass(frozen=True)
class ModerationVerdict:
    """一次审核的结论。`reason` 面向用户，绝不包含命中的词。"""

    allowed: bool
    reason: str = ""
    categories: tuple[str, ...] = ()
    provider: str = ""


class ModerationPort(Protocol):
    """审核端口。文本与媒体分开：两者的能力边界完全不同，不能混成一句"已审核"。"""

    name: str

    def review_text(self, text: str, *, owner: str, context: str) -> ModerationVerdict: ...

    def review_media(self, data: bytes, *, kind: str, owner: str) -> ModerationVerdict: ...


def _normalize(word: object) -> str:
    return str(word or "").strip().lower()


class LocalKeywordModeration:
    """本地关键词审核：**只能审文本**，媒体只查空值。

    `extra_words` 由部署方从 `settings.drama_audit_blocklist`（逗号分隔）传入；
    空白项、重复项在构造时就丢掉，避免运行期反复处理。
    """

    def __init__(self, extra_words: Iterable[str] = (), *, name: str | None = None) -> None:
        self.name = str(name or "local-keywords")
        words: list[str] = []
        for word in (*DEFAULT_BLOCKLIST, *extra_words):
            cleaned = _normalize(word)
            if cleaned and cleaned not in words:
                words.append(cleaned)
        # 元组：审核在并发线程里跑，词表必须构造后不可变。
        self._words: tuple[str, ...] = tuple(words)

    def review_text(self, text: str, *, owner: str, context: str) -> ModerationVerdict:
        del owner, context  # 本地规则不做按 owner / 动作区分的策略
        haystack = str(text or "").lower()
        # 空文本 / 纯空白不算"内容"，放行：否则保存空剧本也会被当成违规。
        if not haystack.strip():
            return ModerationVerdict(allowed=True, provider=self.name)
        for word in self._words:
            if word in haystack:
                # 只回一句固定文案：不回写命中的词，也不暗示是词表里的哪一条。
                return ModerationVerdict(
                    allowed=False,
                    reason="内容未通过安全检查",
                    categories=("keyword",),
                    provider=self.name,
                )
        return ModerationVerdict(allowed=True, provider=self.name)

    def review_media(self, data: bytes, *, kind: str, owner: str) -> ModerationVerdict:
        del kind, owner
        # 本地规则**只看得到字节长度，看不到画面**。这里绝不返回 allowed=True
        # 就宣称"已审核通过"：reason 明写只做了空值检查，服务端日志与排障能看到。
        if not data:
            return ModerationVerdict(allowed=False, reason="文件内容为空", provider=self.name)
        return ModerationVerdict(allowed=True, reason="本地规则不做像素级审核", provider=self.name)


class NullModeration:
    """显式关闭审核的部署用。默认**不启用**：默认必须是 `local`。"""

    name = "null"

    def review_text(self, text: str, *, owner: str, context: str) -> ModerationVerdict:
        del text, owner, context
        return ModerationVerdict(allowed=True, provider=self.name)

    def review_media(self, data: bytes, *, kind: str, owner: str) -> ModerationVerdict:
        del data, kind, owner
        return ModerationVerdict(allowed=True, provider=self.name)


class RemoteModeration:
    """真实审核服务的端口桩：**没有实现**，调用即抛。

    刻意不引入任何 HTTP 客户端：写一个"看起来能用"的请求等于假装像素级审核已经
    打通，一旦被当成已上线的安全能力使用，后果比不做更严重。
    """

    name = "remote"

    def review_text(self, text: str, *, owner: str, context: str) -> ModerationVerdict:
        del text, owner, context
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def review_media(self, data: bytes, *, kind: str, owner: str) -> ModerationVerdict:
        del data, kind, owner
        raise NotImplementedError(_NOT_IMPLEMENTED)


def audit_outcome(audit_status: str | None) -> str | None:
    """上游 `auditStatus` 的语义映射。只有实测确认过的值才映射。

    上游给的是**字符串**（`comicdrama/liblib/client.py` 里
    `str(item.get("auditStatus", ""))`），所以这里按字符串比较，不按 int 猜。

    **`"4"` 只被当作「非通过」标签，不等于「已确认拒审」**：`"4"` 没有任何实测
    样本，同一份协议里 `generateStatus` 的 4 是「审核中」，而我们唯一实测到的成功
    出图样本是 `auditStatus=3`。所以这里返回的 `"rejected"` 只用于驱动后续的
    「待确认/退款/结算」三选一，收口动作默认是 `hold`（见 `parse_reject_action`），
    **不是**这里直接退款。其余任何值——`None`、`""`、`"3"`、`"5"`…——一律返回
    `None`（未知）：未知就当"没发生"，不据此退款、不据此标记。
    """

    if not isinstance(audit_status, str):
        return None
    if audit_status.strip() == "4":
        return "rejected"
    return None


def parse_label_positions(raw: str) -> list[str]:
    """解析 `COMIC_DRAMA_AI_LABEL_POSITIONS`（逗号分隔）。

    非法值**丢弃**而不是抛异常：一个拼错的安全配置项不该把整个服务挡在启动之外。
    全部非法 / 为空时退回默认 `["container_metadata"]`——默认绝不含
    `in_frame_drawtext`（烧进画面不可撤回）。
    """

    positions: list[str] = []
    for part in str(raw or "").split(","):
        item = part.strip()
        if item in LABEL_POSITIONS and item not in positions:
            positions.append(item)
    return positions or [DEFAULT_LABEL_POSITION]


REJECT_ACTIONS: tuple[str, ...] = ("hold", "refund", "settle")
DEFAULT_REJECT_ACTION = "hold"


def parse_reject_action(raw: str) -> str:
    """解析 `COMIC_DRAMA_AUDIT_REJECT_ACTION`：上游报出 `auditStatus=4` 后怎么收口。

    - `hold`（默认）：**先冻结待确认**，不退款也不结算。
    - `refund`：立即全额退款，并清掉产物。
    - `settle`：按定价照常结算，只做标记。

    为什么默认是 `hold`：「4」的语义从未实测。成功出图那一单是 `auditStatus=3`，
    而同一份上游协议里 `generateStatus` 的 4 恰恰是「审核中」。两种解读的代价不对称：
    若 4 其实是「审核中」而我们按拒审退款，等于给一张可能随后通过的单子白送钱，
    上游成本却已经产生；反过来先冻结，最坏只是把收口推迟到人工确认，随时能补退款。

    非法值**退回 `hold`** 而不是抛异常：拼错一个收口策略不该让服务起不来，而且
    退回的那一侧必须是「不钱货两清」的一侧——按 hold 处理至少资金还冻着，可回滚。
    """

    action = str(raw or "").strip().lower()
    if action in REJECT_ACTIONS:
        return action
    return DEFAULT_REJECT_ACTION
