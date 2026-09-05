"""参数合并模块边界与组合测试。

覆盖目标：
1. 题目要求的关键语义：
   - L3 空字符串回退到当前值（不是 base）
   - L3 引入新 key 后后续步骤可见
   - 连续多个 Step 覆盖同一 key，后者覆盖前者
   - L2 空字符串按字面值处理
   - 当前值本身为空字符串时，L3 空字符串仍保持空字符串
2. L1/L2/L3 不同取值（缺省 / 普通值 / 空字符串 / 假值）的组合。
3. 深拷贝隔离：快照之间、快照与入参之间互不污染。
4. 入参校验：非法 step_index、重复 step_index、缺失字段等。
5. 复杂 value（dict/list/None/False/0）按整体替换，不做深合并。
"""

from __future__ import annotations

import pytest

from app.params import (
    apply_group,
    apply_step,
    build_step_param_snapshots,
    get_params_for_step,
)


# ---------------------------------------------------------------
# 基础语义（来自原测试，保持回归）
# ---------------------------------------------------------------

def test_l3_empty_string_keeps_current_not_base():
    base = {"v": 1}
    group = {"v": 2}
    steps = [{"step_index": 1, "override": {"v": ""}}]

    snapshots = build_step_param_snapshots(base, group, steps)

    assert snapshots[0]["v"] == 2


def test_l3_empty_string_does_not_remove_missing_key():
    """L3 空串对 current 中不存在的 key 也不应引入该 key。"""
    snapshots = build_step_param_snapshots(
        base_params={"a": 1},
        group_override={},
        steps=[{"step_index": 1, "override": {"new_key": ""}}],
    )

    assert "new_key" not in snapshots[0]
    assert snapshots[0]["a"] == 1


def test_l3_introduces_new_key_and_sticks_to_following_steps():
    snapshots = build_step_param_snapshots(
        base_params={},
        group_override={},
        steps=[
            {"step_index": 1, "override": {"x": "from-step1"}},
            {"step_index": 2, "override": {}},
        ],
    )

    assert snapshots[0]["x"] == "from-step1"
    assert snapshots[1]["x"] == "from-step1"


def test_consecutive_overrides_last_one_wins():
    snapshots = build_step_param_snapshots(
        base_params={"k": "base"},
        group_override={},
        steps=[
            {"step_index": 1, "override": {"k": "first"}},
            {"step_index": 2, "override": {"k": "second"}},
        ],
    )

    assert snapshots[0]["k"] == "first"
    assert snapshots[1]["k"] == "second"


def test_l2_empty_string_is_literal_value():
    """L2 空字符串按字面值处理，不触发 L3 的特殊语义。"""
    snapshots = build_step_param_snapshots(
        base_params={"v": 1},
        group_override={"v": ""},
        steps=[{"step_index": 1, "override": {}}],
    )

    assert snapshots[0]["v"] == ""


def test_l2_empty_string_overrides_base_even_when_l3_empty():
    """L2 空串字面生效后，Step L3 给空串仍保持空串。"""
    snapshots = build_step_param_snapshots(
        base_params={"v": 1},
        group_override={"v": ""},
        steps=[{"step_index": 1, "override": {"v": ""}}],
    )

    assert snapshots[0]["v"] == ""


def test_current_value_empty_string_stays_empty_on_l3_empty():
    snapshots = build_step_param_snapshots(
        base_params={"v": ""},
        group_override={},
        steps=[{"step_index": 1, "override": {"v": ""}}],
    )

    assert snapshots[0]["v"] == ""


def test_mixed_scenario():
    base = {}
    group = {}
    steps = [
        {"step_index": 1, "override": {"channel": "sms", "retry": 3}},
        {"step_index": 2, "override": {"retry": ""}},  # 空串：不覆盖
        {"step_index": 3, "override": {"retry": 5, "customer": "Alice"}},
    ]

    snapshots = build_step_param_snapshots(base, group, steps)

    # Step1 引入 channel/retry
    assert snapshots[0] == {"channel": "sms", "retry": 3}
    # Step2 空串不覆盖 retry，channel 粘性仍在
    assert snapshots[1] == {"channel": "sms", "retry": 3}
    # Step3 覆盖 retry、引入 customer
    assert snapshots[2] == {
        "channel": "sms",
        "retry": 5,
        "customer": "Alice",
    }


# ---------------------------------------------------------------
# L1 / L2 / L3 不同取值组合矩阵
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("base", "group", "override", "expected"),
    [
        # --- 只有一个 L3 空串，观察应保留当前层 ---
        pytest.param(
            {"v": "L1"}, {},
            {"v": ""}, {"v": "L1"},
            id="L1-value + L3-empty -> L1",
        ),
        pytest.param(
            {"v": "L1"}, {"v": "L2"},
            {"v": ""}, {"v": "L2"},
            id="L1-value+L2-value + L3-empty -> L2",
        ),
        pytest.param(
            {"v": "L1"}, {"v": ""},
            {"v": ""}, {"v": ""},
            id="L2-empty-literal + L3-empty -> empty",
        ),
        pytest.param(
            {}, {"v": "L2"},
            {"v": ""}, {"v": "L2"},
            id="no-L1+L2-value + L3-empty -> L2",
        ),
        pytest.param(
            {}, {}, {"v": ""}, {},
            id="no-L1/no-L2 + L3-empty -> no key",
        ),
        # --- L3 非空覆盖应压过 L1/L2 ---
        pytest.param(
            {"v": "L1"}, {}, {"v": "L3"}, {"v": "L3"},
            id="L1-value + L3-value -> L3",
        ),
        pytest.param(
            {"v": "L1"}, {"v": "L2"}, {"v": "L3"}, {"v": "L3"},
            id="L1+L2+L3 all value -> L3",
        ),
        pytest.param(
            {}, {}, {"v": "L3"}, {"v": "L3"},
            id="no-L1/no-L2 + L3-value -> introduce L3",
        ),
        pytest.param(
            {}, {"v": ""}, {"v": "L3"}, {"v": "L3"},
            id="L2-empty-literal + L3-value -> L3",
        ),
        # --- 没有 L3，L1/L2 组合 ---
        pytest.param(
            {"v": "L1"}, {}, {}, {"v": "L1"},
            id="L1-value only -> L1",
        ),
        pytest.param(
            {}, {"v": "L2"}, {}, {"v": "L2"},
            id="L2-value only -> L2",
        ),
        pytest.param(
            {"v": "L1"}, {"v": "L2"}, {}, {"v": "L2"},
            id="L1+L2-value no-L3 -> L2",
        ),
        pytest.param(
            {"v": "L1"}, {"v": ""}, {}, {"v": ""},
            id="L1+L2-empty no-L3 -> L2-empty-literal",
        ),
        # --- 假值 / 结构值按字面覆盖 ---
        pytest.param(
            {"v": 1}, {}, {"v": 0}, {"v": 0},
            id="override 0 is literal",
        ),
        pytest.param(
            {"v": True}, {}, {"v": False}, {"v": False},
            id="override False is literal",
        ),
        pytest.param(
            {}, {}, {"v": None}, {"v": None},
            id="override None is literal (not empty-string skip)",
        ),
        pytest.param(
            {}, {}, {"v": []}, {"v": []},
            id="override [] is literal",
        ),
        pytest.param(
            {}, {}, {"v": {}}, {"v": {}},
            id="override {} is literal",
        ),
    ],
)
def test_l1_l2_l3_combination_matrix(base, group, override, expected):
    """Step1 应用一组 L1/L2/L3 后，快照应为 expected。"""
    snapshots = build_step_param_snapshots(
        base_params=base,
        group_override=group,
        steps=[{"step_index": 1, "override": override}],
    )
    assert snapshots[0] == expected


def test_l3_empty_only_skips_that_key_other_overrides_still_apply():
    """同一 Step 中一个 key 为空串不应阻止同 Step 其他 key 生效。"""
    snapshots = build_step_param_snapshots(
        base_params={"a": 1, "b": 2},
        group_override={"b": 20},
        steps=[
            {"step_index": 1, "override": {"a": "", "c": "new"}},
        ],
    )
    assert snapshots[0] == {"a": 1, "b": 20, "c": "new"}


def test_l2_introduces_new_key_and_l3_can_override_or_empty_skip():
    """L2 可引入 base 没有的 key；L3 空串不删除该 key。"""
    snapshots = build_step_param_snapshots(
        base_params={},
        group_override={"g": "group-value", "x": "from-group"},
        steps=[
            {"step_index": 1, "override": {"x": ""}},
            {"step_index": 2, "override": {"x": "from-step"}},
        ],
    )
    assert snapshots[0] == {"g": "group-value", "x": "from-group"}
    assert snapshots[1] == {"g": "group-value", "x": "from-step"}


def test_sticky_chain_across_all_three_layers():
    """一个 key 在 L1→L2→L3→空串→L3 链路上的完整演变。"""
    snapshots = build_step_param_snapshots(
        base_params={"k": "L1"},
        group_override={"k": "L2"},
        steps=[
            {"step_index": 1, "override": {"k": "step1"}},
            {"step_index": 2, "override": {"k": ""}},  # 保持 step1
            {"step_index": 3, "override": {"k": ""}},  # 仍保持 step1
            {"step_index": 4, "override": {"k": "step4"}},
        ],
    )
    assert [s["k"] for s in snapshots] == ["step1", "step1", "step1", "step4"]


# ---------------------------------------------------------------
# 复杂 value：整体替换，不做深合并
# ---------------------------------------------------------------

def test_complex_values_are_replaced_whole_not_deep_merged():
    snapshots = build_step_param_snapshots(
        base_params={"config": {"a": 1, "b": 2}},
        group_override={},
        steps=[{"step_index": 1, "override": {"config": {"b": 99}}}],
    )

    # 题目只定义 key-value 覆盖，不做嵌套深合并，因此 dict 整体替换
    assert snapshots[0]["config"] == {"b": 99}


def test_nested_override_does_not_merge_with_current_nested_value():
    snapshots = build_step_param_snapshots(
        base_params={"cfg": {"mode": "base", "retry": 1}},
        group_override={},
        steps=[{"step_index": 1, "override": {"cfg": {"retry": 2}}}],
    )
    # 整体替换：不会保留 mode
    assert snapshots[0]["cfg"] == {"retry": 2}


# ---------------------------------------------------------------
# 深拷贝隔离：快照之间 / 快照与入参之间互不污染
# ---------------------------------------------------------------

def test_snapshots_do_not_share_nested_mutable_values_with_inputs():
    base = {"config": {"a": 1, "b": 2}}
    steps = [
        {"step_index": 1, "override": {}},
        {"step_index": 2, "override": {}},
    ]

    snapshots = build_step_param_snapshots(base, {}, steps)
    snapshots[0]["config"]["a"] = 999
    snapshots[0]["new"] = "mutated"

    assert snapshots[1]["config"] == {"a": 1, "b": 2}
    assert base["config"] == {"a": 1, "b": 2}
    assert "new" not in snapshots[1]


def test_snapshots_do_not_share_nested_values_originating_from_override():
    override_value = {"nested": [1, 2]}
    steps = [
        {"step_index": 1, "override": {"cfg": override_value}},
        {"step_index": 2, "override": {}},
    ]

    snapshots = build_step_param_snapshots({}, {}, steps)
    snapshots[0]["cfg"]["nested"].append(3)

    assert snapshots[1]["cfg"] == {"nested": [1, 2]}
    assert override_value == {"nested": [1, 2]}


def test_apply_step_result_deep_copy_is_independent():
    current = {"cfg": {"a": [1, 2]}}
    override = {"cfg": {"b": 3}}

    merged = apply_step(current, override)
    merged["cfg"]["a"] = "changed"
    merged["cfg"]["c"] = 4

    assert current == {"cfg": {"a": [1, 2]}}
    assert override == {"cfg": {"b": 3}}
    assert merged["cfg"] == {"a": "changed", "b": 3, "c": 4}


def test_apply_group_result_deep_copy_is_independent():
    base = {"cfg": {"a": 1}}
    group = {"new": [1, 2]}

    merged = apply_group(base, group)
    merged["cfg"]["a"] = 99
    merged["new"].append(3)

    assert base == {"cfg": {"a": 1}}
    assert group == {"new": [1, 2]}


def test_repeated_apply_step_does_not_share_between_iterations():
    """手动连续 apply_step 时每次返回值也不与当前值共享嵌套对象。"""
    current = {"cfg": {"v": 1}}
    current = apply_step(current, {})
    current = apply_step(current, {})
    current["cfg"]["v"] = 100
    # 这一步只是修改最终返回的 current；原始对象已不可达，测试主要验证不抛异常。
    assert current["cfg"]["v"] == 100


# ---------------------------------------------------------------
# 排序与顺序契约
# ---------------------------------------------------------------

def test_step_index_ordering_is_guaranteed_even_if_input_unsorted():
    steps = [
        {"step_index": 3, "override": {"k": "third"}},
        {"step_index": 1, "override": {"k": "first"}},
        {"step_index": 2, "override": {"k": "second"}},
    ]

    snapshots = build_step_param_snapshots({}, {}, steps)

    assert snapshots[0]["k"] == "first"
    assert snapshots[1]["k"] == "second"
    assert snapshots[2]["k"] == "third"


def test_get_params_for_step_returns_single_step_snapshot():
    snapshots = build_step_param_snapshots(
        base_params={"v": 1},
        group_override={"v": 2},
        steps=[
            {"step_index": 1, "override": {"x": "a"}},
            {"step_index": 2, "override": {}},
        ],
    )
    params_step2 = get_params_for_step(
        base_params={"v": 1},
        group_override={"v": 2},
        steps=[
            {"step_index": 1, "override": {"x": "a"}},
            {"step_index": 2, "override": {}},
        ],
        step_index=2,
    )

    assert params_step2 == snapshots[1]


def test_get_params_for_step_searches_by_real_step_index_not_position():
    """steps 存在空洞时，不能把数组位置当成 step_index。"""
    steps = [
        {"step_index": 2, "override": {"k": "s2"}},
        {"step_index": 4, "override": {"k": "s4"}},
    ]

    assert get_params_for_step({}, {}, steps, 2) == {"k": "s2"}
    assert get_params_for_step({}, {}, steps, 4) == {"k": "s4"}

    with pytest.raises(ValueError, match="not found"):
        get_params_for_step({}, {}, steps, 3)
    with pytest.raises(ValueError, match="not found"):
        get_params_for_step({}, {}, steps, 1)


def test_get_params_for_step_works_with_unsorted_steps():
    steps = [
        {"step_index": 3, "override": {"k": "s3"}},
        {"step_index": 1, "override": {"k": "s1"}},
    ]
    # 目标 step_index=3 必须在 step1 之后应用
    assert get_params_for_step({}, {}, steps, 3) == {"k": "s3"}
    assert get_params_for_step({}, {}, steps, 1) == {"k": "s1"}


# ---------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------

@pytest.mark.parametrize("bad_index", [0, -1, -100])
def test_get_params_for_step_rejects_non_positive_index(bad_index):
    steps = [{"step_index": 1, "override": {}}]
    with pytest.raises(ValueError, match="positive"):
        get_params_for_step({}, {}, steps, bad_index)


@pytest.mark.parametrize("bad_index", [True, 1.5, "1", None])
def test_get_params_for_step_rejects_non_int_index(bad_index):
    steps = [{"step_index": 1, "override": {}}]
    with pytest.raises(TypeError):
        get_params_for_step({}, {}, steps, bad_index)


def test_get_params_for_step_rejects_missing_step_index():
    steps = [{"step_index": 1, "override": {}}]
    with pytest.raises(ValueError, match="not found"):
        get_params_for_step({}, {}, steps, 99)


@pytest.mark.parametrize("bad_index", [0, -1, True, 1.5, "1"])
def test_build_snapshots_rejects_bad_step_index_values(bad_index):
    steps = [{"step_index": bad_index, "override": {}}]
    with pytest.raises((TypeError, ValueError)):
        build_step_param_snapshots({}, {}, steps)


def test_build_snapshots_rejects_duplicate_step_index():
    steps = [
        {"step_index": 1, "override": {"k": "first"}},
        {"step_index": 1, "override": {"k": "second"}},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        build_step_param_snapshots({}, {}, steps)


def test_build_snapshots_rejects_duplicate_step_index_even_if_unsorted():
    steps = [
        {"step_index": 2, "override": {}},
        {"step_index": 1, "override": {}},
        {"step_index": 2, "override": {}},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        build_step_param_snapshots({}, {}, steps)


def test_build_snapshots_rejects_missing_step_index():
    with pytest.raises(ValueError, match="missing 'step_index'"):
        build_step_param_snapshots({}, {}, [{"override": {}}])


def test_build_snapshots_rejects_non_mapping_step():
    with pytest.raises(TypeError):
        build_step_param_snapshots({}, {}, ["not-a-dict"])


def test_build_snapshots_rejects_non_mapping_override():
    with pytest.raises(TypeError, match="override"):
        build_step_param_snapshots(
            {}, {}, [{"step_index": 1, "override": "not-a-dict"}]
        )


def test_build_snapshots_allows_missing_override_as_empty():
    snapshots = build_step_param_snapshots(
        {"a": 1},
        {},
        [{"step_index": 1}],
    )
    assert snapshots[0] == {"a": 1}


def test_build_snapshots_allows_override_none_as_empty():
    snapshots = build_step_param_snapshots(
        {"a": 1},
        {},
        [{"step_index": 1, "override": None}],
    )
    assert snapshots[0] == {"a": 1}


def test_build_snapshots_empty_steps_returns_empty_list():
    assert build_step_param_snapshots({}, {}, []) == []


def test_apply_group_rejects_non_mapping():
    with pytest.raises(TypeError, match="base_params"):
        apply_group("bad", {})
    with pytest.raises(TypeError, match="group_override"):
        apply_group({}, "bad")


def test_apply_step_rejects_non_mapping():
    with pytest.raises(TypeError, match="current"):
        apply_step("bad", {})
    with pytest.raises(TypeError, match="step_override"):
        apply_step({}, "bad")


# ---------------------------------------------------------------
# 不修改入参（原有回归，扩展深层）
# ---------------------------------------------------------------

def test_apply_group_does_not_mutate_input_dicts():
    base = {"a": 1, "b": 2}
    group = {"b": 3, "c": 4}

    merged = apply_group(base, group)

    assert merged == {"a": 1, "b": 3, "c": 4}
    assert base == {"a": 1, "b": 2}
    assert group == {"b": 3, "c": 4}


def test_apply_step_does_not_mutate_current_or_override():
    current = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}

    merged = apply_step(current, override)

    assert merged == {"a": 1, "b": 3, "c": 4}
    assert current == {"a": 1, "b": 2}
    assert override == {"b": 3, "c": 4}
