# Issue 诊断与修复 Agent

一个 Python Issue 诊断与修复 Agent。它读取问题和代码，提出修复计划，经人工批准后调查仓库、修改代码、在 Docker 中验证，并输出补丁。

## 项目亮点

- **证据驱动重规划**：根据执行反馈修订假设和修复策略。
- **结构化上下文**：保留代码版本、计划、关键证据和读取进度，支持按需展开。
- **持久化与恢复**：使用 PostgreSQL 保存执行状态，通过联合检查点恢复工作区和执行进度。
- **三类记忆**：支持语义知识、历史情景和经过验证的 Skill，提供来源追踪、撤销和版本回滚。
- **受控执行**：文件修改限定在授权范围内，验证使用独立、无网络的 Docker 容器。

## 快速使用

```bash
git clone https://github.com/LinYu022/issue-repair-agent.git
cd issue-repair-agent
uv sync --frozen --no-dev
docker build -f environments/Dockerfile.runner -t issue-agent-runner:latest environments
```

配置真实 DeepSeek 模型，在当前终端设置：

```bash
export DEEPSEEK_API_KEY="你的 API Key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
uv run --frozen --no-dev python -m scripts.run_agent --task examples/quickstart/task.json
```

Agent 提出计划后，阅读修改范围并输入 `approve` 批准。完成后终端会显示补丁和运行目录。

修复自己的项目时，参照示例配置问题说明、可见文件、允许修改的路径和验证命令，并为真实仓库填写完整的 `base_commit`。验证所需依赖应事先安装在运行镜像中，再通过 `--image` 指定镜像。

## 查看工作台

额外需要 Node.js 和 pnpm：

```bash
pnpm --dir app/web install --frozen-lockfile
node app/web/build.mjs
uv run --frozen --no-dev python -m scripts.serve_product --credentials .local/workbench/credentials.json --init-credentials
uv run --frozen --no-dev python -m scripts.serve_product --credentials .local/workbench/credentials.json --data /运行目录/workbench.json
```

打开 `http://127.0.0.1:8018`，使用本机 `.local/workbench/credentials.json` 中对应角色的凭证连接。

需要持久化和记忆时，先设置 `DATABASE_URL` 并执行 `python -m scripts.init_database`，运行 Agent 时增加 `--durable --memory all --user-id <你的UUID>`。
