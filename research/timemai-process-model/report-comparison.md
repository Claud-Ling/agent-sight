# TimemAi vs Claude Code：进程模型对比报告

> 对比两种 agent runtime（TimemAi v0.5 Rust + DeepSeek vs Claude Code v2.1.191 Bun + Anthropic API）的进程行为差异，分析对 agentOS 进程抽象设计的启示。

## 方法学说明

1. **Prompt 对齐**：本实验的 7 个 session 使用与 Claude Code 实验完全相同的 prompt（逐字符一致）。T3/T4/T5 额外添加了 `--supporting-context` 运行时提示（JSON 格式约束），不修改用户 prompt 文本。详见 `prompt-comparison.md`。

2. **TimemAi 版本**：v0.5，`TIMEM_MAX_LLM_OUTPUT=20480`（修复了 v0.4 `max_tokens=2048` 导致的响应截断问题）。

3. **T5b/T5c 多轮上下文差异（重要）**：CC 的 T5b prompt 以"基于你刚对 agentOS 的分析"开头，T5c 以"总结我们讨论的核心设计原则"开头——这些引用交互式 session 中的前序上下文。TM 的 `--once-json` 模式下每个 session 独立运行，没有对话历史。这是 **`--once-json` 模式的固有差异**，不是 prompt 差异。**对定量对比的影响**：CC 的 T5b 有 37 进程和 18 次工具调用（至少部分来自验证前序上下文）、TM 的 T5b 仅 2 进程 0 工具调用（无上下文需验证）。T5b 和 T5c 的进程数/调用数绝对值对比不可靠，下文仅用于定性描述进程模型特征，不用于定量比较。

4. **结论可信度分级**：结构性结论（fork vs 线程池、进程树形态、资源占用模式）不受 task 差异影响，可信度高。定量结论中，T1-T4 和 T5a 的对比可信度较高（task 等价），T5b/T5c 的定量对比可信度低（task 不等价），已在相关表格中标注。

---

## 对比数据源

- **TimemAi v0.5**：7 个 AgentSight eBPF session（T1-T5），`research/timemai-process-model/sessions/`
- **Claude Code v2.1.191**：7 个 AgentSight eBPF session（T1-T5），`research/agentos-process-model/sessions/`
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
| 占比最高进程 | git (42.9% in T3) | grep (38.5% in T3) |

### 进程树可视化对比（T3）

**Claude Code**（14 进程，3 层）：
```
claude                           ← 根（Bun runtime + LLM 进程内 HTTP）
├── git → git → git             ← git 操作链（启动 burst）
├── git                          ← git 操作
├── git                          ← git 操作
├── claude.exe                   ← Bun worker 线程
├── sh → {ps, grep×2}            ← 工具执行（罕见）
├── claude.exe                   ← Bun worker 线程
└── claude.exe                   ← Bun worker 线程
```

**TimemAi v0.5**（26 进程，2 层）：
```
timem-native-rs                  ← 根（Rust runtime，单线程事件循环）
├── curl                         ← LLM#1（子进程 HTTP，~20s）
├── sh → {locale-check, grep×3, find}  ← 工具#1
├── curl                         ← LLM#2（~25s）
├── cat → {locale-check, grep×2} ← 工具#2
├── curl                         ← LLM#3（~30s）
├── sh → {locale-check, grep×4, find}  ← 工具#3
├── curl                         ← LLM#4（~20s）
│   └── sed → {locale-check, grep×2}   ← 工具#4（代码修改）
├── curl                         ← LLM#5（~50s，最终响应）
└── curl                         ← LLM#6（~30s，验证）
```

**关键差异**：CC 在启动阶段一次性产生进程集群（git burst，12 进程），此后 ~104s 无新子进程；TM 在 ~194s 全程持续产生子进程，6 个 curl 集群交替出现。

---

## 2. 进程创建时序对比

| 维度 | Claude Code (T3) | TimemAi v0.5 (T3) |
|------|-----------------|-------------------|
| 进程创建集群数 | 1 | 12 |
| 进程总数 | 14 | 26 |
| 启动 burst 进程数 | 12 | 1 (timem-native-rs) |
| 稳态子进程 | 0 | 持续 |
| post_burst_tail_ms | 104,176ms (104s零子进程) | 8,427ms |
| LLM 调用时的子进程 | 0（进程内 HTTP） | 1 curl / 调用 |

**结论**：Claude Code 是"启动 burst + 稳态零子进程"模型，TimemAi 是"持续交替 burst"模型。v0.5 的 T3 进程数（26）低于 v0.4（39）——因为 `max_tokens=20480` 使每轮 LLM 调用能完成更多工作，减少了总轮次。

---

## 3. 进程生命周期对比

| 维度 | Claude Code (T3) | TimemAi v0.5 (T3) |
|------|-----------------|-------------------|
| p50 | 6ms | 2ms |
| p95 | 104,568ms | 59,172ms |
| INFRA 级子进程 | 无（仅根 claude） | curl（p50≈30,000ms） |
| ACTION 级子进程 | 全部（git/grep/sh/claude.exe） | grep/cat/find/sed/locale-check |

CC 的子进程全部是 ACTION（p50=3-23ms），因为没有 HTTP 子进程。TM 的 curl 是 INFRA 级（p50≈30s），等待 DeepSeek API 返回。TM 的有效"工作单元"（curl）寿命是 CC 子进程的 5,000+ 倍。

---

## 4. 工具执行模式对比

| 维度 | Claude Code | TimemAi |
|------|------------|---------|
| 工具执行方式 | Bun worker 线程池，进程内 | fork `sh -c`，子进程 |
| 每次工具调用的进程开销 | 0（在线程内完成） | 1 sh + 1 locale-check + N 个命令子进程 |
| 文件探索工具 | git（42.9%） | find + grep + cat + ls（不依赖 git） |
| 文件修改工具 | BashTool（sh → sed/awk） | run_bash（sh → sed） |

**核心差异**：CC 的工具执行在线程池内完成，不产生进程事件。TM 每次 `run_bash` 都产生 3-8 个子进程。但 v0.5 的高 max_tokens 使每次工具调用更高效（模型可以一次发出多个文件操作），减少了总工具调用次数。

---

## 5. 资源与并发对比

| 维度 | Claude Code (T3) | TimemAi v0.5 (T3) |
|------|-----------------|-------------------|
| RSS max | 317MB | 17MB |
| CPU max | 36.18% | 0.5% |
| 最大并发进程 | 5 | 3 |
| 并发峰值时刻 | 启动 git burst | 工具执行 sh burst |

TM 的资源占用绝对值远低于 CC（RSS 约 1/18，CPU 约 1/72）。这个差异来自多个叠加因素：(a) Rust 编译型单二进制 vs Bun JIT runtime（基线内存差异），(b) TM 的 LLM 推理完全外置（curl 子进程不占 RSS）vs CC 在进程内维护 HTTP 连接池和 TLS session cache，(c) CC 的 ~120K token 系统提示缓存驻留在进程堆中。因此 RSS 对比反映的是**运行时技术栈 + 架构选择**的综合效应，而非单纯的 agent 架构效率差异。TM 子进程都是极轻量级的 shell 命令（grep/cat 存活 < 10ms），不贡献显著 RSS。

---

## 6. 对比假设验证

| 假设 | 验证结果 | 证据 |
|------|---------|------|
| TM 是 per-request 子进程模型 | ✓ 确认 | 6 curl = 6 LLM 调用，每个存活 5-60s |
| CC 是启动 burst + 线程池稳态 | ✓ 确认 | 1 启动集群（12 进程），~104s 零子进程稳态 |
| TM HTTP = curl 子进程 | ✓ 确认 | 无进程内 HTTP 流量，所有 LLM 调用对应 curl |
| CC HTTP = 进程内 | ✓ 确认 | 零 HTTP 子进程，LLM 流量在线程内 |
| TM 进程数与 LLM 调用正相关 | ✓ 确认 | T3: 26 进程 ≈ 6 curl + 工具 burst |
| CC 进程数主要在启动阶段 | ✓ 确认 | T3: 14 进程中 12 在启动 burst |

---

## 7. 协议修复与可靠性机制

| 维度 | Claude Code | TimemAi v0.5 |
|------|------------|-------------|
| 可靠性模型 | 依赖 Anthropic API 层的 tool use 协议保证 | Runtime 层显式 JSON envelope 验证 |
| 响应截断处理 | API 层返回错误 | v0.5 检测 `finish_reason=length` → 自动修复请求 |
| Chain-of-Thought 泄漏 | 不存在（Anthropic 模型不泄漏 CoT） | 存在（DeepSeek 在复杂任务中泄漏 CoT），通过 JSON 扫描容错 + supporting-context 缓解 |
| 修复触发 | 无 | T3 触发 1 次（CoT 泄漏 → invalid_json → 修复成功） |

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
- TM 的区分依赖测量生命周期（curl p50=30s → INFRA，grep p50=2ms → ACTION）
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

**启示 6：Runtime 层协议验证是可靠性与开销的权衡**

- TM 的 protocol repair 在 T3 中触发 1 次额外 LLM 调用（增加进程开销）
- CC 没有等价机制——依赖 API provider 的协议保证
- agentOS 应感知"防御性 LLM 调用"并将其与业务 LLM 调用区分计量

---

## 9. 数据质量说明

TimemAi session 的 `llm_calls`/`tool_calls`/`token_usage`/`network_targets` 表均为空。原因：AgentSight 的 HTTP 解析器不识别 DeepSeek API 的 OpenAI-compatible 响应格式（`chat/completions` 端点）。

本报告的 LLM 调用数和工具调用数来自 TimemAi 自身的 `audit/api_audit.jsonl` 中的 `stats` 字段（精确计数），进程行为分析来自 `process_nodes` 表（完整数据）。

---

## 10. 总结

| 维度 | Claude Code | TimemAi v0.5 | 差异性质 |
|------|------------|-------------|---------|
| 进程模型 | 启动burst+线程池稳态 | 持续交替burst | 结构性 |
| HTTP实现 | 进程内（Bun HTTP client） | 子进程（curl） | 架构选择 |
| 工具执行 | 线程池内 | fork sh子进程 | 架构选择 |
| 树深度 | 3层 | 2层 | 工具链差异 |
| 进程数（T3） | 14 | 26 | 模型差异 |
| 稳态子进程 | 0 | 持续 | 结构性 |
| 资源占用 | 317MB RSS | 17MB RSS | 运行时差异（Rust vs Bun） |
| 可靠性机制 | API层协议保证 | Runtime层协议验证+修复 | 设计哲学差异 |
| eBPF可观测性 | 低（线程池内执行不可见） | 高（每工作单元=进程事件） | 架构选择 |

---

*Created: 2026-06-25 | Updated: 2026-06-26 (v0.5 re-recording)*
