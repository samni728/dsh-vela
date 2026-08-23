# DSH Novel：独立小说插件最终方案与开发计划

> 文档角色：项目唯一总方案与实施基线  
> 状态：定稿，可进入 MVP 实现  
> 版本：1.0  
> 日期：2026-08-22  
> 独立项目根目录：`/Users/samni/Desktop/开发项目/dsh-vela`  
> 产品工作名：`DSH Novel`（代码包暂用 `dsh-novel`）

配套文档：

- [长篇小说上下文与增量一致性架构](./Long-Novel-Context-and-Incremental-Consistency-Architecture.md)：上下文、状态、审校和修复的详细技术规范。
- [Vela 参考与复用审计](./Vela-Reference-and-Reuse-Audit.md)：参考范围、禁止耦合项、许可证与可借鉴能力。

---

## 1. 最终产品定义

DSH Novel 是一个本地优先、Headless 优先、面向长篇小说自动生产的独立项目。

用户只需要提供题材、目标、章节数量和必要约束，系统负责完成：

```text
全书规划
→ Story Arc 与章节合同
→ 有界上下文编译
→ 本地模型写稿
→ 确定性检查
→ 必要时模型审稿
→ 局部 Patch 修复
→ 定稿与状态增量提交
→ 下一章
→ 全书导出与运行报告
```

产品不以复杂 UI 为中心。核心交付物是：

- 高质量定稿正文；
- 可恢复的自动运行状态；
- 可审计的章节质量报告；
- 可查询的人物、事件、因果和伏笔状态；
- 简洁的进度与错误日志。

默认使用本地模型。外部大模型只是一种可选 Provider，不是系统前提，也不拥有小说数据或工作流。

---

## 2. 已冻结的架构决策

以下决策视为 1.0 方案基线，后续变更需要单独记录原因。

### 2.1 项目完全独立

- 不 fork DeepSeek Harness。
- 不把代码放进 DeepSeek Harness monorepo。
- 不修改 Harness Core、Agent Loop、Session 或数据库。
- 不 fork Vela，也不把 Vela 当作运行依赖。
- 不以 Vela 数据库作为本项目的事实源。
- 当前 `dsh-vela` 目录就是新项目自己的根目录。

### 2.2 Vela 仅作为参考项目

Vela 可以用于：

- 理解已有小说工作流和失败案例；
- 参考数据库实体、状态机和测试场景；
- 提炼可验证的行为需求；
- 比较新旧方案质量与性能。

默认不直接复制 Vela 源码。Vela 使用 GPL-3.0；若复制或修改其有实质性的代码，需要单独评估许可证义务和分发方式。为了保持新项目独立，第一选择是根据行为规格重新实现。

### 2.3 Harness 是可替换宿主

DeepSeek Harness 负责：

- 对话入口；
- Agent / Subagent 调度；
- 工具调用；
- 权限与日志表层；
- Headless 或 Web 运行入口。

DSH Novel 自己负责：

- 小说项目与 SQLite；
- 蓝图、Canon 和版本；
- Context Compiler；
- 写作工作流和恢复；
- Embedding / Rerank / Model Provider；
- 审稿、Patch、定稿和导出。

即使没有 DeepSeek Harness，Novel Core 仍必须能够通过 CLI 或测试代码独立运行。

### 2.4 使用 Sidecar，不做进程内领域核心

第一版采用：

```text
DeepSeek Harness
  → 薄 TypeScript Bundle Adapter
  → 本地协议
  → Python Novel Sidecar
  → Novel Core / SQLite / Local Models
```

不计划把 Python、SQLite、Embedding 和小说业务代码塞进 Harness 进程。这样可以避免：

- Harness 升级导致领域核心一起重构；
- Node 原生 SQLite ABI 与 Python/模型依赖冲突；
- 插件卸载造成长任务和数据层被销毁；
- Harness Session 被错误当作小说运行状态。

### 2.5 Context Compiler 是唯一模型上下文入口

任何 Writer、Reviewer、Extractor 或 Patcher 都不得自行拼接历史正文。所有模型输入必须来自版本化、可审计、有硬 token budget 的 `ContextPackage`。

### 2.6 章节定稿是事务，不是一次文件写入

任何章节进入 finalized 前，必须同时具备：

- finalized revision；
- 已验证的 `ChapterDelta`；
- 已更新的 Canon 状态；
- Chapter Digest；
- 审校记录；
- Commit ID 与幂等键。

任一关键写入失败，章节不得对下一章可见。

---

## 3. 产品边界

### 3.1 核心用户

希望在本机用本地模型自动生成长篇小说，不愿持续操作复杂 UI，也不希望人工管理每一轮 Prompt 的个人创作者。

### 3.2 核心痛点

第一阶段只解决一条主问题：

> 在有限本地模型上下文下，连续、可恢复地完成多章节写作，同时控制重复、蓝图偏离和跨章状态错误。

### 3.3 MVP 主闭环

```text
创建项目
→ 导入/生成全书骨架与前 5 章合同
→ 编译第 N 章上下文
→ 写作
→ 抽取 ChapterDelta
→ 规则检查
→ 局部审稿与 Patch
→ 原子定稿
→ 释放当前上下文
→ 进入第 N+1 章
→ 导出正文与报告
```

### 3.4 MVP 必须能力

- 独立 Novel Core。
- 每个项目一份 SQLite 数据库。
- OpenAI-compatible 本地模型接口。
- `ChapterContract`、`ChapterDelta`、`ContextPackage`。
- 真实硬 token budget。
- 最近衔接 + 相关实体 + 按需历史证据。
- 精确重复与近似重复检测。
- 蓝图和状态转移门禁。
- span 级 Patch。
- 章节状态机、幂等、暂停和恢复。
- DSH 薄 Bundle Adapter。
- CLI 独立运行入口。

### 3.5 不进入 MVP

- 完整桌面 UI。
- 多租户、账号和云端协作。
- 复杂图数据库。
- 每章生成多个全文候选。
- 每章调用外部大模型。
- 直接读取或修改 Vela 数据库。
- 自动重写大范围已定稿章节。
- 与 DSH 内部 Session 格式深度集成。

### 3.6 最小验证成果

MVP 验收不是“工具能生成一章”，而是：

- 连续自动完成 5 章；
- 每章约 3000～4000 字；
- 输入上下文不随章节线性增长；
- 故意注入重复和状态错误时会被阻断；
- 中途终止进程后可以从正确阶段恢复；
- 不打开 Harness 也能用 CLI 完成同一闭环。

---

## 4. 三层独立架构

```text
┌────────────────────────────────────────────────────────────┐
│ Host / Orchestration                                       │
│ DeepSeek Harness | CLI | future Agent hosts                │
└──────────────────────────┬─────────────────────────────────┘
                           │ adapter contract v1
┌──────────────────────────▼─────────────────────────────────┐
│ Transport Adapters                                         │
│ adapter-dsh | CLI | MCP（Next） | optional HTTP client     │
└──────────────────────────┬─────────────────────────────────┘
                           │ stable Novel API
┌──────────────────────────▼─────────────────────────────────┐
│ Python Novel Sidecar / Application                         │
│ Run orchestration | Context Compiler | Quality | Commit    │
├────────────────────────────────────────────────────────────┤
│ Domain Core                                                │
│ Blueprint | Canon | State | Delta | Hook | Patch | Policy  │
├────────────────────────────────────────────────────────────┤
│ Infrastructure                                             │
│ SQLite | FTS | Vector | Model adapters | Files | Logs      │
└────────────────────────────────────────────────────────────┘
```

### 4.1 Domain Core

纯业务规则，不依赖：

- DeepSeek Harness；
- Cordis；
- Electron；
- React / Zustand；
- HTTP / MCP；
- 具体模型服务；
- 具体向量数据库。

核心对象包括：

- Project；
- StorySpine；
- StoryArc；
- OutlineNode；
- ChapterContract；
- ChapterRevision；
- ChapterDelta；
- NarrativeEvent；
- EntityStateVersion；
- PlotThread；
- Hook；
- ReviewIssue；
- PatchOperation；
- NovelRun。

### 4.2 Application Services

负责用 Domain 对象编排完整用例：

- `PlanBookService`；
- `PrepareChapterService`；
- `CompileContextService`；
- `RunChapterService`；
- `ReviewChapterService`；
- `ApplyPatchService`；
- `CommitChapterService`；
- `AuditArcService`；
- `RepairImpactService`；
- `ExportManuscriptService`。

### 4.3 Infrastructure Adapters

- SQLite Repository；
- FTS Repository；
- Vector Index；
- Embedding Provider；
- Rerank Provider；
- Writer / Judge Model Provider；
- Local file storage；
- Runtime logger。

所有外部依赖都经过接口注入，Domain 不 import 具体实现。

### 4.4 Transport Adapters

Transport 只负责：

- 参数解析与验证；
- 调用 Application Service；
- 返回稳定的结果 envelope；
- 映射取消、超时和错误码；
- 不实现小说业务规则。

---

## 5. DeepSeek Harness 接入方式

### 5.1 当前公开插件基线

本方案核对的本地 DeepSeek Harness 基线为：

```text
package version: 0.1.1-rc.2
reference commit: 64b00b5ebe87444a0daf28fadff441492ad6e777
```

这是兼容测试基线，不是 Domain Core 的依赖版本。

当前 Harness 的公开树外安装方式是 npm bundle：

```json
{
  "name": "dsh-novel-plugin",
  "type": "module",
  "main": "lib/index.js",
  "dsh": {
    "bundle": {
      "patch": "./cordis.patch.yml"
    }
  },
  "peerDependencies": {
    "@deepseek-ai/cordis": "4.0.1",
    "@deepseek-ai/dsh-tools": "0.1.1-rc.2"
  }
}
```

preview 阶段先精确固定已验证的 peer 版本；兼容新版本后发布新的 Adapter patch 版本。等 Harness 进入稳定 SemVer 后，再扩大到经兼容矩阵验证的版本范围。

`cordis.patch.yml` 插入一个薄插件入口：

```yaml
- insert:
    - id: dsh-novel
      name: dsh-novel-plugin
      config:
        transport: http
        endpoint: http://127.0.0.1:17861
```

安装方式遵循 Harness profile：

```text
dsh plugin --profile <profile> add <package-or-path>
```

### 5.2 Adapter 允许使用的 Harness 能力

第一版只允许使用公开扩展面：

- `ctx.tools.register()`；
- Cordis `apply(ctx, config)`；
- `inject` 服务依赖；
- `ctx.effect()` 生命周期清理；
- Schemastery Config 校验；
- `dsh.bundle` 与 `cordis.patch.yml`。

可选使用公开事件做日志展示，但不能作为小说运行状态的唯一来源。

### 5.3 Adapter 禁止依赖的内容

- Harness 仓库相对源码路径；
- `packages/*/src/*` 私有实现；
- Agent Loop 内部状态；
- Session SQLite/JSONL 内部格式；
- Web UI 私有组件；
- preview 阶段未声明兼容性的磁盘格式；
- 在 Harness 数据库中增加小说表；
- Monkey patch 或修改内置 Bundle。

### 5.4 DSH Adapter 的职责

```text
1. 启动时读取配置。
2. 与 Sidecar 执行版本和 capability 握手。
3. 注册粗粒度小说工具。
4. 将工具参数转为 Novel API 请求。
5. 转发取消信号、超时和结构化错误。
6. 把结果渲染成简洁模型输出。
7. 卸载时释放连接；不删除任务和数据。
```

Adapter 不加载 Embedding，不连接 SQLite，不决定章节上下文，也不执行多轮修稿循环。

MVP 中 Sidecar 由用户通过独立 CLI 安装和启动，Adapter 只连接与检查健康状态，不在插件安装脚本中隐式安装 Python 环境。自动拉起 Sidecar 可在部署流程稳定后作为显式配置加入。

### 5.5 跨版本兼容策略

每次连接都返回：

```json
{
  "protocol_version": "1.0",
  "core_version": "0.1.0",
  "adapter_version": "0.1.0",
  "capabilities": [
    "chapter.run",
    "chapter.resume",
    "project.status",
    "manuscript.export"
  ]
}
```

兼容规则：

- `protocol_version` 按 SemVer 管理。
- 新增可选字段只升级 minor。
- 删除字段或改变语义升级 major。
- Adapter 必须忽略协议允许的未知可选字段。
- 缺少必需 capability 时启动失败，不静默降级。
- DSH peer dependency 使用经过测试的版本范围，不写 `*`。
- 每个支持的 DSH 版本都有独立 adapter contract test。
- 升级 Harness 时首先升级/替换 adapter，Domain 和数据库不得被迫修改。

兼容等级：

| 等级 | 含义 | 行为 |
|---|---|---|
| Tested | CI 与真实 smoke 均通过 | 正常启用 |
| Compatible | 公共接口满足但未完整长跑 | 给出警告，可启用 |
| Unsupported | 缺少必需接口或版本超范围 | 启动失败并说明升级路径 |

### 5.6 MCP 的位置

MCP 是第二个 Transport Adapter，不是小说核心：

```text
Novel Core/Application
  ├── DSH native bundle adapter
  ├── MCP server adapter
  └── CLI adapter
```

MCP 的价值是让同一个 Novel Core 被其他 Agent Host 调用，并在 DSH 插件接口变化较大时保留替代接入路径。

MVP 先完成稳定 Novel API 和 DSH Adapter；MCP 在核心工具契约稳定后实现，避免同时维护两套尚未定型的传输。

---

## 6. Novel API 与工具设计

### 6.1 粗粒度工具

Harness 默认只暴露以下工具：

```text
novel_project_create
novel_project_status
novel_book_plan
novel_run_start
novel_run_status
novel_run_pause
novel_run_resume
novel_issue_repair
novel_arc_audit
novel_manuscript_export
```

最重要的入口是：

```text
novel_run_start(project_id, from_chapter, to_chapter)
```

一次调用创建持久化 Run。后续内部循环由 Sidecar 执行，不让 Harness 模型逐步管理几十个底层工具。

### 6.2 细粒度内部 API

内部调试、测试和子 Agent 可以使用：

```text
compile_context
get_chapter_contract
get_entity_state
query_events
query_hooks
retrieve_evidence
submit_draft
extract_delta
validate_delta
propose_patch
apply_patch
commit_chapter
compute_impact
replay_branch
```

这些默认不全部暴露给主 Harness Agent，避免工具选择混乱和破坏业务不变量。

### 6.3 统一结果 envelope

```json
{
  "ok": true,
  "request_id": "req_...",
  "project_id": "prj_...",
  "run_id": "run_...",
  "protocol_version": "1.0",
  "result": {},
  "warnings": [],
  "error": null
}
```

错误至少区分：

```text
CONFIG_INVALID
PROTOCOL_INCOMPATIBLE
PROJECT_NOT_FOUND
DATABASE_BUSY
MODEL_UNAVAILABLE
MODEL_OUTPUT_INVALID
CONTEXT_BUDGET_EXCEEDED
QUALITY_GATE_BLOCKED
VERSION_CONFLICT
RUN_PAUSED
RUN_CANCELLED
INTERNAL_ERROR
```

---

## 7. 长篇上下文与状态模型

详细算法以配套技术规范为准，本总方案冻结以下原则。

### 7.1 不把聊天记录当记忆

Harness Session 只保存任务对话。小说历史存储在 Novel Core 中，Writer 每章获得一个新的最小 Context Package。

### 7.2 固定层级

```text
L0 Hard Rules
L1 Global Story Spine
L2 Current Story Arc
L3 Chapter Contract
L4 Relevant Entity State
L5 Continuity Bridge
L6 Retrieved Evidence
L7 Style Guidance
```

### 7.3 复杂度目标

- 章节从 10 增长到 100、300 时，单次 Prompt 不线性增长。
- 64K 本地模型的常规输入目标为 12K～20K tokens。
- 输入硬上限初始设为 24K，输出预留 8K，并保留系统和工具缓冲。
- 超预算必须裁剪或拒绝，不能只记录 warning。

### 7.4 增量正史

每章只提交本章变化：

- Narrative Events；
- Entity State Changes；
- Knowledge Changes；
- Item Transfers；
- Plot Advances；
- Hook Transitions；
- Blueprint Coverage；
- 下一章 Handoff。

旧正文不因摘要而删除；摘要和当前状态都是可从 Delta 重建的 Materialized View。

---

## 8. 数据与检索

### 8.1 SQLite 所有权

SQLite 由 Novel Sidecar 独占管理。Harness Adapter 不执行 SQL。

建议每个小说项目独立目录：

```text
projects/<project_id>/
├── novel.sqlite3
├── manuscript/
├── vectors/
├── exports/
└── runtime/
```

### 8.2 数据库迁移

- 独立 `SCHEMA_VERSION`。
- 单调 migration。
- migration 前自动备份。
- 失败回滚并拒绝启动写任务。
- 不承诺读取 Vela 数据库格式。
- Vela 数据迁移未来通过显式、只读、单向 importer 完成。

### 8.3 检索顺序

```text
SQLite 精确状态
→ 实体/剧情线/Hook 查询
→ FTS
→ Embedding
→ 可选 Rerank
→ 去重和预算打包
```

Embedding 是可选增强；Rerank 不是 MVP 硬依赖。二者失败时可以降级，但 SQLite Canon 和版本系统失败时必须停止。

### 8.4 模型配置

所有模型通过 Provider 接口：

```yaml
models:
  writer:
    provider: openai_compatible
    endpoint: http://127.0.0.1:1234/v1
    model: local-writer
    context_window: 65536
    max_output_tokens: 8192

  judge:
    use: writer

embedding:
  enabled: true
  endpoint: http://127.0.0.1:11434
  model: bge-m3

rerank:
  enabled: false
```

同一模型可以承担多个角色，但每个角色必须使用独立 Prompt、参数、会话和输出 Schema。

---

## 9. 写稿、审稿、修稿和定稿

### 9.1 单章流程

```text
PREPARING
→ CONTEXT_READY
→ DRAFTING
→ EXTRACTING_DELTA
→ VALIDATING
→ REVIEWING_IF_NEEDED
→ PATCHING_IF_NEEDED
→ READY_TO_COMMIT
→ COMMITTING
→ COMMITTED
```

### 9.2 检查分层

| 层级 | 范围 | 默认模型调用 |
|---|---|---:|
| Scene Gate | 当前场景 | 仅歧义时 |
| Chapter Gate | 当前章 + 相关 Canon | 可选一次 |
| Arc Gate | 每 5 章或风险触发 | 异常时 |
| Global Audit | Arc 结束或重大修改 | 低频 |

### 9.3 程序优先检查

- 正文污染和 Prompt 标签泄漏；
- exact / MinHash / SimHash 重复；
- 章节目标和状态前置条件；
- 人物地点、知识和物品归属；
- Hook 期限与因果前置；
- revision hash、版本和幂等；
- Context Package 预算和来源。

### 9.4 Patch 优先

Reviewer 必须返回可定位 Issue：

```text
chapter + scene + span + source_hash
+ issue_type + evidence + expected_state
+ instruction + confidence
```

Patcher 默认输出 `replace_span`、`delete_span`、`insert_after` 或 `move_scene`，不重写整章。

### 9.5 多章错误

找到最早错误事件，沿因果、状态、剧情线和 Hook 关系计算影响闭包；只重放和修复依赖节点。系统不得默认将“错误章节之后的全部章节”视为受影响。

---

## 10. Agent 与模型调度

### 10.1 角色划分

| 角色 | 职责 | 上下文 |
|---|---|---|
| Planner | Chapter Contract | 蓝图、Arc、有效状态 |
| Writer | 正文 | 本章写作 Context Package |
| Extractor | ChapterDelta | 当前正文 + Schema |
| Validator | 确定性检查 | 程序执行 |
| Judge | 歧义判断 | Issue + 局部证据 |
| Patcher | 局部修复 | 目标 span + 约束 |
| Committer | 原子提交 | 程序执行 |
| Arc Auditor | 跨章异常 | Arc Delta + 图谱 |

### 10.2 小模型优先

- Writer 使用最擅长正文的本地模型。
- Extractor 可使用更小、更稳定的结构化模型。
- Judge 默认复用本地模型，但使用低温度和严格 Schema。
- 外部大模型只在用户启用时作为可选 Judge Provider。
- 不把“模型自评”当唯一质量门禁。

### 10.3 Harness Subagent

Harness 子 Agent 只接收：

```json
{
  "project_id": "...",
  "run_id": "...",
  "chapter": 12,
  "task": "review",
  "context_package_id": "..."
}
```

不 fork 全书历史，也不共享 Writer 的长推理记录。真正上下文由插件按任务返回。

### 10.4 内容隔离与模型政策边界

“大模型监督小模型”不是 MVP 必要条件。默认路径是全本地，并按任务所需的最小数据面分层：

| 检查方式 | 可见内容 | 能解决的问题 |
|---|---|---|
| 确定性 Validator | hash、span、Delta、Contract、统计量 | 重复、长度、Schema、状态冲突、必需节拍 |
| 结构 Judge | 章节合同、Delta、Issue 与局部摘要 | 蓝图匹配、因果、人物状态和伏笔进度 |
| 本地正文 Judge | 必要的正文 span 或全章 | 文风、节奏、语义重复和细节质量 |
| 外部 Judge（可选） | 用户明确允许的最小任务包 | 与对应 Provider 能力和政策一致的评估 |

结构化解耦可以降低敏感正文暴露、减少 token，也能让外部 Judge 专注于因果和结构；但它不能也不应被设计成规避 Provider 政策的手段。任何外部模型都可能拒绝其不接受的内容，系统不能承诺通过脱敏必然绕过该限制。

因此：

- 需要阅读完整正文的审稿，默认使用用户选择的本地 Provider。
- 外部 Judge 必须显式开启，且开启前展示将发送的数据类型。
- 外部 Judge 拒绝、超时或不可用时，只能降级到本地 Judge 或标记人工检查，不能影响已保存的草稿。
- 只看结构数据的 Judge 不能给出“文笔已合格”结论；评分必须标注证据范围。

---

## 11. 项目目录定稿

不再创建另一个小说项目目录。当前目录就是独立仓库根，实施阶段按以下结构建立：

```text
dsh-vela/
├── Docs/
│   ├── DeepSeek-Harness-Novel-Plugin-Development-Plan.md
│   ├── Long-Novel-Context-and-Incremental-Consistency-Architecture.md
│   └── Vela-Reference-and-Reuse-Audit.md
├── backend/
│   ├── pyproject.toml
│   ├── src/dsh_novel/
│   │   ├── domain/
│   │   ├── application/
│   │   ├── infrastructure/
│   │   ├── providers/
│   │   └── transports/
│   └── tests/
├── adapter-dsh/
│   ├── package.json
│   ├── cordis.patch.yml
│   ├── src/
│   └── tests/
├── schemas/
│   └── protocol/v1/
├── tests/
│   ├── contracts/
│   ├── compatibility/
│   └── longrun/
├── scripts/
├── examples/
├── README.md
├── LICENSE
└── THIRD_PARTY_NOTICES.md
```

MVP 不建立 UI 目录。若未来确实需要查看运行状态，再增加只读管理表层。

---

## 12. 参考代码与许可证治理

### 12.1 Clean-room 默认策略

```text
读取 Vela 行为
→ 写成独立需求和测试案例
→ 设计自己的 Schema/API
→ 在 dsh-vela 中重新实现
→ 通过行为测试比较
```

禁止无记录地复制文件、函数或长 Prompt。

### 12.2 来源记录

任何第三方代码进入项目必须记录：

- 原项目和 URL；
- 原许可证；
- 原文件和 commit；
- 复制或修改范围；
- 本项目承载文件；
- 所需 NOTICE / source obligation；
- 审核人和日期。

### 12.3 Vela 默认复用政策

| 类型 | 政策 |
|---|---|
| 产品概念、失败案例、公开数据结构思想 | 可参考，重新设计 |
| 测试场景和验收思想 | 可转写为独立测试 |
| 简短通用算法 | 先做来源与许可证审查 |
| 完整 TypeScript 文件、Prompt、SQL、工作流 | 默认不复制 |
| Electron、React、Zustand、IPC | 不引入 |
| Vela SQLite 数据库 | 不直接打开，未来只读导入 |

此处是工程风险控制，不替代正式法律意见。

---

## 13. 配置、安全与隐私

### 13.1 配置分层

```text
代码默认值
→ 项目配置
→ 本机用户配置
→ 环境变量/密钥
→ 单次 Run override
```

可调参数不硬编码：

- 模型 endpoint 和名称；
- context window 与 token budget；
- timeout / retries；
- 数据目录；
- embedding / rerank；
- 质量阈值；
- 章节长度；
- 并发度。

### 13.2 本地数据

- 正文、数据库、Prompt 快照和模型输出默认留在本地。
- 不默认收集遥测。
- 外部 Provider 启用时明确提示哪些文本会离开本机。
- 日志默认不复制完整正文，只保存 ID、hash、计数和错误摘要。
- 数据目录支持备份、导出和完整删除。
- MVP Sidecar 只监听 `127.0.0.1`，不提供远程绑定。
- Adapter 与 Sidecar 使用本机 token 或受限 Unix socket；token 文件不进入项目仓库和日志。
- 外部模型密钥由 Provider 配置读取，不通过 Harness 工具参数传递。

### 13.3 并发与写入

- 一个项目同一时刻只有一个 Committer。
- 读任务可并发，写任务按项目串行。
- 所有写操作携带 expected version。
- 同一 idempotency key 重放不得产生第二份 revision。
- Patch 使用 source hash 乐观锁。

---

## 14. 可观测性与恢复

### 14.1 Run 状态是 Sidecar 权威

Harness Job 消失、对话压缩或进程重启，不得让小说 Run 丢失。Sidecar 持久化：

- 当前章节和阶段；
- 当前 revision / context package；
- 模型调用 attempt；
- repair round；
- pending issue；
- commit 状态；
- last error。

### 14.2 日志最小字段

```text
time
project_id
run_id
chapter
stage
attempt
model_role
context_package_id
input_tokens
output_tokens
duration
result_code
```

### 14.3 恢复规则

- 网络失败只重试当前模型调用。
- Schema 失败只修复结构或重新抽取。
- Review 失败不重新写初稿。
- Patch 失败不覆盖原 revision。
- Commit 失败回滚完整事务。
- Index 失败进入可重试后台任务，不回滚已确认 Canon。

---

## 15. 测试策略

### 15.1 单元测试

- Domain 状态转移；
- Context Packer；
- 状态 as-of 查询；
- Hook 生命周期；
- 重复算法；
- Patch hash；
- token budget；
- 幂等和版本冲突。

### 15.2 Contract 测试

- Protocol v1 JSON Schema；
- Sidecar client/server；
- DSH Tool Schema；
- MCP schema（实现后）；
- Provider 输出。

### 15.3 DSH 兼容测试

每个支持版本至少验证：

```text
bundle install
config dump
plugin load/unload
tool registration
capability handshake
project status call
one chapter smoke
cancellation
Sidecar unavailable failure
```

升级 Harness 不先改 Domain；若测试失败，只评估 adapter 和 package manifest。

### 15.4 长跑测试

- 5 章真实本地模型 MVP；
- 20 章质量回归；
- 100 章合成上下文和状态测试；
- 300 章、百万字规模压力测试；
- 随机断电/终止恢复；
- 历史章节修改与影响闭包。

### 15.5 质量样本集

建立固定 Corpus：

- 完全重复段落；
- 改词重复；
- 合理呼应；
- 人物知识越权；
- 地点瞬移；
- 物品重复持有；
- Hook 超期；
- 蓝图事件缺失；
- Prompt 标签污染；
- 修稿破坏正确段落。

没有固定样本集，就无法判断“换模型、改 Prompt、加 Rerank”到底是否改善。

---

## 16. 实施路线

### Phase 0：独立仓库基础

目标：建立可以独立运行和测试的工程骨架。

交付：

- Python backend；
- Protocol v1 schemas；
- SQLite migration framework；
- CLI health/status；
- Provider interfaces；
- 第三方来源登记规则。

退出标准：不安装 DSH、不读取 Vela，也能启动、建库和运行测试。

### Phase 1：Novel Core MVP

目标：不接 Harness，先跑通单章领域闭环。

交付：

- ChapterContract；
- ContextPackage；
- ChapterDelta；
- Context Compiler v1；
- Writer / Extractor Provider；
- 基础 Gate；
- 原子 Commit；
- CLI `run-chapter`。

退出标准：单章可生成、检查、提交和恢复。

### Phase 2：连续 5 章

目标：证明上下文释放与增量状态有效。

交付：

- Story Spine / Arc；
- Relevant Entity Set；
- 最近衔接窗口；
- FTS 和可选 Embedding；
- Patch Engine；
- 5 章自动 Run。

退出标准：输入不线性增长，重复和故意状态错误被阻断。

### Phase 3：DSH Plugin MVP

目标：通过独立 bundle 调用稳定 Sidecar。

交付：

- `adapter-dsh`；
- `dsh.bundle` manifest；
- `cordis.patch.yml`；
- 粗粒度 tools；
- health/capability handshake；
- profile 安装和卸载说明；
- 当前 rc 基线兼容测试。

退出标准：DSH 升级或退出不会破坏小说数据库，CLI 与 DSH 运行结果一致。

### Phase 4：100 章稳定性

目标：验证长篇工程能力。

交付：

- Arc Gate；
- 因果和 Hook 图；
- 影响闭包；
- 100 章合成长跑；
- Context 和质量指标仪表数据；
- 版本升级矩阵。

### Phase 5：百万字与多 Host

目标：300 章压力规模与通用协议接入。

交付：

- MCP Adapter；
- 可选 Rerank；
- 300 章测试；
- Vela 只读 importer（有实际迁移需求时）；
- 跨 DSH 版本和其他 Agent Host 测试。

---

## 17. Now / Next / Later / Risk

### Now

- 冻结 Protocol v1。
- 建立独立 Python 工程和 SQLite migration。
- 实现三大 Schema。
- 单章 CLI 闭环。
- 5 章上下文稳定性测试。

### Next

- DSH bundle adapter。
- Embedding 检索。
- Arc 状态、Hook 和影响闭包。
- 20～100 章长跑。

### Later

- MCP。
- Rerank。
- 多候选关键章节。
- Vela 数据 importer。
- 轻量只读 UI。

### Risk

| 风险 | 对策 |
|---|---|
| DSH preview 接口变化 | 薄 Adapter + Protocol + 兼容矩阵 |
| Vela GPL 影响独立性 | 默认 clean-room，不复制源码 |
| 小模型结构化输出不稳 | Schema 修复、独立 Extractor、规则兜底 |
| 上下文再次膨胀 | 唯一 Compiler + 硬预算 + 回归测试 |
| Canon 抽取错误 | proposed/confirmed、证据 span、置信度和重放 |
| 多章错误扩散 | 每章 Gate + 因果影响闭包 |
| SQLite 并发冲突 | 单项目单 Committer + expected version |
| 自动流程无限循环 | 阶段恢复预算、修稿轮次上限、明确 PAUSED |
| 提前平台化 | 先 CLI + 单 Sidecar + 单用户单机 |

---

## 18. 交付标准

每个阶段必须同时交付：

- 可运行代码；
- migration；
- Schema 与接口说明；
- 单元/Contract/回归测试；
- 失败与恢复示例；
- 配置样例；
- 变更文件和风险说明。

不接受以下“完成”：

- 只有 Prompt，没有数据协议；
- 只有 Agent 名称，没有状态和输出 Schema；
- 只有 token warning，没有裁剪或拒绝；
- 只有向量检索，没有事实有效期；
- 只有审稿分数，没有可定位 Issue；
- 只有全文重写，没有 Patch；
- 只能在特定 DSH checkout 中运行；
- 直接复制 Vela 代码但没有来源和许可证决策。

---

## 19. 第一批开发任务

按顺序执行：

1. 初始化 `backend/` Python 工程、pytest、ruff、mypy 和 migration 测试。
2. 定义 Protocol v1 的 ID、错误 envelope 和 capability handshake。
3. 定义 `ChapterContract`、`ChapterDelta`、`ContextPackage`。
4. 建立 Project、Chapter、Revision、Run、Commit 最小表。
5. 实现 `state_as_of()` 和 ChapterDelta 重放。
6. 实现 Context Compiler 的固定层、预算和 provenance。
7. 接入一个 OpenAI-compatible 本地 Writer Provider。
8. 实现正文纯净度、exact repeat 和基础状态 Gate。
9. 实现 PatchOperation 与 source hash。
10. 实现 CLI `project-create`、`run-chapter`、`run-status`、`resume`。
11. 完成 5 章真实模型样本和 100/300 章合成测试。
12. 核心稳定后再创建 `adapter-dsh/` bundle。

---

## 20. 最终验收口径

项目成功的判断不是“兼容了多少框架”，而是：

1. DSH Novel 没有 DeepSeek Harness 也能独立完成写作闭环。
2. 安装一个薄 Bundle 后，DeepSeek Harness 可以可靠启动、查询、暂停和恢复小说 Run。
3. DeepSeek Harness 升级时，通常只修改 Adapter，不迁移 Novel Core 数据。
4. Vela 删除、移动或升级都不会影响本项目运行。
5. 第 10、100、300 章的 Context Package 都保持在配置预算内。
6. 章节定稿、Canon、Delta 和摘要保持事务一致。
7. 重复、蓝图偏离和关键状态错误尽量在产生当章被发现。
8. 修改旧章时只修复受影响依赖，不默认重写全部后续章节。
9. 本地模型是完整主路径，外部大模型只是可选 Provider。
10. 用户最终只需看进度、异常和高质量结果，不需要管理 Agent 内部对话。

---

## 21. 参考基线

本方案核对但不依赖以下本地项目：

- DeepSeek Harness：`/Users/samni/Desktop/开发项目/deepseek-harness`，MIT，参考插件 Bundle、工具注册和 profile 安装方式。
- Vela：`/Users/samni/Desktop/开发项目/vela`，GPL-3.0，参考小说工作流、失败案例和领域概念。

外部参考及长篇生成研究见配套技术规范。实现时以本项目 Schema、测试和本文件的架构决策为准。
