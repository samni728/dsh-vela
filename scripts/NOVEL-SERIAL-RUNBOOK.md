# DSH Novel 串行调度规范（Master Agent 版）

> 本地模型一次只服务一个请求。任何并发调用都会让整体速度骤降，
> 因此本项目**严格串行**，禁止并发。

## 串行流水线（唯一正确的驱动方式）

```
┌─────────────────────────────────────────────────────────┐
│ 1. 启动服务器（一次性）                                  │
│    ./start-novel-server.sh                               │
│    （幂等：已运行则跳过；统一读 novel-config.yml）        │
└─────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 2. 提交 autorun（非阻塞，立即返回）                      │
│    python3 novel-agent.py submit <project_id>            │
│    → 编排器守护线程串行执行：                            │
│       for 章节 in 计划:                                  │
│         写稿 → 规则检查 → LLM 审稿评分                   │
│         → 低于阈值/有 blocker → 带意见重写(≤10次)        │
│         → 通过 → 定稿 COMMITTED → 下一章                │
└─────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 3. 等待完成（只轮询，不调模型）                          │
│    python3 novel-agent.py wait <project_id>              │
│    → 直到 state ∈ {completed, failed,                    │
│                    completed_with_rework}                │
└─────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 4. 外部评分复核（autorun 结束后才能跑！）                │
│    python3 novel-agent.py reverify <project_id>          │
│    → 逐章重跑审稿，输出真实分数 + 具体意见               │
│      （重复/废话/bug/逻辑/bug/蓝图偏差）                 │
│    → needs_rewrite = [低分章节...]                       │
│    ★ 内置守卫：autorun 还在 running 时 reverify 会拒绝   │
└─────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 5. 低分章节重写（串行补写队列）                          │
│    python3 novel-agent.py submit <project_id>            │
│    → 编排器自动优先处理补写队列（needs_rewrite）         │
│    → 重写 → 再审稿 → 再打分 → 通过才定稿                │
│    循环 3→4→5 直到 needs_rewrite 为空                    │
└─────────────────────────────────────────────────────────┘
```

## 硬性规则（违反即错误）

1. **绝不并发**：同一时刻只允许一个模型请求。
   - autorun 运行时：只能 `status` / `wait`（纯轮询，零模型调用）。
   - reverify 只能在 autorun 完全结束后运行（脚本已内置守卫）。
2. **不要用同步 CLI**：`dsh-novel run-chapter` / `resume` 是同步阻塞的，
   模型生成一章要 15-25 分钟，任何调用方超时都会把 run 卡死在
   RUNNING/DRAFTING。一律用 `novel-agent.py` 的 autorun 驱动。
3. **不要手动改数据库**：改 runs/chapters 状态绕过状态机会造成
   不可恢复的悬挂状态。需要重试就重新 `submit`（编排器会从补写队列继续）。
4. **数据目录必须一致**：服务器、CLI、脚本统一读 `novel-config.yml`
   （data_dir=/Users/samni/Desktop/cowork/novel-data）。
   不要用 `~/.dsh-novel/config.yml` 的旧 data_dir。

## 每章内部循环（编排器自动完成，无需介入）

```
DRAFTING     写稿（模型生成 ~4000 字）
  ↓
VALIDATING   确定性规则检查（重复段落/跨章复述/短句循环/结尾残缺）
  ↓
REVIEWING    LLM 审稿评分（contract_adherence/era_authenticity/flow）
  ↓             overall = min(三维) < score_threshold(8.0) → blocker
  ↓
QUALITY_BLOCKED → 带审稿意见重写（回到 DRAFTING，≤ max_revisions=10 次）
  ↓
COMMITTING   通过 → 定稿 COMMITTED
  ↓
下一章
```

## 关键配置（novel-config.yml）

```yaml
review_enabled: true        # 必须开启 LLM 审稿
review_timeout_seconds: 600 # 审稿一次最长 10 分钟（实测需 ~4-7 分钟）
outline_timeout_seconds: 600
score_threshold: 8.0        # overall 低于此分 → 重写
max_revisions: 10           # 每章最多重写次数
model_timeout_seconds: 1200 # 写作一次最长 20 分钟
model_max_output_tokens: 8192
```

## 诊断命令

```bash
python3 novel-agent.py ensure-server   # 启动/确认服务器
python3 novel-agent.py status <pid>    # 快照（不调模型）
python3 novel-agent.py wait <pid>      # 等到终态（只轮询）
python3 novel-agent.py reverify <pid>  # 逐章复核分数（串行，守卫防并发）
python3 novel-agent.py export <pid>    # 导出 manuscript.md + README.md
```
