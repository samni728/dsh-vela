# 长篇小说上下文与增量一致性架构

> 文档角色：DSH Novel 的上下文、状态、审校与修复配套技术规范  
> 状态：定稿，受总方案约束  
> 版本：1.0  
> 日期：2026-08-22

总方案：[DSH Novel 独立小说插件最终方案](./DeepSeek-Harness-Novel-Plugin-Development-Plan.md)  
参考边界：[Vela 参考与复用审计](./Vela-Reference-and-Reuse-Audit.md)

本文只定义长篇小说领域算法。项目独立性、DSH Bundle、Sidecar、协议版本和实施路线以总方案为准。

---

## 1. 结论先行

百万字小说不能依靠“越来越长的聊天记录”完成，也不能靠“每隔几章让模型总结一次”解决。

真正可持续的实现必须把小说从一段对话，改造成一个可查询、可验证、可回滚的状态系统：

1. 正文是不可随意丢失的原始记录。
2. 蓝图是目标状态和预期转移，不是每次全部注入的长文档。
3. Canon 是带来源、章节有效期和版本的结构化事实。
4. 每章只产生一个增量 `ChapterDelta`，而不是重新理解整本书。
5. 上下文由确定性的 `Context Compiler` 按预算编译，Agent 不自行拼接历史。
6. 检查分为确定性规则、局部模型判断和低频全局审计。
7. 修复默认是带定位的 Patch，不是重写整章，更不是重写全书。
8. 多章错误从“最早错误点”向后计算影响范围，只重算依赖闭包。
9. Embedding 用于寻找证据，Rerank 用于重新排序；二者都不能代替故事状态数据库。
10. Harness 负责调度，小说插件负责领域状态、上下文、检查、提交和恢复。

核心公式：

```text
下一章输入
  ≠ 全部历史正文 + 全部聊天记录 + 全部摘要

下一章输入
  = 有界全局故事脊柱
  + 当前篇章目标
  + 当前章节状态转移
  + 涉及实体的“截至上一章”状态
  + 最近衔接信息
  + 按需召回的历史证据
  + 固定预算内的写作约束
```

该系统的复杂度目标不是让 Prompt 随章节数线性增长，而是让每章输入基本保持常量：

```text
章节数量从 10 → 100 → 300
单次生成上下文仍稳定在约 12K～20K tokens
```

---

## 2. 规模定义与验收目标

用户示例为 100 章、每章约 3500 字，总量约 35 万字。真正的“百万字”若仍按每章 3500 字计算，约为 286 章。

因此不能只按 100 章设计，应至少以以下规模验收：

| 项目 | MVP 验证 | 正式目标 | 压力测试 |
|---|---:|---:|---:|
| 章节数 | 5 | 100 | 300 |
| 每章正文 | 3000～4000 字 | 3500 字 | 3500～5000 字 |
| 总正文 | 约 2 万字 | 约 35 万字 | 100 万字以上 |
| 主要人物 | 5 | 30 | 100 |
| 活跃剧情线 | 3 | 20 | 80 |
| 单次输入预算 | ≤ 20K tokens | ≤ 20K tokens | ≤ 24K tokens |
| 输入随章节增长 | 不明显 | 基本常量 | 基本常量 |

“支持百万字”不等于承诺模型永远不犯文学错误，而是保证：

- 不因正文总长度增长而导致单次上下文无限增长。
- 不因历史章节增加而必然爆掉 context window。
- 每个已定稿事实都有来源，可以查询和回滚。
- 每章定稿前都检查关键状态转移。
- 能发现并定位重复、状态冲突、蓝图偏离和未闭合伏笔。
- 修改旧章节后，能计算影响范围，而不是盲目重写全部后续章节。
- 任一 Agent 或模型调用失败后，可以从持久化状态恢复。

---

## 3. 最重要的认知：Context Window 不是小说记忆

### 3.1 长上下文只是“可读容量”

即使模型拥有百万 token 上下文，也不代表它能在其中可靠地：

- 找到第 8 章埋下、第 76 章需要回收的伏笔；
- 判断人物在第 34 章是否已经知道某个秘密；
- 区分当前正史与曾被废弃的修稿版本；
- 判断一个人物的物品、位置和关系是在何时发生变化；
- 确认第 90 章的反转是否具备充分前置因果；
- 避免从大量相似段落中模仿并重复旧表达。

上下文越长，相关信息和无关信息就越混杂。对于小模型，这还会进一步导致注意力稀释、指令竞争、输出重复和格式失败。

### 3.2 对话压缩不是领域状态压缩

Hermes 一类 Agent 的压缩机制解决的是：

- 当前任务的历史消息快到模型限制；
- 旧工具输出占用大量 token；
- 需要用一份会话摘要保留“我们之前做了什么”。

小说系统需要解决的是：

- 哪些事实当前有效；
- 哪些状态从哪一章开始、在哪一章结束；
- 哪些因果关系尚未满足；
- 哪些伏笔已经埋下、强化、揭示或回收；
- 修改旧章会影响哪些后续章节。

两者不是同一个问题。Hermes 的会话压缩可以继续使用，但不能成为小说 Canon 的来源。

### 3.3 正确分工

```text
Hermes / DeepSeek Harness 会话
  只保留：任务目标、当前 job_id、进度、异常、最终结果

Novel Plugin
  保留：蓝图、正文、状态、事实、因果、伏笔、版本、审校、补丁

Context Compiler
  每次从 Plugin 中编译一个新的、干净的、固定预算上下文
```

每一章、每一个 Agent 都应当使用新的任务上下文。上一轮修稿的讨论过程、失败输出和工具日志不能自动进入下一章。

---

## 4. 外部开源方案如何处理长时上下文

本节不是简单罗列项目，而是判断哪些思想适合小说插件。

### 4.1 Hermes Agent：通用会话压缩 + 可插拔记忆

Hermes 当前的相关能力包括：

- 内置上下文压缩器在达到阈值后压缩中段消息。
- 压缩前先清理旧工具结果，再调用模型生成摘要。
- `MEMORY.md` / `USER.md` 保存有界、精选的长期信息。
- 外部 Memory Provider 可在每轮前预取相关记忆，并向系统 Prompt 注入上下文。
- Context Engine 和 Memory Provider 都可以通过插件替换。

适合借鉴：

- Stable / context / volatile 的 Prompt 分层。
- 有界常驻记忆，而不是无限追加。
- 外部记忆预取和工具化搜索。
- 子 Agent 的隔离执行。
- 插件不修改 Harness Core。

不能直接解决：

- 小说事实的章节有效期。
- 因果图和伏笔生命周期。
- 旧章节修改后的依赖重算。
- 正文 span 级补丁。
- 蓝图目标与实际事件的结构化对账。

结论：Hermes 很适合做编排宿主，但不应该直接承担小说记忆。

### 4.2 Letta / MemGPT：内外存分层

Letta 将 Agent 记忆区分为：

- 始终在上下文中的可编辑 Memory Blocks；
- 上下文之外的 archival / recall memory；
- Agent 通过工具搜索、编辑或切换 Memory Blocks。

适合借鉴：

- 把“必须一直可见的信息”做成有界 Block。
- 其他信息保存在外部，通过工具按需读取。
- 多个 Agent 可共享同一份只读或可编辑状态块。

小说插件中的对应关系：

| Letta 概念 | 小说插件对应物 |
|---|---|
| 常驻 Memory Block | 有界全局故事脊柱、当前篇章目标 |
| Working Memory | 本章 Context Package |
| Archival Memory | 全文、旧事件、历史版本、检索索引 |
| Tool Search | SQL / 图查询 / FTS / 向量检索 |

不足之处仍然是：通用记忆没有小说专用的状态转移和一致性事务。

### 4.3 SillyTavern：Lorebook + World Info + Vector Storage

SillyTavern 的 World Info 使用关键词触发相关设定；Data Bank / Vector Storage 将长文档切分，只召回相关 chunk 注入 Prompt。

适合借鉴：

- 确定性关键词用于角色、地点、物品等精确实体召回。
- Embedding 用于非结构化历史内容和语义相似片段。
- 设置最多注入多少条、每条多长和注入位置。

不足：

- Lorebook 条目只是在 Prompt 中出现，不保证模型遵守。
- 向量相似不等于事实一致、时间正确或因果成立。
- 没有“本章提交后原子更新全部状态”的事务。
- 没有修改旧章节后的影响分析。

### 4.4 RecurrentGPT：自然语言循环状态

RecurrentGPT 每一步保留：

- 上一步生成的段落；
- 下一步简短计划；
- 存在磁盘上的历史摘要；
- 通过语义检索找回的相关段落。

它证明了“固定长度循环状态 + 外部检索”可以生成任意长度文本。

适合借鉴：

- 每一步都产生下一步计划和更新后的工作记忆。
- 正文留在磁盘，不全部放入上下文。

局限：

- 自然语言摘要仍然是有损的。
- 事实、人物状态和因果若没有结构化，会逐轮变形。
- 更适合段落级连续生成，不足以单独支撑百万字小说工程。

### 4.5 Re3 与 DOC：分层蓝图、候选重排和一致性修订

Re3 的核心是：

- 先生成全局结构计划；
- 重复注入计划和当前故事状态；
- 生成多个候选并按情节一致性、主题相关性重排；
- 对最佳候选做事实修订。

DOC 进一步使用更细的分层蓝图和控制器，让正文更紧密地对应详细大纲。

适合借鉴：

- 把创作压力前移到分层规划阶段。
- 章节生成前先明确目标事件和状态转移。
- 多候选不是必须，但关键反转章节可以启用候选 + Rerank。

局限：

- 公开实验主要是几千词故事，不等于百万字工程。
- 如果每次仍拼接越来越长的“故事状态”，最终仍然会膨胀。
- 候选生成会显著增加本地模型成本，不适合每章默认开启。

### 4.6 DOME：动态分层蓝图 + 时间知识图谱

DOME 是与本项目目标最接近的研究方向：

- 使用动态层级大纲兼顾全局规划和生成后的变化。
- 把生成内容抽取成带时间信息的知识图谱。
- 根据当前计划查询相关图谱信息。
- 对候选冲突进行时间一致性分析。

适合直接吸收：

- 蓝图不是一次写死，而是允许在不破坏全局约束的前提下动态调整未来细纲。
- 事件必须带章节或场景时间坐标。
- 检索以实体和关系为主，再用语义相关性过滤。
- 冲突检查针对可能相关的事实组，不把整本书交给模型审查。

### 4.7 总体判断

外部方案已经给出了正确方向，但没有一个项目可以原样解决我们的目标。

我们应该组合：

```text
Hermes：插件与 Agent 编排
Letta：有界内存 / 外部记忆分层
SillyTavern：实体触发 + 向量检索
RecurrentGPT：固定长度循环状态
Re3 / DOC：分层蓝图与候选控制
DOME：时间图谱与冲突查询

+ 我们自己的：
事件溯源 + 增量事务 + Patch 修复 + 影响分析
```

---

## 5. Vela 参考实现的真实问题

Vela 并不是完全没有上下文管理。以下观察用于建立新项目的失败样本和设计约束，不表示 DSH Novel 依赖或改造 Vela。它已经存在：

- SQLite 正文、草稿、版本、审稿和自动运行状态；
- Canon 时间线、人物当前状态、剧情线、事实和章节摘要；
- LanceDB / Embedding 检索；
- 最近章节摘要；
- 章节定稿后的摘要、角色状态和 Canon 写回；
- 章节级一致性 Gate；
- 最近 10 章左右的漂移检查代码；
- 中断恢复和后处理步骤持久化。

问题不是“没有功能”，而是这些功能没有组成一套严格的长期状态协议。

### 5.1 时间线默认随章节数线性增长

`buildCanonContext()` 的默认 `timelineWindow` 为 `chapterNumber - 1`，随后调用 `getTimeline()` 读取截至当前章之前的全部事件。

结果：第 10 章读 9 章，第 100 章读 99 章，第 300 章读 299 章。Prompt 必然随章节数持续增长。

### 5.2 全部人物和全部事实默认注入

Context Builder 默认读取：

- 全部角色状态；
- 全部 active plot lines；
- 全部 facts；
- 全局 premise、worldbuilding、charactersArch、synopsis。

它没有先根据本章涉及的角色、地点、物品和剧情线建立 `Relevant Entity Set`，因此大量无关信息也进入 Prompt。

### 5.3 同一信息通过多个入口重复注入

生成 Draft 时同时存在：

- Architecture；
- Global Guidance；
- Character State 字符串；
- Chapter notes timeline；
- Future blueprints；
- RAG 结果；
- Canon Rendered Context。

而 Canon 中又包含 world rules、character arch、character states、timeline、recent summaries、plot lines、facts、RAG、style、global guidance。

这会形成同义内容的多次注入，增加 token、指令竞争和模型重复概率。

### 5.4 超预算只告警，不执行裁剪

当前代码估算 Prompt token 超过 28K 后只写日志，没有：

- 按优先级删除低价值块；
- 缩减检索结果；
- 限制人物、事实和剧情线；
- 阻止请求；
- 生成可审计的裁剪报告。

因此“Token Budget”目前不是 Gate，只是 Warning。

### 5.5 所谓压缩没有形成真正的层级替换

每 5 章触发的压缩逻辑只是：

- 读取最近 20 章摘要；
- 取其中 15 条；
- 每条截取约 80 字并用 `|` 连接；
- 使用 `chapterNumber = -1` 写入一条特殊摘要。

它没有：

- 将摘要归属到篇、卷或 Story Arc；
- 标记覆盖的章节范围；
- 从正常检索中替代被覆盖的低层摘要；
- 保存关键事实和未结剧情线；
- 在旧章节改写后重新计算；
- 防止对摘要再摘要造成信息退化。

### 5.6 RAG 查询过于简单

写作检索 query 主要由标题、关键事件和人物名拼接；审稿 query 甚至使用正文前 200 字。

问题：

- 开头 200 字未必代表本章冲突重点；
- 缺少实体 ID、事件类型、时间范围和剧情线过滤；
- 搜索结果没有和 Canon 状态合并去重；
- 没有独立 Rerank；
- 中文全文降级查询以逐字符 LIKE 为主，精度和性能都有限。

### 5.7 Canon 抽取可靠性不足

本地启发式事件抽取依赖有限动作词和正则，状态提取也偏向最后一次出现的段落。这可以作为低成本兜底，但不能成为正史的唯一写入依据。

同时部分 Canon 写回被标记为非关键步骤，失败可能只记录日志。结果是正文已经定稿，但 Canon 可能没有完整更新，下一章继续使用过期状态。

### 5.8 当前状态丢失历史有效期

`canon_character_state` 更像“每个角色当前一行”的 Materialized View。它适合快速读取，但不足以回答：

- 第 27 章时角色在哪里？
- 某件物品何时转手？
- 某段关系是在什么事件后变化？
- 改写第 20 章后，第 21～60 章哪些状态需要重放？

需要保留不可变 Delta 或状态版本历史，而不只是最后状态。

### 5.9 检查模块存在，但没有统一进入提交协议

Vela 有 drift、stability、semantic coherence、reader simulation、narrative tension 等多个模块，但存在“代码有了，不一定在每章主链路上形成硬门禁”的风险。

百万字系统不能以模块数量衡量，而应以以下协议衡量：

```text
任何 finalized chapter
  必须对应唯一 revision
  必须对应已验证 ChapterDelta
  必须原子更新 Canon
  必须生成可重放的状态变化
  必须拥有审校结果和 context provenance
```

---

## 6. 目标架构：小说是状态机，不是聊天

```text
                            ┌──────────────────────┐
                            │ DeepSeek / Hermes    │
                            │ Job & Agent Scheduler│
                            └──────────┬───────────┘
                                       │ plugin tools
                            ┌──────────▼───────────┐
                            │ Novel Orchestrator   │
                            └──────────┬───────────┘
                 ┌─────────────────────┼─────────────────────┐
                 ▼                     ▼                     ▼
        Context Compiler       Incremental Validator     Patch Engine
                 │                     │                     │
                 └─────────────────────┼─────────────────────┘
                                       ▼
                            Chapter Commit Protocol
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
 Immutable Manuscript          Narrative State Store          Retrieval Index
 chapters/scenes/revisions     event/entity/causal/hook       FTS/vector/rerank
        │                              │                              │
        └──────────────────────────────┴──────────────────────────────┘
                                       │
                              SQLite + Vector Store
```

必须明确三类数据：

### 6.1 Source of Truth

- 已定稿正文及其 revision；
- 章节蓝图及其版本；
- 人工或系统确认的 Canon 事件；
- 每次 Patch 和提交记录。

这些数据不可因压缩而删除。

### 6.2 Materialized Views

- 人物当前状态；
- 当前地点占用；
- 当前物品归属；
- 活跃剧情线；
- 当前未解伏笔；
- 当前篇章摘要；
- 当前全局故事脊柱。

这些可以从历史 Delta 重建。

### 6.3 Retrieval Index

- 正文 chunk embedding；
- 摘要 embedding；
- FTS 索引；
- 实体倒排表；
- 事件和证据 span 索引。

索引是可重建的加速层，不是唯一正史。

---

## 7. 分层记忆模型

### 7.1 L0：不变约束

内容：

- 小说题材和核心承诺；
- 世界硬规则；
- 不可变人物身份；
- 叙事视角和语言规范；
- 用户明确的内容边界；
- 结局不可违背的硬约束。

特点：小、稳定、每次可见、版本化。

建议预算：800～1500 tokens。

### 7.2 L1：全局故事脊柱

不是“从第一章到当前章的全文摘要”，而是一份固定大小的结构：

```json
{
  "premise": "核心命题",
  "central_conflict": "中心冲突",
  "ending_constraint": "最终必须抵达的状态",
  "acts": [
    {"id": "act-1", "status": "closed", "result": "..."},
    {"id": "act-2", "status": "active", "goal": "..."},
    {"id": "act-3", "status": "planned", "goal": "..."}
  ],
  "global_promises": ["必须回收的核心承诺"],
  "invariants": ["绝不能推翻的正史"]
}
```

它的大小不能随章节数增长。旧篇章只保留结果，不保留全部过程。

建议预算：1000～1800 tokens。

### 7.3 L2：当前 Story Arc 状态

内容：

- 当前卷/篇章目标；
- 起点、当前状态、目标终点；
- 本 Arc 活跃角色；
- 本 Arc 主要剧情线；
- 已完成节点与下一关键节点；
- 风险和必须回收的钩子。

建议预算：1200～2200 tokens。

### 7.4 L3：章节任务合同

写作之前先定义所需状态转移：

```json
{
  "chapter": 51,
  "purpose": "主角第一次确认导师隐瞒事实",
  "required_events": ["发现账册", "与导师发生试探性对话"],
  "required_state_changes": [
    {"entity": "主角", "field": "knowledge", "add": "导师与旧案有关"}
  ],
  "forbidden_changes": [
    {"entity": "主角", "field": "knowledge", "value": "完整真相"}
  ],
  "hooks_to_plant": ["账册缺失的一页"],
  "hooks_to_advance": ["旧案证人"],
  "handoff": "主角决定暗中调查",
  "target_scenes": 4
}
```

这一层是生成和验收共同使用的 Contract。

建议预算：800～1500 tokens。

### 7.5 L4：相关实体当前状态

只加载本章直接涉及和一跳依赖的实体：

- 当前章角色；
- 这些角色持有的物品；
- 当前场景地点；
- 与这些角色直接相关的关系；
- 本章计划推进的剧情线和伏笔。

不加载全书所有人物。

建议预算：1500～3000 tokens。

### 7.6 L5：局部连续性窗口

默认包括：

- 上一章最后 500～1000 字；
- 上一章结构化 Delta；
- 最近 2～3 章的有界摘要；
- 若跨场景生成，则包括本章已写场景的短摘要和最后一段。

“最近 N 章”不是唯一规则。如果当前章从第 12 章的支线回归，应通过剧情线和实体检索召回第 12 章证据，而不是把第 12～50 章全部放入上下文。

建议预算：2000～4000 tokens。

### 7.7 L6：按需历史证据

Context Compiler 根据章节合同构造多个查询：

- 实体精确查询；
- 剧情线和伏笔查询；
- 时间范围查询；
- FTS 关键词查询；
- Embedding 语义查询；
- 必要时 Rerank。

只返回带来源的证据：

```json
{
  "source": "chapter:12/revision:3/scene:2/span:840-1012",
  "reason": "hook:h-009 origin",
  "text": "……",
  "score": 0.93
}
```

建议预算：1500～3500 tokens。

### 7.8 L7：风格参考

风格不能通过持续塞入大量旧正文维持。应保存：

- 可执行的风格规则；
- 少量经选择的正例；
- 禁止模式，例如重复句式、说明书式对白；
- 本书高频词和近期重复表达黑名单。

建议预算：800～1500 tokens。

---

## 8. Context Compiler：编译，而不是拼接

### 8.1 输入

```ts
interface ContextCompileRequest {
  projectId: string
  chapterNumber: number
  sceneNumber?: number
  task: 'plan' | 'draft' | 'extract' | 'review' | 'patch' | 'finalize'
  modelContextWindow: number
  maxOutputTokens: number
  revisionId?: string
}
```

### 8.2 输出

```ts
interface ContextPackage {
  packageId: string
  projectId: string
  chapterNumber: number
  task: string
  canonVersion: number
  blueprintVersion: number
  tokenBudget: number
  estimatedTokens: number
  blocks: ContextBlock[]
  omitted: OmittedContext[]
  provenance: EvidenceRef[]
  checksum: string
}

interface ContextBlock {
  kind:
    | 'hard_rules'
    | 'story_spine'
    | 'arc_state'
    | 'chapter_contract'
    | 'entity_state'
    | 'continuity_bridge'
    | 'retrieved_evidence'
    | 'style'
  priority: number
  required: boolean
  estimatedTokens: number
  content: string
}
```

### 8.3 编译算法

```text
1. 读取本章 Chapter Contract。
2. 从合同提取角色、地点、物品、剧情线、伏笔等 Entity Set。
3. 读取这些实体截至 chapter - 1 的有效状态。
4. 读取当前 Arc 和全局 Story Spine。
5. 读取上一章 handoff、上一章末尾和最近 2～3 章摘要。
6. 沿事件图和因果图扩展一跳必要依赖。
7. 对旧历史执行 SQL / FTS / vector 多路召回。
8. 按硬规则、时间、实体重合、剧情线重合、语义分数排序。
9. 去除 Canon、摘要和正文证据之间的重复内容。
10. 根据任务类型分配预算并打包。
11. 超预算时从最低优先级块裁剪；required block 超预算则拒绝执行。
12. 保存 ContextPackage、来源、裁剪记录和 checksum。
```

### 8.4 推荐预算

假设本地模型 64K context、最大输出 8K：

```text
模型窗口                         64K
- 预留生成输出                    8K
- 预留 system/tools/格式波动       6K
- 安全缓冲                       10K
= 理论输入上限                   40K

实际目标输入                     12K～20K
硬上限                           24K
```

不要因为窗口有 64K 就主动填满 56K。小模型通常在较短、结构清晰的输入上更稳定。

### 8.5 超预算处理

按以下顺序裁剪：

1. 删除低分风格示例。
2. 减少非必要 RAG 证据。
3. 缩短最近摘要，但不删除上一章 handoff。
4. 删除一跳扩展中的非直接实体。
5. 将 Arc 历史节点压缩成状态结果。
6. 仍超预算则停止，不发送给模型。

绝不能只记一条 Warning 后继续请求。

---

## 9. 摘要金字塔：避免摘要越来越长和越来越假

### 9.1 层级

| 层级 | 输入来源 | 推荐长度 | 用途 |
|---|---|---:|---|
| Scene Digest | 单个场景正文 + Delta | 80～150 字 | 本章内衔接 |
| Chapter Digest | 本章 Scene Digests + Delta | 250～500 字 | 最近章节窗口 |
| Arc Digest | 本 Arc Chapter Deltas | 700～1200 字 | 当前篇章上下文 |
| Story Spine | 各 Arc 结果 + 全局约束 | 1000～1800 tokens | 全局恒定上下文 |

### 9.2 禁止递归摘要退化

不能长期执行：

```text
摘要 A + 摘要 B → 摘要 C
摘要 C + 摘要 D → 摘要 E
摘要 E + 摘要 F → 摘要 G
```

这种方式每轮都会丢失实体、时间和因果细节。

正确方式：

- Scene Digest 从场景正文和 Scene Delta 生成。
- Chapter Digest 从 Scene Deltas 生成，必要时参考正文证据。
- Arc Digest 从结构化 Chapter Deltas 和蓝图节点重建。
- Story Spine 从 Arc 的状态结果重建。

摘要是 Materialized View，可以重建，不是唯一事实。

### 9.3 摘要更新频率

- Scene Digest：每个场景提交时。
- Chapter Digest：每章定稿时。
- Arc Digest：每章增量更新；Arc 结束时完整重建一次。
- Story Spine：仅在 Arc 状态、核心约束或未来计划变化时更新。

这样不需要每一章都让模型重新总结整本书。

---

## 10. 小说状态图谱

不需要一开始引入 Neo4j。SQLite 足以保存图结构，先使用邻接表和索引。

### 10.1 核心实体

```text
Character
Location
Item
Organization
Secret
Event
PlotThread
Hook
OutlineNode
Scene
Chapter
```

### 10.2 事件模型

```ts
interface NarrativeEvent {
  id: string
  projectId: string
  branchId: string
  chapterNumber: number
  sceneNumber: number
  sequence: number
  type: string
  subjectId: string
  predicate: string
  objectId?: string
  value?: string
  locationId?: string
  storyTime?: string
  sourceRevisionId: string
  sourceSpanStart: number
  sourceSpanEnd: number
  confidence: number
  status: 'proposed' | 'confirmed' | 'superseded'
}
```

### 10.3 状态版本

不能只保存人物“现在是什么状态”，还要保存状态变化：

```ts
interface EntityStateVersion {
  entityId: string
  field: string
  value: unknown
  validFromChapter: number
  validFromScene: number
  validToChapter?: number
  sourceEventId: string
  branchId: string
}
```

这样可以查询 `state_as_of(chapter=50)`，也可以在修改第 20 章后从第 20 章之前的状态重新播放 Delta。

### 10.4 因果边

```ts
interface CausalEdge {
  causeEventId: string
  effectEventId?: string
  expectedEffectOutlineNodeId?: string
  relation: 'enables' | 'causes' | 'motivates' | 'reveals' | 'blocks'
  required: boolean
}
```

写第 80 章反转时，系统先查询该反转的 required causes 是否都已确认，而不是要求模型阅读前 79 章。

### 10.5 伏笔生命周期

```text
planned → planted → reinforced → revealed → resolved
                 └──────────────→ abandoned（必须有理由）
```

每个 Hook 至少保存：

- 计划埋设章节范围；
- 实际埋设事件和证据；
- 预计强化次数；
- 最晚回收章节；
- 当前状态；
- 关联剧情线和角色。

因此“第 76 章该回收什么”变成一次 SQL 查询，而不是让模型从百万字中回忆。

---

## 11. ChapterDelta：每章只处理变化

### 11.1 数据结构

```ts
interface ChapterDelta {
  projectId: string
  branchId: string
  chapterNumber: number
  revisionId: string
  blueprintVersion: number

  eventsAdded: NarrativeEvent[]
  stateChanges: StateChange[]
  relationshipsChanged: RelationChange[]
  knowledgeGranted: KnowledgeChange[]
  itemsTransferred: ItemTransfer[]
  plotThreadsAdvanced: PlotAdvance[]
  hooksChanged: HookTransition[]
  factsAdded: FactRecord[]
  factsRetracted: FactRetraction[]

  blueprintCoverage: CoverageResult[]
  handoff: string
  digest: string
  extractionConfidence: number
}
```

### 11.2 提交协议

```text
Draft
  ↓
Scene/Chapter Delta Extraction
  ↓
Schema Validation
  ↓
Precondition Check against state_as_of(chapter - 1)
  ↓
Blueprint Transition Check
  ↓
Deterministic Quality Gates
  ↓
Ambiguous Issues → Local Judge
  ↓
Patch if needed → re-extract changed scene only
  ↓
SQLite Transaction Commit
  ├── finalized revision
  ├── events
  ├── state versions
  ├── plot/hook transitions
  ├── chapter digest
  └── audit record
  ↓
Async index update
```

### 11.3 原子性要求

以下内容必须在同一个逻辑提交中完成：

- 章节 revision 被标记为 finalized；
- ChapterDelta 被确认；
- Canon 状态完成更新；
- Chapter Digest 保存；
- 提交日志保存。

如果 Canon 写回失败，不允许出现“正文已经 finalized，但下一章仍使用旧状态”的情况。向量索引可以异步重试，因为它是可重建层。

---

## 12. 增量审稿：能用程序解决的，不调用模型

### 12.1 四级检查频率

| 级别 | 触发点 | 范围 | 默认是否调用 LLM |
|---|---|---|---:|
| Scene Gate | 每个场景生成后 | 当前场景 + 本章状态 | 否，歧义时才调用 |
| Chapter Gate | 每章定稿前 | 当前章 + 相关 Canon | 可选一次 |
| Arc Gate | 每 5 章或风险触发 | 当前 Arc 增量 | 仅异常时 |
| Global Audit | Arc 结束/重大改写 | 图谱和摘要，不是全文 | 是，但低频 |

### 12.2 确定性检查

无需 LLM 即可完成：

- 正文是否为空、是否混入思考标签或系统说明；
- 字数和段落长度；
- 必须事件是否在 Delta 中出现；
- 禁止状态变化是否发生；
- 人物是否在不可能地点；
- 物品是否被重复持有；
- 人物是否使用尚未获得的知识；
- Hook 是否超出最晚回收章节；
- 因果前置是否缺失；
- 章节 revision、Delta 和 Canon version 是否一致；
- 近似重复段落和高频句式。

### 12.3 重复检测

重复问题不能只交给写作模型自检。推荐组合：

1. 规范化段落后做 exact hash，检测完全重复。
2. 使用 5～8 字符 shingles + MinHash，检测段落结构近似重复。
3. 使用 SimHash 检测轻微改词重复。
4. 用 embedding 召回高相似历史段落，作为候选而不是最终结论。
5. 对高相似候选使用轻量 Rerank 或本地 Judge 判断是否属于合理呼应。
6. 维护最近 10 章的高频开头、动作模板、比喻和过渡句黑名单。

建议初始阈值，需要以样本集校准：

```text
Exact normalized hash              → 直接阻止
MinHash estimated Jaccard ≥ 0.82   → HIGH candidate
SimHash Hamming distance ≤ 3       → HIGH candidate
Embedding cosine ≥ 0.94            → 进入 Judge，不直接删除
连续重复 n-gram 高频异常            → WARNING / REPAIR
```

### 12.4 蓝图匹配不是“语义打个分”

Chapter Contract 中的目标必须转成可检查转移：

```text
Before:
  protagonist.knowledge does_not_include secret-A

Required event:
  protagonist discovers ledger

After:
  protagonist.knowledge includes clue-A
  protagonist.knowledge does_not_include full-secret-A

Hook:
  missing-page is planted
```

模型可以帮助从正文抽取 Delta，但规则引擎负责将 Delta 与 Contract 对账。

### 12.5 风险驱动调用模型

仅当以下情况发生时调用审稿模型：

- 规则发现潜在冲突，但无法确定是否为闪回、比喻或误报；
- Delta 抽取置信度低；
- 章节属于关键反转、主角死亡、阵营变化等高风险节点；
- Rerank 候选之间非常接近；
- Arc Drift 超过阈值；
- 用户要求文学性深度审稿。

这样比“每章让大模型重新审全文和全部历史”高效得多。

---

## 13. 局部修稿：以 Patch 为默认交付物

### 13.1 Issue 必须可定位

审稿器不能只输出“这一章有重复，请重写”。必须输出：

```ts
interface ReviewIssue {
  issueId: string
  type: string
  severity: 'blocker' | 'error' | 'warning'
  chapterNumber: number
  sceneId: string
  spanStart: number
  spanEnd: number
  sourceHash: string
  evidenceRefs: string[]
  affectedEntities: string[]
  expectedState?: unknown
  actualState?: unknown
  instruction: string
  confidence: number
}
```

### 13.2 Patch 操作

```ts
type PatchOperation =
  | { op: 'replace_span'; start: number; end: number; text: string; expectedHash: string }
  | { op: 'delete_span'; start: number; end: number; expectedHash: string }
  | { op: 'insert_after'; offset: number; text: string; expectedHash: string }
  | { op: 'move_scene'; sceneId: string; afterSceneId: string }
```

Patch 应满足：

- 有原文 hash 作为乐观锁；
- 只给 Patcher 当前场景、问题和必要证据；
- Patch 后只重新检查受影响 span、相邻段落和对应 Delta；
- 若语义状态未变化，不重建整章 Canon；
- 若状态变化，只重算相关 Scene Delta 和 Chapter Delta 差异。

### 13.3 修改等级

| 等级 | 修改类型 | 重算范围 |
|---|---|---|
| L0 | 标点、用词、句式 | 当前 span |
| L1 | 场景表达或局部事实 | 当前 scene + chapter digest |
| L2 | 人物状态、物品、知识、Hook | 相关实体依赖闭包 |
| L3 | 核心蓝图或结局约束 | 重新规划未来章节，默认不自动改写已定稿历史 |

---

## 14. 多章节错误：从最早分叉点修复

### 14.1 为什么不能直接重写多章

如果第 20 章错误地让人物知道秘密，而第 23、31、45 章都使用了这条知识，单独改第 20 章会让后面全部悬空；直接重写第 20～45 章又成本过高且容易引入新问题。

正确流程：

```text
1. 找到 earliest_bad_event。
2. 查询依赖该事件的状态变化、因果边、剧情线和章节。
3. 形成 Impact Set，而不是简单取“之后所有章节”。
4. 在临时 branch 上修改最早错误 span。
5. 从错误前的 state snapshot 重放后续 ChapterDelta。
6. 无冲突章节直接保留。
7. 仅对无法重放的章节生成 Repair Issue。
8. 对受影响场景逐个 Patch。
9. 通过后将 branch 原子切换为主版本。
```

### 14.2 影响分析

```sql
-- 概念查询，不是最终 SQL
WITH RECURSIVE impact(id) AS (
  SELECT :bad_event_id
  UNION
  SELECT edge.effect_event_id
  FROM causal_edges edge
  JOIN impact ON edge.cause_event_id = impact.id
)
SELECT DISTINCT chapter_number
FROM narrative_events
WHERE id IN impact;
```

影响范围还应包括：

- 读取了该知识的人物行为；
- 使用了该物品的事件；
- 由该关系变化触发的事件；
- 引用了该 Hook 的后续节点；
- 以该状态为前置条件的蓝图节点。

### 14.3 提前发现原则

多章纠错是兜底，不是默认流程。每章提交时已经执行：

- `state before → event → state after` 校验；
- Hook 状态转移；
- Blueprint coverage；
- 因果 prerequisite；
- 最近 Arc drift 更新。

多数错误应在产生的当章被阻止，而不是积累二十章后再发现。

---

## 15. Agent 调度方式

### 15.1 Agent 职责

| Agent / Service | 输入 | 输出 | 是否应看到完整正文 |
|---|---|---|---:|
| Planner | Story Spine、Arc、蓝图、当前状态 | Chapter Contract | 否 |
| Context Compiler | chapter/task/model budget | Context Package | 程序服务 |
| Writer | Draft Context Package | 场景或章节正文 | 只看所需证据 |
| Extractor | 当前场景/章节 + schema | Scene/Chapter Delta | 只看当前文本 |
| Deterministic Validator | Delta、Canon、Contract | Issues | 程序服务 |
| Judge | 歧义 Issue + 局部证据 | 判定/评分 | 否 |
| Patcher | Issue + 目标 span + 邻接上下文 | Patch Operations | 否 |
| Committer | revision + verified delta | transaction result | 程序服务 |
| Arc Auditor | Arc Deltas + 图谱异常 | Arc audit | 不默认看全部正文 |

### 15.2 同一个本地模型可以承担多个角色

不要求必须配置多个模型。第一版完全可以使用同一个本地模型，但必须：

- 每个角色使用独立 system prompt；
- 每个任务使用独立会话；
- Writer 不看到 Review 的长推理历史；
- Reviewer 不看到 Writer 的思考过程；
- Extractor 只输出结构化 Delta；
- 每个输出都通过 schema validation；
- temperature、max tokens 和采样参数按角色配置。

### 15.3 不让 Agent 自主维护正史

Agent 可以提议：

- 新事实；
- 状态变化；
- 蓝图调整；
- Patch。

但只有 Commit Protocol 能写入 confirmed Canon。否则不同 Agent 会互相覆盖或把错误摘要当成事实。

### 15.4 Hermes 子 Agent 的正确用法

每个子 Agent 接收的不是完整聊天 fork，而是：

```json
{
  "job_id": "novel-job-001",
  "project_id": "p-001",
  "chapter": 51,
  "task": "review",
  "context_package_id": "ctx-abc123"
}
```

子 Agent 再调用插件工具读取任务专用上下文。这样写到第 300 章时，Hermes 对话本身仍然可以很短。

---

## 16. 插件工具契约

建议 Harness 只看到粗粒度、可靠的领域工具：

```text
novel.create_project
novel.plan_book
novel.prepare_chapter
novel.run_chapter
novel.get_run_status
novel.pause_run
novel.resume_run
novel.audit_arc
novel.repair_issue
novel.export_manuscript
```

内部 Agent 或受信任调试模式可看到细粒度工具：

```text
novel.compile_context
novel.get_entity_state
novel.query_events
novel.query_hooks
novel.retrieve_evidence
novel.submit_scene
novel.extract_delta
novel.validate_delta
novel.propose_patch
novel.apply_patch
novel.commit_chapter
novel.compute_impact
novel.replay_branch
```

`novel.run_chapter` 不应让 Harness 自己循环几十次底层工具。插件内部负责：

```text
prepare → draft → extract → validate → patch → finalize → commit
```

Harness 只接收状态和最终结果。

---

## 17. 数据库最小扩展

DSH Novel 应在自己的 SQLite 中从零建立以下表，不复制 Vela migration，也不要求兼容 Vela 表结构：

```text
story_arcs
outline_nodes
scenes
chapter_contracts
chapter_deltas
narrative_events
entity_state_versions
causal_edges
hooks
hook_transitions
review_issues
patch_operations
context_packages
context_evidence_refs
canon_commits
branches
index_jobs
```

关键索引：

```text
narrative_events(chapter_number, scene_number, sequence)
narrative_events(subject_id, predicate)
entity_state_versions(entity_id, field, valid_from_chapter)
hooks(status, due_chapter)
outline_nodes(arc_id, chapter_number)
review_issues(status, severity, chapter_number)
context_evidence_refs(context_package_id)
```

不建议 MVP 引入独立图数据库。SQLite 递归 CTE、普通索引和 Materialized View 足以覆盖第一阶段；只有在实体/边数量和复杂图算法成为真实瓶颈后再评估 Neo4j、Kuzu 或其他图存储。

---

## 18. Embedding 与 Rerank 的正确位置

### 18.1 多路召回顺序

```text
1. SQL 精确状态查询                    必须
2. 实体 / Hook / Plot 倒排查询          必须
3. SQLite FTS 中文分词检索              建议
4. Embedding 语义召回                  可选增强
5. Rerank 候选重排                     可选增强
6. Context Budget Packer               必须
```

Embedding 不能判断：

- 一个事实是否仍然有效；
- 物品当前属于谁；
- 人物是否在某章已经知道秘密；
- Hook 是否已经 resolved。

这些必须由结构化状态查询完成。

### 18.2 什么时候值得用 Rerank

- 历史正文 chunk 很多；
- 语义召回容易找到相似但时间错误的段落；
- 需要区分伏笔原点、强化和回收；
- 关键章节需要更高召回精度。

MVP 可以用规则融合：

```text
final_score =
  entity_overlap * 0.30
  + plot_thread_match * 0.25
  + recency_or_required_time * 0.20
  + vector_similarity * 0.15
  + source_authority * 0.10
```

后续再引入本地 cross-encoder reranker。

---

## 19. 成本与调用次数控制

### 19.1 每章不应调用“全局总结模型”

推荐基础调用：

```text
Context 编译                  0 次 LLM
章节/场景写作                 1～4 次 LLM
Delta 抽取                    1 次小模型，或由可靠结构化输出合并
规则检查                      0 次 LLM
歧义 Judge                    0～1 次 LLM
局部 Patch                    0～N 次，仅问题 span
Commit                        0 次 LLM
Embedding                     增量，仅新/变更 chunk
Arc Audit                     每 5 章或风险触发 0～1 次
```

### 19.2 可缓存内容

- L0 Hard Rules；
- Story Spine；
- 当前 Arc State；
- 模型和工具定义；
- 当前章节写作前不变的角色设定。

动态内容放在后缀：

- Chapter Contract；
- 上一章 handoff；
- 当前 scene；
- 检索证据；
- Issue / Patch 指令。

这可以提高支持 Prompt Cache 的模型命中率，但缓存是性能优化，不是记忆方案。

---

## 20. 失败恢复与可审计性

每个阶段必须持久化：

```text
PREPARING_CONTEXT
CONTEXT_READY
DRAFTING
DRAFT_SAVED
EXTRACTING_DELTA
VALIDATING
PATCHING
READY_TO_COMMIT
COMMITTING
COMMITTED
FAILED_RETRYABLE
PAUSED
```

每次模型调用记录：

- model / parameters；
- context_package_id；
- prompt checksum；
- 输入输出 token；
- 输出 revision；
- schema validation 结果；
- 失败类别和重试次数。

日志默认记录摘要和 hash，不复制全部敏感正文。正文、Prompt 快照和模型输出保存在本地项目数据目录，并允许配置保留期限。

---

## 21. 验收测试

### 21.1 Context 稳定性测试

准备相同复杂度的第 10、100、300 章任务：

- 编译后的 Context Package 均不超过硬预算；
- 第 300 章不会加载前 299 章全部事件；
- required evidence 不被裁剪；
- 同一输入和 Canon version 编译结果可重现；
- 重复信息比例低于设定阈值。

### 21.2 Canon 回放测试

- 从 ChapterDelta 1 重放到 300，当前状态与 Materialized View 一致；
- 修改第 50 章后能从 49 章 snapshot 重放；
- 未受影响章节不产生无意义 revision；
- 向量索引删除后可以完整重建。

### 21.3 重复检测测试

测试集包含：

- 完全重复段落；
- 只替换人物名的重复；
- 改写少量词语的重复；
- 合理的意象呼应；
- 合理的重复台词；
- 结构雷同但含义不同的场景。

目标不是删除所有相似文本，而是高召回候选 + 低误删。

### 21.4 多章影响测试

构造：

- 第 10 章获得物品；
- 第 20 章使用物品；
- 第 40 章物品被毁；
- 修改第 10 章为未获得物品。

系统应定位第 20、40 章为影响节点，而不是默认重写第 11～40 章全部内容。

### 21.5 长跑测试

至少执行：

- 100 章自动化 dry-run，正文可用合成文本；
- 300 章状态写回与 context compilation 压力测试；
- 随机在 10 个历史章节注入变更并验证影响闭包；
- 随机终止进程，验证恢复不重复提交。

---

## 22. MVP 边界

### Now：第一条可运行闭环

- SQLite Chapter Contract、ChapterDelta、Event、State Version、Hook 表。
- Context Compiler v1，具有真实硬 token budget。
- 当前章 + 上一章 + 最近 3 章 + 相关实体状态。
- 精确重复、MinHash/SimHash 和基础状态校验。
- Issue span 定位和 Patch 协议。
- 单章 `prepare → write → validate → patch → commit`。
- 5 章连续全本地模型验证。

### Next：支撑 100 章

- Story Arc 和 Story Spine Materialized Views。
- 实体/事件/Hook 图查询。
- 多路检索和 Context 去重。
- 影响分析与 Branch Replay。
- 100 章合成压力测试。
- 风险驱动 Arc Audit。

### Later：百万字增强

- 本地 Rerank 模型。
- 针对 Canon Delta 的专用抽取模型或微调。
- 关键章节多候选生成和重排。
- 更细的因果路径和读者期待模型。
- 300 章、百万字真实长跑基准。

### 暂不进入 MVP

- Neo4j 等独立图数据库；
- 多租户和复杂权限系统；
- 每章强制大模型审稿；
- 所有角色都使用不同模型；
- 全书每章后重新总结；
- 自动重写大范围已定稿章节。

---

## 23. 对当前方案的最终建议

### 23.1 不要从“优化 Prompt”开始

最优先的不是继续调整 Vela Prompt，而是建立这三个核心协议：

1. `ChapterContract`
2. `ChapterDelta`
3. `ContextPackage`

只要这三者没有稳定，Embedding、Rerank、多个 Agent 和更大模型都会放大系统复杂度。

### 23.2 不把 Vela 当前 Canon 当作新项目基础

Vela 的 Canon 只作为行为参考。DSH Novel 需要独立实现：

- 从“当前一行状态”升级到“状态版本 + Delta”；
- 从“全量渲染”升级到“相关实体查询”；
- 从“非关键后处理”升级到“提交事务”；
- 从“压缩字符串”升级到“摘要金字塔”；
- 从“告警预算”升级到“硬预算编译器”。

### 23.3 Harness 只管理任务，不管理小说上下文

Harness 应知道：

- 当前执行哪一本书、哪一章；
- 处于哪个状态；
- 哪个 Agent 失败；
- 是否通过 Gate；
- 最终输出在哪里。

Harness 不应该知道：

- 该注入哪些历史人物卡；
- 第 12 章哪一段是伏笔证据；
- 如何查询角色第 50 章状态；
- 哪些旧摘要应该被压缩。

这些全部由 Novel Plugin 的 Context Compiler 和 State Store 决定。

### 23.4 第一阶段最小验证

选择一部有 10～20 章蓝图的测试小说：

1. 用新协议只生成连续 5 章。
2. 记录每章 Context Package 的每一层 token。
3. 检查输入是否随章节数增长。
4. 人工标记重复、状态、蓝图和衔接问题。
5. 调整规则和数据结构，而不是先调整文学 Prompt。
6. 5 章稳定后扩大到 20 章，再做 100 章合成长跑。

---

## 24. 第一批工程任务

1. 为 DSH Novel 创建独立 SQLite schema 与单调 migration；Vela 数据导入放入 Later，并采用只读中间格式。
2. 定义 `ChapterContract`、`ChapterDelta`、`ContextPackage` Pydantic/TypeScript Schema。
3. 实现 `state_as_of(chapter, scene)` 查询。
4. 实现实体相关性集合和一跳依赖查询。
5. 实现 Context Budget Packer 和 required/optional 裁剪策略。
6. 实现 Context provenance、checksum 和编译日志。
7. 实现段落 normalized hash、MinHash、SimHash 候选检测。
8. 将 Canon 写回并入 finalized transaction。
9. 将 Reviewer 输出改为 `ReviewIssue[]`，带 span 和 evidence。
10. 实现 hash 保护的 Patch Operations。
11. 将 Writer、Extractor、Reviewer、Patcher 的上下文彻底隔离。
12. Context Compiler 禁止全量 Timeline 注入，只允许相关实体 + 时间窗口查询。
13. Prompt Packer 禁止重复注入 Architecture / Canon / Character State 的同义内容。
14. 为 5、20、100、300 章建立 context-size regression 测试。
15. 通过插件提供一个粗粒度 `novel.run_chapter` 闭环。

---

## 25. 参考资料

- [Hermes Agent Context Compression and Caching](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md)
- [Hermes Agent Memory Providers](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md)
- [Letta Memory Blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/)
- [SillyTavern World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)
- [SillyTavern Data Bank / Vector Storage](https://docs.sillytavern.app/usage/core-concepts/data-bank/)
- [RecurrentGPT: Interactive Generation of Arbitrarily Long Text](https://github.com/aiwaves-cn/RecurrentGPT)
- [Re3: Generating Longer Stories With Recursive Reprompting and Revision](https://arxiv.org/abs/2210.06774)
- [DOC: Improving Long Story Coherence With Detailed Outline Control](https://arxiv.org/abs/2212.10077)
- [DOME: Dynamic Hierarchical Outlining with Memory-Enhancement](https://arxiv.org/abs/2412.13575)
- [ConWriter: Transition-Constrained Stateful Long-Form Story Generation](https://arxiv.org/abs/2608.05169)
