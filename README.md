# Write Agent

小说创作与人工审核工作台，包含章节生成、版本保存、审核和发布队列。Web 与 Worker 分别启动；只有 Worker 消费任务队列。

需要 Python 3.11 或更高版本。在项目根目录执行以下 PowerShell 命令：

```powershell
$writeAgentDeps = python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))"
python -m pip install $writeAgentDeps
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

编辑 `.env`，填写自己的模型服务密钥、接口地址及支持的模型名称，然后启动 Web：

```powershell
python -m app.launch
# 等同于 python -m app.launch web --host 127.0.0.1 --port 8888
```

浏览器入口为 `http://127.0.0.1:8888/ops`。覆盖端口可使用 `python -m app.launch web --port 8001`。

当前按源码目录运行。项目尚未配置 flat-layout 包发现，Alembic 资源也在包外；`pip install -e .` 和 wheel 构建目前不可用，打包支持后续单独处理。

运行 Worker：

```powershell
python -m app.launch worker --batch-limit 10 --poll-interval 5
# 单轮运行：
python -m app.launch worker --max-cycles 1
```

配置通过环境变量或项目根目录 `.env` 加载，字段见 `app/config.py`。常用项包括 `DATABASE_URL`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_REVIEW_MODEL`、`OPENAI_API_KEY`。密钥仅放在本地配置中；文档和共享文件使用 `YOUR_API_KEY_HERE` 等占位值。Embedding 可单独配置 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`；缺省会复用模型连接配置。

配置模板使用文件 SQLite（`sqlite+pysqlite:///./write_agent.db`），Web 与 Worker 需要从同一项目根目录启动，才能共享数据。未配置 `DATABASE_URL` 时，程序回退到内存 SQLite，进程退出后数据消失，且 Web 与 Worker 不共享数据。数据库表和兼容迁移在应用初始化时执行。发布默认为禁用（`PUBLISH_MODE` 为空），浏览器模式默认为 `stub`；真实模型及平台配置需自行验证。

本仓库上传范围包含应用源码、迁移、启动说明及 `.env.example`；测试文件、`docs/`、本地 `.env`、数据库、日志、虚拟环境及缓存通过 `.gitignore` 排除，仅保留在本地。

仓储写入只执行 `flush`，调用方负责 `commit` 或 `rollback`。手动使用 `WriteAgentApplication.from_settings(...)` 时应在结束后调用 `close()`；Web 关闭及 Worker 退出会自动释放会话和模型客户端。
