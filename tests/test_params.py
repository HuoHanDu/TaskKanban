"""参数合并模块边界测试。

覆盖题目要求的关键边界：
1. L3 空字符串回退到当前值（不是 base）
2. L3 引入新 key 后后续步骤可见
3. 连续多个 Step 覆盖同一 key，后者覆盖前者
4. L2 空字符串按字面值处理
5. 当前值本身为空字符串时，L3 空字符串仍保持空字符串
6. 混合场景
7. 复杂 value 按整体替换，不做深合并
"""

from app.params import (
    apply_group,
    apply_step,
    build_step_param_snapshots,
    get_params_for_step,
)


def _snapshot_for(steps, index):
    """按 1-based step_index 取对应快照（测试辅助）。"""
    return steps[index - 1]


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


def test_complex_values_are_replaced_whole_not_deep_merged():
    snapshots = build_step_param_snapshots(
        base_params={"config": {"a": 1, "b": 2}},
        group_override={},
        steps=[{"step_index": 1, "override": {"config": {"b": 99}}}],
    )

    # 题目只定义 key-value 覆盖，不做嵌套深合并，因此 dict 整体替换
    assert snapshots[0]["config"] == {"b": 99}


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
