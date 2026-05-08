# Resilient Scraper

分布式网页抓取服务，基于 PostgreSQL 任务队列 + 浏览器自动化。

## 快速开始

### 1. 启动服务

```bash
# 启动全部服务（API + Worker + Chrome + Xvfb）
./scripts/run.sh start

# 只启动 API（用于提交/查询任务，Worker 在 ASG 上运行）
./scripts/run.sh start-api

# 查看服务状态
./scripts/run.sh status

# 停止所有服务
./scripts/run.sh stop

# 查看日志
./scripts/run.sh logs           # Worker 日志
./scripts/run.sh logs api       # API 日志
./scripts/run.sh logs chrome    # Chrome 日志
```

### 2. 配置抓取任务

编辑 `config/xhs.conf`：

```conf
# key=value 行设置所有任务的参数
skip_existing_days=30    # 只跳过30天内抓过的笔记，0=跳过所有已有笔记（默认0）
max_notes=50             # 每人最多抓50条笔记，0=不限（默认0）

# 其他行是用户 ID
411325471
242650776
```

支持的参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `skip_existing_days` | 跳过 N 天内已抓取的笔记，0 = 跳过所有已有笔记 | 0 |
| `max_notes` | 每个用户最多抓取笔记数，0 = 不限 | 0 |
| `scrape_mode` | 抓取模式：`notes` / `following` / `search` | notes |

### 3. 提交任务

```bash
# 从 config/xhs.conf 读取配置和用户列表（推荐）
./scripts/submit.sh

# 直接指定用户
./scripts/submit.sh 411325471 242650776

# 从其他配置文件读取
./scripts/submit.sh --file path/to/other.conf

# 命令行覆盖配置
./scripts/submit.sh --max-notes 50 --priority 5
```

### 4. 查看任务状态

```bash
# 查看任务列表
./scripts/submit.sh --list

# 按状态过滤
./scripts/submit.sh --list pending
./scripts/submit.sh --list completed

# 查看单个任务详情
./scripts/submit.sh --status <task_id>
```

## 架构

```
开发 EC2 (scripts/run.sh)
┌──────────────────────────────┐
│  API (FastAPI :18000)        │──── submit.sh / curl
│  + Auto-scale controller     │
└──────────┬───────────────────┘
           │ Aurora PostgreSQL (任务队列)
           ↓
┌──────────────────────────────┐
│  ASG Worker EC2 (0~5 台)     │
│  Docker: Xvfb → Chrome →    │
│  Worker (claim_task)         │
└──────────────────────────────┘
```

- **API** 接收任务、查询状态、控制 ASG 扩缩容
- **Worker** 从数据库 claim 任务（`SELECT FOR UPDATE SKIP LOCKED`），启动 Chrome 执行抓取
- **Auto-scale** API 进程定期检查 pending 任务数，通过 boto3 调整 ASG desired capacity

## 部署

```bash
# 首次部署 / 更新基础设施（CDK）
./deploy.sh deploy

# 查看 ASG 状态
./deploy.sh status

# 手动调整实例数
./deploy.sh scale 3
```

## 配置

所有配置集中在 `.env` 文件中：

```env
# 数据库
DB_URL=postgresql+asyncpg://...

# 服务
SCRAPER_API_PORT=18000

# 自动扩缩容
AUTOSCALE_ENABLED=true
AUTOSCALE_ASG_NAME=...
AUTOSCALE_MIN_INSTANCES=1
AUTOSCALE_MAX_INSTANCES=5
AUTOSCALE_TASKS_PER_WORKER=5    # 每台 Worker 处理的任务数（用于计算所需实例数）
AUTOSCALE_CHECK_INTERVAL=60
AUTOSCALE_SCALE_DOWN_COOLDOWN=5
```

## 目录结构

```
config/              # 抓取配置文件
  xhs.conf           # 小红书抓取配置 + 用户列表
scripts/
  run.sh             # 启停服务
  submit.sh          # 提交/查询任务
docker/              # Docker 镜像
infra/               # CDK 基础设施
src/resilient_scraper/
  scraper.py          # ResilientScraper 基类
  service/
    api.py            # FastAPI 服务 + 自动扩缩容
    worker.py         # 任务消费 Worker
    queue.py          # PostgreSQL 任务队列
    config.py         # 服务配置（Pydantic）
  scrapers/
    xiaohongshu/      # 小红书 scraper
```
