# Vela 参考与复用审计

> 文档角色：第三方参考边界与迁移依据  
> 状态：定稿  
> 版本：1.0  
> 日期：2026-08-22  
> 参考项目：`/Users/samni/Desktop/开发项目/vela`  
> 参考版本：`4f7e805aaa98a36ddab00a9121705eb1cc751bcb`

主方案：[DSH Novel 独立小说插件最终方案](./DeepSeek-Harness-Novel-Plugin-Development-Plan.md)

---

## 1. 最终边界

Vela 是第三方 GPL-3.0 小说 IDE。本项目只把它用于代码阅读、行为研究、失败案例和产品验证。

Vela 不是：

- DSH Novel 的上游仓库；
- Git submodule；
- 运行时依赖；
- 小说数据库事实源；
- 必须兼容的内部 API；
- 第一阶段的数据迁移目标。

DSH Novel 必须在 Vela 不存在时完整运行。

---

## 2. 许可证策略

Vela 根许可证为 GPL-3.0。直接复制、修改或组合其代码可能对分发许可证产生义务。为了保持新项目的独立性，默认采取：

```text
理解行为
→ 写成独立规格和测试
→ 重新设计数据结构
→ 在新仓库独立实现
```

工程规则：

1. 不直接复制完整文件、类、Prompt 或 SQL migration。
2. 不把 Vela 目录加入 Python/Node import path。
3. 不从 DSH Novel 构建脚本读取 Vela 源码。
4. 不在发布包中包含 Vela 构建产物。
5. 若确需复制有实质性的代码，先暂停实现，记录来源并确定项目许可证后再继续。
6. `THIRD_PARTY_NOTICES.md` 必须记录任何实际进入本项目的第三方代码。

这是工程风险控制，不替代正式法律意见。[GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html) 也说明了不同组合和交互方式可能导致不同许可结果；实际分发前应按最终代码关系复核。

---

## 3. 已确认可参考的能力

### 3.1 数据持久化概念

Vela 已经验证以下实体在小说工作流中有价值：

- project core；
- blueprints；
- characters；
- drafts / revisions / reviews；
- auto novel run；
- post-process steps；
- timeline events；
- character state；
- plot lines；
- facts；
- chapter summaries。

借鉴方式：重新设计本项目 Schema，引入 version、source span、valid range、branch 和 commit，不复制原表定义。

### 3.2 自动工作流概念

可借鉴：

- 生成、审稿、修稿、定稿分阶段；
- transport retry、模型输出恢复和业务修稿轮次分开计数；
- 草稿和 revision 保留；
- 后处理步骤可恢复；
- finalized 后生成摘要和人物状态。

需要重构：

- Canon 写回必须进入定稿事务；
- Reviewer Issue 必须可定位；
- 修稿默认 Patch；
- Writer / Reviewer / Patcher 上下文隔离；
- Run 权威转移到独立 Sidecar。

### 3.3 一致性概念

可参考模块：

- `src/services/narrative-consistency/validator.ts`
- `src/services/narrative-consistency/canon-store.ts`
- `src/services/narrative-consistency/drift-monitor.service.ts`
- `src/services/narrative-consistency/consistency-gate.service.ts`
- `src/services/chapter-content-guard.ts`

借鉴内容：检查维度和失败样本。

不直接复用实现，原因包括：

- 启发式词表和正则覆盖有限；
- 多个模块没有统一进入提交协议；
- 当前状态缺少完整历史有效期；
- 与 IPC、前端 Store 和现有数据库结构耦合。

### 3.4 检索概念

可参考：

- Embedding 维度校验；
- 向量失败后的降级思路；
- 章节范围过滤；
- 文档与 chunk 分离。

新项目重新实现：

- SQL 精确查询优先；
- source scope 分离；
- 中文 FTS；
- 多路召回融合；
- Context provenance；
- 可选 Rerank。

---

## 4. 已确认不能沿用的关键逻辑

### 4.1 全量时间线

`context-builder.ts` 默认把当前章以前的时间线全部读取。该模式随章节线性增长，新项目禁止采用。

### 4.2 全人物和全 facts 注入

新项目必须先构造 `RelevantEntitySet`，再查询截至上一章的相关状态。

### 4.3 重复 Prompt 块

Vela 的 Architecture、Global Guidance、Character State、notes、RAG 和 Canon 存在重叠。新项目由唯一 Context Compiler 负责去重和预算。

### 4.4 Token warning

Vela 超预算后仍可能继续请求。新项目必须执行裁剪或返回 `CONTEXT_BUDGET_EXCEEDED`。

### 4.5 伪压缩

每五章截取摘要并拼接不是长期记忆。新项目使用 Scene / Chapter / Arc / Story Spine 摘要金字塔，且摘要可以从 Delta 重建。

### 4.6 粗糙审稿检索

正文前 200 字不能代表本章冲突。新项目通过 ChapterContract、实体、剧情线、Hook 和时间范围生成检索计划。

### 4.7 非关键 Canon 写回

正文 finalized 而 Canon 失败会污染下一章。新项目的 finalized revision 和 confirmed ChapterDelta 必须在同一逻辑事务中提交。

---

## 5. 复用矩阵

| Vela 资产 | 处理方式 | MVP |
|---|---|---:|
| 产品流程和失败案例 | 形成独立需求与测试 | 是 |
| 数据实体思想 | 重新设计 Schema | 是 |
| 自动 Run 状态思想 | 独立实现 | 是 |
| Prompt 内容 | 仅分析问题，不复制 | 否 |
| `chapter-content-guard` 规则 | 转写为独立测试，再实现 | 是 |
| Canon validator | 参考维度，独立实现 | 是 |
| SQLite repository 代码 | 不复制 | 否 |
| LanceDB vector store | 不复制，按 Adapter 接口重写 | Next |
| Electron IPC | 不引入 | 否 |
| React / Zustand / UI | 不引入 | 否 |
| Vela 数据库文件 | 不直接读写 | 否 |
| Vela 导入器 | 未来显式只读转换工具 | Later |

---

## 6. Clean-room 实施流程

每个参考功能按以下流程进入新项目：

```text
1. 在本文档记录参考文件和观察到的行为。
2. 写与 Vela 源码表达无关的输入/输出测试案例。
3. 由本项目 Schema 定义预期结果。
4. 在 Python Domain/Application 中独立实现。
5. 用合成数据验证，不连接 Vela 数据库。
6. 比较行为结果，不做代码逐行迁移。
```

代码评审检查：

- 是否出现 Vela 专属类名、IPC channel 或路径；
- 是否出现大段相同字符串或 Prompt；
- 是否出现 Vela 数据库表的无修改复制；
- 是否记录了第三方来源；
- 是否能在没有 Vela checkout 的 CI 中通过。

---

## 7. 未来 Vela 数据导入

只有当存在真实旧项目迁移需求时才实现。

原则：

- 独立命令运行，不作为 Novel Core 启动依赖；
- 只读打开 Vela 数据库；
- 先导出中间 JSON，再导入 DSH Novel；
- 不对原库执行 migration 或更新；
- 每条导入事实保留 `legacy_source`；
- 低置信度事实进入 proposed，不直接进入 confirmed Canon；
- 导入后运行完整一致性审计。

```text
Vela DB
→ read-only exporter
→ versioned neutral JSON
→ validation report
→ DSH Novel importer
→ proposed Canon
```

---

## 8. 可借鉴测试清单

将以下问题转写为本项目永久回归测试：

- Prompt 标签、JSON 或分析过程进入正文；
- 修稿候选未经检查直接合并；
- 审稿失败导致重新生成整章；
- 第 100 章加载前 99 章全部事件；
- token 超预算仍请求模型；
- 旧正文被 RAG 召回后产生复述；
- Canon 写回失败但章节已 finalized；
- 同一重试产生重复 revision；
- 人物知识、地点或物品状态冲突；
- 修改旧章后 Materialized View 未重建。

---

## 9. 最终判断

Vela 的最大价值不是一批可复制代码，而是一套已经暴露真实问题的实验样本。

DSH Novel 应当吸收：

- 小说领域需要持久状态；
- 自动流程需要恢复；
- Canon 和检索有价值；
- 局部修稿优于无条件重写；
- 日志和版本不可缺失。

DSH Novel 必须重新实现：

- 独立数据模型；
- 增量正史；
- 硬预算 Context Compiler；
- 原子 Commit；
- Patch 和影响闭包；
- 与 Harness 解耦的 Sidecar API。

这保证新项目可以参考 Vela 的经验，却不会继承其 Electron/UI 耦合、上下文膨胀和许可证不确定性。
