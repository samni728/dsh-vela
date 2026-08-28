# DSH Novel

DSH Novel 是一个本地优先、Headless 优先的长篇小说自动写作系统，并以薄插件的形式接入 [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness)。

它不把整本小说和 Agent 聊天历史反复塞进上下文，而是将正文、章节合同、增量状态和运行记录保存在自己的 SQLite 中，按任务编译出有 token 硬预算的上下文。

> 当前版本：插件 `0.3.0` / Sidecar core `0.5.0`。已用 DeepSeek Harness `0.1.1-rc.2` 插件契约设计。DeepSeek Harness 仍在 Preview 阶段，因此 DSH 适配层会独立版本化，Novel Core 不依赖 Harness 内部实现。

## 当前能力

- 独立 Python Sidecar 和 SQLite 项目数据。
- 项目创建、状态查询、章节准备、写作、恢复与导出。
- 默认的 deterministic fake provider，可在不连接任何模型时完成 smoke test。
- OpenAI-compatible 本地模型 Provider，可接 LM Studio、llama.cpp 或兼容服务。
- 审稿系统：八条确定性质量规则 + 可选的 LLM 审稿 agent（fail-open）+ 分数阈值循环审改 + 蓝图感知审查，见下文「审稿系统」。
- 提纲 agent：从书名/前提/硬性规则一键生成全书结构化大纲（story spine + 章节合同），见下文「全自动模式」。
- 全自动模式：`POST /api/v1/auto` 一步建项目、生成大纲并自动逐章长跑，失败自愈断点续跑，完成后自动产出 manuscript.md 与报告。
- 管理与创作解耦（0.5.0）：零正文管理面端点 `GET /api/v1/projects/{id}/pipeline`、按项目生效的策略对象（policy）、跳过续跑与补写队列、带审稿意见的定向修复，见下文「管理与创作解耦（Master Agent 协议）」。
- 章节 revision、Context Package、Chapter Delta 和 finalized 状态持久化。
- DeepSeek Harness health/capability handshake 和十二个粗粒度工具（含 0.4.0 的提纲、全自动与 autorun 工具，以及 0.5.0 的零正文 pipeline 状态工具与 policy 覆盖参数），见下文「Master Agent 协议」。

当前还没有实现 Embedding、Rerank 和多 Arc 图谱。这些已进入路线图，但不会在 MVP 中以空壳功能冒充完成。

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

其他可调配置（环境变量均可用对应 snake_case key 写入 `config.yml`，见下节「配置文件」；优先级为 环境变量 > config.yml > 内置默认值）：

| 环境变量 | 默认值 | 用途 | 对应 config.yml key |
|---|---|---|---|
| `DSH_NOVEL_DATA_DIR` | `~/.dsh-novel` | SQLite 和项目数据目录 | `data_dir` |
| `DSH_NOVEL_PORT` | `17861` | Sidecar 端口 | `port` |
| `DSH_NOVEL_MODEL_TIMEOUT` | `180` | 模型请求超时秒数 | `model_timeout_seconds` |
| `DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS` | `8192` | 单次最大输出 | `model_max_output_tokens` |
| `DSH_NOVEL_CONTEXT_TOKEN_BUDGET` | `20000` | 编译上下文硬预算 | `context_token_budget` |
| `DSH_NOVEL_REVIEW_ENABLED` | `true` | 是否启用 LLM 审稿 agent | `review_enabled` |
| `DSH_NOVEL_REVIEW_TIMEOUT` | `120` | LLM 审稿超时秒数（超时按 fail-open 处理） | `review_timeout_seconds` |
| `DSH_NOVEL_SCORE_THRESHOLD` | `8.0` | 审稿总分阈值（三 维度最低分低于它即阻塞重写） | `score_threshold` |
| `DSH_NOVEL_MAX_REVISIONS` | `3` | 单章总尝试上限（resume 重试与阈值循环共用） | `max_revisions` |
| `DSH_NOVEL_OUTLINE_TIMEOUT` | `180` | 大纲生成请求超时秒数 | `outline_timeout_seconds` |
| `DSH_NOVEL_TOKEN` | 未设置 | 可选的 Sidecar Bearer token；Sidecar 和 Harness 进程需使用相同值 | `auth_token` |

### 配置文件

Sidecar 支持把配置写入 YAML 文件，三层合并规则为：**环境变量 > config.yml > 内置默认值**。某个字段设了环境变量就用环境变量；否则文件里有就用文件；否则用内置默认。文件不存在时静默跳过，行为与不配置完全一致。

- 默认路径：`~/.dsh-novel/config.yml`。
- 可用环境变量 `DSH_NOVEL_CONFIG` 覆盖文件自身路径（例如自定义了 `DSH_NOVEL_DATA_DIR` 时，把配置也放到同一目录，避免鸡生蛋问题）。
- 随时执行 `uv run dsh-novel config-path` 可打印当前生效的配置文件路径、是否存在及来源。
- `host` 不开放配置，固定为 `127.0.0.1`。
- 错误处理是 fail-loud 的：YAML 解析失败、顶层不是映射、出现未知 key、字段类型不符（如 `port` 配成字符串）都会让 Sidecar 启动时直接报错，并列出全部合法 key 与出错字段。

合法 key 与 Settings 字段同名（snake_case）：

| config.yml key | 类型 | 对应环境变量 | 默认值 |
|---|---|---|---|
| `data_dir` | 字符串路径 | `DSH_NOVEL_DATA_DIR` | `~/.dsh-novel` |
| `model_provider` | 字符串（`fake` / `openai_compatible`） | `DSH_NOVEL_MODEL_PROVIDER` | `fake` |
| `model_endpoint` | 字符串 | `DSH_NOVEL_MODEL_ENDPOINT` | `http://127.0.0.1:1234/v1` |
| `model_name` | 字符串 | `DSH_NOVEL_MODEL_NAME` | `local-writer` |
| `model_api_key` | 字符串或 null | `DSH_NOVEL_MODEL_API_KEY` | 未设置 |
| `model_timeout_seconds` | 数字 | `DSH_NOVEL_MODEL_TIMEOUT` | `180` |
| `model_max_output_tokens` | 整数 | `DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS` | `8192` |
| `context_token_budget` | 整数 | `DSH_NOVEL_CONTEXT_TOKEN_BUDGET` | `20000` |
| `review_enabled` | 布尔 | `DSH_NOVEL_REVIEW_ENABLED` | `true` |
| `review_timeout_seconds` | 数字 | `DSH_NOVEL_REVIEW_TIMEOUT` | `120` |
| `score_threshold` | 数字 | `DSH_NOVEL_SCORE_THRESHOLD` | `8.0` |
| `max_revisions` | 整数 | `DSH_NOVEL_MAX_REVISIONS` | `3` |
| `outline_timeout_seconds` | 数字 | `DSH_NOVEL_OUTLINE_TIMEOUT` | `180` |
| `auth_token` | 字符串或 null | `DSH_NOVEL_TOKEN` | 未设置 |
| `port` | 整数 | `DSH_NOVEL_PORT` | `17861` |

完整示例（所有 key 均可省略，省略时回退内置默认值）：

```yaml
# ~/.dsh-novel/config.yml
data_dir: ~/.dsh-novel        # SQLite 与项目数据目录
port: 17861                   # Sidecar 监听端口
context_token_budget: 20000   # 编译上下文硬预算
model_timeout_seconds: 180    # 模型请求超时秒数
model_max_output_tokens: 8192 # 单次最大输出
review_enabled: true          # LLM 审稿 agent 开关
review_timeout_seconds: 120   # LLM 审稿超时秒数（fail-open）
score_threshold: 8.0          # 审稿总分阈值（低于即阻塞重写）
max_revisions: 3              # 单章总尝试上限
outline_timeout_seconds: 180  # 大纲生成请求超时秒数
# auth_token: change-me       # 可选 Bearer token，与 Harness 侧 DSH_NOVEL_TOKEN 保持一致

# 切换到 OpenAI-compatible 本地模型（LM Studio / llama.cpp 等）时取消注释：
# model_provider: openai_compatible
# model_endpoint: http://127.0.0.1:1234/v1
# model_name: local-writer
# model_api_key: sk-...       # 仅当你的推理服务需要鉴权时设置
```

> 安全提示：如果在 config.yml 中写入 `model_api_key` 或 `auth_token`，请把文件权限收紧为仅本人可读：`chmod 600 ~/.dsh-novel/config.yml`。也可以继续只用环境变量传递这两个敏感值，完全不落盘。

### 代码目录迁移

Sidecar 的代码目录与小说数据目录彼此独立。`scripts/novel-agent.py` 和
`scripts/start-novel-server.sh` 会从脚本自身定位仓库，并默认读取
`~/.dsh-novel/config.yml`；因此仓库可以移动到 `~/Developer` 等任意位置，
无需让 `novel-data/` 与 `dsh-vela/` 保持同级。

迁移 checkout 后只需在新目录重建虚拟环境（venv 中的 shebang 和 editable
安装都包含绝对路径）：

```bash
cd /new/path/dsh-vela/backend
uv sync --extra dev
python ../scripts/novel-agent.py paths
python ../scripts/novel-agent.py ensure-server
```

路径覆盖优先级如下：

- `DSH_NOVEL_REPO`：显式指定代码仓库；一般无需设置。
- `DSH_NOVEL_CONFIG`：显式指定配置文件；默认 `~/.dsh-novel/config.yml`。
- `DSH_NOVEL_DATA_DIR`：覆盖配置里的 `data_dir`，用于临时切换数据集。
- `DSH_NOVEL_WORKSPACE`：仅保留给旧调用兼容，不再推荐使用。

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

Sidecar 推荐先保持运行。使用默认 `handshakeMode: lazy` 时，插件挂载不要求 Sidecar 已在线——未在线时工具调用会返回可重试的错误提示，启动 Sidecar 后即可正常使用。对于从源码运行的 DeepSeek Harness，将下面的 `<dsh-repo>` 换成 Harness checkout 路径。

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
dsh plugin --profile novel add ./dsh-novel-plugin-0.3.0.tgz
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
    handshakeMode: lazy
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `endpoint` | `http://127.0.0.1:17861` | Sidecar 地址 |
| `token` / `tokenEnv` | `DSH_NOVEL_TOKEN` | Bearer token，二者互斥，只能配置其一 |
| `requestTimeoutMs` | `30000` | 单次工具调用超时（毫秒） |
| `handshakeTimeoutMs` | `5000` | health/capability 握手超时（毫秒） |
| `maxRenderChars` | `20000` | 模型可见渲染输出的字符上限 |
| `handshakeMode` | `lazy` | 握手时机：`lazy` / `boot` / `off` |

`handshakeMode` 决定适配器何时与 Sidecar 做 capability 握手：

- `lazy`（默认）：插件挂载期零网络请求，Sidecar 缺失不会导致 Harness profile 引导失败。首次工具调用前执行一次握手；若 Sidecar 未启动，该次调用返回结构化错误信封 `ADAPTER_SIDECAR_UNAVAILABLE`（`retryable: true`），并附可操作指引（`uv run dsh-novel serve`）。失败不会被缓存，下次调用会自动重试握手。
- `boot`：旧的严格行为——挂载期握手，Sidecar 不可达、协议不兼容或缺能力时直接抛错、引导失败。适合希望 fail-loud 尽早暴露问题的部署。
- `off`：完全不握手，工具调用直接访问 Sidecar。

旧字段 `requireHandshake` 已废弃但仍兼容：仅当未设置 `handshakeMode` 时，`requireHandshake: false` 等价于 `handshakeMode: off`，其余取值（含缺省）等价于新的默认 `lazy`；两者同时设置时以 `handshakeMode` 为准。

如果设置 `DSH_NOVEL_TOKEN`，请在启动 Sidecar 和 DeepSeek Harness 的两个进程中提供相同值。Sidecar 将校验所有 `/api/*` 请求；`/health` 仍保留为不含敏感数据的本机存活检查。

## Harness 工具

| 工具 | 作用 |
|---|---|
| `novel_project_create` | 创建小说项目 |
| `novel_project_status` | 查询项目、章节和进度 |
| `novel_outline_generate` | 为已有项目生成全书结构化大纲（story spine + 章节合同） |
| `novel_chapter_run` | 准备、生成、检查并定稿一章 |
| `novel_run_status` | 查询一次运行 |
| `novel_run_resume` | 恢复可重试或暂停的运行 |
| `novel_manuscript_export` | 导出已定稿正文 |
| `novel_auto_create` | 一键全自动：建项 + 大纲 + 启动逐章长跑（对应 `POST /api/v1/auto`）；可选 `policy` 覆盖（省略字段不发送） |
| `novel_autorun_start` | 启动/续跑服务器端自动长跑，可选 `from_chapter`/`to_chapter` 与 `policy` 覆盖；优先重试补写队列章节 |
| `novel_autorun_status` | 查询 autorun 进度（state、当前章、已完成数、分数、last_error） |
| `novel_pipeline_status` | **管理面零正文快照**（0.5.0）：仅返回分数与状态，不含正文——state、生效 policy、每章分数/状态/字数、补写队列与 totals |
| `novel_report` | 读取长跑完成后自动生成的 README 报告文本 |

## 审稿系统

每章在 VALIDATING 阶段先过确定性规则，全部通过后、COMMITTING 之前再执行可选的 LLM 审稿。任何 blocker/error 都会把运行置为 `QUALITY_BLOCKED`（可 resume 触发重写）；每条 issue 带 `source` 字段（`rule` = 确定性规则，`llm` = LLM 审稿）。

### 八条确定性规则（quality.py）

| 规则 | 严重级 | 判定标准 | 抓的是什么 |
|---|---|---|---|
| `empty_content` | blocker | 正文为空 | 空章节 |
| `prompt_pollution` | blocker | 出现 `<think>` 等标签、系统提示词痕迹、代码围栏 | 提示词污染 |
| `exact_paragraph_repeat` | blocker | 同章内归一化后 ≥24 字的段落完全重复 | 同章复读 |
| `near_paragraph_repeat` | error | 与最近章节段落 shingle 相似度 ≥0.88 | 跨章高相似复述 |
| `dense_short_line_repeat` | blocker | 归一化后 6~23 字的短行按文本分组，存在相邻第 1 次与第 3 次出现间隔 ≤80 个短行槽 | 短对话行三连循环（如同一场景循环写三遍，单行均不足 24 字的老漏网 case） |
| `cross_chapter_exact_repeat` | blocker | 当前章归一化 ≥40 字的段落与任何 recent chapter 中段落完全相同 | 整段原样过章复制 |
| `truncated_ending` | blocker | 最后一个非空段不以句末标点（。！？…"'」』!?.）收尾 | 半句截断的残缺结尾 |
| `required_event_keyword_missing` | warning | 合同事件的全部关键词（按事件描述切词取 ≥2 字词元，中文以字符二元词元近似）都未在正文出现 | 疑似漏写合同事件（不阻塞，供 LLM 与人参考） |

> 表中前四行为既有规则；其后三行为 0.3.0 新增；`required_event_keyword_missing` 为 0.4.0 新增。`dense_short_line_repeat` 用"短行槽"计距，散布全章的修辞性反复（如同句每隔 100 行出现一次）不会误伤。

### LLM 审稿 agent（可选）

- **时机**：确定性门全部通过后、COMMITTING 之前，stage 显示为 `REVIEWING`。
- **请求**：结构化审稿请求 = 全书蓝图（story spine 摘要）+ 章节合同全文 + 待审正文 + 最近章节 digest + 本次草稿的 attempt 序号；provider 通过 `review_chapter(request) -> ReviewVerdict` 方法调用模型，要求输出严格 JSON：`{"verdict":"pass|blocked","issues":[{"severity":"blocker|warning","type":...,"description":...}],"scores":{"contract_adherence":0-10,"era_authenticity":0-10,"flow":0-10}}`。
- **蓝图核对清单**（0.4.0 起的审稿 system 提示）：① 本章是否完成合同 purpose、required_events 是否覆盖、hooks 种植/推进是否符合规划、章末衔接是否落实；② 人物关系与性格是否与已定稿章节及蓝图一致、本章反转是否落在蓝图规划的位置；③ 年代质感；④ 叙事流畅。
- **解析**：解析前先剥离 `<think>...</think>` 思考块；JSON 解析失败或字段非法时降级为一条 warning issue（`review_invalid_response`），不阻塞提交。
- **分数阈值循环**（0.4.0）：总分 overall = 三个维度的最低分。`overall < score_threshold`（默认 8.0）或 verdict=blocked 都视为 blocking：低分会产生一条 `score_below_threshold` blocker（description 带三个维度分数），走与确定性规则相同的 `QUALITY_BLOCKED → resume 重写` 链路。attempt 达到 `max_revisions`（默认 3，即单章总尝试上限，与 resume 重试预算统一）仍不达标时运行置为 `PAUSED`，error_message 注明最终分数。
- **审稿历史**：每次审稿的 verdict 记录追加进 runs 表的 `review_json` 列（schema migration，向后兼容旧库）；`GET /api/v1/runs/{id}` 与项目状态里的 `recent_runs` 均返回 `review` 字段（按 attempt 排列的完整历史）。
- **fail-open**：审稿超时或异常只记一条 warning issue（`review_unavailable`）后继续提交，绝不卡死长跑。超时由独立参数控制，并有服务侧硬性 wall-clock 兜底。
- **开关**：`review_enabled: true/false`（默认开启），`review_timeout_seconds`（默认 120）。关闭或 provider 不支持审稿时自动退回纯确定性管线。
- **能力上报**：`/api/v1/capabilities` 的 `optional_capabilities.llm_review` 反映是否启用。
- fake provider 返回固定 pass verdict（三项 scores 各 8），离线 smoke 不受影响；测试可设 `FAKE_REVIEW_SCORES="6.5,9"` 让第 1 次草稿 6.5 分、重写后 9 分，脚本化验证阈值循环。

## 全自动模式

0.4.0 起提供服务器端编排器（autorun）：一条 daemon 线程按章节顺序自动完成「生成大纲 → 逐章写作 → 质量门 → LLM 审稿 → 阈值循环重写 → 定稿」，全程自愈，无需人工介入。

### 一键全入口

```bash
curl -X POST http://127.0.0.1:17861/api/v1/auto \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "雾港档案",
    "premise": "一名档案员发现城市每晚都会忘记一个人",
    "target_chapters": 10,
    "hard_rules": ["每章必须推进一次调查"],
    "target_words": 4000,
    "idempotency_key": "fog-port-book-v1"
  }'
```

该入口只做一次幂等提交并立即返回：建项目 → 启动后台串行 autorun；提纲生成由 autorun 在后台完成。返回体含 `project_id`、`state`、`reused` 与 `next_action=poll_status`。相同 `idempotency_key`（旧客户端未提供时则使用请求内容指纹）的重试始终返回同一个项目，不会复制任务。

### 提纲 agent

```bash
curl -X POST http://127.0.0.1:17861/api/v1/projects/<project_id>/outline \
  -H 'Content-Type: application/json' -d '{"target_words": 4000}'
```

用 LLM 从项目的 title/premise/hard_rules/target_chapters 生成结构化大纲：`story_spine` 对象 + `chapters` 数组（chapter_number 连续 1..N、title、purpose、required_events、hooks_to_plant、hooks_to_advance、target_words），写入 `projects.story_spine` 并 upsert `chapter_contracts`。解析前剥离 `<think>`，随后严格 JSON 校验（章节号连续、字段类型），失败自动重试一次，仍失败返回 CONFIG_INVALID 信封并附解析错误详情（fail-loud，项目仍可手动跑）。任一章已 COMMITTED 时拒绝重生成（VERSION_CONFLICT）。

### autorun / pipeline / report 端点

| 端点 | 作用 |
|---|---|
| `POST /api/v1/projects/{id}/autorun` | 启动自动长跑，body 可选 `{"from_chapter":1,"to_chapter":N,"policy":{...}}`；整个 Sidecar 全局只允许一个 autorun，已有任何项目运行时返回 409，并给出 `active_project_id` |
| `GET /api/v1/projects/{id}/autorun` | 查询进度：`{"state":"idle\|running\|completed\|completed_with_rework\|failed","current_chapter","chapters_committed","rework_queue","scores":[{chapter,scores…,verdict}],"last_error"}` |
| `GET /api/v1/projects/{id}/pipeline` | **管理面零正文快照**（0.5.0）：state、outline_generated、生效 policy、每章分数/状态/字数、补写队列与 totals，绝不返回任何正文，见下文「管理与创作解耦」 |
| `GET /api/v1/projects/{id}/report` | 返回自动生成的 README.md 报告内容（未生成时 404） |

每章流程：合同缺失则用 LLM 单章补齐（失败重试 1 次）→ 复用 run_chapter 内部逻辑（质量门 + 审稿 + 阈值循环）→ `FAILED_RETRYABLE` 自动 resume（退避 2s/5s/10s，最多 policy 的 `max_revisions` 次）→ `QUALITY_BLOCKED` 同样自动 resume → 尝试耗尽后按策略处理：默认 `skip_continue` 把该章记入补写队列并继续下一章；`pause` 则停在 failed 并记录断点（`failed_at_chapter`）。全部章节走完后 state 为 `completed_with_rework`（有未提交章节）或 `completed`。连续 ≥3 章因 `MODEL_UNAVAILABLE` 失败时触发系统性故障保护：无视 skip_continue 直接置为 failed（模型都不可达时继续没有意义）。全部完成后自动把 markdown 全稿写入 `data_dir/projects/<id>/manuscript.md`，并在同目录生成 README.md（项目元数据 + 每章分数表 + 补写队列 + 质量事件摘要）。

**自愈续跑**：中途模型不可达或进程重启后，重新 POST autorun 会优先重试补写队列中「已尝试但未提交」的章节，再继续写 committed+1 之后的新章，已定稿章节不动。

串行保证：整个 Sidecar 只有一条 autorun 通道；outline、写稿、审稿、重写、状态抽取共享 `max_concurrency=1` 的模型执行锁。OpenAI-compatible provider 另用 `data_dir/runtime/model-request.lock` 做跨进程互斥，避免 Sidecar、CLI、脚本同时请求本地模型。同一章节还有独立执行锁，重复 `run` 返回忙状态而不排队制造第二份草稿。SQLite 每次操作使用独立且必定关闭的连接。

### Master Agent 用法（Harness 工具）

在 DeepSeek Harness 中，Master Agent 不需要手动拼装 HTTP 请求。0.5.0 起推荐循环以零正文的 `novel_pipeline_status` 为唯一轮询入口：

```text
novel_auto_create(policy?) → 轮询 novel_pipeline_status → completed_with_rework 时对 rework 章节重发 novel_autorun_start(policy?) → novel_report
```

1. **只提交一次**：调用 `novel_auto_create {title, premise, target_chapters, target_words?, hard_rules?, policy?, idempotency_key?}`，立即取得 `project_id`。同一意图的重试必须复用同一个 key；返回 `state=running` 后禁止再调用 auto/start/chapter/resume。
2. **轮询至终态**：循环调用 `novel_pipeline_status {project_id}`（并发安全、只读、零正文），直到返回的 `state` 变为 `completed`、`completed_with_rework` 或 `failed`；轮询间隔建议 ≥5 秒，期间可随时读 `totals`、每章分数与补写队列汇报进度。
3. **补写重发**：`state=completed_with_rework` 时，对 `rework_queue` 中的章节直接重发 `novel_autorun_start {project_id, policy?}`——autorun 会优先重试补写队列章节、再继续新章，已定稿章节不动；可在同一次调用里调整 policy（如降低 `score_threshold`）。随后回到第 2 步继续轮询。
4. **收取成果**：`state=completed` 后调用 `novel_report {project_id}` 获取自动生成的 README 报告文本（项目元数据 + 每章分数表 + 质量事件摘要）；需要正文时再用 `novel_manuscript_export`（人类动作，见下文「Master 不读不改正文」）。

**状态优先，不盲重试**：任何 timeout/断连后，Master Agent 必须先用已知 `project_id` 查询 `novel_pipeline_status`；若 auto 响应丢失，可用同一 `idempotency_key` 重发一次以取回同一项目。`running` 只轮询；`completed` 导出；`completed_with_rework` 才可重发 `novel_autorun_start`；`failed` 或 Run 的 `FAILED_RETRYABLE/QUALITY_BLOCKED` 在修复故障后才调用 start/resume。`ORCHESTRATOR_BUSY` 的动作永远是查询 `active_project_id`，不是继续提交。进程重启遗留的 `RUNNING` 会自动改为 `FAILED_RETRYABLE/PROCESS_INTERRUPTED`，不会永久假运行。

### config 新 key（0.4.0）

| key | 默认值 | 说明 |
|---|---|---|
| `score_threshold` | `8.0` | 审稿总分阈值（overall = 三维度最低分），低于即阻塞重写；0.5.0 起可作为项目 policy 覆盖 |
| `max_revisions` | `3` | 单章总尝试上限，resume 重试与阈值循环共用；0.5.0 起可作为项目 policy 覆盖 |
| `outline_timeout_seconds` | `180` | 大纲生成请求的独立超时 |

三者均可被对应环境变量覆盖（见上文配置表），优先级为 环境变量 > config.yml > 内置默认值。

## 管理与创作解耦（Master Agent 协议，0.5.0）

0.5.0 把 Sidecar 明确劈成两个平面：**创作面**（写正文、审正文、存正文）与**管理面**（只读数字与状态）。Master Agent（Harness 里的对话 Agent）只消费管理面；它永远不需要、也不应该拿到小说正文。

### 职责表

| 平面 | 端点 / 载体 | Harness 工具 | 返回什么 | 给谁用 |
|---|---|---|---|---|
| 管理面 | `GET /api/v1/projects/{id}/pipeline` | `novel_pipeline_status` | state、outline_generated、生效 policy、每章 status/attempt/分数/verdict/issue 计数/字数、rework_queue、totals —— **只有数字与状态** | Master Agent 轮询与决策 |
| 管理面 | `GET /api/v1/projects/{id}/autorun` | `novel_autorun_status` | 进度 state、当前章、已完成数、补写队列、last_error | Master Agent 轮询与决策 |
| 创作面 | `POST .../chapters/{n}/run`、`GET /api/v1/runs/{id}`、`/export`、`/report` | `novel_chapter_run`、`novel_run_status`、`novel_manuscript_export`、`novel_report` | 正文、digest、上下文、报告全文 | 人类审阅 / 导出流程，不进 Agent 上下文 |

管理面的启动类工具（`novel_auto_create`、`novel_autorun_start`）只发送指令与可选 policy，不携带也不返回正文。

### 管理面零正文保证

`GET /pipeline` 的响应在构造后强制过一遍零正文校验（`assert_management_payload`）：

- 序列化后的 JSON **不含 `content` / `digest` / `prose` 键**（递归检查所有层级）；
- **任何字符串值长度 ≤200 字符**（标题远短于该上限）；
- 正文字数以 `word_count` 数字表达（由已定稿正文字符统计而来），而不是文本片段。

违反即抛错拒绝返回，因此「Agent 偷看正文」在协议层不可能发生。

### 策略对象（policy）

每个项目有一份生效策略，合并优先级为：**请求 policy > 项目已存 policy（`projects.policy_json`，schema migration v3，向后兼容）> settings 默认**。首次设置即持久化。

| 字段 | 类型 / 取值 | 默认 | 说明 |
|---|---|---|---|
| `score_threshold` | number 0..10 | settings（默认 8.0） | 审稿总分阈值，低于即阻塞重写 |
| `max_revisions` | int ≥1 | settings（默认 3） | 单章总尝试上限（resume 重试与阈值循环共用） |
| `target_words` | int 100..20000 | 4000 | 每章目标字数；`/auto` 未显式给 target_words 时提纲 agent 使用它 |
| `on_chapter_failure` | `skip_continue` \| `pause` | `skip_continue` | 章节尝试耗尽后跳过续跑还是整跑暂停 |

`POST /api/v1/auto` 与 `POST /api/v1/projects/{id}/autorun` 请求体均可选携带 `policy` 字段做部分覆盖；service / orchestrator / reviewer 全部改读生效策略，不再直接读全局 settings。

### 补写队列（rework queue）

- 定义：**已尝试但未提交**的章节集合，动态推导自 runs/chapters 状态，不需要新表。
- `skip_continue`（默认）：某章尝试耗尽即进入补写队列，orchestrator 继续下一章；全部走完后 state = `completed_with_rework`（有未提交）或 `completed`。
- `pause`：沿用旧行为，失败即停，state = `failed` 并记录 `failed_at_chapter`。
- 系统性故障保护：连续 ≥3 章因 `MODEL_UNAVAILABLE` 失败 → 无视 skip_continue 直接 `failed`（模型都不可达，继续没有意义），`last_error` 说明原因。
- 续跑语义：重新 POST autorun 会**优先重试补写队列中的章节**，再写 committed+1 之后的新章。
- 报告：自动生成的 README.md 新增「补写队列」小节，列出 rework 章节与最近失败原因；每章分数表保持不变。

### 定向修复（带反馈的重写）

被拦截（质量门 blocker 或分数不达标）的章节在 resume 重写时，会把最近一次拦截的 blocking issues（type/description/severity）与上次分数组装成 `revision_feedback` + `previous_scores` 注入 `WriterRequest`：

- openai_compatible provider 在 system/user 提示中追加「上一稿审稿意见，本次必须针对性解决」段落；
- fake provider 把收到的反馈以 `[feedback:类型列表]` 标记写入正文末尾，供测试断言；
- 无拦截的直接成功路径不带任何反馈字段。

### 推荐轮询循环（伪代码）

Master Agent 通过 Harness 工具走同一条循环；以下伪代码与「Master Agent 用法」小节一一对应：

```text
# 启动（一次性）；policy 可选，省略字段由适配器丢弃、不发送
res = novel_auto_create {title, premise, target_chapters, policy?}
project_id = res.result.project_id

# 轮询（建议间隔 ≥5s；novel_pipeline_status 并发安全、只读、零正文）
loop:
  p = novel_pipeline_status {project_id}
  case p.result.state:
    "running"                -> 汇报 p.result.totals 后继续轮询
    "completed"              -> break  # 全部定稿
    "completed_with_rework"  -> break  # 有补写队列，见下
    "failed"                 -> 用 novel_autorun_status 查 last_error；
                                模型类错误修复后重发 novel_autorun_start 即断点续跑

# completed_with_rework：对 rework 章节重发 autorun（优先重试补写队列，
# 已定稿章节不动），可顺带调整 policy，然后回到上面的轮询
if p.result.rework_queue 非空:
    novel_autorun_start {project_id, policy?}   # 例如降低 score_threshold

# 收尾（人类动作）
novel_report {project_id}              # 自动生成的报告文本
需要正文时: novel_manuscript_export {project_id}
```

**Master 不读不改正文**：整个循环里 Master Agent 只见到数字与状态——它启动运行、读取分数与队列、调整 policy，但从不读取、也从不改写任何章节正文；正文只在人类明确要求导出/报告时才离开 Sidecar。

## 方案 A：共享端点流水线调度（同模型零争抢）

**何时启用**：当 Master Agent 与写作 Sidecar **共用同一个本地推理端点**时（典型场景：Master 也配置了写作用的那个本地模型）。此时并发会让两边的生成请求在推理服务上排队互踩，表现为明显变慢。解法不是并发而是**严格串行**——流水线。

**判据**：比较 Master 模型端点与 Sidecar `model_endpoint`（`GET /api/v1/capabilities`）。**端点相同**→同模型共用资源，必须走流水线；**端点不同**→互不干扰，可继续用异步长跑。

**流水线语义**（同步逐章，两阶段严格交替）：

```text
for 章 in 1..N:
  ① 写作阶段: novel_chapter_run(第N章)     ← 阻塞到该章定稿;此间 Master 模型零请求
     起草 → 规则审查 → LLM 审稿 → 分数阈值 → 修复定稿(全部在 Sidecar 内,写作模型独占)
  ② 判定阶段: Master 读该章分数/问题计数     ← 此间写作模型空闲
     达标 → 下一章;不达标且可救 → 重发该章;不可救 → 记入补写清单
完成后: Master 汇总全部章节分数,输出评分
```

**为什么这样就不争抢**：Agent 循环的天然时序是「模型生成→工具执行→模型再生成」。`novel_chapter_run` 是同步调用，工具执行期间 Master 模型不发任何请求——写作阶段与判定阶段在时间上互斥，共享端点的利用率从"互相等待"变成"轮流独占"。

**落地方式（二选一）**：

1. **Master 工具循环**（推荐，任何模型都可当 Master）：判据命中同端点后，Master 用 `novel_chapter_run` 逐章调用代替 `novel_auto_create`，每章收到结果后判定再进下一章。伪代码：

```text
if capabilities.model_endpoint == 本会话模型端点:
    for n in 1..target_chapters:
        res = novel_chapter_run {project_id, chapter_number: n}
        s = novel_run_status {run_id}      # 读分数与 issue 计数
        if s.result.overall < threshold and s.result.retryable:
            novel_run_resume {run_id}       # 同章重写,仍在写作阶段
        elif s.result.overall < threshold:
            record_rework(n)                # 进补写清单,继续下一章
        # 达标 → 直接进入下一章
else:
    novel_auto_create {...}                 # 端点不同,并发长跑
```

2. **流水线驱动脚本**（独立运行，不依赖 Agent 纪律）：`scripts/pipeline_loop.py <project_id> [from] [to]`——严格串行、客户端断连转服务端监视、失败自动恢复，每章打印分数。共享端点时由脚本作"写入泵"，Master（人或任何模型）在脚本结束后统一评分：

```bash
DSH_NOVEL_ENDPOINT=http://127.0.0.1:17861 python3 scripts/pipeline_loop.py prj_xxxx
```

**边界提醒**：流水线解决的是"Master 自动调度"的争抢；若用户**主动聊天**占用 Master 模型，争抢仍会发生——生成期间不催、不聊，是共享端点模式的使用纪律。

## Git 版本管理

仓库已建立标签化版本管理，随时可回滚、备份或建分支：

```bash
# 查看所有版本点与分支
git tag -l && git branch -a

# 回滚到某个稳定版本(先备份当前工作再回)
git stash                        # 有未提交改动时先存起来
git checkout v0.5.0              # 回到该版本点的代码

# 建分支做实验(推荐,不污染 main)
git checkout -b experiment-xxx   # 基于当前 main 新建
git push origin experiment-xxx   # 推到远端

# 打备份标签
git tag backup-2026-08-25        # 当前状态存档
git push origin --tags

# 查看历史
git log --oneline --graph
```

**版本约定**：`main` 为稳定主线；每轮重大特性合入后打语义化标签（`v0.5.0` 为"管理与创作解耦 + 懒迁移修复"稳定点）；实验性改动一律走分支，验证后再合回 main 并打新标签。

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
- 审稿系统的确定性规则与可选 LLM 审稿都默认使用本地 Provider；外部 Judge 未启用且不是前提。

## 文档

- [最终架构与开发计划](Docs/DeepSeek-Harness-Novel-Plugin-Development-Plan.md)
- [长篇上下文与增量一致性架构](Docs/Long-Novel-Context-and-Incremental-Consistency-Architecture.md)
- [Vela 参考与复用审计](Docs/Vela-Reference-and-Reuse-Audit.md)

## 许可证

DSH Novel 使用 [MIT License](LICENSE)。Vela 是独立的 GPL-3.0 第三方参考项目，不属于本仓库。
