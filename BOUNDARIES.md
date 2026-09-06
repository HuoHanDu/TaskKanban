# BOUNDARIES.md — 边界情况与修复记录

> 本文记录开发过程中**自己发现、验证并修复的边界情况**，以及**当前有意识保留的设计边界 / 已知限制**。
> README 只保留简短“设计边界”，详细过程放在这里，便于答辩或面试官深挖。

---

## 约定

- ✅ **已发现并修复**：代码已处理，并有测试 / 演示 / 日志证据。
- ⚖️ **设计边界**：主动做的取舍，不属于漏做。
- ⚠️ **已知限制**：当前未做或需要演进，交付前应能讲清。

---

## 1. 参数合并模块

### 1.1 ✅ 已发现并修复

| 边界 | 风险 / 现象 | 修复后行为 | 验证 / 证据 |
| --- | --- | --- | --- |
| L3 空串回退错层 | 曾把 `""` 回退到 L1 base，粘性链断裂 | L3 空串表示“本 Step 不覆盖”，保留当前生效值 | `tests/test_params.py::test_l3_empty_string_keeps_current_not_base` |
| L2 空串语义混用 | 若把 L2 空串也当“不覆盖”会错误回退到 L1 | L2 空串按字面值覆盖，不走 L3 特殊语义 | `test_l2_empty_string_is_literal_value` |
| 假值被误跳过 | `if not value` 会把 `0/False/None/[]/{}` 当成空串跳过 | 只对 `value == ""` 特殊处理，假值均按字面覆盖 | `test_l1_l2_l3_combination_matrix` |
| 浅拷贝共享 | `dict()` / 浅拷贝会让多个 Step 快照共享嵌套 dict/list | 合并与返回全程 `deepcopy` 隔离 | `test_snapshots_do_not_share_nested_mutable_values_with_inputs` |
| 乱序 steps | 按输入顺序合并会算错参数 | 先按真实 `step_index` 升序排序，再应用粘性覆盖 | `test_step_index_ordering_is_guaranteed_even_if_input_unsorted` |
| step_index 空洞 | 例如只有 1、3，用数组下标会错位 | `get_params_for_step` 按真实 `step_index` 查找 | `test_get_params_for_step_searches_by_real_step_index_not_position` |
| bool step_index | Python 中 `True` 是 `int` 子类，会被当成 Step1 | 显式排除 bool | `test_get_params_for_step_rejects_non_int_index` |
| 重复 step_index | 粘性链路语义混乱，快照难以对应 | 校验同一任务内唯一 | `test_build_snapshots_rejects_duplicate_step_index` |
| 非法 step_index | 0 / 负数 / 非 int 曾静默出错或抛深层异常 | 入口显式抛 `TypeError/ValueError`，错误信息带上下文 | `test_build_snapshots_rejects_bad_step_index_values` |
| 非 Mapping 入参 | base / group / override / step 类型错误难排查 | `_ensure_mapping` / `_normalize_steps` 带参数名与位置报错 | `test_apply_group_rejects_non_mapping`、`test_build_snapshots_rejects_non_mapping_override` |
| 嵌套 dict/list 语义未定义 | 容易“想当然”做深合并，产生额外语义 | 明确为整体替换，不做嵌套深合并 | `test_complex_values_are_replaced_whole_not_deep_merged` |
| 当前值本身是空串 | L3 再给空串时可能被误删 key | 空串仍保持空串，不删除 key | `test_current_value_empty_string_stays_empty_on_l3_empty` |
| 同 Step 多个 key | 一个 key 是空串时可能误伤同 Step 其他 key | 只跳过值为空串的 key，其他 key 正常生效 | `test_l3_empty_only_skips_that_key_other_overrides_still_apply` |
| L3 只能覆盖已有 key | 容易漏掉“可引入全新 key”的语义 | L3 非空值可新增 base/L2 不存在的 key | `test_l3_introduces_new_key_and_sticks_to_following_steps` |
| 快照与入参互相污染 | 修改快照会影响输入或后续 Step | 所有返回值与入参深度隔离 | `test_apply_step_does_not_mutate_current_or_override` |

### 1.2 ⚖️ 设计边界

- L3 的 `""` 只能表达“不覆盖”，**不能表达“显式把值覆盖成空串”**。若要清空某 key，需要额外约定。
- L3 只能持续覆盖，**不能“删除 / 回退某个 key”**，除非后续用其他值覆盖。
- 嵌套 dict/list 采用**整体替换**，不做深合并，避免引入题目未定义的局部更新语义。
- 每次从 L1 开始重放合并是 O(N×M)，Step 很多时可做增量缓存；当前规模（≤10 worker、≤5 任务/s）可接受。

---

## 2. 并发认领 / 状态机 / Worker 持有权 / 崩溃恢复

### 2.1 ✅ 已发现并修复

| 边界 | 风险 / 现象 | 修复后行为 | 验证 / 证据 |
| --- | --- | --- | --- |
| 认领不是原子操作 | 普通 `SELECT + UPDATE` 存在两个 Worker 同时读到同一 pending 的窗口 | 单事务 `SELECT ... FOR UPDATE SKIP LOCKED` + 条件 `UPDATE` | `tests/test_concurrent_claim.py`；`scripts/demo_concurrency.py` |
| 认领外层缺状态条件 | 锁内读到旧状态时仍可能误更新 | `UPDATE ... WHERE id=? AND status='pending'` 双保险 | `tests/test_state_machine.py` 中重复认领用例 |
| 认领后长事务持锁 | 一直持有 tasks 行锁会降低并发吞吐 | 认领 commit 后再另开连接读详情 | 代码 `claim_next_task` |
| 状态推进无条件更新 | pending 可直接 done、done 可被改 failed | 所有推进函数均为条件 UPDATE + 状态白名单 | `test_pending_cannot_go_directly_done`、`test_done_cannot_be_failed_after_success` |
| 不校验认领者 | 非持有者可推进 / 释放任务 | 函数接收 `worker_id`，要求 `claimed_by == worker_id` | `test_non_owner_cannot_mark_done`、`test_non_owner_cannot_release_task` |
| claim 后没有执行权确认 | 任务被释放 / 回收后 Worker 仍继续执行 | 必须 `mark_task_running` 成功才取得执行权 | `tests/test_worker_behavior.py` |
| Worker 崩溃后 claimed 孤儿 | 认领后未进入 running 就崩溃，任务长期卡 claimed | `release_task` 主动释放 + `recover_expired_claims` 超时回收 | `test_recover_expired_claims_resets_stale_claimed` |
| MySQL 1213 / 40001 死锁 | 并发事务被牺牲，导致任务卡 claimed | 死锁 / 锁等待自动重试 3 次 + 小退避 | `tests/test_deadlock_retry.py`；`logs/*.log` 中的死锁记录 |
| 认领后读详情失败 | 可能留下无人执行的 claimed | 读详情失败时主动释放回 pending | `test_claim_next_task_releases_when_detail_read_fails` |
| running 中 Worker 崩溃 | 任务可能永远 running | 增加 `lease_expires_at` 租约；Worker 每 Step 前续约；`recover_expired_running()` 回收过期 running | `test_recover_expired_running_resets_stale_running` |
| 终态残留租约 | done / failed 后仍带 `lease_expires_at`，存在被误回收风险 | 进入 done / failed 时清空租约 | `test_terminal_done_clears_running_lease` |
| running 回收时间重复扣减 | 曾写 `lease_expires_at < NOW() - INTERVAL %s SECOND`，逻辑上多扣一次 | 统一按绝对到期时间 `lease_expires_at < NOW()` 判断 | commit `3d6ea8c` |
| 回收频率语义不清 | 容易把“每轮循环都回收”当“间隔回收” | 明确为真实间隔：启动先回收一次，之后 elapsed ≥ interval 再回收；`<=0` 关闭 | `test_recover_claimed_interval_is_actual_interval_not_every_loop` |
| 读/写任务摘要缺租约字段 | 看板与详情无法展示 / 判断租约状态 | `list_tasks` / `get_task_with_steps` 返回 `lease_expires_at` | API / 看板字段 |

### 2.2 ⚖️ 设计边界 

- ⚠️ **Worker 执行 Step 前会续约，但未检查续约返回值**。若租约已被回收 / 所有权已丢失，Worker 仍会跑完内存中的 mock Step，直到 `complete_task_atomically` 才因非持有者被拒绝。当前 mock 无外部副作用，所以不会产生错误业务结果；若 Step 有真实外部副作用，应改为“续约失败立即中止”。
- ⚠️ **单个 Step 执行时长超过 `lease_seconds` 时，Step 执行期间没有心跳**，可能被其他 Worker 回收。当前 mock Step 约 0.1s，不会触发；真实长任务需要执行中续约或更细粒度心跳。
- ⚠️ `release_task` 可以把 running 任务释放回 pending；在当前“先内存执行完再原子提交”模型下安全，若未来改为逐步写库需重新设计释放语义。
- ⚠️ 没有 `attempt` / 重试次数 / 执行周期字段，无法区分任务是第几次执行，也没有最大重试次数。
- ⚠️ `create_task` 目前是纯 INSERT 新任务，未套死锁重试；并发风险极低，但“所有写事务都覆盖死锁重试”的说法应限定为已套装饰器的事务函数。

---

## 3. 幂等日志 / 原子上报 / Step 执行顺序 / Worker 失败语义

### 3.1 ✅ 已发现并修复

| 边界 | 风险 / 现象 | 修复后行为 | 验证 / 证据 |
| --- | --- | --- | --- |
| 同一 Step 重复上报出现重复日志 | 违反“只保留一条日志” | `step_logs` 唯一键 `UNIQUE(task_id, step_index)` | `tests/test_idempotent_log.py` |
| `INSERT IGNORE` 吞错误 | 超长、非空等非唯一键错误也可能被静默忽略 | 改为 `INSERT ... ON DUPLICATE KEY UPDATE id=id`，只容忍唯一键冲突 | `test_duplicate_with_on_duplicate_key_still_keeps_single_log` |
| 后到失败覆盖成功 | 只靠唯一键不够，Step / Task 仍会被改成 failed | 已有 success 日志时，后到 failure 整体忽略 | `test_duplicate_report_does_not_overwrite_existing_success` |
| 日志 + Step + Task 多次事务 | 任一步失败会留下不一致中间态 | `report_step_execution` 单事务原子完成日志 + Step + Task | `tests/test_atomic_report.py` |
| Worker 逐步写库 | 可能留下“Step1 done、任务仍 running”的半成品 | Worker 内存执行完，再 `complete_task_atomically` 一次提交 | `tests/test_e2e_flow.py` |
| 上报不校验任务状态 | pending 也能写日志 | 事务内锁定任务并校验 claimed / running | `test_report_rejected_when_task_not_active` |
| 上报不校验持有者 | 非持有者可上报 | 事务内 `_require_owner` | `test_report_rejected_when_not_owner` |
| Step 不存在也写日志 | 会产生悬空日志 | 事务内锁定 Step 并校验存在 | `test_report_rejected_when_step_not_found` |
| 只提交部分 Step 就把任务 done | 任务有 Step1/2，只提交 Step1 却提前 done | `complete_task_atomically` 强制 results 必须覆盖任务全部 Step | `test_complete_task_atomically_rejects_partial_results` |
| 乱序上报 | 先报 Step2 再补 Step1，绕过顺序语义 | 存在更小且未 done 的前置 Step 时拒绝 | `test_report_out_of_order_step_is_rejected` |
| 重复上报最后一步的计数问题 | 可能把任务重复 done 或状态计算错误 | 基于事务内锁定的最新状态计算 done 数，不重复推进 | `test_duplicate_report_last_step_does_not_change_done_state` |
| 日志超长 | MySQL TEXT 65535 字节可能静默截断 | 入库前按 UTF-8 字节数校验，超长直接拒绝 | `test_message_too_long_is_rejected_not_truncated` |
| Worker 失败即停但未执行 Step 仍 pending | 任务 failed 后残留 pending Step，破坏“全部 Step 有状态” | Worker 将未执行 Step 以失败结果补齐后原子提交 | `test_worker_failure_stops_and_backfills_remaining_steps` |
| 已有 success 后又来 late failure | Step 状态可能与成功日志不一致 | 忽略 failure，并将 Step 修复为 done，保证“日志/Step 状态”自洽 | `test_complete_task_atomically_mixed_ignored_failure_repairs_step_and_done` |
| results 入参类型宽松 | `1.5/True/"false"/None` 等可能被静默转换 | 严格校验 step_index 为 int 且非 bool、success 为 bool、message 类型合法 | `test_complete_task_atomically_rejects_invalid_step_index`、`..._invalid_success` |

### 3.2 ⚖️ 设计边界

- 重复 success 不会更新 message，保持第一次写入内容。
- 同一 Step **先 failure 后 success** 的重试语义未支持：任务 failed 后不能重新打开继续上报成功。
- `complete_task_atomically` 限制最多 1000 个 results，超限拒绝，未做分批提交。
- API / 手动 `report_step_execution` 上报 failure 时，只把当前 Step 置 failed 并把任务置 failed，**不会像 Worker 路径那样把后续未执行 Step 补齐为 failed**；后续 Step 保持 pending。正常 Worker 链路使用 `complete_task_atomically` 不受影响，但调用方需知道两种路径差异。
- 日志 message 目前把参数快照拼进文本，不是结构化字段；生产建议增加 `params_snapshot JSON` 列。

---

## 4. API / 看板 / 前端

### 4.1 ✅ 已发现并修复

| 边界 | 风险 / 现象 | 修复后行为 | 验证 / 证据 |
| --- | --- | --- | --- |
| 看板上报 403 | API 加持有者校验后，前端上报未带 `worker_id` | 前端从任务详情读取实际 `claimed_by`，上报 body 携带该 `worker_id` | `tests/test_api.py`、`web/index.html` |
| 幂等演示选错 Step | 若选已 done Step，全部 `inserted=false`，演示无效 | 优先选“pending 且不是最后一步”的 Step | `web/index.html` |
| 单 Step 任务并发演示误读 | 第一次上报后任务 done，其余请求 409，容易被认为是幂等失败 | 页面提示建议使用多步任务 | `web/index.html` |
| done / failed 后再上报 | 终态可被破坏 | API 409 / repository 条件拒绝 | `test_report_after_done_is_rejected` |
| 日志 message 超长返回 500 | 属于请求数据问题，不是服务端错误 | API 捕获 `ValueError` 返回 422 | `test_report_too_long_message_returns_422_and_no_log` |
| 审计脚本误报 active | 曾把大量 done 当 active 扫描 | 默认只扫描 active 状态 | `scripts/audit_data.py` |

### 4.2 ⚠️ 已知限制

- API 无认证 / 权限：任何能访问的人都能 claim / start / report / 看详情。
- 全局异常处理器返回 `str(exc)`，会泄露内部信息，生产应改为记录日志 + 通用错误。
- 看板 2 秒轮询，非实时推送。
- `GET /tasks` 无分页，任务量大时返回全量。
- `GET /tasks/{id}/logs` 无分页。
- API 没有 `POST /tasks` 创建任务接口，只能通过脚本 / Repository 创建。
- 前端使用 `innerHTML` 拼接 `id/claimed_by/created_at` 等字段；当前数据源可信，但存在 XSS 面。
- 无连接池，每次连接 / 写操作新建 MySQL 连接。
- 无日志自动归档 / 清理，只有手动清理脚本。

---

## 5. 数据 / 运维 / 部署 / 测试 / 交付

### 5.1 ✅ 已发现并修复

| 边界 | 风险 / 现象 | 修复后行为 | 验证 / 证据 |
| --- | --- | --- | --- |
| 清理脚本误删 | 演示/测试数据可能被直接删除 | cleanup 默认只统计；加 `--delete` 才真正删除 | `scripts/cleanup_data.py` |
| 审计范围过大 | 把大量 done 误报为 active 异常 | 默认只扫 active 状态 | `scripts/audit_data.py` |
| 本地 worker 与服务器 worker 同时消费 | 测试数据可能被抢，影响测试结果 | 测试前需停止 worker；文档已注明 | `doc/demo-and-run-guide.md` |
| schema 含账号密码风险 | 公共仓库可能泄露数据库凭据 | `db/schema.sql` 只建库表，不创建账号；账号由部署者手动配置 | `db/schema.sql`、`.env.example` |

### 5.2 ⚠️ 已知限制

- `logs/` 与 `doc/` 被 `.gitignore` 忽略。若 README / 本文件要引用日志、截图，需要先放入一个可提交的 `evidence/` 或 `assets/` 目录，否则上传 GitHub 后链接会失效。
- 回收逻辑依赖 Worker 循环触发；如果所有 Worker 都停止，`claimed/running` 孤儿不会被自动回收，需要独立 scheduler 或运维兜底。
- `producer.py` 的 `--tasks-per-second` 按 int 截断，非整数速率不精确。
- 没有 HTTPS / 反向代理 / 防火墙自动化；远程 MySQL 3306 与 API 端口不应长期公网暴露。
- 多进程并发测试在 Windows 受限沙箱中可能无法运行；需要真实终端或 GitHub Actions 留一次完整输出。

---

## 6. 试题需求符合度速查

| 试题硬性需求 | 状态 | 主要证据 |
| --- | --- | --- |
| 参数 L1/L2/L3 粘性合并、空串语义、自己列边界并用测试证明 | ✅ 完成 | `tests/test_params.py`；本文第 1 节 |
| 同一任务只能被唯一 worker 持有，真实多进程/多连接验证 | ✅ 代码完成 | `claim_next_task`；`tests/test_concurrent_claim.py`；`scripts/demo_concurrency.py` |
| Step 日志幂等写入，重复上报只留一条，后到失败不覆盖成功 | ✅ 完成 | `UNIQUE(task_id, step_index)`；`tests/test_idempotent_log.py`、`test_atomic_report.py` |
| 极简状态看板 + 并发 5 次重复上报演示 | ✅ 完成 | `web/index.html`；`tests/test_api.py`、`test_e2e_flow.py` |
| README 写清选型、真实并发方案、边界、砍掉内容 | ✅ 主体完成 | 根 `README.md`（短版边界可再精简）；本文为详细版 |

### 未完成项

1. Worker 续约返回值未校验；单 Step 超过租约时长时无执行中心跳。
2. 手动 `report_step_execution` failure 不会补齐后续未执行 Step，与 Worker 整任务提交路径不一致。
3. 无任务 `attempt/retry` 语义，失败后不能“重新打开继续成功”。
4. 生产化能力：认证、分页、错误信息脱敏、连接池、结构化日志、XSS 转义、自动归档。
5. 公开仓库证据文件处理：日志/截图目前被 gitignore，README 引用前需复制到可提交目录。
