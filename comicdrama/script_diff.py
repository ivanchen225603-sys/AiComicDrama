"""剧本版本对比：**纯函数、无 IO、无副作用**，输出直接渲染到「版本对比」页。

为什么单独一个模块：`service.py` 里的 `rollbackScript` 只做「把旧版本的
layers 盖回去」，用户看得到版本标签，看不到「到底改了什么」。对比逻辑如果
塞进 service，就会跟读写库、鉴权、审计混在一起，既不好测，也容易在某次
重构里悄悄多做一次落盘。这里只接受两个版本对象（或两份 layers），返回一个
纯数据结构的 diff 结果，调用多少次都不会碰 state。

**这个模块的任何输入都不许抛异常。** 它唯一的消费者是页面：前端把 state 里的
版本对象直接喂进来，里面可能有 `None`、缺字段、甚至上一版留下的数组。一旦
这里抛异常，整个对比页白屏，而用户只是想看看「我改了什么」。因此所有入参都
在函数内部做类型兜底，坏输入按「空」处理，绝不让异常冒到 HTTP 层。

两条口径，写在这里以免后来者「顺手优化」掉：

1. **`\\r\\n` 与 `\\r` 不算变更，行尾空格算变更。** 前者是编辑器/系统换行差异，
   同一份稿子在 Windows 上保存一次就全量「变更」，纯噪声。后者是用户真的在
   行尾加了或删了空格——剧本正文里缩进、对白前的空格都可能是有意为之，
   吞掉它等于替用户决定「这个改动不重要」，比多报一次更糟。
2. **行号是从 1 开始的真实行号，且必须能对上原文。** `difflib` 的 opcode 用的是
   0 起始的下标区间 `[i1, i2)`，直接当行号用会整体偏移一位；这里统一 +1，
   并有用例逐行断言具体数字。
"""

from __future__ import annotations

import difflib
from typing import Any, Literal, TypedDict

# 单行变更的种类：页面据此决定渲染成「划掉」「新增」还是普通上下文行。
LineChangeKind = Literal["same", "add", "remove"]

# 对外暴露的合法取值，前端做 switch 时可以直接引用，避免两边各写一份字符串。
LINE_CHANGE_KINDS: tuple[LineChangeKind, ...] = ("same", "add", "remove")

# 字段顺序固定：页面按这个顺序稳定渲染，不随 dict 插入顺序变化。
LAYER_FIELDS: tuple[str, ...] = ("logline", "outline", "body")

# context 非法（负数、None、非整数）时统一回落到 2，见 `_norm_context`。
DEFAULT_CONTEXT = 2

# 无界增长的闸门：单字段超过 2000 行时，hunks 只留前 200 条。
# 3000 行的剧本逐行对比后可能有上千条 hunk，整包塞给浏览器会拖垮渲染。
MAX_FIELD_LINES = 2000
MAX_HUNKS = 200


class Hunk(TypedDict):
    """一条按行展示的记录：变更行本身，或变更附近的上下文行。"""

    kind: LineChangeKind
    line: str
    baseLine: int | None
    targetLine: int | None


class FieldDiff(TypedDict):
    """单个字段（logline / outline / body）的对比结果。"""

    field: str
    added: int
    removed: int
    hunks: list[Hunk]
    truncated: bool


class DiffSummary(TypedDict):
    """跨字段的合计。`changed` 只看 added + removed 是否大于 0。"""

    added: int
    removed: int
    changed: bool


class DiffLabels(TypedDict):
    """两侧版本的展示标签；`diff_layers` 不知道版本，填空串。"""

    base: str
    target: str


class DiffResult(TypedDict):
    """`diff_layers` / `diff_scripts` 的完整返回结构。"""

    fields: list[FieldDiff]
    summary: DiffSummary
    labels: DiffLabels


def _field_text(layers: object, field: str) -> object:
    """取出某字段的原始文本；layers 不是 dict 或字段缺失时返回空串。

    返回 `object` 而不是 `str`，是为了把「字段可能是 None / 数字 / 数组」这件
    事显式交给 `_as_lines` 兜底，中间不做隐式 str()——`str(123)` 会凭空造出
    一行 "123"，把「脏数据」变成「用户改了一行」。
    """
    if not isinstance(layers, dict):
        return ""
    return layers.get(field)


def _as_lines(text: object) -> list[str]:
    """把字段值规整成行列表。

    统一 `\\r\\n` → `\\n` 是为了让 Windows 保存过的稿子不产生全量假变更；
    剩下的孤立 `\\r` 会被 `splitlines()` 当行边界，同样不会留下空 diff。
    末尾换行产生的空片段由 `splitlines()` 丢弃，因此 "a\\n" 与 "a" 等价；
    但 "a\\n\\n" 会多出一条真空行，那是真实差异，照常计为变更。

    行尾空格**不**做 strip：那是用户的真实改动，见模块 docstring 的取舍说明。
    """
    if not isinstance(text, str):
        return []
    return text.replace("\r\n", "\n").splitlines()


def _norm_context(value: object) -> int:
    """把 context 归一成非负整数；非法值一律当 2，绝不抛异常。

    `bool` 单独排除：`True` 是 `int` 的子类，放行会变成「context=1」，
    与「非整数一律当默认值」的口径不一致。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_CONTEXT
    return value


def _diff_rows(base_lines: list[str], target_lines: list[str]) -> list[Hunk]:
    """逐行对比，产出「全量」行记录（含 same），行号从 1 开始对齐原文。

    `SequenceMatcher` 的 autojunk 保持默认开启：3000 行的剧本里空行和场景分隔
    极易重复，关闭后最坏是平方级比较，页面请求扛不住。开启后对重复行可能多给
    几对 add/remove（把相同的空行判成「删一行加一行」），但每条记录的行号仍然
    严格指向原文的真实行，不会错行——这是比「最小编辑脚本」更重要的性质。

    opcode 的 `i1/i2/j1/j2` 是 0 起始的下标区间 `[start, end)`，所以行号是
    `下标 + 1`，不是 `下标`。这里是最容易整体偏移一位的地方。
    """
    rows: list[Hunk] = []
    matcher = difflib.SequenceMatcher(None, base_lines, target_lines)
    for tag, base_start, base_end, target_start, target_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(base_end - base_start):
                rows.append(
                    {
                        "kind": "same",
                        "line": base_lines[base_start + offset],
                        "baseLine": base_start + offset + 1,
                        "targetLine": target_start + offset + 1,
                    }
                )
            continue
        # replace 拆成「先 remove 后 add」：页面顺序渲染就是「旧行划掉、新行顶上」，
        # 与用户读 diff 的习惯一致；若先 add 后 remove，同一处改动看起来像两处。
        for index in range(base_start, base_end):
            rows.append(
                {
                    "kind": "remove",
                    "line": base_lines[index],
                    "baseLine": index + 1,
                    "targetLine": None,
                }
            )
        for index in range(target_start, target_end):
            rows.append(
                {
                    "kind": "add",
                    "line": target_lines[index],
                    "baseLine": None,
                    "targetLine": index + 1,
                }
            )
    return rows


def _pick_hunks(rows: list[Hunk], context: int) -> list[Hunk]:
    """按 context 从全量行里截出要展示的片段。

    两个变更块之间的 `same` 行数不超过 `2 * context` 时合并成一个块：否则两个
    窗口会重叠或首尾相接，同一行上下文在页面上出现两次，用户会以为剧本重复了。
    """
    changed = [index for index, row in enumerate(rows) if row["kind"] != "same"]
    if not changed:
        return []

    groups: list[list[int]] = [[changed[0], changed[0]]]
    for index in changed[1:]:
        last = groups[-1]
        if index - last[1] - 1 <= 2 * context:
            last[1] = index
        else:
            groups.append([index, index])

    hunks: list[Hunk] = []
    for start, end in groups:
        low = max(0, start - context)
        high = min(len(rows), end + 1 + context)
        hunks.extend(rows[low:high])
    return hunks


def _diff_field(field: str, base_text: object, target_text: object, context: int) -> FieldDiff:
    """单字段对比。added/removed 是全量计数，不随 context 截断。"""
    base_lines = _as_lines(base_text)
    target_lines = _as_lines(target_text)
    rows = _diff_rows(base_lines, target_lines)
    added = sum(1 for row in rows if row["kind"] == "add")
    removed = sum(1 for row in rows if row["kind"] == "remove")
    hunks = _pick_hunks(rows, context)

    truncated = False
    # 只有「字段确实超长」时才截断；短字段即使变更块很多也保持完整，
    # 因为短字段的完整 diff 本来就有界，截断只会让用户看不到改动。
    if max(len(base_lines), len(target_lines)) > MAX_FIELD_LINES and len(hunks) > MAX_HUNKS:
        hunks = hunks[:MAX_HUNKS]
        truncated = True

    return {
        "field": field,
        "added": added,
        "removed": removed,
        "hunks": hunks,
        "truncated": truncated,
    }


def diff_layers(
    base_layers: dict[str, Any],
    target_layers: dict[str, Any],
    *,
    context: int = 2,
) -> DiffResult:
    """逐字段给出按行的变更。

    三个字段恒定出现（即使没有改动），页面因此可以稳定渲染三栏，不用先判断
    「这个字段这次有没有出现」。入参声明成 dict 是给调用方的契约，运行时仍然
    接受任何东西并按空 layers 处理——契约描述「应该传什么」，函数保证「传错也
    不炸」，两者不冲突。
    """
    ctx = _norm_context(context)
    fields = [
        _diff_field(name, _field_text(base_layers, name), _field_text(target_layers, name), ctx)
        for name in LAYER_FIELDS
    ]
    added = sum(item["added"] for item in fields)
    removed = sum(item["removed"] for item in fields)
    return {
        "fields": fields,
        "summary": {"added": added, "removed": removed, "changed": added + removed > 0},
        "labels": {"base": "", "target": ""},
    }


def _version_parts(version: object) -> tuple[dict[str, Any], str]:
    """从版本对象里取 (layers, label)；方向不对的一律按「空版本」处理。

    版本对象来自 state，可能是 `None`（比如「与当前草稿对比」时一侧不存在），
    也可能是历史数据里的脏值。这里不猜、不补，缺就别显示。
    """
    if not isinstance(version, dict):
        return {}, ""
    layers = version.get("layers")
    label = version.get("label")
    return (layers if isinstance(layers, dict) else {}), (label if isinstance(label, str) else "")


def diff_scripts(
    base_version: dict[str, Any] | None,
    target_version: dict[str, Any] | None,
    *,
    context: int = 2,
) -> DiffResult:
    """按版本对象对比（只取 `.layers`），并把两侧的 `label` 带进结果。

    任一版本为 None 时按「空版本」处理：所有行都算新增或删除，而不是抛异常。
    「和空白对比」本身就是有效问题（用户想看这版到底写了什么），不该报错。
    """
    base_layers, base_label = _version_parts(base_version)
    target_layers, target_label = _version_parts(target_version)
    result = diff_layers(base_layers, target_layers, context=context)
    result["labels"] = {"base": base_label, "target": target_label}
    return result
