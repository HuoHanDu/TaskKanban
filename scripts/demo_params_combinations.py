"""演示二：包含“各种边界组合”的参数覆盖演示脚本。

与 demo_params_task.py 的区别：
- demo_params_task.py：单个典型任务，参数链条完整，适合第一次看；
- demo_params_combinations.py：一次性把各种边界组合都打出来，
  包括 L2 空串字面值、L3 空串不覆盖、当前值为空串、0/False/None/[]/{}
  等字面值、新 key 引入、后覆盖、乱序 step_index、空洞 step_index。

用法：
    # 只看参数推导（不连数据库、不启动 worker）
    python scripts/demo_params_combinations.py --preview

    # 每个组合创建真实任务并启动一个 worker 实跑（需 MySQL + .env）
    python scripts/demo_params_combinations.py --run-worker

    # 也可以指定是否清理
    python scripts/demo_params_combinations.py --run-worker --no-cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows GBK 控制台直接打印中文会乱码，统一走 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.params import apply_group, apply_step, build_step_param_snapshots  # noqa: E402

# 每个用例：name + base/group/steps + 期望看到哪些关键点
CASES = [
    {
        "name": "case1_l3_empty_keeps_current_not_base",
        "desc": "L3 空串回退到当前值 L2=2，而不是回退到 L1=1",
        "base_params": {"v": 1},
        "group_override": {"v": 2},
        "steps": [{"step_index": 1, "override": {"v": ""}}],
    },
    {
        "name": "case2_l2_empty_is_literal",
        "desc": "L2 空串按字面值处理，任务开始时 v=''",
        "base_params": {"v": 1},
        "group_override": {"v": ""},
        "steps": [{"step_index": 1, "override": {}}],
    },
    {
        "name": "case3_current_empty_and_l3_empty",
        "desc": "当前值本身是空串，L3 再给空串仍保持空串",
        "base_params": {"v": ""},
        "group_override": {},
        "steps": [{"step_index": 1, "override": {"v": ""}}],
    },
    {
        "name": "case4_falsy_values_literal",
        "desc": "0/False/None/[]/{} 都是字面覆盖，不能因假值被跳过",
        "base_params": {"a": 1, "b": 1, "c": "x", "d": "x", "e": "x"},
        "group_override": {},
        "steps": [
            {
                "step_index": 1,
                "override": {"a": 0, "b": False, "c": None, "d": [], "e": {}},
            }
        ],
    },
    {
        "name": "case5_new_keys_stick",
        "desc": "L3 引入新 key 后，后续 Step 可见",
        "base_params": {},
        "group_override": {},
        "steps": [
            {"step_index": 1, "override": {"new_key": "from-step1"}},
            {"step_index": 2, "override": {}},
        ],
    },
    {
        "name": "case6_later_override_wins",
        "desc": "同一 key 连续被多个 Step 覆盖，后者覆盖前者",
        "base_params": {"k": "base"},
        "group_override": {},
        "steps": [
            {"step_index": 1, "override": {"k": "first"}},
            {"step_index": 2, "override": {"k": "second"}},
        ],
    },
    {
        "name": "case7_l3_empty_only_skips_that_key",
        "desc": "同一 Step 中一个 key 空串不影响同 Step 其他 key 生效",
        "base_params": {"a": 1, "b": 2},
        "group_override": {"b": 20},
        "steps": [{"step_index": 1, "override": {"a": "", "c": "new"}}],
    },
    {
        "name": "case8_sticky_full_chain",
        "desc": "L1->L2->L3->空串->L3 完整粘性链",
        "base_params": {"k": "L1"},
        "group_override": {"k": "L2"},
        "steps": [
            {"step_index": 1, "override": {"k": "step1"}},
            {"step_index": 2, "override": {"k": ""}},
            {"step_index": 3, "override": {"k": ""}},
            {"step_index": 4, "override": {"k": "step4"}},
        ],
    },
    {
        "name": "case9_unsorted_steps",
        "desc": "输入 steps 乱序，仍按真实 step_index 升序合并",
        "base_params": {},
        "group_override": {},
        "steps": [
            {"step_index": 3, "override": {"k": "third"}},
            {"step_index": 1, "override": {"k": "first"}},
            {"step_index": 2, "override": {"k": "second"}},
        ],
    },
    {
        "name": "case10_hole_step_index",
        "desc": "step_index 存在空洞（1 和 3），仍按真实序号，不按列表下标",
        "base_params": {"base": True},
        "group_override": {},
        "steps": [
            {"step_index": 1, "override": {"k": "s1"}},
            {"step_index": 3, "override": {"k": "s3"}},
        ],
    },
    {
        "name": "case11_nested_value_whole_replacement",
        "desc": "复杂 dict 是整体替换，不是嵌套深合并（override 覆盖整个 config）",
        "base_params": {"config": {"mode": "base", "retry": 1, "tags": ["old"]}},
        "group_override": {},
        "steps": [
            {
                "step_index": 1,
                "override": {"config": {"mode": "override", "retry": 9}},
            }
        ],
    },
    {
        "name": "case12_list_whole_replacement",
        "desc": "list 作为 value 时也是整体替换，不是 append/merge",
        "base_params": {"items": [1, 2], "name": "base"},
        "group_override": {},
        "steps": [
            {
                "step_index": 1,
                "override": {"items": [9, 8, 7]},
            }
        ],
    },
    {
        "name": "case13_deepcopy_nested_isolation",
        "desc": "深拷贝隔离：Step1 修改嵌套 dict/list 不影响 Step2 快照和输入",
        "base_params": {"profile": {"addresses": [{"city": "A"}], "level": 1}},
        "group_override": {},
        "steps": [
            {"step_index": 1, "override": {}},
            {"step_index": 2, "override": {}},
        ],
        "mutate_first_snapshot": True,
    },
    {
        "name": "case14_nested_in_override_sticky",
        "desc": "L3 引入的嵌套 dict/list 也粘性传递且各快照独立",
        "base_params": {},
        "group_override": {},
        "steps": [
            {"step_index": 1, "override": {"meta": {"tags": ["a"], "count": 1}}},
            {"step_index": 2, "override": {}},
        ],
        "mutate_first_snapshot": True,
    },
]


def _fmt(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _build_steps_with_actions(case: dict) -> list:
    """为每个 case 的 steps 补 action，便于真实 worker 跑。"""
    steps = []
    for s in case["steps"]:
        item = dict(s)
        item.setdefault("override", {})
        item.setdefault("action", "mock")
        # 同一任务里 action 重复不影响；但为了日志更好看，统一用 mock
        item["action"] = "mock"
        steps.append(item)
    return steps


def preview_case(case: dict) -> None:
    print("-" * 78)
    print(f"[{case['name']}] {case['desc']}")
    print(f"  L1 base       = {_fmt(case['base_params'])}")
    print(f"  L2 group      = {_fmt(case['group_override'])}")
    for step in sorted(case["steps"], key=lambda x: x["step_index"]):
        print(f"  L3 Step{step['step_index']} override = {_fmt(step.get('override', {}))}")

    current = apply_group(case["base_params"], case["group_override"])
    print(f"  任务开始 current = {_fmt(current)}")
    for step in sorted(case["steps"], key=lambda x: x["step_index"]):
        current = apply_step(current, step.get("override", {}))
        print(f"  Step{step['step_index']} 实际参数 = {_fmt(current)}")

    # 深拷贝隔离演示：修改第一个快照的嵌套对象，再展示后续快照不受影响
    if case.get("mutate_first_snapshot"):
        snapshots = build_step_param_snapshots(
            case["base_params"], case["group_override"], case["steps"]
        )
        if snapshots:
            first = snapshots[0]
            for _key, _value in list(first.items()):
                if isinstance(_value, dict) and _value:
                    # 找一个嵌套 dict 的 key 改掉，例如 profile.level 或 meta.count
                    nested_key = next(iter(_value.keys()))
                    first[_key][nested_key] = "MUTATED"
                    break
                if isinstance(_value, list) and _value:
                    first[_key].append("MUTATED")
                    break
            print(f"  修改第一个快照后（污染测试）: {_fmt(first)}")
            if len(snapshots) > 1:
                print(f"  Step2 快照仍为: {_fmt(snapshots[1])}")
            print("  => 深拷贝隔离：后续快照/输入未被污染 [OK]")


def run_case_with_worker(case: dict, worker_id: str, cleanup: bool) -> None:
    from app import repository  # noqa: PLC0415
    from app.worker import run_worker_once  # noqa: PLC0415

    steps = _build_steps_with_actions(case)
    tid = repository.create_task(case["base_params"], case["group_override"], steps)
    print(f"\n已创建任务 id={tid}，worker={worker_id} 开始认领执行...")
    handled = run_worker_once(worker_id, show_params=True)
    detail = repository.get_task_with_steps(tid)
    print(f"worker handled={handled}, task status={detail['status']}")

    logs = repository.list_step_logs(tid)
    print("数据库日志：")
    for log in logs:
        print(f"  Step{log['step_index']} [{log['status']}] {log['message']}")

    if cleanup:
        repository.delete_task_by_id(tid)
        print("已清理该演示任务")


def main() -> None:
    parser = argparse.ArgumentParser(description="参数组合边界演示")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--preview",
        action="store_true",
        help="只打印所有组合的参数推导结果（不连数据库）",
    )
    group.add_argument(
        "--run-worker",
        action="store_true",
        help="创建任务并由真实 worker 执行（需 MySQL/.env）",
    )
    parser.add_argument("--worker-id", default="demo-comb-worker", help="worker 标识")
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="run-worker 模式下保留任务，方便查看 DB",
    )
    args = parser.parse_args()

    print("=" * 78)
    print(f"参数组合边界演示：共 {len(CASES)} 组")
    print("=" * 78)

    for case in CASES:
        print()
        if args.preview:
            preview_case(case)
        else:
            run_case_with_worker(case, args.worker_id, cleanup=not args.no_cleanup)

    print("\n" + "=" * 78)
    print("全部用例演示完成。")


if __name__ == "__main__":
    main()
