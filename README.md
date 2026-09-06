# TaskKanban 任务调度看板

TaskKanban 是一个**基于 MySQL 行锁实现并发安全认领、三层参数粘性合并、执行日志幂等写入**的轻量级任务调度系统，包含 FastAPI 核心后端、真实多进程 Worker 和原生 JS 轮询看板。

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- MySQL 8.0+（原生 SQL + mysql-connector-python）
- 原生 HTML + JavaScript（轮询看板）
- pytest

## 技术栈与选型理由

- **Python 3.10+**：标准库 `multiprocessing` 可直接启动真实多进程 Worker；开发效率高。
- **FastAPI**：轻量、自带 OpenAPI `/docs`；本系统业务都在 Repository 层同步读写数据库，API 层薄。
- **MySQL 8.0**：原生支持 `FOR UPDATE SKIP LOCKED`，能用数据库事务 + 行锁在 ≤10 Worker、≤5 任务/秒规模下安全解决并发认领，不需要引入 Redis/RabbitMQ/Kafka 等外部中间件。
- **SQL + mysql-connector-python**：事务边界、行锁、唯一约束都显式可控，不被 ORM 隐藏实现细节。
- **HTML + JavaScript**：看板只需展示状态 + 触发重复上报，不引入构建链。
- **pytest**：参数纯函数可单测；DB 集成测试验证真实事务与状态机。

### 并发测试

Python 的 `thread` 受 GIL 限制，`asyncio` 是单线程协程级调度，二者都不是题目要求的“多个 Worker 同时访问数据库”。本项目使用真实多进程并发：

```text
multiprocessing.Process（默认 spawn）
→ 每个子进程拥有独立 Python 解释器、独立 GIL 与独立内存
→ 每个进程调用 repository.claim_next_task() 时都 create_connection()
→ 多个独立 TCP 连接在同一时刻竞争 MySQL 行锁
```

Windows 的 `spawn` 会重新 import 模块，因此 Worker 主体放在可导入的 `main_loop_forever()` 中，而不是写在模块顶层。

## 架构总览

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

- **API 层**：查询看板、手动认领/开始/上报演示。
- **Repository 层**：所有 SQL、事务、状态机约束、幂等与回收逻辑。
- **Worker 层**：轮询认领、按顺序执行 Step、最后整任务原子提交。

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
- MySQL 8.0+
- pip

## 快速开始

### 1. 准备数据库

先启动本地 MySQL 8.0，然后用 root 执行建库建表脚本：

```bash
mysql -uroot -p < db/schema.sql
```

创建专用账号：

```sql
CREATE USER 'taskkanban'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON taskkanban.* TO 'taskkanban'@'localhost';
FLUSH PRIVILEGES;
```

或者修改`.env`文件，填入`root`账号与密码

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

打开 <http://0.0.0.0:8000> 查看看板。

### 5. 启动 Worker

开一个终端启动 worker：

```bash
python -m app.worker --worker-id worker-1
```

一次性拉多个 Worker：

```bash
python scripts/run_workers.py --workers 5 --prefix local --wait
```

压力演示（5 个 Worker + 每秒 5 个随机任务）：

```bash
python scripts/stress_demo.py --workers 5 --duration 10 --tasks-per-second 5
```

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



## 边界情况

### 参数合并

1. L3 空字符串表示“不覆盖”，回退到当前生效值而不是 L1 base
2. L2 空字符串按字面值处理，与 L3 空串语义不同
3. 当前值本身是空字符串时，L3 再给空串仍保持空串
4. `0 / False / None / [] / {}` 都是合法覆盖值，不能因假值被跳过
5. override 可引入 base 中不存在的新 key，且粘性持续
6. 嵌套 dict/list 是整体替换，不做嵌套深合并
7. 快照/返回值与输入深度隔离，修改快照不污染输入
8. step_index 允许乱序/空洞，按真实序号而非数组下标处理
9. step_index 必须正整数且任务内唯一，bool/0/负数/重复会报错
10. L3 无法表达“显式覆盖成空字符串”和“删除某个 key”（设计限制）

### 并发认领与恢复

1. 认领是单事务 `FOR UPDATE SKIP LOCKED` + 条件 UPDATE，不会重复认领
2. `claimed` 任务超时后可回收；`running` 中 Worker 崩溃仍需心跳/租约续期（已知限制）
3. Worker 认领后未成功进入 running 会主动 release，避免孤儿 claimed
4. 死锁 1213 会自动重试 3 次

### 状态机 / 原子上报

1. 只有当前持有者能推进状态；pending/done/failed 不能乱跳
2. 任务终态与 Step 明细的一致性由 `complete_task_atomically`/`report_step_execution` 保证，不能直接调用低层状态函数绕过
3. `complete_task_atomically` 必须提交任务全部 Step，部分结果会被拒绝
4. `report_step_execution` 必须按 step_index 顺序上报，不能跳过前置 Step
5. 后到 failure 不会覆盖已有 success
6. 重复 success 只保留第一条日志，message 不更新
7. 失败后不支持同一执行周期内“失败重试成功”（需要新执行周期）

### 看板 / API / 运维

1. 看板固定 `manual-claim` 所有权链路
2. 任务已 done/failed 后不能再上报
3. API 无认证、异常返回内部信息、看板无分页（生产化已知限制）
4. Step 执行是 mock，不真实发送消息
5. 无连接池；无自动清理/归档；审计脚本只读不自动修复
