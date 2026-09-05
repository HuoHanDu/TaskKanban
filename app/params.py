"""参数合并模块：处理任务 L1/L2/L3 三层参数与粘性覆盖规则。

本模块是纯函数模块，不依赖数据库 / IO，便于单元测试。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence


def apply_group(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
) -> Dict[str, Any]:
    """任务开始时的一次性合并：L1 base + L2 group override。

    L2 覆盖 L1；L2 的空字符串按“字面值”处理，直接覆盖，
    不使用 L3 的空字符串特殊语义。
    """
    merged = dict(base_params)
    merged.update(group_override)
    return merged


def apply_step(
    current: Mapping[str, Any],
    step_override: Mapping[str, Any],
) -> Dict[str, Any]:
    """在 current 基础上应用一个 Step 的 L3 override，返回新字典。

    粘性规则：
    - 返回的新字典会作为下一个 Step 的 current，因此 override 会持续生效。
    - L3 中 value == "" 表示“本 Step 不覆盖此 key”，跳过；
      当前值继续保持（不回退到 base）。
    - L3 中非空 value 会覆盖旧值或引入新 key。
    - 当前值本身是 "" 时，L3 再给 "" 仍然保持 ""。
    """
    merged = dict(current)
    for key, value in step_override.items():
        if value == "":
            # 空字符串 = 不覆盖，沿用 current 中的当前值
            continue
        merged[key] = value
    return merged


def build_step_param_snapshots(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """按 step_index 升序生成每个 Step 执行时应看到的参数快照。

    steps 中每个元素需包含:
      - step_index: int，从 1 开始
      - override: dict，该步骤的 L3 override

    返回列表与排序后的 steps 一一对应：
      snapshots[0] -> step_index 最小的 Step
      snapshots[i] -> 排序后第 i 个 Step
    """
    ordered_steps = sorted(steps, key=lambda s: s["step_index"])
    current = apply_group(base_params, group_override)
    snapshots: List[Dict[str, Any]] = []

    for step in ordered_steps:
        current = apply_step(current, step["override"])
        # 保存独立快照副本，防止调用方误修改污染后续步骤
        snapshots.append(dict(current))

    return snapshots


def get_params_for_step(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    step_index: int,
) -> Dict[str, Any]:
    """获取指定 step_index 执行时应看到的参数。

    这是 Worker 执行单步时的便捷入口：
    它会按顺序合并到目标步骤，然后返回该步骤的参数快照。

    注意：若 Worker 需要连续执行多个步骤，优先使用
    build_step_param_snapshots() 一次算出全部快照，避免重复合并。
    """
    snapshots = build_step_param_snapshots(base_params, group_override, steps)
    # steps 的 step_index 从 1 开始
    return snapshots[step_index - 1]
