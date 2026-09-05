# TaskKanban 任务调度看板

全栈方向笔试题：任务调度系统核心后端 + 极简状态看板。

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- MySQL 8.0+（原生 SQL + mysql-connector-python）
- 原生 HTML + JavaScript（轮询看板）
- pytest

## 为什么选择 Python

- `multiprocessing` 可启动真实多进程 worker，符合题目“真实并发测试”要求。
- 开发效率高，适合 2 天完成核心功能。
- FastAPI 轻量，自带 `/docs`，便于联调。

## 目录结构

```text
app/
  config.py        # .env 配置读取
  db.py            # MySQL 连接
  params.py        # 参数合并（L1/L2/L3 粘性规则）
  repository.py    # 数据库访问层（建任务/认领/日志/状态/原子上报）
  executor.py      # Step 模拟执行器
  worker.py        # Worker 进程循环
  api.py           # FastAPI 接口
  main.py          # 启动入口
db/schema.sql      # 建库建表 DDL
web/index.html     # 极简看板
scripts/create_task.py  # 造数脚本
tests/             # 参数/状态机/幂等/API/端到端/并发认领测试
```

## 环境要求

- Python 3.10+
- MySQL 8.0+（需要支持 `FOR UPDATE SKIP LOCKED`）
- pip

## 快速开始

### 1. 准备数据库

先启动本地 MySQL 8.0，然后用 root 执行建库建表脚本：

```bash
mysql -uroot -p < db/schema.sql
```

创建专用账号（或直接使用 root，自行调整）：

```sql
CREATE USER 'taskkanban'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON taskkanban.* TO 'taskkanban'@'localhost';
FLUSH PRIVILEGES;
```

复制环境变量文件：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，填入你的数据库账号密码。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 创建演示任务

```bash
python scripts/create_task.py --count 5
```

### 4. 启动 API 看板

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 <http://127.0.0.1:8000> 查看看板。

### 5. 启动 Worker

开一个终端启动 worker：

```bash
python -m app.worker --worker-id worker-1
```

可另开终端启动多个：

```bash
python -m app.worker --worker-id worker-2
```

Worker 默认会：

- 轮询并原子认领 pending 任务；
- 执行任务的全部 Step；
- 一次性原子提交日志 + Step 状态 + 任务状态；
- 定期回收超过租约仍未进入 running 的 claimed 任务。

### 6. 看板手动演示

如果没有启动 worker，也可以在页面上手动演示状态流：

1. 任务 `pending` 时点击 **“认领”**，任务变为 `claimed`；
2. 点击 **“开始”**，任务变为 `running`；
3. 点击 **“并发幂等测试”**，对当前 Step 并发触发 5 次完成上报；
4. 页面弹窗会显示 5 次结果中只有第一次 `inserted=true`，其余为幂等忽略；
5. 上报最后一个 Step 后任务变为 `done`。

### 7. 运行测试

```bash
pytest -v
```

## 核心设计

### 参数合并（L1/L2/L3）

- L1 base 是任务默认参数。
- L2 group override 在任务开始时一次性合并，空字符串按字面值处理。
- L3 step override 有粘性：从声明 key 的 Step 开始持续生效。
- L3 空字符串表示“本 Step 不覆盖此 key”，沿用当前值。

代码位于 `app/params.py`，是纯函数模块。

### 并发认领

使用 MySQL 行锁 + 原子更新：

```sql
SELECT id FROM tasks
WHERE status = 'pending'
ORDER BY created_at, id
LIMIT 1
FOR UPDATE SKIP LOCKED
```

同一任务只能被一个 worker 认领。

测试方式：`multiprocessing` 启动 10 个进程同时认领 5 个 pending 任务，断言无重复认领。

### 状态机与持有权约束

所有状态推进函数都使用条件更新，并返回 `bool`：

- `claim`: `pending -> claimed`，同一任务只能被一个 worker 认领；
- `mark_task_running(worker_id)`: 仅 `claimed_by == worker_id` 且状态为 `claimed` 时成功；
- `mark_task_done/failed(worker_id)`: 仅当前持有者且状态为 `claimed/running` 时成功；
- `mark_step_status(worker_id)`: 仅任务由该 worker 持有且处于 `claimed/running` 时成功；
- `release_task(task_id, worker_id)`: 持有者主动释放，回到 `pending`；
- `recover_expired_claims(max_claimed_seconds)`: 回收超过租约时间仍未进入 `running` 的 `claimed` 任务。

合法状态流转：

```text
pending --claim--> claimed --running--> running --done--> done
                                \
                                 \--failed--> failed
```

非法流转会被条件更新拒绝并返回 `False`：

```text
pending 直接 -> running/done/failed      拒绝
claimed 被非持有者 -> running/done/failed 拒绝
done -> failed/running/claimed           拒绝
failed -> running/done                   拒绝
```

### 原子 Step 上报 / 整任务原子提交

`report_step_execution()` 在单个事务内完成：

```text
Step 日志 + Step 状态 + 任务终态
```

`complete_task_atomically()` 让 worker 先执行完所有 Step，再一次性提交：

```text
全部日志 + 全部 Step 状态 + 任务 done/failed
```

已成功 Step 如果之后收到重复的 failure 上报，默认忽略该失败，不会覆盖已有成功记录。

### 幂等日志

`step_logs` 表对 `(task_id, step_index)` 建唯一索引。

写入使用：

```sql
INSERT IGNORE INTO step_logs (task_id, step_index, status, message)
VALUES (...)
```

重复上报不会覆盖已有记录。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情（含 steps） |
| POST | `/tasks/{id}/claim` | 手动认领演示 |
| POST | `/tasks/{id}/start` | 手动开始执行（claimed -> running，看板演示用） |
| POST | `/tasks/{id}/steps/{n}/report` | 重复完成上报（幂等演示） |

### Worker 参数

```bash
python -m app.worker --worker-id worker-1 \
  --claim-lease-seconds 300 \
  --recover-claimed-interval 30
```

- `--claim-lease-seconds`：claimed 任务超过该秒数未进入 running 则允许回收；
- `--recover-claimed-interval`：worker 每次循环前执行过期 claimed 回收；`<=0` 关闭。

## 测试证据

在完整环境（本机 MySQL + 可运行 multiprocessing）下执行 `pytest -v`，覆盖：

```text
tests/test_params.py            65 passed   # 参数合并 L1/L2/L3 组合与边界
tests/test_state_machine.py     20 passed   # 状态机合法/非法流转/持有权/回收
tests/test_idempotent_log.py     2 passed   # 幂等日志重复写入
tests/test_atomic_report.py     12 passed   # 原子 Step 上报/整任务原子提交
tests/test_api.py               15 passed   # API 查询/认领/上报/权限
tests/test_api_start.py          4 passed   # API 手动 start 演示路径
tests/test_e2e_flow.py           5 passed   # 端到端完整链路
tests/test_concurrent_claim.py   1 passed   # 真实多进程并发认领无重复
------------------------------------------------
合计 124 passed
```

> 若在受限沙箱/CI 中无法创建 multiprocessing 管道，`test_concurrent_claim.py` 可能无法运行；
> 其余 123 个用例应全部通过。并发认领测试需要在可运行 `multiprocessing` 的本机环境执行。

## 已知限制

- Worker 的 Step 执行是 mock，不真正发送消息。
- 未做用户认证（本地/笔试场景不需要）。
- 未做 WebSocket，看板使用 2 秒轮询。
- 超时回收只回收长期停留在 `claimed` 的任务；`running` 中 worker 真实崩溃仍需心跳/租约续期机制，当前属于简化方案。
- 异常处理器会把内部错误字符串返回给客户端，仅适合本地笔试场景。
