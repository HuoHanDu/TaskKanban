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
  repository.py    # 数据库访问层（建任务/认领/日志/状态）
  executor.py      # Step 模拟执行器
  worker.py        # Worker 进程循环
  api.py           # FastAPI 接口
  main.py          # 启动入口
db/schema.sql      # 建库建表 DDL
web/index.html     # 极简看板
tests/             # 参数单测 / 幂等 / 多进程并发认领测试
scripts/create_task.py  # 造数脚本
```

## 快速开始

### 1. 准备数据库

```bash
mysql -uroot -p < db/schema.sql
```

创建专用账号（或自行调整）：

```sql
CREATE USER 'taskkanban'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON taskkanban.* TO 'taskkanban'@'localhost';
FLUSH PRIVILEGES;
```

复制 `.env.example` 为 `.env` 并填入数据库连接信息。

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

```bash
python -m app.worker --worker-id worker-1
```

可另开终端启动多个：

```bash
python -m app.worker --worker-id worker-2
```

### 6. 运行测试

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

### 幂等日志

`step_logs` 表对 `(task_id, step_index)` 建唯一索引。

写入使用：

```sql
INSERT IGNORE INTO step_logs (task_id, step_index, status, message)
VALUES (...)
```

重复上报不会覆盖已有记录。

## 测试证据

```text
16 passed in 1.90s
```

覆盖：

- 参数合并边界 13 项
- 幂等日志重复写入 / 并发 5 次上报只留一条
- 真实多进程并发认领无重复

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务详情（含 steps） |
| POST | `/tasks/{id}/claim` | 手动认领演示 |
| POST | `/tasks/{id}/steps/{n}/report` | 重复完成上报（幂等演示） |

## 已知限制

- Worker 的 Step 执行是 mock，不真正发送消息。
- 未做用户认证（本地/笔试场景不需要）。
- 未做 WebSocket，看板使用 2 秒轮询。
