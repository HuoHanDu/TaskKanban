- # TaskKanban 任务调度看板

  TaskKanban 是一个**基于 MySQL 行锁实现并发安全认领、三层参数粘性合并、执行日志幂等写入**的轻量级任务调度系统，包含 FastAPI 核心后端、真实多进程 Worker 和原生 JS 轮询看板。

  自9月5日14时开始需求分析到9月6日17时基本开发完成，用时27小时

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

## 运行结果证据

详见根目录下logs文件夹

## 边界情况

具体发现的问题以及修复详见根目录下`BOUNDARIES.md`

### 参数合并

- L3 空串 = “不覆盖”，沿用当前生效值；L2 空串按字面值覆盖。
- 0 / False / None / [] / {} 都按字面覆盖，不做假值跳过。
- 嵌套 dict/list 整体替换，不做深合并。
- step_index 按真实序号解析，乱序/空洞安全；同一任务内必须唯一。

### 并发与恢复

- 认领使用 `FOR UPDATE SKIP LOCKED` + 条件 UPDATE。
- 只有持有者可推进状态；终态不可回改。
- running 任务有租约，Worker 每 Step 续约；过期任务可被回收。

### 幂等日志

- 每个 `(task_id, step_index)` 只有一条日志；后到 failure 不覆盖 success。
- Worker 整任务原子提交；Step 失败即停，未执行 Step 以失败补齐。

