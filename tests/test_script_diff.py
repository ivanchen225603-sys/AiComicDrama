"""剧本版本对比的纯函数契约：行号、上下文、截断，以及「任何输入都不抛」。

这组用例守的不是实现细节，而是页面能不能安全消费这个结果：

1. 行号必须能对上原文（1 起始）。`difflib` 给的是 0 起始下标，差一位用户就会
   在错误的行上看到「你删了这句」，比不显示还糟。
2. `added` / `removed` 是全量计数，`hunks` 只是展示用的截断。
3. `\\r\\n`、行尾 `\\r` 不算变更，行尾空格算变更——这两条是对用户的承诺，
   不是内部约定，所以逐条断言。
4. 坏输入（None、数字、数组、缺字段）一律降级成空，绝不抛异常：抛了就是白屏。
"""

from __future__ import annotations

import copy

from comicdrama.script_diff import (
    LAYER_FIELDS,
    LINE_CHANGE_KINDS,
    diff_layers,
    diff_scripts,
)


def test_identical_layers_report_three_untouched_fields() -> None:
    """1. 完全相同：三个字段都在、计数为 0、changed 为 False、hunks 全空。"""
    layers = {"logline": "一句话梗概", "outline": "第一幕\n第二幕", "body": "场景一\n场景二"}
    result = diff_layers(layers, copy.deepcopy(layers))

    assert [field["field"] for field in result["fields"]] == ["logline", "outline", "body"]
    for field in result["fields"]:
        assert field["added"] == 0
        assert field["removed"] == 0
        assert field["hunks"] == []
        assert field["truncated"] is False
    assert result["summary"] == {"added": 0, "removed": 0, "changed": False}
    assert result["labels"] == {"base": "", "target": ""}


def test_replaced_line_keeps_real_line_numbers() -> None:
    """2. 只改一行：一个 remove + 一个 add，行号逐一对上原文第 3 行。"""
    base = {"body": "ACT ONE\nhero enters\nhero speaks\nhero exits"}
    target = {"body": "ACT ONE\nhero enters\nhero shouts\nhero exits"}

    field = diff_layers(base, target, context=0)["fields"][2]

    assert field["field"] == "body"
    assert field["added"] == 1
    assert field["removed"] == 1
    assert field["hunks"] == [
        {"kind": "remove", "line": "hero speaks", "baseLine": 3, "targetLine": None},
        {"kind": "add", "line": "hero shouts", "baseLine": None, "targetLine": 3},
    ]
    # 其余字段没变，也要出现且为空。
    assert [item["added"] for item in diff_layers(base, target)["fields"]] == [0, 0, 1]


def test_pure_insert_only_adds_without_base_line() -> None:
    """3. 纯新增：只有 add，baseLine 必须是 None。"""
    field = diff_layers({"body": "a\nb"}, {"body": "a\nx\nb"}, context=0)["fields"][2]

    assert field["added"] == 1
    assert field["removed"] == 0
    assert field["hunks"] == [
        {"kind": "add", "line": "x", "baseLine": None, "targetLine": 2},
    ]


def test_pure_delete_only_removes_without_target_line() -> None:
    """4. 纯删除：只有 remove，targetLine 必须是 None。"""
    field = diff_layers({"body": "a\nx\nb"}, {"body": "a\nb"}, context=0)["fields"][2]

    assert field["added"] == 0
    assert field["removed"] == 1
    assert field["hunks"] == [
        {"kind": "remove", "line": "x", "baseLine": 2, "targetLine": None},
    ]


def test_context_zero_and_two_produce_expected_windows() -> None:
    """5. context=0 只留变更行；context=2 带前后各 2 行 same（且窗口不重叠）。"""
    base_lines = [f"L{index}" for index in range(1, 11)]
    target_lines = list(base_lines)
    target_lines[4] = "L5-new"
    target_lines[5] = "L6-new"
    base = {"body": "\n".join(base_lines)}
    target = {"body": "\n".join(target_lines)}

    tight = diff_layers(base, target, context=0)["fields"][2]
    assert tight["hunks"] == [
        {"kind": "remove", "line": "L5", "baseLine": 5, "targetLine": None},
        {"kind": "remove", "line": "L6", "baseLine": 6, "targetLine": None},
        {"kind": "add", "line": "L5-new", "baseLine": None, "targetLine": 5},
        {"kind": "add", "line": "L6-new", "baseLine": None, "targetLine": 6},
    ]

    wide = diff_layers(base, target, context=2)["fields"][2]
    assert wide["hunks"] == [
        {"kind": "same", "line": "L3", "baseLine": 3, "targetLine": 3},
        {"kind": "same", "line": "L4", "baseLine": 4, "targetLine": 4},
        {"kind": "remove", "line": "L5", "baseLine": 5, "targetLine": None},
        {"kind": "remove", "line": "L6", "baseLine": 6, "targetLine": None},
        {"kind": "add", "line": "L5-new", "baseLine": None, "targetLine": 5},
        {"kind": "add", "line": "L6-new", "baseLine": None, "targetLine": 6},
        {"kind": "same", "line": "L7", "baseLine": 7, "targetLine": 7},
        {"kind": "same", "line": "L8", "baseLine": 8, "targetLine": 8},
    ]
    # 计数与 context 无关。
    assert (wide["added"], wide["removed"]) == (tight["added"], tight["removed"]) == (2, 2)


def test_invalid_context_falls_back_to_two() -> None:
    """6. context 传 -1 / "x" / None 都等同 2，且不抛异常。"""
    base = {"body": "一\n二\n三\n四\n五\n六\n七"}
    target = {"body": "一\n二\n三\n改\n五\n六\n七"}
    expected = diff_layers(base, target, context=2)

    for bogus in (-1, "x", None):
        assert diff_layers(base, target, context=bogus) == expected


def test_crlf_and_stray_cr_are_not_changes() -> None:
    """7. 行尾 `\\r`、`\\r\\n` 换行差异判为未变更（Windows 保存不算改动）。"""
    result = diff_layers({"body": "一\r\n二\r\n三"}, {"body": "一\n二\n三\n"})
    assert result["summary"] == {"added": 0, "removed": 0, "changed": False}
    assert result["fields"][2]["hunks"] == []

    stray = diff_layers({"body": "一\n二\r\n三\r"}, {"body": "一\n二\n三"})
    assert stray["summary"]["changed"] is False


def test_trailing_space_is_a_change() -> None:
    """8. 行尾空格算变更：用户可能真的调了缩进，吞掉等于替用户判断。"""
    field = diff_layers({"body": "abc\ndef"}, {"body": "abc \ndef"}, context=0)["fields"][2]

    assert (field["added"], field["removed"]) == (1, 1)
    assert field["hunks"] == [
        {"kind": "remove", "line": "abc", "baseLine": 1, "targetLine": None},
        {"kind": "add", "line": "abc ", "baseLine": None, "targetLine": 1},
    ]


def test_diff_scripts_uses_version_labels_and_treats_none_as_empty() -> None:
    """9. labels 取自版本 label；None 版本当空版本，added 等于对方全部行数。"""
    base_version = {"id": "v_1", "label": "v1 · 初稿", "layers": {"body": "一\n二"}}
    target_version = {"id": "v_2", "label": "v2 · 改稿", "layers": {"body": "一\n三"}}

    result = diff_scripts(base_version, target_version)
    assert result["labels"] == {"base": "v1 · 初稿", "target": "v2 · 改稿"}
    assert result["summary"] == {"added": 1, "removed": 1, "changed": True}

    empty_base = diff_scripts(None, target_version)
    assert empty_base["labels"] == {"base": "", "target": "v2 · 改稿"}
    # 对方两层文字共 2 行，全部算新增。
    assert empty_base["summary"] == {"added": 2, "removed": 0, "changed": True}


def test_garbage_inputs_never_raise() -> None:
    """10. 非 dict / 字段为 None、数字、列表 → 一律按空处理，不抛异常。"""
    for base, target in ((None, "字符串"), (["a"], 123), ({}, {}), (0, None)):
        result = diff_layers(base, target)
        assert [field["field"] for field in result["fields"]] == list(LAYER_FIELDS)
        assert result["summary"]["changed"] is False

    dirty = diff_layers({"logline": 123, "outline": ["一"], "body": None}, {"body": "新一行"})
    assert dirty["summary"] == {"added": 1, "removed": 0, "changed": True}
    assert dirty["fields"][0]["added"] == 0
    assert dirty["fields"][1]["added"] == 0

    # 版本对象的 layers 本身是脏值时同样不抛。
    assert diff_scripts({"layers": "不是字典"}, {"layers": None})["summary"]["changed"] is False
    assert diff_scripts("不是版本", ["也不是"])["summary"]["changed"] is False


def test_field_order_is_fixed_even_when_untouched() -> None:
    """11. 字段顺序恒为 logline → outline → body，未变更的字段也要出现。"""
    result = diff_layers({"body": "一"}, {"body": "二"})

    assert [field["field"] for field in result["fields"]] == ["logline", "outline", "body"]
    assert LAYER_FIELDS == ("logline", "outline", "body")
    assert LINE_CHANGE_KINDS == ("same", "add", "remove")
    assert result["fields"][0]["hunks"] == []
    assert result["fields"][1]["hunks"] == []
    assert result["fields"][2]["added"] == 1


def test_long_field_truncates_hunks_but_keeps_full_counts() -> None:
    """12. 单字段 3000 行：hunks 截到 200 条并标 truncated，计数仍是全量。"""
    base_lines = [f"scene {index:04d}" for index in range(3000)]
    target_lines = list(base_lines)
    for index in range(5, 3000, 10):
        target_lines[index] = f"scene {index:04d} 改"

    field = diff_layers({"body": "\n".join(base_lines)}, {"body": "\n".join(target_lines)})[
        "fields"
    ][2]

    assert field["truncated"] is True
    assert len(field["hunks"]) == 200
    # 全量计数：每 10 行改 1 行，共 300 处。
    assert (field["added"], field["removed"]) == (300, 300)
    # 未被截断的字段不应误标。
    short = diff_layers({"body": "a"}, {"body": "b"})["fields"][2]
    assert short["truncated"] is False


def test_pure_function_is_repeatable_and_does_not_mutate_inputs() -> None:
    """13. 同输入两次结果相等，且入参字典调用前后完全一致。"""
    base = {"logline": "A", "outline": "一\n二", "body": "x\ny"}
    target = {"logline": "A", "outline": "一\n三", "body": "x\ny\nz"}
    base_before = copy.deepcopy(base)
    target_before = copy.deepcopy(target)

    first = diff_layers(base, target, context=1)
    second = diff_layers(base, target, context=1)

    assert first == second
    assert base == base_before
    assert target == target_before
