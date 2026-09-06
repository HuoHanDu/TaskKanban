# TaskKanban 任务调度看板

TaskKanban 是基于 MySQL 行锁 + 真实多进程 Worker 的轻量级任务调度系统，核心是**并发安全认领、三层参数粘性合并、Step 日志幂等写入、整任务原子提交**。

单人实际耗时：**约 23 小时（自 2026-09-05 14:00 起，至 2026-09-06 13:00 收尾）**。

## 技术栈与选型理由

- **Python 3.10+**：标准库 multiprocessing 直接启用真实多进程 Worker。
- **FastAPI**：API 层薄，同步业务放在 Repository 层，便于事务与测试。
- **MySQL 8.0**：`FOR UPDATE SKIP LOCKED` + InnoDB 行锁，在 10 Worker / 5 任务/s 规模无需 Redis/MQ。
- **原生 SQL + mysql-connector-python**：事务/锁/唯一键显式可控，不被 ORM 隐藏。
- **原生 JS 看板**：只做轮询与并发演示，无构建链。
- **pytest**：纯函数单测 + 真实 MySQL 集成/并发测试。

## 架构

```text
┌────────────┐   HTTP    ┌──────────────────┐
│ 原生看板    │ ────────> │ FastAPI app/api   │
│ index.html │  /tasks   │                  │
└────────────┘           └────────┬─────────┘
                                  │ repository.*
┌────────────────────┐            ▼
│ Worker 进程 N 个    │ ─────────> MySQL (InnoDB)
│ main_loop_forever  │ 原子认领/条件更新/事务
└────────────────────┘
```

- Repository 层：SQL、事务、状态机、幂等、回收。
- Worker 层：轮询认领、按 `step_index` 升序执行、失败即停、整任务原子提交。
- API 层：看板查询、手动认领/开始/上报。

## 关键语义（不可破坏）

1. L3 空字符串 = “不覆盖并跳过”，沿用当前生效值；L2 空字符串 = 字面值。
2. `step_logs` 唯一键 `(task_id, step_index)`：重复上报只保留首条。
3. 并发认领使用 `FOR UPDATE SKIP LOCKED`；任务/Step 状态只能由当前持有者推进。
4. 死锁/锁等待（1213/40001）自动重试 3 次，覆盖认领、释放、回收、状态推进、`report_step_execution`、`complete_task_atomically`、日志写入等事务入口。
5. `recover_claimed_interval` 是真正间隔：启动时若开启先回收一次，之后 elapsed >= 间隔才再回收；`<=0` 关闭。
6. Worker Step 按 `step_index` 升序执行；任一步失败立即停止后续执行，并把未执行 Step 补齐为失败后原子提交，任务置 `failed`。
7. 日志 message 写入前按 UTF-8 字节数校验不超过 MySQL TEXT 上限（65535），不做静默截断。
8. running 任务有租约：`tasks.lease_expires_at`，Worker 每个 Step 前续约；`recover_expired_running()` 会回收租约过期的 running 任务。

## 快速开始

```bash
mysql -uroot -p < db/schema.sql   # 建库建表
cp .env.example .env              # 填数据库账号
pip install -r requirements.txt
python scripts/create_task.py --count 5
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker --worker-id worker-1
```

看板：认领 → 开始 → “并发幂等测试”会从任务详情读取 `claimed_by`，用 `Promise.all` 同时发 5 次上报；应只有 1 次 `inserted=true`，`step_logs` 只有 1 条。

测试：

```bash
pytest -v                                   # 全量回归
pytest tests/test_concurrent_claim.py -v    # 真实多进程并发认领
python scripts/concurrency_demo.py --tasks 5 --workers 10 --rounds 10
python scripts/demo_duplicate_report.py     # 幂等演示
python scripts/audit_data.py                # 数据体检
```

### 并发认领演示证据

```text
python scripts/concurrency_demo.py --tasks 5 --workers 10 --rounds 10
并发认领演示：每轮 5 个任务，10 个 worker 进程，跑 10 轮
======================================================================
round=1   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=2   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=3   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=4   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=5   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=6   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=7   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=8   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=9   tasks=5 workers=10 claimed=5 unique=5 duplicates=0
round=10  tasks=5 workers=10 claimed=5 unique=5 duplicates=0
======================================================================
总轮数 10，总重复认领次数 = 0
结果：0 次重复认领，并发安全验证通过 [OK]
```

### 并发认领测试证据

```text
pytest tests/test_concurrent_claim.py -v
============================= test session starts =============================
tests/test_concurrent_claim.py::test_concurrent_claim_no_duplicate PASSED [100%]
============================== 1 passed in 2.29s ==============================
```

### CI

仓库托管在 GitHub，已配置 `.github/workflows/ci.yml`：Python 3.10 + MySQL 8.0 service，执行 `db/schema.sql` 后跑 `pytest -v`，保证多进程并发测试在 Linux runner 上可复现。

## 边界清单

### 参数合并
- L3 空串回退当前值，不回退 L1 base；当前值本身为空时保持空串。
- L2 空串字面生效；`0/False/None/[]/{}` 都合法覆盖。
- 嵌套 dict/list 整体替换；快照与输入深拷贝隔离。
- step_index 按真实值而非数组下标，乱序/空洞安全，重复/非法报错。

### 并发认领与恢复
- 认领单事务 `FOR UPDATE SKIP LOCKED` + 条件 UPDATE，不重复、不丢任务。
- claimed 超时回收；running 租约过期后可被 `recover_expired_running` 回收。
- Worker 认领后未进入 running 会主动 release；读详情失败也会释放回 pending。

### 状态机 / 原子上报
- 只有持有者能推进；终态不能乱跳。
- 整任务提交必须覆盖全部 Step；success log 是权威，存在 success log 的 Step 应修复为 done。
- 提交终态基于事务内全部 Step 的最新状态：全部 done -> done，存在 failed -> failed，不再只依赖 effective_results。
- `report_step_execution` 按 step_index 升序上报，不能跳过前置 Step。
- 后到 failure 不覆盖已有 success；重复 success 只保留首条。
- Step 失败即停：后续 Step 不再执行，但以失败日志补齐，保证 `complete_task_atomically` 提交全部 Step。

### 看板 / API / 运维
- 看板手动认领/开始固定使用 `manual-claim`；并发上报使用任务实际 `claimed_by`。
- 单 Step 任务点“并发幂等测试”会提示建议使用多步任务，避免并发时产生部分 409。
- API 超长 message 返回 422；已 done/failed 任务不能再上报。
- API 无认证、异常仍可能返回内部信息、看板无分页、Step 为 mock、无连接池/自动归档（生产化已知限制）。
