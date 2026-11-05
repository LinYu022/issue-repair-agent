# Issue 诊断与修复 Agent

面向 Python 项目的 Issue 修复工具：读取问题和可见源码，提出计划，经人工审批后调查、修改并在 Docker 中验证，输出补丁和运行记录。适合本地辅助修复和需要审阅执行过程的场景。

## 核心能力

- **受控执行**：按授权路径、工作区版本和注册测试配置执行，模型不能自行批准计划。
- **证据驱动修复**：结合基线复现、失败分类、假设与重复检测调整后续动作。
- **结构化上下文**：组织当前代码、关键观察和反证，支持按需回查完整工件。
- **隔离验证**：每次测试使用无网络、非特权且有资源限制的 Docker 容器。
- **可选持久化与记忆**：PostgreSQL 保存执行记录、检查点及三类记忆；记忆具有来源和作用域约束。
- **本地工作台**：查看运行快照、工具观察、补丁和公开测试状态。

## 快速开始

需要 Linux 或 WSL2、Python 3.12、uv，以及已启动且当前用户可访问的 Docker Engine。以下命令在项目根目录的 Bash 中执行。

```bash
uv sync --frozen --no-dev
docker build -f environments/Dockerfile.runner -t issue-agent-runner:latest environments

export DEEPSEEK_API_KEY="<你的 API Key>"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
```

`.env.example` 仅是配置模板，程序不会自动加载 `.env`。

先在本机创建一个最小任务输入，再运行：

```bash
mkdir -p .local/task
uv run --frozen --no-dev python - <<'PY'
import json
from pathlib import Path
p = Path(".local/task")
(p / "subject.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
check = ["/usr/local/bin/python", "-c", "from subject import add; assert add(2, 3) == 5"]
task = {
    "repo": "fixture/local-demo", "base_commit": None,
    "issue": "修复 subject.py 中 add：返回两个数的和，保持函数接口不变。",
    "files": ["subject.py"], "write_paths": ["subject.py"], "protected_paths": ["tests"],
    "test_profiles": {"reproduce": check, "public-test": check},
    "reproduction_profile": "reproduce"
}
(p / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
PY
uv run --frozen --no-dev python -m scripts.run_agent \
  --task .local/task/task.json --output .local/run
```

审核显示的计划和修改范围，输入 `approve` 批准。`--output` 必须指向尚不存在的目录；再次运行请换一个目录。结束后终端会显示补丁路径，工作台快照为 `.local/run/workbench.json`。

修复自己的项目时，按上面的任务字段准备 JSON：`files` 相对于任务 JSON 所在目录，必须显式提供可见源码；真实仓库的 `repo` 使用 `组织/仓库`，`base_commit` 填完整的 40 位提交哈希。工具不会自动克隆仓库。测试命令仅接受以 `/usr/local/bin/python` 开头的参数数组，所需依赖须提前装入 runner 镜像，再通过 `--image` 指定。

## 查看工作台

额外需要 Node.js 22+、pnpm 11。esbuild 是构建依赖，安装时保留开发依赖：

```bash
pnpm --dir app/web install --frozen-lockfile --prod=false
node app/web/build.mjs
uv run --frozen --no-dev python -m scripts.serve_product \
  --credentials .local/p2-18/credentials.json --init-credentials
uv run --frozen --no-dev python -m scripts.serve_product \
  --credentials .local/p2-18/credentials.json --data .local/run/workbench.json
```

打开 `http://127.0.0.1:8018`，用本机凭证文件中对应角色的令牌连接。此启动方式浏览已导出的运行快照；实时任务和恢复控制需由宿主绑定运行实例。

## 可选数据库与容器部署

启用持久化或 Memory 前，先自行部署 PostgreSQL、创建数据库与有建表权限的账号，然后初始化：

```bash
export DATABASE_URL="postgresql://<用户>:<密码>@127.0.0.1:5432/issue_agent"
uv run --frozen --no-dev python -m scripts.init_database
```

运行 Agent 时增加 `--durable`；启用记忆再增加 `--memory all --user-id <用户UUID>`。恢复、检查点和记忆管理入口在 `scripts/recover_task.py`、`scripts/checkpoint.py`、`scripts/semantic_memory.py`、`scripts/episodic_memory.py` 与 `scripts/procedural_memory.py`，参数可通过 `python -m scripts.<模块名> --help` 查看。

部署工作台容器时，先完成前端构建及本机凭证初始化，再准备锁定的 Linux amd64 wheels 和运行快照：

```bash
uv run --frozen --no-dev python -m scripts.prepare_product_wheels
cp .local/run/workbench.json .local/p2-18/workbench.json
docker buildx build --load --platform linux/amd64 \
  --build-context wheels=./.local/p2-18/wheels \
  -f environments/Dockerfile.product -t issue-agent-workbench:p2-18 .
docker compose -f compose.product.yaml up -d
```

这需要 Docker Buildx/BuildKit、可获取固定基础镜像和依赖包的网络。Compose 仅部署工作台，凭证和快照以只读方式挂载；应保证容器 UID 1000 可读取这两个本地文件。数据库、模型 API 和实际待修复源码由使用者自行配置或提供，密钥、任务输入和运行结果均留在本机。
