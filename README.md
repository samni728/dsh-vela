# DSH Novel

DSH Novel 是一个本地优先、Headless 优先的长篇小说自动写作系统，并以薄插件的形式接入 [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness)。

它不把整本小说和 Agent 聊天历史反复塞进上下文，而是将正文、章节合同、增量状态和运行记录保存在自己的 SQLite 中，按任务编译出有 token 硬预算的上下文。

> 当前版本：`0.1.0` MVP。已用 DeepSeek Harness `0.1.1-rc.2` 插件契约设计。DeepSeek Harness 仍在 Preview 阶段，因此 DSH 适配层会独立版本化，Novel Core 不依赖 Harness 内部实现。

## 当前能力

- 独立 Python Sidecar 和 SQLite 项目数据。
- 项目创建、状态查询、章节准备、写作、恢复与导出。
- 默认的 deterministic fake provider，可在不连接任何模型时完成 smoke test。
- OpenAI-compatible 本地模型 Provider，可接 LM Studio、llama.cpp 或兼容服务。
- 确定性正文纯净度、精确重复和基础质量检查。
- 章节 revision、Context Package、Chapter Delta 和 finalized 状态持久化。
- DeepSeek Harness health/capability handshake 和六个粗粒度工具。

当前还没有实现 Embedding、Rerank、多 Arc 图谱和百章自动长跑。这些已进入路线图，但不会在 MVP 中以空壳功能冒充完成。

## 架构

```text
DeepSeek Harness
  └─ dsh-novel-plugin（TypeScript 薄 Adapter）
       └─ HTTP / Protocol v1
            └─ dsh-novel-core（Python Sidecar）
                 ├─ Application workflow
                 ├─ Context Compiler
                 ├─ Quality gates
                 ├─ Model Providers
                 └─ SQLite project store
```

Harness 只负责对话、Agent 和 Tool 调度。SQLite、模型调用、章节事务和小说正史全部归 Sidecar 所有。即使没有 DeepSeek Harness，Sidecar 也能独立工作。

## 1. 启动 Novel Sidecar

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/samni728/dsh-vela.git
cd dsh-vela/backend
uv sync --extra dev
uv run dsh-novel serve
```

默认地址为 `http://127.0.0.1:17861`，默认使用可重现的 fake provider，数据保存在 `~/.dsh-novel`。

另开一个终端检查：

```bash
curl http://127.0.0.1:17861/health
```

### 连接本地模型

```bash
export DSH_NOVEL_MODEL_PROVIDER=openai_compatible
export DSH_NOVEL_MODEL_ENDPOINT=http://127.0.0.1:1234/v1
export DSH_NOVEL_MODEL_NAME=local-writer
# export DSH_NOVEL_MODEL_API_KEY=...   # 仅在你的服务需要时设置

uv run dsh-novel serve
```

其他可调配置：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `DSH_NOVEL_DATA_DIR` | `~/.dsh-novel` | SQLite 和项目数据目录 |
| `DSH_NOVEL_PORT` | `17861` | Sidecar 端口 |
| `DSH_NOVEL_MODEL_TIMEOUT` | `180` | 模型请求超时秒数 |
| `DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS` | `8192` | 单次最大输出 |
| `DSH_NOVEL_CONTEXT_TOKEN_BUDGET` | `20000` | 编译上下文硬预算 |
| `DSH_NOVEL_TOKEN` | 未设置 | 可选的 Sidecar Bearer token；Sidecar 和 Harness 进程需使用相同值 |

## 2. 先用 HTTP 跑通一章

```bash
curl -X POST http://127.0.0.1:17861/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "雾港档案",
    "premise": "一名档案员发现城市每晚都会忘记一个人",
    "target_chapters": 5
  }'
```

从返回结果取出 `project_id`，再执行：

```bash
curl -X POST \
  http://127.0.0.1:17861/api/v1/projects/<project_id>/chapters/1/run \
  -H 'Content-Type: application/json' \
  -d '{}'
```

## 3. 安装 DeepSeek Harness 插件

Sidecar 必须先保持运行。对于从源码运行的 DeepSeek Harness，将下面的 `<dsh-repo>` 换成 Harness checkout 路径。

### 方式 A：安装本地 checkout（开发时推荐）

```bash
cd <dsh-repo>
pnpm dsh plugin --profile novel add /absolute/path/to/dsh-vela
pnpm dsh --profile novel --dump-config
pnpm dsh --profile novel
```

如果你已经全局安装 `dsh`，去掉前面的 `pnpm`即可。

### 方式 B：从 GitHub 安装

```bash
dsh plugin --profile novel add github:samni728/dsh-vela
```

GitHub 安装直接使用仓库中随版本提交的已编译 Adapter，不需要在安装期间授权 `prepare` 构建脚本。在正式环境中建议锁定已验证的 commit：

```bash
dsh plugin --profile novel add github:samni728/dsh-vela#<commit>
```

也可在信任的本地 checkout 构建 tarball：

```bash
cd dsh-vela
pnpm install
pnpm pack
dsh plugin --profile novel add ./dsh-novel-plugin-0.1.0.tgz
```

### Adapter 配置

插件默认连接 `http://127.0.0.1:17861`。如需覆盖，在该 profile 的 `cordis.patch.yml` 中重述完整配置：

```yaml
- id: dsh-novel
  name: dsh-novel-plugin
  config:
    endpoint: http://127.0.0.1:17861
    tokenEnv: DSH_NOVEL_TOKEN
    requestTimeoutMs: 30000
    handshakeTimeoutMs: 5000
    maxRenderChars: 20000
    requireHandshake: true
```

如果设置 `DSH_NOVEL_TOKEN`，请在启动 Sidecar 和 DeepSeek Harness 的两个进程中提供相同值。Sidecar 将校验所有 `/api/*` 请求；`/health` 仍保留为不含敏感数据的本机存活检查。

## Harness 工具

| 工具 | 作用 |
|---|---|
| `novel_project_create` | 创建小说项目 |
| `novel_project_status` | 查询项目、章节和进度 |
| `novel_chapter_run` | 准备、生成、检查并定稿一章 |
| `novel_run_status` | 查询一次运行 |
| `novel_run_resume` | 恢复可重试或暂停的运行 |
| `novel_manuscript_export` | 导出已定稿正文 |

## 开发与测试

```bash
# Python
cd backend
uv sync --extra dev
uv run pytest
uv run ruff check .

# TypeScript Adapter（仓库根目录）
cd ..
pnpm install
pnpm check
pnpm test
pnpm build
pnpm pack
```

## 项目边界

- 不 fork 或修改 DeepSeek Harness Core。
- 不依赖 Harness Session 数据库。
- 不依赖 Vela 运行。
- Vela 只用于行为参考和失败样本，不复制其 GPL 代码。
- 完整正文审查默认使用本地 Provider；外部 Judge 未启用且不是 MVP 前提。

## 文档

- [最终架构与开发计划](Docs/DeepSeek-Harness-Novel-Plugin-Development-Plan.md)
- [长篇上下文与增量一致性架构](Docs/Long-Novel-Context-and-Incremental-Consistency-Architecture.md)
- [Vela 参考与复用审计](Docs/Vela-Reference-and-Reuse-Audit.md)

## 许可证

DSH Novel 使用 [MIT License](LICENSE)。Vela 是独立的 GPL-3.0 第三方参考项目，不属于本仓库。
