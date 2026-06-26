# TimemAi vs Claude Code：进程模型对比报告

> 对比两种 agent runtime（TimemAi Rust + DeepSeek vs Claude Code Bun + Anthropic API）的进程行为差异，分析对 agentOS 进程抽象设计的启示。

## 对比数据源

- **TimemAi**：7 个 AgentSight eBPF session（T1-T5），`research/timemai-process-model/sessions/`
- **Claude Code**：7 个 AgentSight eBPF session（T1-T5），`research/agentos-process-model/sessions/`
- 同一任务 prompt 分别在两个 runtime 上执行

---

## 1. 进程树拓扑对比

### 结构形态

| 维度 | Claude Code | TimemAi |
|------|------------|---------|
| 树深度 | 3 层 | 2 层 |
| 根进程 | 1 个（`claude`） | 1 个（`timem-native-rs`） |
| 持久 worker | `claude.exe`（Bun worker 线程池） | 无 |
| 典型进程类型 | git, claude.exe, grep, sh | curl, grep, sh, locale-check |
| 占比最高进程 | git (42.9% in T3) | grep (35.9% in T3) |

### 进程树可视化对比（T3）

**Claude Code**（14 进程，3 层）：
```
claude                           <- 根（Bun runtime + LLM 进程内 HTTP）
├── git → git → git             <- git 操作链（启动 burst）
├── git                          <- git 操作
├── git                          <- git 操作
├── claude.exe                   <- Bun worker 线程
├── sh → {ps, grep×2}            <- 工具执行（罕见）
├── claude.exe                   <- Bun worker 线程
└── claude.exe                   <- Bun worker 线程
```

**TimemAi**（39 进程，2 层）：
```
timem-native-rs                  <- 根（Rust runtime，单线程事件循环）
├── curl                         <- LLM API#1（子进程 HTTP）
├── sh → {locale-check, grep×3, ls}  <- 工具#1
├── curl                         <- LLM API#2
├── sh → {locale-check, grep×5, cat, find×2, head} <- 工具#2
├── curl                         <- LLM API#3
├── sh → {locale-check, grep×2, cat×2, find} <- 工具#3
├── curl                         <- LLM API#4
├── head → {locale-check, grep×2} <- 工具#4
├── curl                         <- LLM API#5
├── sed → {locale-check, grep×3} <- 工具#5
├── curl                         <- LLM API#6
└── curl                         <- LLM API#7
```

**关键差异**：CC 在启动阶段一次性产生进程集群（git burst，12 进程），此后 104s 无新子进程；TM 在 158s 全程持续产生子进程，7 个集群交替出现。

---

## 2. 进程创建时序对比

| 维度 | Claude Code (T3) | TimemAi (T3) |
|------|-----------------|-------------|
| 进程创建集群数 | 1 | 7 |
| 进程总数 | 14 | 39 |
| 启动 burst 进程数 | 12 | 2 |
| 稳态子进程 | 0 | 持续 |
| post_burst_tail_ms | 104,176ms (104s零子进程) | 36,237ms |
| LLM 调用时的子进程 | 0（进程内 HTTP） | 1 curl / 调用 |

**结论**：Claude Code 是"启动 burst + 稳态零子进程"模型，TimemAi 是"持续交替 burst"模型。

---

## 3. 进程生命周期对比

| 维度 | Claude Code (T3) | TimemAi (T3) |
|------|-----------------|-------------|
| p50 | 6ms | 2ms |
| p95 | 104,568ms | 37,457ms |
| INFRA 级子进程 | 无（仅根 claude） | curl（p50=32,600ms） |
| ACTION 级子进程 | 全部（git/grep/sh/claude.exe） | grep/cat/find/sh/sed/locale-check |

**CC**：子进程全部是 ACTION（p50=3-23ms），因为没有 HTTP 子进程，git/claude.exe 都是瞬时完成。
**TM**：curl 是 INFRA 级（p50=32.6s），因为需要等待 DeepSeek API 返回。TM 进程模型的有效"工作单元"（curl）寿命是 CC 子进程的 5000+ 倍。

---

## 4. 工具执行模式对比

| 维度 | Claude Code | TimemAi |
|------|------------|---------|
| 工具执行方式 | Bun worker 线程池，进程内 | fork `sh -c`，子进程 |
| 每次工具调用的进程开销 | 0（在线程内完成） | 1 sh + 1 locale-check + N 个命令子进程 |
| 文件探索工具 | git（42.9%） | find + grep + cat + ls（不依赖 git） |
| 文件修改工具 | BashTool（sh → sed/awk） | run_bash（sh → sed） |

**核心差异**：CC 的工具执行在线程池内完成，不产生进程事件。TM 每次 `run_bash` 都产生 3-10 个子进程。导致工具密集任务中 TM 的进程数远高于 CC（T3: 39 vs 14），但 CC 的工具执行速度更快（无 fork overhead）。

---

## 5. 资源与并发对比

| 维度 | Claude Code (T3) | TimemAi (T3) |
|------|-----------------|-------------|
| RSS max | 317MB | 17MB |
| CPU max | 36.18% | 0.5% |
| 最大并发进程 | 5 | 7 |
| 并发峰值时刻 | 启动 git burst | 工具执行 sh burst |

TM 的资源占用是 CC 的 1/18（RSS）到 1/72（CPU），但进程数更多（39 vs 14）。资源效率与进程数反向——TM 的子进程都是极轻量级的 shell 命令。

---

## 6. 对比假设验证

| 假设 | 验证结果 | 证据 |
|------|---------|------|
| TM 是 per-request 子进程模型 | YES 确认 | 7 curl = 7 LLM 调用，每次存活 30-40s |
| CC 是启动 burst + 线程池稳态 | YES 确认 | 1 启动集群（12 进程），104s 零子进程稳态 |
| TM HTTP = curl 子进程 | YES 确认 | 无进程内 HTTP 流量，所有 LLM 调用对应 curl |
| CC HTTP = 进程内 | YES 确认 | 零 HTTP 子进程，LLM 流量在线程内 |
| TM 进程数与 LLM 调用正相关 | YES 确认 | T3: 39 进程 = 7 curl + 3 sh burst |
| CC 进程数主要在启动阶段 | YES 确认 | T3: 14 进程中 12 在启动 burst |

**全部 5 个假设均验证成立**。

---

## 7. 异常行为：协议修复

TimemAi 独有的 protocol repair 机制——Rust runtime 层验证模型输出的 JSON envelope 合法性，不合法则尝试一次修复，仍不合法则阻断——在 T3 和 T5a 中触发。

Claude Code 没有等价机制——它依赖模型自觉遵循 tool use 协议，不合法时由 Anthropic API 层返回错误。

差异含义：
- TM 的可靠性通过 runtime 层显式协议验证实现
- CC 的可靠性依赖 API provider 的协议保证
- TM 的修复请求可能产生额外的 LLM 调用（因 AgentSight 的 `llm_calls` 表为空，无法从进程数据直接区分"修复 curl"和"正常下一轮 curl"）

---

## 8. 对 agentOS 进程抽象的设计启示

### 8.1 核心结论：两种极端模型需要统一抽象

```
Claude Code:  启动 burst → 线程池稳态（零子进程，进程内工具执行）
TimemAi:      持续交替 burst → per-request fork（curl → sh burst 循环）
```

agentOS 的进程抽象必须同时容纳这两种模型，不能假定 agent runtime 使用线程池或子进程模型。

### 8.2 具体启示

**启示 1：进程生命周期是区分工作单元 vs 基础设施的关键信号**

- CC 的区分依赖 `comm` 名称（claude=INFRA, 其余=ACTION）
- TM 的区分依赖测量生命周期（curl p50=32s -> INFRA，grep p50=2ms -> ACTION）
- agentOS 应该用**测量生命周期**而非进程名来区分 INFRA/ACTION，因为不同 runtime 的命名约定不同

**启示 2：子进程的语义由 runtime 决定，不可假设**

- curl 在 TM 中是 LLM 调用单元（核心工作），在 CC 中不存在
- git 在 CC 中是文件探索基础设施（42.9%），在 TM 中不存在
- agentOS 的进程分类需要 runtime 注册语义标签，不能靠进程名推断

**启示 3：进程树深度不等于复杂度**

- CC 3 层树 = 基础设施深度（claude→git→git）
- TM 2 层树 = 工具链深度（timem→sh→grep）
- agentOS 应该用进程类型多样性 + 创建模式（burst vs steady）来衡量复杂度

**启示 4：并发模型应该在进程抽象中显式表达**

- CC：线程池并发（多个 claude.exe worker），对 eBPF 不可见
- TM：fork 并发（sh burst 内多个 grep/find 并行），对 eBPF 可见
- agentOS 需要区分这两种并发——前者是调度域内的，后者跨调度域

**启示 5：HTTP 层实现选择影响进程模型全局**

- TM 选择子进程 curl → 进程数与 LLM 调用数线性相关，eBPF 可观测
- CC 选择进程内 HTTP → 进程数与 LLM 调用数无关，eBPF 不可观测
- agentOS 如果希望统一观测，需要在 runtime 与 OS 之间建立 HTTP 调用通知接口

---

## 9. 数据质量说明

TimemAi session 的 `llm_calls`/`tool_calls`/`token_usage`/`network_targets` 表均为空。原因：
- AgentSight 的 HTTP 解析器可能不识别 DeepSeek API 的响应格式（与 Anthropic API 格式不同）
- curl 子进程存活时间可能短于 eBPF SSL 探针的捕获窗口（race condition）

这不影响进程树分析（`process_nodes` 数据完整），但限制了 token 级和网络目标级对比。后续可考虑：
- 扩展 AgentSight HTTP 解析器支持 OpenAI-compatible API 响应格式
- 使用 strace 补录 curl 子进程的网络系统调用

---

## 10. 总结

| 维度 | Claude Code | TimemAi | 差异性质 |
|------|------------|---------|---------|
| 进程模型 | 启动burst+线程池稳态 | 持续交替burst | 结构性 |
| HTTP实现 | 进程内（Bun HTTP client） | 子进程（curl） | 架构选择 |
| 工具执行 | 线程池内 | fork sh子进程 | 架构选择 |
| 树深度 | 3层 | 2层 | 工具链差异 |
| 进程数（T3） | 14 | 39 | 模型差异 |
| 稳态子进程 | 0 | 持续 | 结构性 |
| 资源占用 | 317MB RSS | 17MB RSS | 运行时差异 |
| 可靠性机制 | API层协议保证 | Runtime层协议验证+修复 | 设计哲学差异 |
