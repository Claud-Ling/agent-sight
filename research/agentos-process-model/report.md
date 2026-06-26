# AgentOS 进程模型研究 — Claude Code 观测报告

## §1 实验概览

**目标**：通过 AgentSight 观测 Claude Code 的运行时进程行为，为 agentOS 进程模型设计提供实证基础。

**实验方法**：录制 7 个 session，覆盖 5 类任务（T1-T4 + T5a-T5c，其中 T5 采用回退方案——3 个独立深度递进 session 替代原计划 1 个交互式多轮 session）。

| Session | 任务类型 | LLM 调用 | 总进程数 | 进程类型数 | 工具调用 | 时长 |
|---------|---------|---------|---------|----------|---------|------|
| T1 | 简单问答（eBPF 解释） | 2 | 14 | 6 | 0 | 20s |
| T2 | 多文件读取（架构阅读） | 6 | 33 | 14 | 13 | 65s |
| T3 | 代码修改（添加 --version） | 5 | 14 | 6 | 3 | 105s |
| T4 | 工具密集（TODO 扫描 + git） | 7 | 36 | 15 | 16 | 79s |
| T5a | agentOS 基础概念 | 7 | 46 | 17 | 14 | 124s |
| T5b | agentOS 进程模型深入 | 17 | 37 | 15 | 18 | 170s |
| T5c | agentOS 架构设计总结 | 4 | 37 | 15 | 2 | 105s |

**录制环境**：Linux 5.15, Claude Code v2.1.191（Bun runtime），后端 `api.deepseek.com/anthropic/v1/messages`（deepseek-v4 经 anthropic 兼容端点），AgentSight（本仓库构建）。

**方法学备注**：T5 因非交互环境无法执行原始方案（交互式多轮 `record -c claude` 附着），改用 3 个独立 `record -- claude -p` session。T5a 的录制启动略晚于 claude 进程创建，导致其 startup 阶段仅捕获到部分 git（缺失若干），但 action 阶段完整。

**数据源说明**：AgentSight SQLite 数据库含 7 张表——`network_targets`、`llm_calls`、`token_usage`、`audit_events`（process/file/llm 三类事件）、`process_nodes`、`tool_calls`、`resource_samples`。**注意：没有 `sessions` 表**（早期脚本曾试图读取它，已修正——session 元数据从时间戳跨度派生）。LLM 调用中仅首个有完整 provider/duration 数据，后续调用来自 agent-native session 文件（`view_source='session_file'`），`end_timestamp_ms` 多为空（7 个 session 中 16/17、6/7 等均缺失结束时间）——这是 AgentSight 当前 SSL 捕获覆盖面的限制。

---

## §2 核心发现：进程创建是"一次性引导"，稳态工具执行是"进程内"

**统一事件时间线分析（D15/D16）是这一结论的核心证据。**

### §2.1 决定性证据：进程在 ~17s 后停止创建，但 session 继续工作数十到上百秒

把 `process_nodes`（exec 时刻）、`llm_calls`、`tool_calls`、`audit_events(file)` 合并到同一时间线，跨 7 个 session 一致呈现：

| Session | 最后一个子进程 exec | session 结束 | **引导后"静默尾巴"** | 尾巴内工具调用 | 尾巴内 LLM 调用 | 尾巴内新子进程 |
|---------|------------------|------------|-------------------|-------------|-------------|-------------|
| T1 | +0.4s | +20s | 19.7s | 0 | 1 | **0** |
| T2 | +20.0s | +65s | 44.9s | 12 | 4 | **0** |
| T3 | +0.4s | +105s | 104.2s | 3 | 4 | **0** |
| T4 | +16.3s | +79s | 62.7s | 16 | 6 | **0** |
| T5a | +20.2s | +124s | 103.5s | 13 | 5 | **0** |
| T5b | +17.1s | +170s | 152.5s | 16 | 15 | **0** |
| T5c | +17.0s | +105s | 85.0s | 0 | 1 | **0** |

**所有 7 个 session：在最后一个子进程创建之后，零新子进程，但工具调用（最多 16 次）和 LLM 调用（最多 15 次）继续发生。** T5b 最极端——17 次 LLM 调用、18 次工具调用，但所有子进程在前 17s 内创建完毕，之后 152s 内进程树纹丝不动。

`audit_events` 交叉验证排除了"eBPF 探针脱钩"的可能：exec 事件在 +17s 停止，但 file/llm audit 事件、resource 采样都持续到 session 结束（T5b 的 resource 采样覆盖 +0 .. +168s）。探针一直在线，只是没有新进程可记录。

### §2.2 那个"爆发"是什么：一次性 shell 环境快照，不是文件读取流水线

这组进程（`env + bash + locale-check + grep×4 + base64×6 + head + awk + sed×2 + cat×3`）是一次性 shell 环境快照，而非文件读取流水线。证据：

1. **每 session 只出现一次**，与工具调用次数无关。T5b 有 18 次工具调用却只有 1 次这种爆发；T5c 有 2 次工具调用，也是 1 次爆发；T4 有 16 次工具调用，仍是 1 次爆发。如果它是"每次 Bash 工具调用的流水线"，次数应该与工具调用数成比例。它没有。
2. **它早于第一个工具调用**。T4 中这组进程在 +16233ms 创建，而第一个 `tool_calls` 记录在 +16810ms——爆发比工具调用早 ~577ms。时序上它不是工具调用的产物。
3. **特征签名指向环境探测**：`locale-check`（探测 locale）+ `base64×6`（典型的 shell 启动脚本指纹采集/编码）。这是 Claude Code 第一次需要执行 shell 命令时，对交互式 shell 环境做的一次性快照（加载 `.bashrc`/`.profile`、探测 locale、采集环境），之后缓存复用。

### §2.3 稳态工具工作由进程内 worker 线程完成（Bun Pool）

引导后 session 继续做事，那"做事"在哪里发生？`audit_events(file)` 给出答案——在最后一个子进程之后的所有文件写入，执行者 comm 是 **`Bun Pool N`** 和 `claude` 本身，而非任何子进程：

```
T4, +16829ms 起（最后一个子进程在 +16254ms）:
  file write  comm=Bun Pool 14  → /tmp/claude-1000/.../c8e5...
  file write  comm=Bun Pool 13  → /tmp/claude-1000/.../c8e5...
  file write  comm=Bun Pool 8   → ...
  （T4 共 28 次引导后文件写入，全部由 Bun Pool / claude 执行）
```

Claude Code v2.1.191 是一个 **Bun runtime** 进程。`Read`/`Write`/`Edit`/`Grep` 等工具直接在 Bun 的 worker 线程池里执行（文件 I/O、文本处理都是进程内系统调用），**不 fork 子进程**。即使是 `Bash` 工具，其常见用途（如 `git diff` 的输出读取、文件 grep）在观测中也没有持续触发新子进程——真正落到子进程的，只有第一次 shell 环境探测那一组。

### §2.4 进程模型

```
Phase 0  启动引导（0 .. ~0.4s，14 个进程，跨 session 不变）:
         claude(根) → git×6（版本/状态自检）+ claude.exe×3（Bun 短命 worker，1-7ms）
                    + sh → {ps, grep×2}（进程快照自检）
    ↓
Phase 1  首次 shell 环境快照（任务首次需要 shell 时，一次性，~20 进程，~20ms）:
         env + bash → {locale-check, grep×4, cut, base64×6, head×3, awk, sed×2, cat×3}
    ↓
Phase 2  稳态（数十到上百秒，零新子进程）:
         [LLM 调用] ↔ [工具调用：Read/Write/Edit/Bash 在 Bun Pool 线程内执行]
         反复交替直到任务完成；进程树完全静止
```

这**不是** Web 服务器的 per-request fork 模型。它更接近一个**单进程事件循环 runtime + 固定 worker 线程池**（Bun/Node 自身的模型），子进程仅用于：(a) 启动期不可避免的外部命令（git 自检），(b) 首次 shell 环境探测。任务的实际"认知↔行动"循环全部在单个长寿进程内闭合。

### §2.5 对 agentOS 的启示

- **进程边界 ≠ agent 行动边界**。一个 agent 在整个 session 里可能只 fork 二三十个进程（且集中在前 20s），却执行了十几次工具调用。agentOS 若按"进程"做调度/记账/隔离单元，会**完全错过稳态期的 agent 行为**——那些行为是进程内线程级的。agentOS 需要的隔离/记账原语应下沉到**线程/协程 + 系统调用**粒度，而不是进程粒度。
- **子进程是"边缘事件"而非"主循环"**。agentOS 可以把"agent fork 子进程"当作需要审计的稀有事件（如启动自检、shell 逃逸），用更重的安全策略对待；而把高频的进程内工具执行交给运行时内的轻量沙箱（如能力受限的 syscall 过滤）。
- **环境探测可预计算**。Phase 1 的 shell 环境快照（locale + 环境采集）是确定性的、可缓存的。agentOS 可以在 agent 镜像构建期就固化这份快照，省掉运行时第一次 shell 调用的 ~20 进程开销。

---

## §3 进程树拓扑

### 核心发现：恒定 3 层深度，单一 agent 根

所有 7 个 session 的进程树深度均为 3，且**有且仅有 1 个 agent 根**（`claude`，其 ppid 指向录制器/启动 shell，属外部）。

```
claude (唯一 agent 根, 深度 0)
  ├── git (深度 1，直接子进程，绕过 shell)
  ├── sh / bash (深度 1，通道节点)
  │   ├── grep / ps / locale-check (深度 2，动作节点)
  │   └── ... (深度 2-3)
  └── claude.exe (深度 1，Bun 短命 worker)
```

**根 vs 孤儿**：脚本（`process_tree_stats`）区分两类无父节点：

| Session | agent 根（claude） | 缺父孤儿（base64） |
|---------|------------------|------------------|
| T1, T3 | 1 | 0 |
| T2, T4, T5a, T5b, T5c | 1 | 6 |

`base64×6` 的 ppid 指向**未被捕获的中间进程**（见 §9），它们不是 agent 树的独立根，而是 eBPF 竞态窗口下丢失父节点的叶子。**agent 树永远是单根。**

**对 agentOS 的启示**：进程树深度有严格上界（3 层），且单根。3 层映射"agent 主进程 → 通道 → 工具"。根节点唯一，可作为资源记账与隔离的天然锚点。但注意（§2）：这棵树只描述启动+引导期，稳态行为不在树上。

---

## §4 进程类型分布

跨 session 共享的稳定进程骨架：

| comm | 角色 | 出现率 | 说明 |
|------|------|--------|------|
| claude | agent 主进程（唯一长寿 INFRA） | 7/7 | session 全程存活 |
| claude.exe | Bun 短命 worker | 7/7 | **存活仅 1-7ms 即退出**，属 ACTION |
| git | 动作（版本控制自检） | 6/7 | 启动期 |
| grep | 动作（文本搜索） | 6/7 | — |
| bash / sh | 通道（shell） | 6/7 | — |
| base64 | 动作（环境快照编码） | 5/7 | 缺父孤儿 |

进程 comm 里**没有任何 `node`**。被观测到的 `claude.exe`（共 21 个实例）**不是常驻 runtime**：每个实例存活 1-7ms 就退出（见 §5）。唯一真正长寿的进程是 `claude` 自身（Bun 主进程）。

**对 agentOS 的启示**：agent 进程骨架的"基础设施层"实际只有**单个**长寿进程（claude/Bun 主进程），而非"claude+node 两个常驻进程"。进程类型分布仍可作为 agent 能力指纹用于安全策略（出现 `git`/`base64`/`ssh` 即表明该 agent 触达了版本控制/编码/网络）。

---

## §5 进程存活时间

### 核心发现：双峰分布——1 个长寿主进程 + 全部子进程亚 10ms

| Session | 全部进程 p50 (ms) | p95 (ms) | claude 主进程存活 | claude.exe 存活 |
|---------|-----------------|---------|-----------------|----------------|
| T1 | 7 | 20,102 | 20,102ms | 2/4/7ms |
| T2 | 1 | 22 | 64,931ms | 1/3/6ms |
| T3 | 6 | 104,568 | 104,568ms | 2/5/6ms |
| T4 | 1 | 22 | 78,999ms | 1/3/6ms |
| T5a | 1 | 1,012 | 123,760ms | 1/6ms |
| T5b | 1 | 25 | 169,611ms | 2/5/7ms |
| T5c | 1 | 22 | 104,537ms | 1/3/6ms |

**p50 = 1-7ms**：半数子进程存活不到 7ms（`git`/`grep`/`base64` 级）。所有子进程（包括 `claude.exe`）采用"创建→执行→销毁"模式。

按**实测存活时间**分类——p50>1s 为 INFRA，p50<100ms 为 ACTION：

- **INFRA（实测 p50 > 1s）**：只有 `claude`（20-170s）。T5a 额外有一个 `ssh`（一次远程 fetch 的瞬时连接，归类边缘）。
- **ACTION（实测 p50 < 100ms）**：其余全部，含 `claude.exe`（1-7ms）、`git`/`grep`/`bash`/`base64`/`env`/`cut`/`head`/`awk`/`sed`/`cat`。

两类之间有 **4 个数量级**的存活时间差距（claude ~10⁵ms vs 子进程 ~10⁰ms），是 agent 进程中"长寿运行时"与"瞬时动作"的天然分界。

**对 agentOS 的启示**：p50=1-7ms 要求 agentOS 提供 1ms 级进程创建原语——传统 fork+exec 开销（mmap 重建、页表拷贝）占比过高，需要类似 `clone(CLONE_VFORK)` 或 posix_spawn 的轻量语义。但更重要的（呼应 §2）：这些瞬时子进程**集中在启动+引导期**，稳态期根本不创建进程。所以这条原语的价值主要在"加速 agent 冷启动"，而非"加速稳态工具执行"。

---

## §6 进程并发度

### 核心发现：最大并发仅 5-6 个

| Session | 最大并发 | 总进程数 |
|---------|---------|---------|
| T1 | 5 | 14 |
| T2 | 5 | 33 |
| T5a | 6 | 46 |
| T5b | 5 | 37 |

尽管总进程数可达 46，任意时刻同时存活的进程不超过 6。结合 §2：进程在引导期短促爆发后立即退出，稳态期并发恒为 1（只有 claude 主进程）。

**对 agentOS 的启示**：agentOS 不需要为单个 agent 预留大量进程槽位。5-6 个并发槽位即可覆盖引导期峰值；稳态期单进程。进程池可保守设计。真正需要并发资源的是 Bun 主进程内部的 worker 线程池（§2.3）——这是**线程级**而非进程级的并发需求。

---

## §7 退出状态分布

### 核心发现：非零退出率约 7-9%

| Session | 已退出进程 | 非零退出数 | 失败率 | 非零退出明细 |
|---------|----------|----------|--------|------------|
| T1 | 14 | 1 | 7.1% | claude.exe=1 |
| T2 | 33 | 3 | 9.1% | claude.exe=1, grep=1×2 |
| T3 | 14 | 1 | 7.1% | claude.exe=1 |
| T4 | 36 | 3 | 8.3% | claude.exe=1, grep=1×2 |
| T5a | 41 | 3 | 7.3% | git=1, grep=1×2 |
| T5b | 37 | 3 | 8.1% | claude.exe=1, grep=1×2 |
| T5c | 37 | 3 | 8.1% | claude.exe=1, grep=1×2 |

跨 7 个 session 共 17 次非零退出：`claude.exe=1`（×6，Bun worker 探测性退出）、`grep=1`（×10，grep 无匹配的正常退出码 1）、`git=1`（×1）。

这些非零退出多数是**预期的控制流信号**而非真错误——`grep` 无匹配返回 1 是正常语义（参见 [bestpractice_07-bash_strict_mode_pipes.md]）。非零退出率约 8%。

**对 agentOS 的启示**：agent 子进程的非零退出**普遍且语义多样**——既有真失败，也有"无匹配/探测失败"这类控制流信号。agentOS 的进程记账不能简单地把 `exit_code != 0` 当错误统计，需要区分"语义性非零"（如 grep 无匹配）和"真失败"。这与传统 OS 的二元成功/失败模型不同。

---

## §8 进程-LLM 时序关联

| Session | llm_calls | avg_procs_per_call（±1s 窗口） |
|---------|----------|------------------------------|
| T1 | 2 | 7.0 |
| T2 | 6 | 2.3 |
| T4 | 7 | 5.1 |
| T5b | 17 | 2.2 |
| T5c | 4 | 9.2 |

**这个 ±1s 窗口指标有严重误导性，保留仅为说明问题。** 它把启动期密集进程错误地归入时间上邻近的 LLM 调用。真实的时序关系由 §2 的统一时间线给出：

```
启动引导（0-0.4s）→ LLM1(flash, 系统提示) → [首次 shell 时] 环境快照爆发(~20ms)
  → LLM2(pro, 全上下文注入) → 稳态：LLM ↔ 工具(进程内) 反复交替，零新进程
```

**"认知↔行动"相位关系**：推理阶段（LLM 调用，3-30s）不创建子进程；**行动阶段同样不创建子进程**——工具执行在 Bun 线程池内。子进程创建唯一集中在 (a) 启动自检、(b) 首次 shell 环境探测，这两件事都发生在 session 前 ~20s。

**对 agentOS 的启示**：agentOS 在推理阶段（LLM I/O 等待，数秒到数十秒）应让出 CPU——让出的是**整个 agent 进程的 CPU 时间片**（稳态就是单进程），让其他 agent 使用。这是会话级（而非进程级）的 CPU 调度。

---

## §9 父子关系模式

Top 父子对：`bash → grep`（6/7）、`claude → git`（6/7）、`bash → git`（5/7）、`claude → bash`（6/7）、`claude → claude.exe`（7/7）。

`claude → bash → tool` 是最稳定的 2-hop 模式，bash 是纯通道节点（传递意图不贡献意图）。Claude Code 也经常绕过 bash 直接执行（`claude → git`），说明对高频自检路径做了优化。

**缺父孤儿（base64）的机制**：每个 session 的 6 个 `base64` 进程，ppid 指向 5 个不同的、未被捕获的中间 pid（如 T4 的 302732/302735/...）。这些中间进程是 bash 管道里 `cmd | base64` 的左侧环节，在 eBPF 记录其 exec 前已退出（亚毫秒）。这是 eBPF 进程追踪的已知竞态盲区：极短命的管道中间进程可能在父子关系被记录前消失。脚本把它们明确标为 `missing_parent_orphans`，不混入"多根"统计。

**对 agentOS 的启示**：bash/sh 通道节点应在 agentOS 调度器中透明化——调度和记账直接对动作节点（git/grep）进行。更关键的是 §2 揭示的：通道节点本身也只在引导期出现，稳态期连 bash 都不 fork。agentOS 的进程追踪应保证父子事件的原子提交，避免 base64 这种竞态孤儿。

---

## §10 Token 用量与模型切换

### 核心发现：flash→pro 双模型策略，两种变体

观测到两种 token 流模式：

**模式 A（4/7：T1, T2, T5a, T5b）**：小 flash 系统提示 → 大 pro 完整上下文。

首个 LLM 调用用 `deepseek-v4-flash`，输入仅 ~430 tokens（系统提示 + 简短任务指令）：

| Session | Flash S1 输入 | 输出 | 耗时 |
|---------|-------------|------|------|
| T1 | 424 | 339 | 4.4s |
| T2 | 430 | 213 | 3.7s |
| T5a | 435 | 523 | 6.6s |
| T5b | 485 | 695 | 7.2s |

第二个调用切到 `deepseek-v4-pro`，输入跃升至 ~120K（完整系统上下文注入）：

| Session | Pro S2 输入 | 输出 |
|---------|-----------|------|
| T1 | 120,407 | 275 |
| T2 | 120,471 | 318 |
| T5a | 120,594 | 263 |
| T5b | 120,691 | 317 |

**模式 B（3/7：T3, T4, T5c）**：双 flash 全上下文 → pro。前两次 LLM 调用均 `flash`，输入均 ~120K：

| Session | Flash S1 | Flash S2 | 首次 pro |
|---------|----------|----------|---------|
| T3 | 120,509 in | 120,509 in | S3: pro, 9,068 in |
| T4 | 120,560 in | 120,560 in | S3: pro, 1,667 in |
| T5c | 120,743 in | 120,743 in | S3: pro, 630 in |

两种模式下，后续调用均在 flash/pro 间按上下文大小切换。

**对 agentOS 的启示**：agentOS 调度器需感知 LLM 调用的成本差异——flash ~4s，pro 按 token 量约 10-60s。若一个进程内工具调用（亚毫秒）后跟一个 30s 的 pro 推理，调度器应把 CPU 让给其他 agent。

### Token 缓存模式

所有 session 的 `cache_create_tokens=0`、`cache_read_tokens>0`，缓存命中率 100%。系统提示和工具定义预缓存，session 期间不重建：T2 读 153,600、T4 读 616,192、T5b 读 1,895,040。

**对 agentOS 的启示**：agentOS 的系统提示缓存应在 agent 间共享——多个同类 agent（多个 Claude Code 实例）若使用相同系统提示和工具定义，token 缓存可复用，显著降低首个 LLM 调用的输入成本。

---

## §11 工具调用分析

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
- `related_pid` 全部指向 `claude` 主进程，**不是因为关联粒度不足，而是因为工具确实由主进程（及其 Bun 线程池）执行**。这与 §2 一致：工具不 fork 子进程。`tool_calls.related_pid → claude` 是对现实的准确反映，而非数据缺陷。
- `tool_calls.input_json` 在本数据集中**全部为空**（来自 session_file 的元数据未含 input），无法还原具体命令——这是 AgentSight session_file 采集的字段缺失。
- **工具调用数与子进程数无对应关系**：T5b 有 18 次工具调用却 0 个稳态子进程；T3 有 3 次工具调用 0 个动作子进程。工具→子进程不是多对多，而是**几乎不映射**（绝大多数工具进程内执行）。

**对 agentOS 的启示**：agentOS 的资源计量主粒度应是**工具调用（tool_call）**，而非进程。进程级计量会系统性低估 agent 的实际工作量（稳态工具执行不产生进程）。需要运行时配合上报工具级事件。

---

## §12 资源使用

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
- RSS 增长集中在 session 前半段（100→~300MB），之后稳定。增长来源是 Bun heap 扩张和文件缓存，不是进程创建（瞬时子进程内存可忽略）。
- CPU p50 = 1.5-2.5%，说明 agent 绝大多数时间在 I/O 等待（等 LLM 响应）。CPU max = 33-41%，峰值对应引导期进程爆发 + 进程内文本处理。
- 资源采样仅覆盖 claude 主进程——这恰好与 §2 吻合：稳态期只有主进程，子进程在采样间隔内已退出。

**对 agentOS 的启示**：agent 进程 CPU 利用率极低（p50 < 2.5%），大量时间浪费在 LLM I/O 等待上。agentOS 可在 LLM 调用期间将整个 agent 进程置于休眠（会话级让出），让其他 agent 使用 CPU。RSS 稳定在 ~300MB 也说明：agentOS 为每个 agent 预留的内存可按 ~300-400MB 估算（单 Bun runtime + 上下文缓存）。

---

## §13 网络与遥测

Claude Code 的网络连接含三类目标：

1. **LLM API**：`api.deepseek.com/anthropic/v1/messages?beta=true` ——实际推理请求（deepseek-v4 经 anthropic 兼容端点）。频率 = LLM 调用数（T5b 17 次）。
2. **MCP Registry**：`api.anthropic.com/mcp-registry/v0/servers?version=latest` ——MCP 服务发现，每次启动查询 4 次（带分页）。
3. **Telemetry**：`api.anthropic.com/api/event_logging/v2/batch` ——遥测批量上报。

遥测/LLM 调用比约 0.6-1.0（T5b 17 LLM : 11 遥测 ≈ 0.65，因批量上报）。T5a 额外有 `downloads.claude.ai/.../plugins`（插件下载）和一次 `messages/count_tokens`。

**对 agentOS 的启示**：agentOS 应内置遥测——若每次进程创建/LLM 调用/工具调用都被 agentOS 自动记录（类 systemd journal），agent 开发者无需自行实现遥测，可减少遥测相关的网络往返与 I/O。

---

## §14 审计事件分析

audit_events 表记录三类事件：

| 事件类型 | T1 | T2 | T3 | T4 | T5a | T5b | T5c |
|---------|----|----|----|----|----|----|-----|
| process (exec/exit) | 28 | 67 | 28 | 73 | 88 | 75 | 75 |
| file (write) | 11 | 29 | 11 | 43 | 32 | 21 | 13 |
| llm (request/response) | 3 | 7 | 6 | 8 | 8 | 18 | 5 |
| **总计** | **42** | **103** | **45** | **124** | **128** | **114** | **93** |

- process 事件 ≈ 2×进程总数（每进程 exec+exit），exec 略多于 exit 因 session 结束时主进程未退出。
- **file write 事件的执行者**揭示了 §2 的核心证据——引导后的 file write 全部由 `Bun Pool N` / `claude` 执行（T4 中 28/43 次发生在最后一个子进程之后）。这是"稳态工具进程内执行"的直接审计证据。
- 最大 128 条（T5a），远低于 Claude Code 默认 10,000 上限，无截断。

**对 agentOS 的启示**：audit 的三维度（进程生命周期、文件 I/O、LLM 调用）恰好构成 agent 行为完整画像。**关键：文件 I/O 维度必须按线程/comm 记录**，否则会漏掉稳态期由 Bun Pool 线程完成的全部工具工作。agentOS 审计子系统应原生支持线程级 file I/O 归因。

---

## §15 对 agentOS 进程模型的启示

### 回扣三个核心问题

**问题 1：子进程数量/类型/层级。** Claude Code 子进程 14-46 个，但**集中在 session 前 ~20s**。三阶段：(a) 启动引导（14 进程，0.4s，claude+git×6+claude.exe×3+sh+ps+grep×2），(b) 首次 shell 环境快照（~20 进程，一次性），(c) 稳态（零新进程）。进程分类学：**唯一长寿 INFRA = claude（Bun 主进程）**；其余全是瞬时 ACTION（含 claude.exe，存活 1-7ms）。进程树深度恒 3 层、单根。最大并发 5-6，稳态并发 1。

**问题 2：进程↔LLM 时序关联。** **推理和行动都不在稳态创建子进程**。子进程创建唯一集中在启动自检 + 首次 shell 探测（前 20s）。之后 LLM 调用与工具调用反复交替达数十到上百秒，进程树完全静止——工具执行在 Bun 线程池内进程内完成。首次 LLM 用 flash 处理系统提示，第二次用 pro 注入 120K 上下文。Token 缓存 100% 命中。

**问题 3：对进程抽象设计的启示。**
- **进程不是 agent 的行动单元**（最重要）→ agentOS 的隔离/记账/调度原语应下沉到**线程/syscall + 工具调用**粒度。按进程做单元会完全错过稳态行为。
- **agent ≈ 单长寿 runtime + 线程池**，不是 per-request fork → agentOS 进程抽象应支持"长寿 agent 进程 + 内部 worker 线程池"，而非"每动作一进程"。
- **子进程是边缘事件**（启动自检 / shell 逃逸）→ 用重安全策略审计这些稀有 fork，用轻量 syscall 沙箱管稳态进程内工具。
- p50=1-7ms 的瞬时进程 → 1ms 级创建原语，但价值在加速**冷启动**而非稳态。
- 失败率 ~8%（非 0），语义多样（grep 无匹配 vs 真失败）→ 记账需区分语义性非零与真失败。
- 环境快照可预计算 → 镜像构建期固化，省掉运行时首次 shell 的 ~20 进程。
- CPU p50<2.5% + 稳态单进程 → **会话级** CPU 让出（推理期休眠整个 agent），而非进程级。
- RSS 稳定 ~300-400MB → 每 agent 内存预算的估算基线。
- 工具→进程几乎不映射 → 资源计量主粒度用 tool_call，不用 process_node。

### 总体判断

agent 进程模型是"单进程事件循环 runtime + 固定 worker 线程池"（Bun/Node 自身的形态），而非 Web 服务器的 per-request fork。子进程不随 LLM/工具调用产生，而是集中在启动+引导期；任务的认知↔行动主循环全部在单个长寿进程内闭合。

```
单进程 runtime 模型:

  claude (Bun runtime, 长寿)
    ├─ 启动期: fork {git×6, claude.exe×3, sh→ps/grep} 做环境自检     ← 唯一的进程密集期
    ├─ 首次 shell: fork {bash→locale-check/base64×6/...} 探测环境一次  ← 一次性
    └─ 稳态: 内部 Bun Pool 线程执行所有工具
              [LLM I/O 等待] ↔ [Read/Write/Edit/Bash 进程内执行]
              ↑ 数十到上百秒，零新进程
```

对 agentOS 最深的一条启示：**如果照搬 Unix"进程=隔离/调度/记账单元"的假设来设计 agentOS，会在 agent 稳态期完全失明**——因为现代 agent runtime（Bun/Node 型）把行动收敛进了单进程的线程池。agentOS 真正需要的新原语，是**进程内的、线程/syscall/工具调用粒度的**可观测、可隔离、可记账机制。

---

## §16 已知限制

1. **T5 非交互式**：回退方案使多轮交互的进程复用行为不可见——但 §2 已强证明：即使多轮（T5b 17 轮 LLM），稳态也不创建进程，"复用"问题部分被回答（无进程可复用，复用的是 Bun 线程池）。
2. **LLM 调用元数据不完整**：仅首个调用有完整 provider/duration，后续 `end_timestamp_ms` 多为空，来自 session_file 延迟写入。
3. **tool_calls.input_json 全空**：无法还原具体命令内容（session_file 采集字段缺失）。这是确认"哪个 Bash 命令触发了什么"的主要障碍。
4. **argv/cwd 捕获为空**：所有进程的 `argv_json=[]`、`cwd=NULL`，`command` 仅含 exe 路径（如 `/usr/bin/git`），无完整参数。AgentSight `process_nodes` 采集限制。
5. **子进程资源采样缺失**：`resource_samples` 仅含 claude 主进程——与 §2 吻合（稳态只有主进程），但也意味着无法测量瞬时子进程的 CPU/RSS。
6. **base64 缺父孤儿**：每 session 6 个，eBPF 管道中间进程的竞态窗口（§9），已在脚本中明确分类。
7. **进程内工具执行靠 audit file 事件间接证明**：本报告"工具在 Bun 线程池内执行"的结论依赖 `audit_events(file)` 的 comm 字段（Bun Pool N）+ "exec 事件 17s 后停止但工具调用继续"的反证，未能直接观测线程级 syscall（AgentSight 当前不追踪线程级行为）。这是结论的主要证据强度边界。
8. **单 agent 类型**：仅观测 Claude Code（Bun）。其他 agent（OpenClaw、Codex、Cursor）若用不同 runtime（如真 Node、Python、或大量 shell 逃逸），进程模型可能截然不同——尤其"进程内执行"结论可能不成立。
9. **非生产负载**：人工设计的示例 prompt，真实开发 session（更多 Bash/编译/测试）可能产生持续的子进程流。

### 后续验证建议

- **直接验证进程内执行**：增强 AgentSight 追踪线程级 syscall（如 `openat`/`write` 的发起线程），把"Bun Pool 线程执行工具"从间接推断升级为直接观测。
- **真实长 session**：录制一个含编译/测试/大量 Bash 的真实开发 session，检验稳态是否仍零子进程（预期 `make`/`cargo`/`pytest` 会破坏"零进程"——这正是 agentOS 需要区分的"进程内工具" vs "外部命令工具"边界）。
- **跨 agent 对比**：对 OpenClaw/Codex 录制同样 5 类任务，验证"单 runtime + 线程池"是否是 agent 通则还是 Claude Code/Bun 特有。

---

*Created: 2026-06-25 | Updated: 2026-06-26*
*Based on 7 AgentSight sessions of Claude Code v2.1.191 (Bun runtime)*
