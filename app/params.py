"""参数合并模块：处理任务 L1/L2/L3 三层参数与粘性覆盖规则。

本模块是纯函数模块，不依赖数据库 / IO，便于单元测试。

语义约定：
- L1 base：任务创建时的默认参数。
- L2 group override：任务开始时一次性生效，空字符串按“字面值”处理，
  与 L3 的特殊语义无关。
- L3 step override：具有粘性。某个 Step 声明某 key 后，从该 Step 开始
  持续生效，直到被更晚的 Step 再次覆盖。
- L3 中 value == "" 表示“本 Step 不覆盖此 key”，沿用当前生效值，
  不删除 key、不回退到 L1/L2。
- 除 "" 以外的值（包括 0、False、None、[]、{} 等）都按普通字面值覆盖，
  不做深度合并。
- 所有输入/输出都做隔离：返回的 dict 以及内部嵌套 dict/list 不与入参共享，
  修改返回值不会污染 base/group/override，也不会污染其他 Step 快照。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Dict, List, cast


def _ensure_mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping, got {type(value).__name__}")
    return cast(Dict[str, Any], value)


def _validate_step_index(step_index: Any) -> int:
    if isinstance(step_index, bool) or not isinstance(step_index, int):
        raise TypeError(f"step_index must be an int, got {type(step_index).__name__}")
    if step_index <= 0:
        raise ValueError("step_index must be a positive integer (>= 1)")
    return step_index


def _normalize_steps(
    steps: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """校验步骤并返回按 step_index 升序排列的规范化步骤。

    规则：
    - step_index 必须存在、为 int 且 >= 1；
    - step_index 在同一个任务内必须唯一；
    - override 可选，缺省视为 {}；若提供则必须是 Mapping；
    - 不强制 step_index 连续，但 get_params_for_step 会按真实
      step_index 查找，而不是按列表下标。
    """
    normalized: List[Dict[str, Any]] = []
    seen: set[int] = set()

    for position, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            raise TypeError(
                f"steps[{position - 1}] must be a mapping, got {type(step).__name__}"
            )
        if "step_index" not in step:
            raise ValueError(f"steps[{position - 1}] is missing 'step_index'")
        step_index = _validate_step_index(step["step_index"])
        if step_index in seen:
            raise ValueError(f"duplicate step_index: {step_index}")
        seen.add(step_index)

        override = step.get("override", {})
        if override is None:
            override = {}
        if not isinstance(override, Mapping):
            raise TypeError(
                f"step_index {step_index} override must be a mapping, "
                f"got {type(override).__name__}"
            )
        normalized.append({"step_index": step_index, "override": override})

    normalized.sort(key=lambda s: s["step_index"])
    return normalized


def apply_group(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
) -> Dict[str, Any]:
    """任务开始时的一次性合并：L1 base + L2 group override。

    L2 覆盖 L1；L2 的空字符串按“字面值”处理，直接覆盖，
    不使用 L3 的空字符串特殊语义。

    返回值与入参深度隔离。
    """
    base = _ensure_mapping(base_params, "base_params")
    group = _ensure_mapping(group_override, "group_override")
    merged = deepcopy(base)
    merged.update(deepcopy(group))
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

    返回值与 current / step_override 深度隔离。
    """
    current_mapping = _ensure_mapping(current, "current")
    override_mapping = _ensure_mapping(step_override, "step_override")
    merged = deepcopy(current_mapping)
    for key, value in override_mapping.items():
        if value == "":
            # 空字符串 = 不覆盖，沿用 current 中的当前值
            continue
        merged[key] = deepcopy(value)
    return merged


def build_step_param_snapshots(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """按 step_index 升序生成每个 Step 执行时应看到的参数快照。

    steps 中每个元素需包含:
      - step_index: int，从 1 开始，同一任务内唯一
      - override: dict（可选，缺省 {}），该步骤的 L3 override

    返回列表与排序后的 steps 一一对应：
      snapshots[0] -> step_index 最小的 Step
      snapshots[i] -> 排序后第 i 个 Step

    每个快照都是独立深拷贝：修改任意快照不会影响其他快照或输入。
    """
    normalized_steps = _normalize_steps(steps)
    current = apply_group(base_params, group_override)
    snapshots: List[Dict[str, Any]] = []

    for step in normalized_steps:
        current = apply_step(current, step["override"])
        snapshots.append(current)

    return snapshots


def get_params_for_step(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    step_index: int,
) -> Dict[str, Any]:
    """获取指定 step_index 执行时应看到的参数。

    按真实 step_index 查找，不使用列表下标。若 step_index 非法
    （非 int / bool / < 1）或不存在于 steps 中，抛出 ValueError/TypeError。

    注意：若 Worker 需要连续执行多个步骤，优先使用
    build_step_param_snapshots() 一次算出全部快照，避免重复合并。
    """
    normalized_steps = _normalize_steps(steps)
    _validate_step_index(step_index)

    current = apply_group(base_params, group_override)
    for step in normalized_steps:
        current = apply_step(current, step["override"])
        if step["step_index"] == step_index:
            return current

    raise ValueError(f"step_index {step_index} not found in steps")
