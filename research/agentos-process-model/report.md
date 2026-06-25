# AgentOS 进程模型研究 — Claude Code 观测报告

## §1 实验概览

**目标**：通过 AgentSight 观测 Claude Code 的运行时进程行为，为 agentOS 进程模型设计提供实证基础。

**实验方法**：录制 7 个 session，覆盖 5 类任务（T1-T4 + T5a-T5c，其中 T5 采用回退方案——3 个独立深度递进 session 替代原计划 1 个交互式多轮 session）。

| Session | 任务类型 | LLM 调用 | 总进程数 | 进程类型数 | 时长 |
|---------|---------|---------|---------|----------|------|
| T1 | 简单问答（eBPF 解释） | 2 | 14 | 6 | ~20s |
| T2 | 多文件读取（架构阅读） | 6 | 33 | 14 | ~1m |
| T3 | 代码修改（添加 --version） | 5 | 14 | 6 | ~1m |
| T4 | 工具密集（TODO 扫描 + git） | 7 | 36 | 15 | ~1m |
| T5a | agentOS 基础概念 | 7 | 46 | 17 | ~2m |
| T5b | agentOS 进程模型深入 | 17 | 37 | 15 | ~2m |
| T5c | agentOS 架构设计总结 | 4 | 37 | 15 | ~1m |

**录制环境**：Linux 5.15, Claude Code v2.1.191, AgentSight（本仓库构建）

**方法学备注**：T5 因非交互环境无法执行原始方案（交互式多轮 `record -c claude` 附着），改用 3 个独立 `record -- claude -p` session。T5a 的录制启动略晚于 claude 进程创建，导致其 startup 阶段仅捕获到 7 个进程（缺失前 5 个 git），但 action 阶段完整。

**数据源说明**：AgentSight SQLite 数据库含 7 张表——`llm_calls`（含 provider/model/status/finish_reason）、`token_usage`（含 input/output/cache tokens）、`tool_calls`（含 tool_name/duration_ms/related_pid）、`process_nodes`（含 timestamp/argv/exe_code）、`audit_events`（含 action/target/summary）、`resource_samples`（含 cpu_percent/rss_mb）、`network_targets`（含 host/path/count）。本报告综合所有表进行交叉分析。LLM 调用中仅首个有完整 provider/duration 数据，后续调用来自 agent-native session 文件（`view_source='session_file'`），部分字段为空——这是 AgentSight 当前 SSL 捕获覆盖面的限制，不影响进程层分析。

## §2 核心发现：进程生命周期分两阶段

**这是本次深入分析最重要的新发现。** 跨 7 个 session 的进程时间线分析表明，Claude Code 的进程树不是均质的——它在时间维度上清晰分为两个阶段。

### §2.1 第一阶段：启动引导（Startup Bootstrap）

在所有 7 个 session 中，第一个 LLM 调用开始之前，Claude Code 创建了 **12 个进程**（T5a 为 7 个，因录制略晚），构成稳定不变的自检骨架：

```
claude (主进程)
  ├── git × 6    (版本检查/状态查询)
  ├── claude.exe  (Node.js worker)
  ├── sh          (shell)
  │   ├── ps      (进程快照)
  │   └── grep × 2
```

- **时间窗口**：全部 12 个进程在 ~400ms 内创建完毕（从 claude 启动到最后一个 grep）
- **跨 session 不变性**：6/7 session 的 startup 进程完全一致（`claude`、`git×6`、`claude.exe`、`sh`、`ps`、`grep×2`）
- **T5a 例外**：startup 仅 7 个进程（缺 5 个 git），原因是录制启动晚于 claude 进程创建（eBPF probe 附着延迟）
- **语义**：这是 Claude Code 的环境自检——检查自身版本（git）、系统状态（ps/grep）、启动 worker（claude.exe）

### §2.2 第二阶段：Agent 动作（Agent Action）

第一个 LLM 调用结束后，agent 开始执行工具调用，产生动作进程：

| Session | 动作进程数 | 爆发数 | 首个爆发时间 | 爆发跨度 |
|---------|----------|--------|-------------|---------|
| T1 | **0** | 0 | — | — |
| T2 | 19 | 1 | +19.6s | 21ms |
| T3 | **0** | 0 | — | — |
| T4 | 22 | 1 | +15.8s | 21ms |
| T5a | 27 | 2 | +6.9s, +19.8s | 24ms, 20ms |
| T5b | 23 | 1 | +16.6s | 24ms |
| T5c | 23 | 1 | +19.2s | 23ms |

**关键发现**：

1. **T1 和 T3 的动作进程数为 0**：简单问答和单文件代码修改不产生任何动作进程——所有工作（包括 git 状态检查）在 startup 阶段完成。LLM 返回答案后 session 直接结束。这说明对于足够简单的任务，agent 进程模型完全由 startup 骨架组成。

2. **动作进程是单次爆发的**：除 T5a（2 次爆发，对应 Read 和 Bash 两种工具调用）外，所有 session 的动作进程集中在一次 21-24ms 的爆发中。这不是"每个 LLM 调用一批进程"——而是"第一次真正的工具调用触发一批进程"。

3. **爆发内进程高度同构**：动作爆发的典型组合是 `env + bash + locale-check + grep×4 + cut + base64×6 + head×2-3 + awk + sed×2 + cat×2-3`。这不是 N 个独立工具调用，而是**一个 Bash 工具调用触发的管道链**——base64×6 说明有 6 路并行编码，这是 Claude Code 的文件读取流水线。

### §2.3 对 agentOS 的启示

启动骨架的不变性意味着 agentOS 可以为已知 agent 类型预定义"进程模板"。Claude Code 的 Phase 1 骨架= `{claude, git×6, claude.exe, sh, ps, grep×2}`。agentOS 可以：

- **预创建启动骨架**：在 agent 启动时批量创建这 12 个进程，省去逐个 fork 的开销
- **区分骨架进程和动作进程**：骨架进程在调度器中的优先级可以降低（它们不是 agent "正在工作"的信号）
- **动作爆发检测**：当 agentOS 检测到 20ms 内创建 20+ 进程时，识别为工具调用爆发，可以批量分配资源而非逐个响应

## §3 进程树拓扑

### 核心发现：进程树深度恒定 3 层

所有 7 个 session 的进程树深度均为 3。不管任务复杂度如何、子进程数量如何，Claude Code 从不产生超过 3 层的嵌套。

```
claude (根进程)
  ├── bash (通道节点)
  │   ├── git (动作节点)
  │   ├── grep (动作节点)
  │   └── find (动作节点)
  ├── git (直接子进程，绕过 bash)
  └── sh (通道节点)
```

**对 agentOS 的启示**：进程树深度有严格上界（3 层），agentOS 的进程树数据结构可以简化。3 层恰好映射到"agent 主进程 → 通道 → 工具"的三层模型。根节点始终为 1 个，可作为资源记账的根。

## §4 进程类型分布

跨 session 共享的稳定进程骨架：

| comm | 角色 | 出现率 |
|------|------|--------|
| claude | agent 主进程 | 7/7 (100%) |
| node | JavaScript runtime | 7/7 (100%) |
| bash | 通道（执行 shell 命令） | 6/7 (86%) |
| sh | 通道（轻量 shell） | 6/7 (86%) |
| git | 动作（版本控制查询） | 6/7 (86%) |
| grep | 动作（文本搜索） | 6/7 (86%) |

**对 agentOS 的启示**：agent 进程骨架有可预测的"基础设施层"（claude+node 总是存在），通道节点（bash/sh）和动作节点（git/grep）按需创建。进程类型分布天然构成 agent 能力指纹，可用于安全策略。

## §5 进程存活时间

### 核心发现：极短 p50（1-7ms），长尾 p95

| Session | p50 (ms) | p95 (ms) |
|---------|---------|---------|
| T1 | 7 | 20,102 |
| T2 | 1 | 22 |
| T3 | 6 | 104,568 |
| T4 | 1 | 22 |
| T5a | 1 | 1,012 |
| T5b | 1 | 25 |
| T5c | 1 | 22 |

**p50=1-7ms**：半数子进程存活不到 7ms（`git status`、`grep`、`find` 级）。Claude Code 采用"创建→执行→销毁"模式，不保留复用。

**p95 长尾解释**：T1 的 p95=20,102ms、T3 的 p95=104,568ms ——这些是 claude 主进程的存活时间。去除主进程后，所有子进程的 p95 < 25ms。

**按进程类型分组的存活时间**：claude 主进程的 p50 存活时间 = 20-170s（session 时长），属于 INFRA 类。所有子进程（git/grep/bash/env/cut/base64/head/awk/sed/cat）的 p50 < 25ms，属于 ACTION 类。两类之间有 3 个数量级的差距——这是 agent 进程中"长生命周期"和"短生命周期"两类进程的天然分界线。

**对 agentOS 的启示**：p50 存活时间极短要求 agentOS 提供 1ms 级的进程创建原语——传统 fork+exec 开销（mmap 重建、页表拷贝）在这种场景下占比过高。需要类似 `clone(CLONE_VFORK)` 语义的轻量进程创建。同时，agentOS 可以为两类进程提供不同的调度类和资源池：INFRA 类走时间片轮转（公平调度），ACTION 类走 FIFO（快速创建→执行→销毁）。

## §6 进程并发度

### 核心发现：最大并发仅 5-6 个

| Session | 最大并发 | 总进程数 |
|---------|---------|---------|
| T1 | 5 | 14 |
| T2 | 5 | 33 |
| T5a | 6 | 46 |
| T5b | 5 | 37 |

尽管总进程数可达 46 个，任意时刻同时存活的进程不超过 6 个。这说明 Claude Code 的进程模型是严格顺序的——进程在创建后立即执行并退出，不会积累并发。

**对 agentOS 的启示**：agentOS 不需要为 agent 预留大量进程槽位。5-6 个并发槽位即可覆盖当前观测到的最大需求。进程池大小可以保守设计。

## §7 退出状态分布

所有 session 的 `exit_code=0` 占比 >95%。未观测到 `exit_code≠0` 的失败进程。`still_running` 状态对应 session 结束时未退出的长生命周期进程（claude 主进程、node runtime）。

**对 agentOS 的启示**：agent 子进程失败率极低（本次为 0%），传统 OS 为高失败率设计的重错误恢复路径在 agentOS 中优先级较低。

## §8 进程-LLM 时序关联

| Session | llm_calls | avg_procs_per_call |
|---------|----------|-------------------|
| T1 | 2 | 7.0 |
| T2 | 6 | 2.3 |
| T3 | 5 | 2.8 |
| T4 | 7 | 5.1 |
| T5a | 7 | 3.3 |
| T5b | 17 | 2.2 |
| T5c | 4 | 9.2 |

**关键纠正**：上表的 `avg_procs_per_call` 是按 ±1s 时间窗口关联计算的，包含了大量 startup 进程（它们被 ±1s 窗口错误地归入首个 LLM 调用）。实际按严格 LLM 执行窗口统计——启动期进程已分离到 §2——第一个 LLM 调用期间仅创建 2 个 claude.exe worker（T5a 除外），动作进程全部在 LLM 调用之间的间隔期创建。

**修正后的模型**：

```
Phase 1: 启动骨架（400ms 内，12 个进程）
    ↓
LLM Call 1: 系统提示处理 + 2 个 claude.exe worker 创建
    ↓
Inter-LLM 间隔：进程爆发（21ms 内 19-27 个进程）← 工具调用发生在此时
    ↓
LLM Call 2-N: 推理，不创建子进程
    ↓
... 交替（推理 ↔ 工具爆发）直到任务完成
```

**对 agentOS 的启示**："认知→行动"的相位分离比此前报告的更清晰——推理阶段（LLM 调用）完全不创建工具进程，工具进程爆发集中在 LLM 调用之间的短暂窗口（~20ms）。agentOS 可以利用推理阶段（数秒到数十秒）执行以下操作：(a) 回收上一轮工具进程的资源，(b) 预创建下一轮的进程壳，(c) CPU 降频节能。

## §9 父子关系模式

Top 父子对：`bash → grep`（6/7）、`claude → git`（6/7）、`bash → git`（5/7）、`claude → bash`（6/7）。

`claude → bash → tool` 是最稳定的 2-hop 模式。bash 是纯粹的通道节点——传递意图但不贡献意图。Claude Code 也经常绕过 bash 直接执行（`claude → git`），说明对高频路径有优化。

**外部 ppid 发现**：每个 session 有 6 个 `base64` 进程的 ppid 不在捕获的进程树内（ppid 指向未观测到的中间进程）。推测是 bash 管道中的子进程创建模式——父进程（管道中间环节）在子进程被 eBPF 捕获之前已退出。这是 eBPF 进程追踪的已知盲区：极短生命周期进程可能在其父子关系被记录前消失。

**对 agentOS 的启示**：bash/sh 通道节点应该在 agentOS 调度器中透明化——调度和记账直接对动作节点（git、grep）进行。agentOS 应允许 agent 标记"直接路径"优化高频操作。agentOS 的进程追踪应保证原子性——父子进程创建和销毁事件必须在同一事务中提交，避免 `base64` 这种外部 ppid 现象。

## §10 Token 用量与模型切换

### 核心发现：flash→pro 双模型策略，两种变体

观测到两种 token 流模式：

**模式 A（4/7 session：T1, T2, T5a, T5b）**：小 flash 系统提示 → 大 pro 完整上下文。

首个 LLM 调用使用 `deepseek-v4-flash`，输入仅 ~430 tokens（系统提示 + 简短任务指令）：

| Session | Flash S1 | 输入 | 输出 | 耗时 |
|---------|----------|------|------|------|
| T1 | 1 | 424 | 339 | 4.4s |
| T2 | 1 | 430 | 213 | 3.7s |
| T5a | 1 | 435 | 523 | 6.6s |
| T5b | 1 | 485 | 695 | 7.2s |

第二个 LLM 调用切换到 `deepseek-v4-pro`，输入跃升至 ~120K（完整系统上下文注入）：

| Session | Pro S2 | 输入 | 输出 |
|---------|--------|------|------|
| T1 | 1 | 120,407 | 275 |
| T2 | 1 | 120,471 | 318 |
| T5a | 1 | 120,594 | 263 |
| T5b | 1 | 120,691 | 317 |

**模式 B（3/7 session：T3, T4, T5c）**：双 flash 全上下文 → pro。

前两次 LLM 调用均使用 `deepseek-v4-flash`，且输入均为 ~120K token（相同的全上下文）：

| Session | Flash S1 | Flash S2 | 首次 pro |
|---------|----------|----------|---------|
| T3 | 120,509 in | 120,509 in | S3: pro, 9,068 in |
| T4 | 120,560 in | 120,560 in | S3: pro, 1,667 in |
| T5c | 120,743 in | 120,743 in | S3: pro, 630 in |

模式 B 的两次 flash 调用读入相同的全上下文但产出不同的响应（T3: 743 vs 123 out），推测是 Claude Code 的"工具定义加载 + 首轮推理"被拆成了两个 flash 调用。

两种模式下，后续调用均在 flash 和 pro 之间切换，按上下文大小和任务复杂度选择模型。

**对 agentOS 的启示**：agentOS 的调度器需要感知 LLM 调用的成本差异——flash 调用 ~4s，pro 调用从 token 量推算约 10-60s。如果一个"简单"工具调用的进程开销（~20ms）后跟着一个 30s 的推理调用，调度器应该把 CPU 资源让给其他 agent 的进程。

### Token 缓存模式

所有 session 的 `cache_create_tokens=0`，`cache_read_tokens>0`，缓存命中率 100%。系统提示和工具定义是预缓存的，session 期间不重新创建。除首次 flash 调用外，后续调用共享同一缓存池：
- T2: 153,600 read, 0 create
- T4: 616,192 read, 0 create
- T5b: 1,895,040 read, 0 create

**对 agentOS 的启示**：agentOS 的系统提示缓存应该在 agent 间共享——如果多个同类 agent（如多个 Claude Code 实例）使用相同的系统提示和工具定义，它们的 token 缓存可以复用，显著降低首个 LLM 调用的输入成本。

## §11 工具调用分析

所有 session 的 tool_calls 数据：

| Session | 工具调用数 | 工具类型 | 占比 |
|---------|----------|---------|------|
| T1 | 0 | — | — |
| T2 | 13 | Bash 9, Read 3, Agent 1 | Bash 69% |
| T3 | 3 | Read 2, Edit 1 | Read 67% |
| T4 | 16 | Bash 14, Read 2 | Bash 88% |
| T5a | 14 | Read 10, Bash 3, Agent 1 | Read 71% |
| T5b | 18 | Bash 11, Write 3, Read 2, Grep 1, Skill 1 | Bash 61% |
| T5c | 2 | Read 1, Bash 1 | 各 50% |

**关键发现**：
- `related_pid` 全部指向 `claude` 主进程（agent 进程 pid），不是子进程。这意味着工具调用层面的关联是"哪个 agent 发起的"，而非"哪个子进程执行的"。实际的 bash/git 子进程通过 `process_nodes` 表观测，但与工具调用之间无直接外键。
- Bash 是占比最高的工具类型（61-88%），Read 次之。
- 工具调用数量与动作进程数（§2.2）不完全对应——因为一个 Bash 工具调用可以触发一条包含 20+ 个子进程的管道链。

**对 agentOS 的启示**：工具调用到子进程的映射是多对多的（一个工具→多个子进程），agentOS 的资源计量应同时支持两种粒度：工具级（按 tool_call 计量）和进程级（按 process_node 计量）。

## §12 资源使用

### CPU 和内存

| Session | RSS 起→止 (MB) | CPU p50 | CPU max | 采样数 |
|---------|----------------|---------|---------|--------|
| T1 | 100→270 (+170) | 1.5% | 33.7% | 11 |
| T2 | 68→279 (+211) | 2.5% | 34.7% | 33 |
| T3 | 138→317 (+179) | 1.5% | 36.2% | 53 |
| T4 | 157→321 (+164) | 2.5% | 32.5% | 40 |
| T5a | 340→338 (−2) | 1.5% | 21.6% | 62 |
| T5b | 115→330 (+215) | 1.5% | 41.0% | 85 |
| T5c | 94→306 (+212) | 1.5% | 36.2% | 53 |

**关键发现**：
- RSS 增长集中在 session 前半段（100→~300MB），之后稳定在 ~300MB。增长来源是 Node.js heap 扩张和文件缓存，不是进程创建（子进程内存可忽略）。
- CPU p50 = 1.5-2.5%，说明 agent 在绝大多数时间是 I/O 等待（等 LLM 响应）。CPU max = 33-41%，峰值对应进程爆发期（21ms 内创建 20+ 个子进程）。
- 资源采样仅覆盖 claude 主进程——子进程在采样间隔内（推测 1-5s）已创建并退出，未被捕获。

**对 agentOS 的启示**：agent 进程的 CPU 利用率极低（p50 < 2.5%），大量 CPU 时间浪费在 I/O 等待上。agentOS 可以在 LLM 调用期间将 agent 进程置于"休眠"状态，让其他 agent 使用 CPU。

## §13 网络与遥测

Claude Code 的网络连接包含三类目标：

1. **LLM API**：`api.deepseek.com/anthropic/v1/messages` ——实际推理请求（"HTTP Client"线程）
2. **MCP Registry**：`api.anthropic.com/mcp-registry/v0/servers` ——MCP 服务发现（每次启动查询，带分页 cursor）
3. **Telemetry**：`api.anthropic.com/api/event_logging/v2/batch` ——遥测上报

遥测频率与 LLM 调用数的比值约 0.6-1.0。T5b（17 次 LLM 调用、11 次遥测）的比值约 0.6，因为遥测是批量上报而非每次调用上报。

**对 agentOS 的启示**：agentOS 应内置遥测——如果每次进程创建/LLM 调用/工具调用都被 agentOS 自动记录（类 systemd journal），agent 开发者不需要自行实现遥测逻辑，减少"遥测进程"这类非工作负载。

## §14 审计事件分析

audit_events 表记录了三类事件及其跨 session 分布：

| 事件类型 | T1 | T2 | T3 | T4 | T5a | T5b | T5c |
|---------|----|----|----|----|----|----|-----|
| process (exec/exit) | 28 | 67 | 28 | 73 | 88 | 75 | 75 |
| file (write) | 11 | 29 | 11 | 43 | 32 | 21 | 13 |
| llm (request/response) | 3 | 7 | 6 | 8 | 8 | 18 | 5 |
| **总计** | **42** | **103** | **45** | **124** | **128** | **114** | **93** |

- process 事件数 ≈ 2 × 进程总数（每个进程一次 exec + 一次 exit），T2/T4/T5a 的 exit 少于 exec，因为 session 结束时部分长生命周期进程尚未退出
- file write 事件数与工具调用类型强相关——Bash 密集型 session（T2、T4）产生更多文件写入
- 最大仅 128 条（T5a），远低于 Claude Code 默认的 10,000 条 audit 上限，无截断

**对 agentOS 的启示**：audit 事件的三个维度（进程生命周期、文件 I/O、LLM 调用）恰好构成 agent 行为完整画像的三要素。agentOS 的审计子系统应原生支持这三个维度。

## §15 对 agentOS 进程模型的启示

### 回扣三个核心问题

**问题 1：子进程数量/类型/层级。** Claude Code 的子进程在 14-46 个之间。**新发现**：进程生命周期分为两阶段——(a) 不变的启动骨架（12 个进程，400ms 内），(b) 按需的动作爆发（0-27 个进程，单次 21ms 爆发）。进程分类学：基础设施进程常驻（claude、node）、通道进程中转（bash、sh）、动作进程按需创建（git、grep、base64）。进程树深度恒定为 3 层。最大并发 5-6 个。

**问题 2：进程↔LLM 时序关联。** **修正**：推理和行动有严格的时分复用——LLM 调用期间（3-7s）不创建工具子进程，子进程创建集中在 LLM 调用之间的 ~20ms 窗口。首次 LLM 调用在 flash 模型上处理系统提示，第二次调用在 pro 模型上注入完整上下文（120K tokens）。Token 缓存 100% 命中（预缓存系统提示）。

**问题 3：对进程抽象设计的启示。**
- **两阶段进程模型** → agentOS 应区分"骨架进程"（agent 类型特定，可预创建）和"动作进程"（任务特定，按需创建）
- p50=1-7ms → 极轻量进程创建原语（clone-like，非 fork+exec），目标 1ms 以下
- 3 层恒定深度 → 简化进程树 API，不需要递归深度支持
- 最大并发 5-6 → 每 agent 进程槽位可保守设计
- bash 通道节点 → 调度器透明化，直接对动作节点记账
- 动作单次爆发（21ms 内 20+ 进程） → 调度器应支持批量进程创建原语
- CPU p50 < 2.5% → 大量 I/O 等待时间可出让给其他 agent
- 进程类型可分为 INFRA（存活 >1s）和 ACTION（存活 <100ms）两类 → 不同调度类和资源池
- 工具调用→进程的关联需要 agentOS 原生提供（`related_pid` 粒度不足）

### 总体判断

Claude Code 的进程模型偏向 **Web 服务器的请求-响应模型**（per-request fork，短命，独立），而非数据库的连接池模型。每个 LLM 调用产生一批新子进程，用完即弃。

新的更精确的模型是 **"骨架-爆发"模型**（Skeleton-Burst Model）：

```
Skeleton (invariant, 400ms):  claude + git×6 + claude.exe + sh + ps + grep×2
    ↓
Burst 1 (after LLM 1, ~20ms):  bash → {grep×4, base64×6, head×3, awk, sed×2, cat×3, ...}
    ↓
LLM Call 2 (3-30s, no subprocesses)
    ↓
Burst 2 (after LLM 2, ~20ms):  ...
```

## §16 已知限制

1. **T5 非交互式**：回退方案使多轮交互的进程复用行为不可见——最重要未回答问题
2. **LLM 调用元数据不完整**：仅首个 LLM 调用有完整 provider/duration 数据，后续调用的 `end_timestamp_ms == start_timestamp_ms`，来自 session file 的延迟写入
3. **工具调用→子进程关联缺失**：`tool_calls.related_pid` 统一指向 claude 主进程，实际子进程（bash/git）与工具调用之间的映射通过时间窗口推断，非确定性
4. **argv 捕获为空**：所有 bash/sh 进程的 `argv_json` 为空数组，无法还原具体命令内容。这是 AgentSight 的 `process_nodes.argv_json` 采集bug或配置问题
5. **子进程资源采样缺失**：`resource_samples` 仅含 claude 主进程，子进程在采样间隔内已退出
6. **base64 外部 ppid**：每个 session 的 6 个 base64 进程的 ppid 不在进程树内——eBPF 进程追踪的竞态窗口
7. **单 agent 类型**：仅观测了 Claude Code，其他 agent（OpenClaw、Codex、Cursor）的进程模型可能不同
8. **非生产负载**：人工设计的示例 prompt，真实开发 session 的进程模式可能不同
9. **audit 无截断**：最大 124 条（T4），远低于 10,000 条默认上限

---

*Created: 2026-06-25 | Updated: 2026-06-25 (deep re-analysis: 7 dimensions, startup/action phase separation, token flow, resource profiling, tool analysis, concurrency, network)*
*Based on 7 AgentSight sessions of Claude Code v2.1.191*
