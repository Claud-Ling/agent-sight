# TimemAi 进程模型观察报告

> 基于 AgentSight eBPF 录制的 7 个 session（T1-T5），从进程树、生命周期、并发、退出状态、父子关系、审计事件、时序等维度描述 TimemAi（Rust + DeepSeek）的运行时进程行为。

**版本信息**：TimemAi v0.5（`TIMEM_MAX_LLM_OUTPUT=20480`），DeepSeek v4-pro（OpenAI-compatible 端点），AgentSight（本仓库构建）。非交互模式：`timem --once-json "<prompt>" --bash-approval approve`。

**Prompt 对齐**：本实验全部 7 个 session 使用与 Claude Code 实验**完全相同的 prompt**（参见 `prompt-comparison.md`）。T3/T4/T5 额外添加了 `--supporting-context` 提示以确保模型输出合法 JSON（不改变用户 prompt 文本，详见 §13）。

---

## §1 实验概览

| Session | 任务类型 | 进程数 | curl (LLM代理) | sh (工具代理) | 耗时 | 树深度 | 唯一comm |
|---------|---------|--------|---------------|-------------|------|--------|---------|
| T1 | 简单问答（eBPF 解释） | 2 | 1 | 0 | 5.2s | 1 | 2 |
| T2 | 多文件读取（架构阅读） | 16 | 3 | 2 | 54.2s | 2 | 7 |
| T3 | 代码修改（添加 --version） | 26 | 6 | 1 | 193.8s | 2 | 7 |
| T4 | 工具密集（TODO 扫描 + git） | 11 | 2 | 1 | 55.6s | 2 | 6 |
| T5a | agentOS 基础概念 | 2 | 1 | 0 | 32.1s | 1 | 2 |
| T5b | 进程模型设计 | 2 | 1 | 0 | 41.6s | 1 | 2 |
| T5c | 架构设计概要 | 3 | 2 | 0 | 61.3s | 1 | 2 |

**录制环境**：Linux 5.15, TimemAi v0.5（Rust release build），后端 `api.deepseek.com/chat/completions`（deepseek-v4-pro, OpenAI-compatible 端点）。

**Prompt 对齐说明**：全部 7 个 session 的 prompt 与 Claude Code 实验完全一致。T1-T4 运行在 AgentSight 仓库目录（`/home/lixiang/EMU/agentsight`），T5a-T5c 为纯推理任务（无需特定工作目录）。

**数据源说明**：AgentSight SQLite 数据库含 7 张表。以下 4 张表在 TimemAi session 中**均为空**：
- `llm_calls` — AgentSight HTTP 解析器不识别 DeepSeek API 的 OpenAI-compatible 响应格式
- `tool_calls` — 同上
- `token_usage` — 同上
- `network_targets` — 同上

以下 3 张表**有完整数据**：
- `process_nodes` — 完整（本报告的核心数据源）
- `audit_events` — 完整（含 process/file 两类事件）
- `resource_samples` — 完整（仅覆盖 `timem-native-rs` 根进程）

LLM 调用数以 `curl` 子进程数量为代理指标，工具调用数以 `sh` 子进程数量为代理指标。TimemAi 自身的 `audit/api_audit.jsonl` 提供 LLM 调用和工具调用的精确计数（从 `stats` 字段提取）。

---

## §2 核心发现：Per-Request Fork 模型 — 持续交替爆发

### §2.1 决定性证据：每个 LLM 调用和工具调用都对应子进程

T3 session（193.8s，26 进程）的进程创建时序展示了这一模式：

```
timem-native-rs (PID 744458, 持续 194s)
  ├── curl (LLM#1, ~20s)  →  sh (工具#1: 阅读 main.rs, ~30ms)
  ├── curl (LLM#2, ~25s)  →  sh (工具#2: 搜索 Cli 结构, ~20ms)
  ├── curl (LLM#3, ~30s)  →  sh (工具#3: 阅读更多源码, ~25ms)
  ├── curl (LLM#4, ~20s)  →  sh (工具#4: 修改文件, ~20ms)
  ├── curl (LLM#5, ~50s)  →  (最终响应: 任务完成)
  └── curl (LLM#6, ~30s)  →  (验证/补充)
```

T3 有 6 个 curl（= 6 次 LLM 调用）和 1 个 sh（实际工具调用次数 > 1，部分工具执行不经过 sh 通道）。T2（16 进程）有 3 个 curl + 2 个 sh burst。

### §2.2 T5 纯推理任务的最简形态

T5a（2 进程）、T5b（2 进程）、T5c（3 进程）都是纯推理任务——仅 `timem-native-rs → curl`，零工具调用。T5c 有 2 个 curl（第 1 次记忆检查 + 第 2 次生成回答），T5a/T5b 各 1 个 curl。

与 Claude Code 的对比：CC 在纯推理时仍然是启动 burst + 稳态零子进程，但 CC 的启动 burst 产生 ~14 个进程（git 自检 + claude.exe worker）。TM 的纯推理是最简的 1-hop 子树。

### §2.3 进程模型

```
timem-native-rs (Rust runtime, 长寿, 单线程事件循环)
  │
  ├─→ curl (LLM API 调用, 每轮1个, 存活取决于 API 延迟)
  │     └─ 退出 → 解析响应 → 决定下一步
  │
  ├─→ sh → {locale-check, grep×N, cat, sed, ...} (工具执行 burst)
  │     └─ 退出 (全部 < 100ms) → 收集输出 → 下一轮 LLM 调用
  │
  └─→ curl → ... (循环反复直到任务完成)
```

Per-request fork 模型：每次 LLM API 调用 = 1 个 curl 子进程，每次工具调用 = 1 个 sh + N 个命令子进程。子进程是 TimemAi 完成工作的**唯一**方式（无进程内 HTTP、无线程池）。

---

## §3 进程树拓扑

### 核心发现：恒定 2 层深度，单根，无中间基础设施层

所有 7 个 session 的进程树深度 ≤ 2，**有且仅有 1 个 agent 根**（`timem-native-rs`）。

```
timem-native-rs (唯一 agent 根, 深度 0)
  ├── curl (深度 1, LLM API 调用代理)
  └── sh (深度 1, 工具入口进程)
      ├── grep (深度 2, 动作节点)
      ├── locale-check (深度 2, 进程指纹)
      ├── find / cat / ls (深度 2, 文件探索)
      └── ...
```

**结构特征**：
- 树深度永远 2 层，无中间基础设施层（CC 有 3 层：claude → git/bash → tools）
- 无 `git` 子树（TM 不依赖 git 进行版本自检——CC 在启动期 fork 6 个 git）
- 无 worker 线程池进程（CC 的 `claude.exe` 等价物不存在）
- `locale-check` 每次 `sh` 调用都携带——TM 的进程指纹
- 零缺少父进程的孤儿（所有父子关系完整捕获）

---

## §4 进程类型分布

以 T3（26 进程，最具代表性）为例：

| comm | 数量 | 占比 | 角色 |
|------|------|------|------|
| grep | 10 | 38.5% | 文本搜索（ACTION） |
| curl | 6 | 23.1% | LLM API 调用代理（INFRA） |
| locale-check | 4 | 15.4% | 环境检测——进程指纹（ACTION） |
| sed | 3 | 11.5% | 文本替换（ACTION） |
| timem-native-rs | 1 | 3.8% | 根进程（INFRA） |
| sh | 1 | 3.8% | 工具执行器——通道（CHANNEL） |
| cat | 1 | 3.8% | 文件读取（ACTION） |

跨 session 共享的稳定进程骨架：
- `timem-native-rs`（7/7）+ `curl`（7/7）= 最小进程形态
- `grep`（3/7）+ `locale-check`（3/7）+ `sh`（3/7）= 工具执行三元组
- 与 CC 的差异：TM 用 `find/grep/cat` 做文件探索，CC 用 `git`（42.9% in T3）

---

## §5 进程存活时间

### 核心发现：双峰分布 — INFRA 级 curl（万 ms）vs ACTION 级命令（个位 ms）

| Session | 全部 p50 | 全部 p95 | timem 根 (INFRA) | curl p50 (INFRA) | grep p50 (ACTION) |
|---------|---------|---------|-----------------|------------------|-------------------|
| T1 | 5,168ms | 5,168ms | 5,168ms | 5,164ms | — |
| T2 | 2ms | 22,072ms | 54,213ms | ~17,000ms | 2ms |
| T3 | 2ms | 59,172ms | 193,810ms | ~30,000ms | 2ms |
| T4 | 2ms | 4,773ms | 55,574ms | ~28,000ms | 2ms |
| T5a | 32,122ms | 32,122ms | 32,122ms | ~32,000ms | — |
| T5b | 41,580ms | 41,580ms | 41,580ms | ~41,000ms | — |
| T5c | 37,079ms | 37,079ms | 61,330ms | ~36,000ms | — |

按实测存活时间分类（p50 > 1s = INFRA，p50 < 100ms = ACTION）：

| comm | 角色 | 解释 |
|------|------|------|
| timem-native-rs | INFRA | session 全长，Rust runtime |
| curl | INFRA | 等待 DeepSeek API 响应，5-60s |
| sh | ACTION/CHANNEL | 工具执行通道，< 100ms |
| grep | ACTION | 瞬时文本搜索，1-2ms |
| cat/find/sed | ACTION | 瞬时文件操作，1-10ms |
| locale-check | ACTION | 瞬时环境检测，1ms |

INFRA 与 ACTION 之间有 **4 个数量级**的存活时间差距（curl ~10⁴ms vs grep ~10⁰ms），跨 runtime 通用。

---

## §6 进程创建时序：多集群交替爆发

| Session | 进程数 | 集群数 | post_burst_tail_ms | session 全长 |
|---------|--------|--------|-------------------|-------------|
| T1 | 2 | 1 | 0ms | 5.2s |
| T2 | 16 | 5 | 710ms | 54.2s |
| T3 | 26 | 12 | 8,427ms | 193.8s |
| T4 | 11 | 4 | 1,479ms | 55.6s |
| T5a | 2 | 1 | 0ms | 32.1s |
| T5b | 2 | 1 | 0ms | 41.6s |
| T5c | 3 | 2 | 19,685ms | 61.3s |

时序模式：`curl (LLM I/O 等待) → sh burst (瞬时工具执行) → curl → sh burst → ...`。推理阶段（30-50s）→ 行动阶段（< 100ms），推理时间是行动的 300-500 倍。

与 CC 对比：TM 的进程创建贯穿 session 全程（per-request fork），CC 的进程创建集中在前 ~17s（启动 burst + 稳态零子进程）。

---

## §7 进程并发度

| Session | 最大并发 | 总进程数 | 并发峰值时刻 |
|---------|---------|---------|------------|
| T1 | 2 | 2 | timem + curl |
| T2 | 4 | 16 | sh burst（grep/find 并行） |
| T3 | 3 | 26 | sh burst（grep/locale-check 并行） |
| T4 | 3 | 11 | sh burst |
| T5a | 2 | 2 | timem + curl |
| T5b | 2 | 2 | timem + curl |
| T5c | 2 | 3 | timem + curl |

最大并发 4（T2），出现在 sh 工具执行 burst 内。curl 之间严格串行——同一时刻只有 1 个 curl 存活。纯推理 session（T5a/T5b/T5c）并发恒为 2。

---

## §8 退出状态分布

| Session | 已退出 | 非零退出 | 失败率 | 非零退出明细 |
|---------|--------|---------|--------|------------|
| T1 | 2 | 0 | 0.0% | — |
| T2 | 16 | 5 | 31.3% | grep=1 (×4), find=1 (×1) |
| T3 | 26 | 9 | 34.6% | grep=1 (×9) |
| T4 | 11 | 3 | 27.3% | grep=1 (×3) |
| T5a | 2 | 0 | 0.0% | — |
| T5b | 2 | 0 | 0.0% | — |
| T5c | 3 | 0 | 0.0% | — |

全部非零退出均为 `grep=1`（无匹配）或 `find=1`（无结果），属于预期的控制流信号。非零退出率 27-35%（工具密集 session），与 CC 的 ~8% 的差异来自 grep 的大量使用。

---

## §9 父子关系模式

Top 父子对（跨 session）：

| 父子对 | 出现率 | 语义 |
|--------|--------|------|
| timem-native-rs → curl | 7/7 | LLM API 调用 |
| sh → grep | 3/7 | 文本搜索（每次工具调用内） |
| timem-native-rs → sh | 3/7 | 工具执行入口 |
| sh → locale-check | 3/7 | 环境检测（每次工具调用携带） |

**零孤儿进程**：所有 7 个 session 的 `missing_parent_orphan_count` = 0。与 CC 的 `base64×6` 孤儿形成对比——TM 的子进程存活时间更长（curl 5-60s），没有管道中间进程竞态问题。

---

## §10 审计事件分析

| Session | process 事件 | file 事件 | 总计 |
|---------|------------|----------|------|
| T1 | 4 | 1 | 5 |
| T2 | 32 | 5 | 37 |
| T3 | 53 | 9 | 62 |
| T4 | 22 | 3 | 25 |
| T5a | 4 | 1 | 5 |
| T5b | 4 | 1 | 5 |
| T5c | 6 | 1 | 7 |

- process 事件 ≈ 2×进程总数（每进程 exec+exit）
- file write 事件全部由 `timem-native-rs` 执行（写 audit 日志），与 CC 的 `Bun Pool N` 执行形成对比
- 无 llm 类型审计事件（`llm_calls` 表为空）

---

## §11 进程-LLM 时序关联

从进程创建集群推断的 LLM-工具时序（T3 示例）：

| 轮次 | curl 存活 | 后续 sh burst 进程数 | 推断 |
|------|----------|-------------------|------|
| 1 | ~20s | 5 | LLM 返回文件探索指令 |
| 2 | ~25s | 2 | LLM 返回进一步查看源码 |
| 3 | ~30s | 6 | LLM 返回深入分析指令 |
| 4 | ~20s | 1 (sed) | LLM 返回代码修改指令 |
| 5 | ~50s | 0 | LLM 生成最终响应 |
| 6 | ~30s | 0 | LLM 验证/补充 |

推理阶段（curl 存活，5-60s）与行动阶段（sh burst，< 100ms）交替出现。推理时间是行动时间的数百倍。

---

## §12 资源使用

| Session | RSS max (MB) | CPU max (%) | 采样数 |
|---------|-------------|------------|--------|
| T1 | 16 | 0.0 | 3 |
| T2 | 17 | 0.5 | 28 |
| T3 | 17 | 0.5 | 97 |
| T4 | 17 | 0.0 | 28 |
| T5a | 17 | 0.0 | 17 |
| T5b | 17 | 0.5 | 21 |
| T5c | 17 | 0.5 | 31 |

- RSS 恒定 16-17MB（Rust 编译单体二进制），与 CC 的 100→317MB 形成 ~1/18 的数量级差异
- CPU max < 1%，LLM 推理完全外置
- 资源采样仅覆盖 `timem-native-rs` 根进程

---

## §13 协议修复与 v0.5 行为

### §13.1 v0.5 的截断修复

v0.5 将默认 `max_tokens` 从 2048 提升至 20480（本实验设置），大幅减少了响应截断。v0.5 的截断检测覆盖全部三种协议（OpenAI-compatible / OpenAI-Responses / Anthropic），当 `finish_reason=length` 时自动触发一次修复请求。

### §13.2 Chain-of-Thought 泄漏

即使解决了截断问题，DeepSeek v4-pro 在复杂编码任务中仍会在 JSON envelope 前输出 chain-of-thought 文本（如 `_to_user empty, next_actions array with two actions...`）。TimemAi 的 `parse_json_value_from_model_text` 函数能容错处理部分 CoT 泄漏（扫描 `{` 起点 + 平衡括号提取），但当 CoT 文本过长或 JSON 结构不完整时，仍会触发 `repair_issue="invalid_json"`。

### §13.3 supporting-context 缓解

T3、T4、T5a、T5b、T5c 添加了 `--supporting-context` 运行时提示：

> IMPORTANT: Your entire response must be a single valid JSON object starting with {. Do not include any text before the JSON. Do not add markdown code fences.

这**不改变用户 prompt 文本**——`--supporting-context` 以独立的 prompt delta segment 追加到运行时上下文。它在语义上等价于 CC 的 system prompt 中的格式约束。

效果：T3（代码修改）从 v0.4 的协议修复失败变为成功完成；T2/T4/T5a/T5b/T5c 均成功。

### §13.4 协议修复触发记录

| Session | 协议修复触发 | 最终结果 |
|---------|------------|---------|
| T1 | 0 | ✓ |
| T2 | 0 | ✓ |
| T3 | 1（Resp#2 触发了修复，Resp#4 成功） | ✓ |
| T4 | 0 | ✓ |
| T5a | 0 | ✓ |
| T5b | 0 | ✓ |
| T5c | 0 | ✓ |

T3 是唯一触发协议修复的 session——第一次响应因 CoT 泄漏被标记为 `invalid_json`，修复请求后第二次响应成功（包含正确 JSON + 工具调用）。

---

## §14 T5 深度 session 特征

T5a（2 进程）、T5b（2 进程）、T5c（3 进程）是纯推理任务——仅 `timem-native-rs → curl`：

| Session | 进程 | curl | LLM 调用 | 工具调用 |
|---------|------|------|---------|---------|
| T5a | 2 | 1 | 1 | 0 |
| T5b | 2 | 1 | 1 | 0 |
| T5c | 3 | 2 | 2 | 1 (记忆查询) |

与 CC T5 的对比：
- CC T5a-T5c 均有大量进程（37-46），因为 CC 在启动期执行 git 自检 + shell 环境快照
- TM T5a-T5c 仅 2-3 进程——无任何启动开销
- T5b 在两个 runtime 上都产生了长时间纯推理（TM 42s, CC 170s），但 CC 的推理伴随 18 次工具调用（读文件），TM 的推理是单轮 LLM 调用

**任务差异说明**：T5b/T5c 的 CC prompt 引用了前序上下文（"基于你刚对 agentOS 的分析""总结我们讨论的核心设计原则"），而 TM 的 `--once-json` 模式下每个 session 独立运行。这是 `--once-json` 模式的固有差异，在比较报告中详细讨论。

---

## §15 对 agentOS 进程模型的启示

### 回扣三个核心问题

**问题 1：子进程数量/类型/层级。** TimemAi 子进程 2-26 个，**贯穿 session 全程**。进程分类学：INFRA = timem 根（1 个）+ curl（N 个，N = LLM 调用数），ACTION = grep/cat/find/sed/locale-check，CHANNEL = sh。进程树深度恒 2 层、单根。最大并发 4，每轮 sh burst 内并发。

**问题 2：进程↔LLM 时序关联。** 每个 LLM 调用 = 1 个 curl 子进程（存活 5-60s），每个工具调用 = 1 个 sh + N 个命令子进程（存活 < 100ms）。推理与行动交替的认知循环直接映射为进程创建时序。per-request fork 模型天然对 eBPF 观测友好——每个工作单元 = 一个进程事件。

**问题 3：对进程抽象设计的启示。**
- **两种极端模型需要统一抽象**（最重要）→ agentOS 必须同时容纳 per-request fork（TM）和线程池稳态（CC）
- **进程存活时间是跨 runtime 的 INFRA/ACTION 分类信号**→ 1s 阈值在 CC 和 TM 上均有效
- **进程创建频率区分工作模式**→ 持续周期性 burst = per-request fork，集中 burst + 长尾零进程 = 线程池
- **eBPF 观测兼容性不同**→ per-request fork 对 eBPF 友好，线程池模型需要 runtime 配合上报
- **资源效率 vs 观测性的权衡**→ per-request fork：17MB RSS, 2-26 进程；线程池：317MB RSS, 14-46 进程
- R**untime 层的协议验证（protocol repair）是 TM 独有的可靠性机制**→ 对进程行为的影响是额外 LLM 调用（在 T3 中触发 1 次修复），agentOS 应区分"防御性 LLM 调用"和"业务 LLM 调用"

---

## §16 已知限制

1. **llm_calls/tool_calls/token_usage/network_targets 表全空**：AgentSight HTTP 解析器不识别 DeepSeek API 格式
2. **LLM 调用计数以 curl 子进程为代理**：协议修复可能产生额外 curl（T3 中确认 1 次），curl 计数可能略高于业务 LLM 调用
3. **argv/cwd 捕获为空**：无法还原具体 API 请求内容或 bash 命令参数
4. **子进程资源采样缺失**：`resource_samples` 仅含根进程
5. **单 API provider**：仅测试 DeepSeek，其他 provider 的 API 延迟差异可能改变 curl 存活时间分布
6. **非交互模式**：`--once-json` 模式下每个 session 独立，T5b/T5c 的多轮上下文延续不同于 CC 的交互模式
7. **--supporting-context 差异**：T3/T4/T5 使用了 JSON 格式提示（运行时上下文，非用户 prompt），可能与 CC 的 system prompt 中的隐式格式约束不完全等价
8. **单 agent 类型**：仅观测 TimemAi（Rust + DeepSeek），"curl 子进程 HTTP"是 TimemAi 特异的架构选择

---

*Created: 2026-06-25 | Updated: 2026-06-26 (v0.5 re-recording)*
*Based on 7 AgentSight sessions of TimemAi v0.5 (Rust + DeepSeek, non-interactive mode, TIMEM_MAX_LLM_OUTPUT=20480)*
