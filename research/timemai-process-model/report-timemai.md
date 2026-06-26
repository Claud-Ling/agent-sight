# TimemAi 进程模型观察报告

> 基于 AgentSight eBPF 录制的 7 个 session（T1-T5），从进程树、生命周期、并发、退出状态、父子关系、审计事件、时序等维度描述 TimemAi（Rust + DeepSeek）的运行时进程行为。

## §1 实验概览

**目标**：通过 AgentSight 观测 TimemAi 的运行时进程行为，与 Claude Code 形成对比，为 agentOS 进程模型设计提供跨 runtime 实证基础。

**实验方法**：录制 7 个 session，覆盖 5 类任务（T1-T5a/b/c），与 Claude Code 实验使用相同 prompt，仅在 runtime 不同（TimemAi Rust + DeepSeek vs Claude Code Bun + Anthropic API）。

| Session | 任务类型 | 进程数 | curl (LLM代理) | sh (工具代理) | 耗时 | 树深度 | 唯一comm |
|---------|---------|--------|---------------|-------------|------|--------|---------|
| T1 | 简单问答（eBPF 解释） | 2 | 1 | 0 | 9.6s | 1 | 2 |
| T2 | 多文件读取（架构阅读） | 29 | 4 | 4 | 65.1s | 2 | 10 |
| T3 | 代码修改（添加 --version） | 39 | 7 | 3 | 158.2s | 2 | 10 |
| T4 | 工具密集（TODO 扫描 + git） | 18 | 3 | 3 | 66.5s | 2 | 6 |
| T5a | agentOS 基础概念 | 15 | 4 | 1 | 89.9s | 2 | 8 |
| T5b | 进程模型设计 | 3 | 2 | 0 | 72.0s | 1 | 2 |
| T5c | 架构设计概要 | 5 | 4 | 0 | 62.7s | 1 | 2 |

**录制环境**：Linux 5.15, TimemAi（Rust release build），后端 `api.deepseek.com/chat/completions`（deepseek-v4, OpenAI-compatible 端点），AgentSight（本仓库构建）。非交互模式：`timem --once-json "<prompt>" --bash-approval approve`。

**方法学备注**：T5 采用与 Claude Code 实验相同的回退方案——3 个独立深度递进 session 替代交互式多轮 session。T5a 在简单 prompt 下录制正常，但 DeepSeek 模型在 T5a 早期版本中因 prompt 过长触发协议修复；已用缩短 prompt 重新录制。

**数据源说明**：AgentSight SQLite 数据库含 7 张表。以下 4 张表在 TimemAi session 中**均为空**：

- `llm_calls` — 空（AgentSight HTTP 解析器不识别 DeepSeek API 的 OpenAI-compatible 响应格式）
- `tool_calls` — 空（同上）
- `token_usage` — 空（同上）
- `network_targets` — 空（同上）

以下 3 张表**有完整数据**：

- `process_nodes` — 完整（39 进程 in T3），本报告的核心数据源
- `audit_events` — 完整（87 条 in T3），含 process/file 两类事件
- `resource_samples` — 完整（80 条 in T3），仅覆盖 `timem-native-rs` 根进程

因此，LLM 调用数以 `curl` 子进程数量为代理指标，工具调用数以 `sh` 子进程数量为代理指标。本报告的分析边界是**进程级行为**——token 级、网络目标级分析因数据缺失不可行。

---

## §2 核心发现：Per-Request Fork 模型 — 持续交替爆发

**TimemAi 的进程模型与 Claude Code 构成两个极端：CC 是"启动 burst + 稳态零子进程"，TM 是"每个 LLM 调用 + 每个工具调用都 fork 子进程"。**

### §2.1 决定性证据：进程创建贯穿整个 session，curl 与 sh burst 交替出现

T3 session（158.2s，39 进程）的 7 个创建集群完整展示了这一模式：

```
集群1 (+0.0s，2进程):    curl                                  — LLM#1 调用
集群2 (+0.3s，6进程):    sh → locale-check + grep×3 + ls       — 第1轮文件探索
集群3 (+38.5s，1进程):   curl                                  — LLM#2 调用
集群4 (+38.8s，11进程):  sh → locale-check + grep×5 + cat +    — 第2轮文件探索+修改
                         find×2 + head
集群5 (+68.5s，8进程):   curl + sh → locale-check + grep×2 +   — LLM#3 + 第3轮
                         cat×2 + find
集群6 (+99.0s，5进程):   curl + head → locale-check + grep×2    — LLM#4 + 第4轮
集群7 (+129.0s，6进程):  curl + sed → locale-check + grep×3     — LLM#5 + 第5轮(代码修改)
```

**关键特征**：
- curl 存活 30-40s（等待 DeepSeek API 返回），sh burst 存活 < 100ms
- action 之间严格串行：上一轮 sh burst 完全退出后才发出下一个 curl
- 最后 36s（`post_burst_tail_ms` = 36,237ms）无新子进程——模型在做最终输出
- 与 CC 的对比：CC 在 +17s 后零子进程，TM 在 +129s 仍在创建子进程

### §2.2 每次工具执行 fork 新进程链

与 CC 在 Bun 线程池内执行工具不同，TM 每次 `run_bash` 调用都产生 3-10 个子进程：`sh -c` 作为入口 + `locale-check` 环境检测 + N 个业务命令（grep/find/cat/sed）。这条进程链**每次工具调用都重新创建，无缓存复用**。

### §2.3 进程模型

```
timem-native-rs (Rust runtime, 长寿, 单线程事件循环)
  │
  ├─→ curl (LLM API 调用, 每轮1个, 存活30-40s等待响应)
  │     └─ 退出 → 解析响应 → 决定下一步
  │
  ├─→ sh → {locale-check, grep×N, find×M, cat, sed} (工具执行 burst)
  │     └─ 退出 (全部 < 100ms) → 收集输出 → 下一轮 LLM 调用
  │
  ├─→ curl (下一轮)
  └─→ sh → ... (下一轮工具)
  （循环反复直到任务完成）
```

这**是** per-request fork 模型——每次 LLM API 调用 = 1 个 curl 子进程，每次工具调用 = 1 个 sh + N 个命令子进程。子进程是 TimemAi 完成工作的**唯一**方式（无进程内 HTTP、无线程池）。

### §2.4 对 agentOS 的启示

- **进程边界 = agent 行动边界**。TM 的每个 LLM 调用和工具调用都对应明确的子进程，eBPF 可完整观测 agent 行为。这与 CC 形成互补——agentOS 需要同时支持"进程可见"和"进程不可见"两种 agent 执行模型。
- **子进程生命周期直接编码了工作单元语义**。curl 的 30-40s 存活 = INFRA（LLM I/O 等待），grep 的 1-2ms 存活 = ACTION（瞬时计算）。agentOS 可以用进程存活时间作为 INFRA/ACTION 分类的信号，而非依赖进程名。
- **进程创建模式反映了 agent 的认知-行动节奏**。curl→sh burst→curl 的交替模式直接对应"推理→行动→推理"的认知循环，agentOS 可以通过进程创建时序来推断 agent 的工作阶段。

---

## §3 进程树拓扑

### 核心发现：恒定 2 层深度，单根，无中间基础设施层

所有 7 个 session 的进程树深度 ≤ 2，且**有且仅有 1 个 agent 根**（`timem-native-rs`）。

```
timem-native-rs (唯一 agent 根, 深度 0)
  ├── curl (深度 1, LLM API 调用代理)
  └── sh / head / sed (深度 1, 工具入口进程)
      ├── grep (深度 2, 动作节点)
      ├── locale-check (深度 2, 环境检测)
      ├── find / cat / ls (深度 2, 文件探索)
      └── ...
```

**结构特征**：
- 树深度永远 2 层（timem → {curl, sh → tools}），无中间基础设施层（CC 有 3 层：claude → git/bash → tools）
- 无 `git` 子树（TM 不依赖 git 进行版本自检——CC 在启动期 fork 6 个 git）
- 无 worker 线程池进程（CC 有 `claude.exe` 短命 worker）
- `locale-check` 每次 `sh` 调用都携带——这是 TM 的进程指纹

**根 vs 孤儿**：所有 7 个 session 均无缺父孤儿（与 CC 的 `base64×6` 孤儿形成对比）。这是因为 TM 的子进程链短且存活时间长（curl 30-40s），eBPF 探针有充足时间捕获父子关系。

**T3 非 sh 入口点**：集群 6 和 7 中 `head`（PID 642140）和 `sed`（PID 642248）替代 `sh` 成为工具通道节点，各自携带 `locale-check` + `grep` 子树。这是 shell 在管道命令中优化掉 sh 包装层的结果——`run_bash` 传入的管道首命令被 exec 为入口进程。这说明 TimemAi 的"工具入口"不总是 `sh`，具体命令取决于 shell 的进程替换优化。

**对 agentOS 的启示**：2 层浅树是"无缓存、无基础设施复用"的直接体现——每次行动都从根创建全新的进程子树。agentOS 可以通过树深度判断 agent 的进程复用程度：深度浅 + 进程多 = per-request fork，深度深 + 进程集中在启动期 = 线程池稳态。

---

## §4 进程类型分布

以 T3（39 进程，最具代表性）为例：

| comm | 数量 | 占比 | 角色 | 分类 |
|------|------|------|------|------|
| grep | 14 | 35.9% | 文本搜索 | ACTION |
| curl | 7 | 17.9% | LLM API 调用代理 | INFRA |
| locale-check | 5 | 12.8% | 环境检测（进程指纹） | ACTION |
| sh | 3 | 7.7% | 工具执行器（通道） | CHANNEL |
| cat | 3 | 7.7% | 文件内容读取 | ACTION |
| find | 2 | 5.1% | 文件扫描 | ACTION |
| head | 2 | 5.1% | 输出截断 / 通道替代 | CHANNEL |
| timem-native-rs | 1 | 2.6% | 根进程 | INFRA |
| ls | 1 | 2.6% | 目录列表 | ACTION |
| sed | 1 | 2.6% | 文本替换 | ACTION |

跨 session 共享的稳定进程骨架：

| comm | 出现率 | 角色 | 说明 |
|------|--------|------|------|
| timem-native-rs | 7/7 | INFRA — 唯一长寿根 | session 全程存活，Rust 单线程事件循环 |
| curl | 7/7 | INFRA — LLM 调用代理 | 每次 LLM API 调用 = 1 个 curl，存活 9.6-42s |
| locale-check | 4/7 | ACTION — 进程指纹 | 每次 `run_bash` 携带，区别于 CC 的一次性快照 |
| grep | 4/7 | ACTION — 文本搜索 | 最高频工具（T3 占比 35.9%） |
| sh | 4/7 | CHANNEL — 工具入口 | 每次工具调用 fork 新 shell |

与 CC 的进程类型差异：

- TM 用 `find/grep/cat/ls` 做文件探索，CC 用 `git`（42.9% in T3）
- TM 无 `git` 子树（除非 prompt 明确要求，如 T4 的 `git log`）
- TM 无 worker 线程进程（`claude.exe` 等价物不存在）
- TM 的 `locale-check` 是每轮工具调用的固定开销，CC 的 `locale-check` 仅在首次 shell 快照出现一次

**对 agentOS 的启示**：进程类型分布是 agent 能力指纹。出现 `curl` + `grep` + `locale-check` 三元组可高置信度识别 TimemAi 类 agent。进程指纹的稳定性（跨 session 和跨任务）使其可用于 agent 身份识别和安全策略。

---

## §5 进程存活时间

### 核心发现：双峰分布 — INFRA 级 curl（万 ms）vs ACTION 级命令（个位 ms）

| Session | 全部 p50 | 全部 p95 | timem 根 (INFRA) | curl p50 (INFRA) | grep p50 (ACTION) |
|---------|---------|---------|-----------------|------------------|-------------------|
| T1 | 9,568ms | 9,568ms | 9,568ms | 9,563ms | — |
| T2 | 2ms | 38,511ms | 65,057ms | 11,003ms | 1ms |
| T3 | 2ms | 37,457ms | 158,207ms | 32,600ms | 2ms |
| T4 | 2ms | 33,938ms | 66,495ms | 9,591ms | 1ms |
| T5a | 3ms | 41,968ms | 89,888ms | 37,700ms | 2ms |
| T5b | 38,499ms | 38,499ms | 72,030ms | 38,499ms | — |
| T5c | 15,197ms | 34,551ms | 62,711ms | 15,197ms | — |

**p50 = 2-3ms** 在工具密集 session（T2-T4），因为大量瞬时 grep/cat 子进程拉低了中位数。但在纯推理 session（T5b/T5c），p50 跳到 15-38s——因为只有 curl 子进程（无瞬时 ACTION 进程）。

按**实测存活时间**分类——p50 > 1s 为 INFRA，p50 < 100ms 为 ACTION：

| comm | T3 p50 | 分类 | 解释 |
|------|--------|------|------|
| timem-native-rs | 158,207ms | INFRA | session 全长，Rust runtime |
| curl | 32,600ms | INFRA | 等待 DeepSeek API 响应 |
| find | 51ms | ACTION | 文件系统扫描 |
| sh | 34ms | ACTION | 工具执行通道 |
| sed | 19ms | ACTION | 文本替换 |
| head | 17ms | ACTION | 输出截断 |
| grep | 2ms | ACTION | 瞬时文本搜索 |
| cat | 1ms | ACTION | 瞬时文件读取 |
| locale-check | 1ms | ACTION | 瞬时环境检测 |

INFRA 与 ACTION 之间有 **4 个数量级**的存活时间差距（curl ~10⁴ms vs grep ~10⁰ms）。这与 CC 一致——两种 runtime 都在进程存活时间上呈现相同的双峰分布。差异在于：CC 的 INFRA 只有 1 个（claude 主进程），TM 的 INFRA 有 `N_llm_calls + 1` 个（N 个 curl + 1 个 timem）。

**对 agentOS 的启示**：存活时间是区分 INFRA/ACTION 的可靠信号，跨 runtime 通用。但 agentOS 需要处理"INFRA 数量可变"的情况——CC 的 INFRA 数 = 1（固定），TM 的 INFRA 数 = 1 + curl 调用数（动态）。

---

## §6 进程创建时序：多集群交替爆发 vs 启动集中爆发

### §6.1 集群数量与分布

| Session | 进程数 | 集群数 | 首集群进程 | 末集群进程 | post_burst_tail_ms | session 全长 |
|---------|--------|--------|-----------|-----------|-------------------|-------------|
| T1 | 2 | 1 | 2 | 2 | 0ms | 9.6s |
| T2 | 29 | 9 | 2 (curl) | 1 (curl) | 36,840ms | 65.1s |
| T3 | 39 | 7 | 2 (curl) | 6 (curl+sed→...) | 36,237ms | 158.2s |
| T4 | 18 | 7 | 2 (curl) | 1 (curl) | 6,935ms | 66.5s |
| T5a | 15 | 7 | 2 (curl) | 1 (curl) | 22,358ms | 89.9s |
| T5b | 3 | 2 | 1 (curl) | 1 (curl) | 30,508ms | 72.0s |
| T5c | 5 | 4 | 2 (curl) | 1 (curl) | 25,487ms | 62.7s |

### §6.2 时序模式

TM 的进程创建在时间轴上呈现**周期性 burst**，而非 CC 的"一次性启动 burst + 长尾零进程"：

```
TM:  curl ████████████░░░░░░░░░░░░░░░░░░░░░░░ sh-burst ██░ curl ████████████░░░░░░░░░░░░ sh-burst ██░ ...
     ├─ LLM I/O 等待 (30-40s) ─┤├ 工具执行 (<100ms) ┤├─ LLM I/O 等待 ─┤├ 工具 ┤

CC:  git-burst ████░ sh-snapshot ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ [稳态: 零进程]
     ├ 启动+引导 ~20s ┤├────────────── 稳态: 数十到上百秒，零新子进程 ──────────────┤
```

**核心差异**：TM 的进程创建与 agent 工作节奏同步（每轮推理→行动都创建进程），CC 的进程创建集中在启动引导期。TM 的 `post_burst_tail_ms`（末集群到 session 结束）是模型做最终输出的时间——这段时间内无新进程，与 CC 的稳态类似但性质不同（TM 是因为不再需要工具调用，CC 是因为工具在进程内执行）。

**对 agentOS 的启示**：进程创建频率是区分 agent 工作模式的关键指标。持续 burst = per-request fork 模型，集中 burst + 长尾零进程 = 线程池模型。agentOS 可以在 agent 启动后 ~30s 判断其进程创建模式，据此选择合适的调度策略。

---

## §7 进程并发度

| Session | 最大并发 | 总进程数 | 并发峰值时刻 |
|---------|---------|---------|------------|
| T1 | 2 | 2 | curl + timem |
| T2 | 4 | 29 | sh burst（grep×3 + find 等并行） |
| T3 | 7 | 39 | sh burst（grep×5 + find×2 等） |
| T4 | 3 | 18 | sh burst |
| T5a | 4 | 15 | sh burst（grep×2 并行） |
| T5b | 2 | 3 | timem + curl |
| T5c | 2 | 5 | timem + curl |

最大并发 7（T3），出现在 sh 工具执行 burst 内。并发进程全部是瞬时 ACTION（grep/find/locale-check），存活 < 100ms。curl 之间严格串行——同一时刻只有 1 个 curl 存活。

与 CC 对比：TM 的并发峰值更高（7 vs CC 的 6），但总进程数可能更低（T3: 39 vs CC 的 14——CC 进程少但并发集中）。TM 的并发是分布式的（分散在多个 sh burst），CC 的并发是集中式的（集中在启动 git burst）。

**对 agentOS 的启示**：per-request fork 模型的并发需求是**间歇性峰值**（每个 sh burst 短暂爆发后立即回落），而非持续高并发。agentOS 进程槽位按峰值 7-8 预留即可，但需要支持快速创建/销毁（ms 级）。

---

## §8 退出状态分布

### 核心发现：非零退出率 ~30%，远高于 CC 的 ~8%

| Session | 已退出 | 非零退出 | 失败率 | 非零退出明细 |
|---------|--------|---------|--------|------------|
| T1 | 2 | 0 | 0.0% | — |
| T2 | 29 | 9 | 31.0% | grep=1 (×8), ls=2 (×1) |
| T3 | 39 | 13 | 33.3% | grep=1 (×13) |
| T4 | 18 | 6 | 33.3% | grep=1 (×6) |
| T5a | 15 | 4 | 26.7% | grep=1 (×4) |
| T5b | 3 | 0 | 0.0% | — |
| T5c | 5 | 0 | 0.0% | — |

**全部非零退出均为 `grep=1`（无匹配）**，这是 grep 的正常语义（模式未找到返回 1），并非真错误。T2 额外有一次 `ls=2`（文件不存在）。没有观测到段错误、OOM、或信号终止。

与 CC 对比：
- TM 的非零退出率 26-33%（工具密集 session），CC 的 ~8%——差异来自 TM 大量使用 grep 做文件探索（每次 grep 无匹配 = exit 1）
- CC 的 `claude.exe=1` 非零退出（Bun worker 探测性退出）在 TM 中无等价物（TM 无 worker 进程）
- 两种 runtime 的非零退出多数是**预期的控制流信号**而非真错误（参见 [bestpractice_07-bash_strict_mode_pipes.md]）

**对 agentOS 的启示**：agent 子进程的非零退出**普遍且语义多样**——grep 无匹配（exit 1）是最常见的"语义性非零"。agentOS 的进程记账不能简单地把 `exit_code != 0` 当错误统计，需要区分"语义性非零"（grep 无匹配）和"真失败"（信号终止、非零退出来自非 grep 命令）。per-request fork 模型的非零退出率天然高于线程池模型。

---

## §9 父子关系模式

### 核心发现：两种稳定的 2-hop 模式，无孤儿

Top 父子对（跨 session）：

| 父子对 | 出现率 | 语义 |
|--------|--------|------|
| timem-native-rs → curl | 7/7 | LLM API 调用 |
| sh → grep | 4/7 | 文本搜索（每次工具调用内） |
| timem-native-rs → sh | 4/7 | 工具执行入口 |
| sh → locale-check | 4/7 | 环境检测（每次工具调用携带） |

**两种稳定的 2-hop 模式**：

1. **LLM 调用链**：`timem-native-rs → curl`（1-hop，直接子进程）
2. **工具执行链**：`timem-native-rs → sh → {grep, locale-check, find, cat, ...}`（2-hop，sh 是通道节点）

与 CC 对比：
- TM 的父子关系是**重复性**的——同一对 `timem-native-rs → curl` 在 T3 中出现 7 次（7 轮 LLM 调用），`sh → grep` 出现 9 次。CC 的父子关系是**一次性**的（如 git burst 只在启动期出现一次）。
- TM **无孤儿进程**（所有 7 个 session 的 `missing_parent_orphan_count` = 0）。CC 每 session 有 6 个 `base64` 孤儿（eBPF 管道中间进程竞态窗口）。这是因为 TM 的子进程存活时间更长（curl 30-40s），不会有亚毫秒管道中间进程的竞态问题。
- TM 无 `sh → sh` 链（无嵌套 shell），最大深度 2。CC 偶尔有 3 层嵌套。

**对 agentOS 的启示**：父子关系模式可以区分 agent 的进程复用行为。重复性父子对（同一对出现 N 次）= per-request fork 签名；一次性父子对 = 启动/引导期行为。agentOS 应区分这两类父子关系，对前者施加更宽松的审计策略（预期行为），对后者施加更严格的审计（稀有事件）。

---

## §10 审计事件分析

audit_events 表记录了 process（exec/exit）和 file（write）两类事件：

| Session | process 事件 | file 事件 | 总计 | 主要 comm |
|---------|------------|----------|------|----------|
| T1 | 4 | 2 | 6 | timem-native-rs, curl |
| T2 | 58 | 6 | 64 | grep(16), sh(12), curl(8) |
| T3 | 80 | 7 | 87 | grep(28), curl(14), sh(13) |
| T4 | 36 | 6 | 42 | grep(14), sh(9), curl(6) |
| T5a | 31 | 4 | 35 | curl(8), grep(8), sh(5) |
| T5b | 6 | 2 | 8 | timem-native-rs(4), curl(4) |
| T5c | 10 | 2 | 12 | curl(8), timem-native-rs(4) |

**关键发现**：

- **process 事件 ≈ 2×进程总数**（每进程 exec+exit），与 CC 一致。但 TM 的 process 事件分布在 session 全程（而非前 20s），直接反映了 per-request fork 的持续进程创建。
- **file write 事件少于 CC**（T3 中 7 次 vs CC T3 的 11 次）。TM 的 file write 全部由 `timem-native-rs` 执行（写 audit 日志到 `/tmp/timem-research-*/.test_mem/api_audit.jsonl`）。CC 的 file write 由 `Bun Pool N` 线程执行（工具输出写入临时文件）。
- **file write 执行者差异**：TM 的 file write comm = `timem-native-rs`（根进程），CC 的 file write comm = `Bun Pool N`（worker 线程）。这直接反映了两者的 IO 模型差异——TM 的 IO 在主线程（单线程事件循环），CC 的 IO 在 worker 线程池。
- 无 llm 类型审计事件（因为 `llm_calls` 表为空，AgentSight 未生成 LLM 审计事件）。

**对 agentOS 的启示**：audit 事件的时间分布直接反映进程模型。CC 的 process 事件集中在前 20s，TM 的 process 事件贯穿全程。agentOS 审计子系统应记录事件的时间戳分布作为 agent 行为分类的特征。file write 执行者 comm 揭示了 IO 架构（主线程 vs 线程池）。

---

## §11 进程-LLM 时序关联

**数据限制**：`llm_calls` 表为空，无法从 AgentSight 直接获取 LLM 调用的精确起止时间。但**进程创建集群的时序**提供了 LLM 调用的代理信号——每个 curl 子进程的 `start_timestamp_ms` 标记了一次 LLM 调用的发起，curl 的存活时间 = LLM I/O 等待的下界。

以 T3 为例，从进程集群推断的 LLM-工具时序：

| 轮次 | curl 启动偏移 | curl 存活 | 后续 sh burst 进程数 | 推断 |
|------|-------------|----------|-------------------|------|
| 1 | +0.0s | ~38s | 6 | LLM 返回文件探索指令 |
| 2 | +38.5s | ~30s | 11 | LLM 返回文件修改指令 |
| 3 | +68.5s | ~30s | 8 (含 curl) | LLM 返回进一步探索 |
| 4 | +99.0s | ~30s | 5 (含 curl) | LLM 返回最终探索 |
| 5 | +129.0s | ~29s | 6 (含 curl) | LLM 返回代码修改 |

**"认知↔行动"相位关系**：推理阶段（curl 存活，30-40s）→ 行动阶段（sh burst，< 100ms）→ 推理阶段 → 行动阶段 → ...。推理阶段的时间是行动阶段的 300-400 倍——TM 将 99.7% 的时间花在等待 LLM I/O 上。

与 CC 的差异：
- CC 的推理和行动都在进程内（Bun 线程池），eBPF 进程事件无法观测认知-行动节奏
- TM 的推理和行动都 fork 子进程（curl + sh），eBPF 可直接从进程创建模式推断认知-行动节奏
- TM 的进程模型天然对 eBPF 观测友好，CC 的进程模型需要补充线程/syscall 级追踪

**对 agentOS 的启示**：per-request fork 模型的 agent 可以通过进程事件推断工作节奏，无需 runtime 配合上报。线程池模型的 agent 需要 runtime 主动上报工具调用事件。agentOS 应支持两种观测路径——eBPF 进程事件（被动，适用于 fork 模型）和 runtime 上报（主动，适用于线程池模型）。

---

## §12 资源使用

| Session | RSS max (MB) | CPU max (%) | 采样数 | RSS 特征 |
|---------|-------------|------------|--------|---------|
| T1 | 16 | 0.0 | 5 | 稳定 16MB |
| T2 | 17 | 0.0 | 33 | 稳定 17MB |
| T3 | 17 | 0.5 | 80 | 稳定 17MB |
| T4 | 17 | 0.5 | 34 | 稳定 17MB |
| T5a | 17 | 0.5 | 45 | 稳定 17MB |
| T5b | 17 | 0.0 | 37 | 稳定 17MB |
| T5c | 17 | 0.0 | 32 | 稳定 17MB |

**关键发现**：

- **RSS 恒定 16-17MB**，不随 session 时长或进程数变化。TM 是 Rust 编译的单体二进制，内存占用极小且稳定。与 CC 的 100→317MB RSS 增长（Bun heap 扩张）形成数量级差异（~1/18）。
- **CPU max < 1%**。TM 的 CPU 时间几乎全部花在 I/O 等待（等 curl 返回 DeepSeek 响应）。计算密集的 LLM 推理在远端。与 CC 的 CPU max 33-41%（Bun 进程内文本处理 + JIT）形成数量级差异（~1/72）。
- **资源采样仅覆盖 `timem-native-rs` 根进程**——瞬时子进程（grep/cat/locale-check 存活 < 100ms）在秒级采样间隔内已退出。这与 CC 一致（采样仅覆盖 claude 主进程），但原因不同——CC 是因为稳态只有主进程，TM 是因为子进程太短命。
- TM 的资源效率优势来自架构选择：(a) Rust 编译型语言（无 JIT、无 GC），(b) 子进程 curl/sh 借用系统已有二进制（无额外内存开销），(c) LLM 推理完全外置。

**对 agentOS 的启示**：per-request fork 模型的资源效率极高（17MB RSS），agentOS 可以为每个此类 agent 预留极小的内存配额（~20MB）。但需要注意的是——TM 的 curl 子进程虽然不占 RSS，但消耗网络带宽和 TCP 连接（每个 curl = 1 个 TCP 连接到 DeepSeek API）。agentOS 的资源计量需要包含网络连接数，不能仅看 RSS。

---

## §13 协议修复（Protocol Repair）行为

在 T3（7 curl）和 T5a（4 curl）中，DeepSeek 模型的响应不符合 TimemAi 的 JSON envelope 协议（需包含 `response_to_user` + `next_actions` + `acceptance_check`），触发 `agent_core/src/lib.rs:317-344` 的两层防御：

1. **首次违规** → runtime 向模型发送修复请求，要求重新生成合法 JSON
2. **修复后仍不合法** → 阻断原始报文，返回兜底消息

这是 TimemAi 独有的可靠性设计——在 Rust runtime 层做显式协议验证，而非依赖模型自觉。

与 CC 的对比：
- TM 的可靠性通过 runtime 层显式协议验证 + 修复请求实现
- CC 的可靠性依赖 Anthropic API 层的 tool use 协议保证（模型训练时已对齐）
- TM 的修复请求可能产生额外的 LLM 调用（curl），但 AgentSight `llm_calls` 表为空，无法从进程数据区分"修复 curl"和"正常下一轮 curl"

对进程行为观测来说，协议修复不影响 eBPF 层面的进程创建/销毁事件捕获：curl 子进程的创建和退出仍被完整记录。协议修复仅影响进程数量——如果修复触发额外 LLM 调用，会增加 1 个 curl 子进程。

**对 agentOS 的启示**：Runtime 层协议验证是可靠的可靠性机制，但它以额外的 LLM 调用（和进程创建）为代价。agentOS 应感知这种"防御性 LLM 调用"，将其与正常业务 LLM 调用区分计量——前者是可靠性成本，后者是业务成本。

---

## §14 T5 深度 session 特征

T5a（15 进程）、T5b（3 进程）、T5c（5 进程）的进程数明显低于 T2-T4：

| Session | 进程 | curl | sh | 特征 |
|---------|------|------|-----|------|
| T5a | 15 | 4 | 1 | 中等复杂度，概念分析+少量文件探索 |
| T5b | 3 | 2 | 0 | 纯推理——最简形态 |
| T5c | 5 | 4 | 0 | 纯推理——多次 LLM 调用但无工具 |

- **T5b 是 TimemAi 能达到的最简进程形态**：3 进程（timem + 2 curl），持续 72s。两个 curl 各存活约 38s，中间无工具调用。等价于 Claude Code 的稳态期——两者的"纯推理"模式在进程层面表现一致（零工具子进程）。
- **T5c 的 5 进程 = 1 timem + 4 curl**——4 轮 LLM 调用，无工具执行。说明模型在回答架构设计问题时不需要文件探索。
- **T5a 与 T2-T4 形态相似**——包含文件探索（find + grep + cat），因为"agentOS 基础概念" prompt 触发了对已有研究文件的内容阅读。

**对 agentOS 的启示**：进程形态由任务类型决定，而非 runtime 决定。纯推理任务在两种 runtime 上都表现为最小进程树（TM: 3 进程，CC: 1 进程）。agentOS 的任务调度器可以根据任务类型（推理 vs 工具密集）预估进程资源需求。

---

## §15 对 agentOS 进程模型的启示

### 回扣三个核心问题

**问题 1：子进程数量/类型/层级。** TimemAi 子进程 2-39 个，**贯穿 session 全程**。进程分类学：INFRA = timem 根（1 个）+ curl（N 个，N = LLM 调用数），ACTION = grep/cat/find/sed/locale-check/sh，CHANNEL = sh/head/sed（工具入口进程）。进程树深度恒 2 层、单根。最大并发 7，每轮 sh burst 内并发。

进程类型是 agent 能力指纹：`curl` 是 LLM 调用代理的进程签名，`grep` + `find` + `cat` 是文件探索工具链签名，`locale-check` 是每次工具调用的固定开销签名。

**问题 2：进程↔LLM 时序关联。** 每个 LLM 调用 = 1 个 curl 子进程（存活 30-40s），每个工具调用 = 1 个 sh + N 个命令子进程（存活 < 100ms）。推理与行动交替的认知循环直接映射为进程创建时序：curl burst → sh burst → curl burst → ...。**TM 的进程创建模式是对 agent 认知循环的直接观测窗口**——这是 per-request fork 模型的独特优势。

**问题 3：对进程抽象设计的启示。**

- **两种极端模型需要统一抽象**（最重要）→ agentOS 不能假定 agent runtime 使用线程池或子进程模型。必须同时容纳"每轮 LLM 调用 fork curl"和"进程内 HTTP + 线程池工具执行"。
- **进程存活时间是跨 runtime 的 INFRA/ACTION 分类信号** → 1s 阈值在 CC 和 TM 上均有效，但 INFRA 数量因模型而异（CC=1, TM=1+N）。
- **进程创建频率区分工作模式** → 持续周期性 burst = per-request fork，集中 burst + 长尾零进程 = 线程池。
- **eBPF 观测兼容性不同** → per-request fork 天然对 eBPF 友好（每个工作单元 = 一个进程），线程池模型需要 runtime 配合上报。
- **非零退出率差异是进程模型的副产品** → per-request fork 模型的 grep 大量使用导致 ~30% 非零退出率，线程池模型 ~8%。agentOS 不应将非零退出直接等同于错误。
- **资源效率 vs 观测性的权衡** → per-request fork 模型的资源效率极高（17MB RSS）但进程数多，线程池模型进程数少但内存占用大（317MB RSS）。agentOS 需要权衡这两者。
- **协议修复是防御性 LLM 调用** → 应区别于正常业务 LLM 调用进行计量和计费。
- **任务类型决定进程形态，runtime 决定进程实现方式** → 纯推理任务在两种 runtime 上都表现为最小进程树，工具密集任务则放大两者的架构差异。

### 总体判断

TimemAi 是 per-request fork 模型——每次 LLM API 调用 fork curl，每次工具调用 fork sh 及其子树，无进程复用，无缓存，无线程池。这是 Unix 哲学在 agent runtime 中的直接体现：每个工作单元 = 一个进程，进程边界 = 工作边界。

Claude Code 是单进程事件循环 + worker 线程池模型——工具执行在 Bun Pool 线程内，子进程仅用于启动自检和一次性环境快照。

agentOS 的进程抽象设计必须从这两种极端中提取共性（单根、浅树、双峰存活时间），同时容纳差异（进程创建时序、INFRA 数量、eBPF 可观测性、资源占用）。

```
Per-Request Fork 模型 (TimemAi):

  timem-native-rs (Rust runtime, 长寿, ~17MB RSS)
    ├─ curl#1 (INFRA, 30-40s) → [LLM 推理]
    ├─ sh#1 → {locale-check, grep×N, ...} (ACTION, <100ms) → [工具执行]
    ├─ curl#2 (INFRA, 30-40s) → [LLM 推理]
    ├─ sh#2 → {locale-check, grep×N, ...} (ACTION, <100ms) → [工具执行]
    └─ ... (循环反复直到任务完成，进程创建贯穿全程)

线程池稳态模型 (Claude Code):

  claude (Bun runtime, 长寿, ~300MB RSS)
    ├─ 启动期: fork {git×6, claude.exe×3, sh→ps/grep} — 一次性环境自检
    ├─ 首次shell: fork {bash→locale-check/base64×6/...} — 一次性环境快照
    └─ 稳态: 内部 Bun Pool 线程执行所有工具 — 零新进程，数十到上百秒
```

---

## §16 已知限制

1. **llm_calls/tool_calls/token_usage/network_targets 表全空**：AgentSight HTTP 解析器不识别 DeepSeek API 的 OpenAI-compatible 响应格式。这导致本报告的 LLM 调用分析完全依赖进程代理指标（curl 子进程数量），工具调用分析完全依赖 sh 子进程数量。无法验证 token 用量、工具调用类型分布、或网络目标细节。

2. **LLM 调用计数可能高估**：协议修复（§13）可能触发额外 curl，但无法从进程数据区分"修复 curl"和"正常 curl"。实际业务 LLM 调用数可能略低于 curl 子进程数。

3. **工具调用计数可能低估**：`run_bash` 可能不总是 fork `sh`（T3 集群 6/7 中 `head`/`sed` 替代 `sh` 作为入口进程）。以 `sh` 子进程数作为工具调用代理可能漏计管道首命令优化的 case。

4. **argv/cwd 捕获为空**：所有进程的 `argv_json=[]`、`cwd=NULL`，`command` 仅含 exe 路径（如 `/usr/bin/curl`），无完整参数。无法还原具体 API 请求内容或 bash 命令。

5. **子进程资源采样缺失**：`resource_samples` 仅含 `timem-native-rs` 根进程，瞬时子进程（grep/cat 存活 < 100ms）在秒级采样间隔内无法捕获 CPU/RSS。

6. **单 API provider**：仅测试 DeepSeek API（OpenAI-compatible 端点）。其他 provider（Anthropic、OpenAI 原生）的 API 延迟差异可能改变 curl 存活时间分布，但不会改变 per-request fork 的进程模型结构。

7. **非交互模式**：`--once-json` 模式下 agent 在完成 prompt 后立即退出，无法观测交互式会话中的进程复用行为。但 per-request fork 模型的本质——每轮 LLM 调用 fork curl——在交互式模式下预期不变（TimemAi 无连接池、无 HTTP keep-alive）。

8. **单 agent 类型**：仅观测 TimemAi（Rust + DeepSeek）。其他 Rust agent（如 OpenClaw）若使用不同 HTTP 库（如 `reqwest` 进程内 HTTP），进程模型可能不同——"curl 子进程 HTTP"的结论是 TimemAi 特异的，不是 Rust agent 通则。

### 后续验证建议

- **扩展 AgentSight HTTP 解析器**：支持 OpenAI-compatible API 响应格式（`chat/completions` 端点），以填充 `llm_calls`/`token_usage`/`network_targets` 表。
- **交互式 session 录制**：录制 `timem` 交互模式（`record -c timem-native-rs`）的完整 session，验证多轮交互中进程模型的稳定性。
- **跨 API provider 对比**：切换 TimemAi 后端到 Anthropic API（`--api-protocol anthropic`），检验不同 API 延迟对 curl 存活时间分布的影响。
- **增加 strace 补充数据**：对单次 `run_bash` 调用做 `strace -f`，精确还原子进程创建链和系统调用序列。

---

*Created: 2026-06-25 | Updated: 2026-06-26*
*Based on 7 AgentSight sessions of TimemAi (Rust + DeepSeek, non-interactive mode)*
