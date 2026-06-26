# TimemAi vs Claude Code：实验 Prompt 一致性审计

> 审计两个实验（agentos-process-model 和 timemai-process-model）在相同测试任务上使用的 prompt 是否一致，以及差异对对比结论的影响评估。

## 审计范围

- **Claude Code**：7 个 session（T1-T5c），prompt 来自 `audit_events` 表中 `comm='claude'` 的 `exec` 事件的 `full_command` 字段
- **TimemAi**：7 个 session（T1-T5c），prompt 来自 `/tmp/timem-research-*/` 下 `api_audit.jsonl` 中首次 `llm_request` 的 `User question` 字段
- T5a/T5b 的 TimemAi v2（复录成功版本）被用于对比

---

## §1 逐任务 Prompt 对比

### T1 — 简单问答

| | Prompt |
|---|---|
| **CC** | 请用3-5句话解释eBPF是什么，以及它在Linux内核中的作用 |
| **TM** | 请用3-5句话解释eBPF是什么，它和传统网络监控有什么区别 |

**差异**：CC 问"Linux 内核中的作用"，TM 问"与传统网络监控的区别"——不同的知识角度。

**对进程行为的影响**：无。T1 对两个 runtime 都是纯推理任务（0 工具调用）。进程行为（TM: 2 进程，CC: 启动 burst）由 runtime 架构决定，与问题措辞无关。

---

### T2 — 多文件读取

| | Prompt |
|---|---|
| **CC** | 阅读 bpf/ 目录和 collector/src/ 目录，用5句话总结 AgentSight 的整体架构 |
| **TM** | 阅读当前目录下的所有源代码文件，总结项目的整体架构和模块职责 |

**差异**：
- CC 指定了具体子目录（`bpf/`、`collector/src/`），缩小了搜索范围
- TM 要求"当前目录下的所有源代码文件"，范围更广但更模糊
- CC 有明确的 5 句话输出长度约束

**对进程行为的影响**：中等。TM 需要更多 `find`/`grep` 来发现文件（因为没有指定目录路径），这会增加进程数。但文件探索的工具链（find/grep/cat）在两个 runtime 上是相同的——差异在探索的范围而非方法。

---

### T3 — 代码修改

| | Prompt |
|---|---|
| **CC** | 在 collector/src/main.rs 中添加 --version CLI 参数，使其打印版本号后退出。只修改这一个文件，不要运行构建或测试。 |
| **TM** | 在项目主入口文件中添加一个 --version 命令行参数，打印版本号 0.1.0 后退出 |

**差异**：
- CC 指定了确切文件路径（`collector/src/main.rs`），TM 需自行发现（"项目主入口文件"）
- CC 明确禁止构建/测试，TM 无此约束
- TM 指定了具体版本号（`0.1.0`）

**对进程行为的影响**：显著。TM 需要额外的文件探索轮次来定位主入口文件，导致更多的 `curl` 和 `sh` burst（T3 中 TM 有 7 次 curl + 3 次 sh burst，CC 仅 3 次工具调用）。T3 的进程数差异（TM 39 vs CC 14）部分来自探索开销，非纯 runtime 差异。

---

### T4 — 工具密集

| | Prompt |
|---|---|
| **CC** | 扫描项目中所有包含 TODO 或 FIXME 注释的文件，列出位置和内容。然后用 git log --oneline -5 查看最近提交。最后总结项目的技术债务情… |
| **TM** | 扫描当前目录中所有的 TODO/FIXME/HACK 注释，列出文件、行号和内容；然后用 git log 查看最近 5 次提交的作者和主题；最后输出一份代码质量简报 |

**差异**：
- TM 多要求 `HACK` 注释扫描
- TM 要求输出文件**行号**和提交**作者/主题**（更细粒度）
- CC 要求"技术债务总结"，TM 要求"代码质量简报"——措辞不同但语义相近
- CC prompt 在提取时被截断（`技术债务情…`），完整内容不可知

**对进程行为的影响**：低。两者都是 grep + git log 类操作，工具链相同。TM 多扫一个关键字（HACK）和多输出的要求不影响进程模型结构。这是 5 个任务中 prompt 一致性最好的一个。

---

### T5a — agentOS 基础概念

| | Prompt |
|---|---|
| **CC** | 什么是 agentOS？它和传统操作系统（如 Linux）的根本区别是什么？从进程管理、资源调度、隔离模型三个维度分析。 |
| **TM** | 设计一个面向AI agent的操作系统（agentOS），它的核心设计原则是什么？请阅读项目中的AGENTS.md文件了解当前项目架构，然后从进程模型、调度策略、隔离机制三个维度分析 |

**差异**：
- CC 问"agentOS 是什么、与 Linux 的区别"——**定义型问题**
- TM 问"agentOS 的设计原则"——**设计型问题**
- TM 明确要求先阅读文件（`AGENTS.md`），触发了工具调用（阅读+文件探索）
- TM 维度名略有不同（"进程管理"→"进程模型"，"资源调度"→"调度策略"，"隔离模型"→"隔离机制"）

**这是两个本质上不同的问题。** CC 在做一个比较分析，TM 在做一个设计方案。

**对进程行为的影响**：严重。
- TM 的工具调用（1 次 `sh` burst + 15 进程）来自阅读 `AGENTS.md` 的指令，而非来自"分析 agentOS 设计原则"这个任务本身
- CC 的 T5a 是纯推理任务（无工具调用的内在需求），但由于 Claude Code 的引导机制，它仍然触发了文件读取（46 进程, 14 次工具调用，大部分是 session 文件自检）
- 两个 session 的工具调用来自不同驱动力，不可直接对比

---

### T5b — 进程模型深入

| | Prompt |
|---|---|
| **CC** | 基于你刚对 agentOS 的分析，进一步思考：agentOS 的进程模型应该是什么样的？考虑以下方面：(1) agent 的进程分类学——哪些是长生命… |
| **TM** | agent的思考（LLM调用）和行动（工具执行）是两个不同的相位。这对操作系统调度器设计意味着什么？传统Unix的fork+exec进程抽象能否满足agent runtime的需求？如果不能，缺少什么？ |

**差异**：
- CC 从 T5a 延续（"基于你刚对 agentOS 的分析"），聚焦**进程分类学**（taxonomy）
- TM 是独立问题，聚焦**思考/行动相位对调度的影响**以及 **fork+exec 的局限性**
- CC prompt 在提取时被截断（`长生命…`），完整维度列表不可知

**这是两个完全不同的问题。** 唯一共同点是都涉及 agent 进程模型，但切入角度和核心关注点不同。

**对进程行为的影响**：严重。这两个任务的工作量、工具需求、输出性质完全不同。T5b 的定量对比（TM 3 进程 vs CC 37 进程）反映的是任务差异（TM 纯推理 vs CC 多轮工具调用）而非 runtime 差异。

---

### T5c — 架构设计总结

| | Prompt |
|---|---|
| **CC** | 现在从 agentOS 架构设计角度，总结我们讨论的核心设计原则。从以下角度：(1) agent 进程的生命周期管理——何时创建、何时回收；(2… |
| **TM** | 综合前面的分析，给出一个 agentOS 的架构设计概要：核心组件、关键接口、与传统 OS 的差异、以及在 Linux 上的实现路径 |

**差异**：
- CC 从 T5a/T5b 的讨论延续（"总结我们讨论的核心设计原则"），聚焦生命周期管理
- TM 是独立总结，聚焦架构组件和实现路径
- CC prompt 在提取时被截断（`回收；(2…`），完整维度列表不可知

**对进程行为的影响**：中等。两者都是总结性推理任务，大概率不需要大量工具调用。TM 的 T5c 仅 5 进程（4 curl + 1 timem），CC 的 T5c 也以推理为主。但 CC 的"总结讨论"语义可能触发对之前 session 上下文的回顾，导致不同的工具行为。

---

## §2 差异来源分析

### §2.1 为何 T1-T4 prompt 不同

首次实验设计时，T1-T4 的 prompt 被独立撰写而非从 agentos-process-model 复制。两个实验的设计者凭记忆重写了 prompt，保留了任务意图但引入了措辞差异。T3 的差异最显著——CC 版给了具体文件路径（反映 CC 实验是在已知仓库上运行），TM 版没有（反映 TM 实验可能在不同目录执行）。

### §2.2 为何 T5a/T5b prompt 不同

T5a/T5b 的 TM prompt 是协议修复问题的**应急修改**。原始 prompt（v1）触发了 DeepSeek 的 free-form 分析模式，导致 JSON envelope 协议违规并最终失败：

- T5a v1: "从第一性原理出发，分析一个 agentOS…应该具备哪些核心设计原则？请从进程模型、调度策略、内存管理、隔离机制四个维度展开"
- T5b v1: "深入分析 agentOS 的进程模型设计：agent 的思考和行动是两个不同的相位…"

v1 prompt 与 CC prompt 的差异相对较小（都是分析型问题），但因为 DeepSeek 协议合规问题被迫改为 v2。v2 修改方向是让 prompt 更行动导向（触发工具调用而非纯分析），这改变了任务的性质。详见 [§2.3](#§23-t5at5b-v1-失败详情)。

### §2.3 T5a/T5b v1 失败详情

#### §2.3.1 协议修复机制

TimemAi 要求模型输出严格的 JSON envelope 格式（`response_to_user` + `next_actions` + `acceptance_check`）。`agent_core/src/lib.rs` 中的协议验证链路：

```
模型响应
  │
  ├─ parse_json_value_from_model_text()   ← JSON 提取（容错：扫描{起点、平衡括号、
  │   引号修复、markdown 代码块剥离）
  │
  ├─ parse_envelope()                     ← 协议验证（字段存在性、类型、约束）
  │
  ├─ repair_issue == None                 → 正常执行 next_actions
  │
  └─ repair_issue == Some(issue)
       │
       ├─ repair_attempted == false       → 发送修复请求，重新调用模型
       │   "Protocol repair request\nissue: {issue}\n
       │    Return exactly one valid JSON object with response_to_user.
       │    Do not use markdown fences."
       │
       └─ repair_attempted == true        → 阻断，返回兜底消息，turn 终止
```

JSON 提取层（`parse_json_value_from_model_text`）有较强的容错能力——即使模型用 ````json...```` 包裹、在 JSON 前输出 chain-of-thought 文本、或字符串引号有问题，也能尝试恢复。但**协议验证层**（`parse_envelope`）对字段语义有严格要求——缺少 `response_to_user`、`next_actions` 中缺少 `action`/`intent`、`acceptance_check.is_satisfied` 缺失等，都会触发 `repair_issue`。

T5a/T5b v1 的失败都发生在 JSON 提取层——模型根本没有输出 JSON，而是输出了 free-form 中文分析文本。

#### §2.3.2 T5a v1 失败逐轮分析

**Prompt（507 chars）：**
> 从第一性原理出发，分析一个 agentOS（面向AI agent的操作系统）应该具备哪些核心设计原则？请从进程模型、调度策略、内存管理、隔离机制四个维度展开

**第 1 轮（初始调用）：**

| 属性 | 值 |
|------|-----|
| `finish_reason` | `length`（命中 max_tokens=2048，响应被截断） |
| `content` 长度 | 3,589 字符 |
| JSON 合法性 | **不合法**——非 JSON 文本 |
| 实际输出 | 中文分析文本："第一性原理思考：什么是agentOS？它是在AI agent时代，面向智能体的操作系统，管理agent的计算、记忆、调度、通信等。类比传统OS，但agent是软件实体…" |
| `parse_envelope` 结果 | `repair_issue = "invalid_json"` |

模型进入 free-form 分析模式，完全忽略 JSON envelope 格式要求。响应在 3,589 字符处被 max_tokens 截断（从内容看，模型正在展开"内存管理"维度时被截断）。

**第 2 轮（协议修复请求）：**

| 属性 | 值 |
|------|-----|
| 修复 prompt | `"Protocol repair request\nissue: invalid_json\nReturn exactly one valid JSON object with response_to_user. Do not use markdown fences."` |
| `finish_reason` | `length`（再次命中 max_tokens=2048） |
| `content` 长度 | 1,291 字符 |
| JSON 合法性 | **不合法**——部分 JSON 但残缺 |
| 实际输出 | 以 `{` 开头，包含 `"thought"` 和 `"response_to_user"` 字段的前半部分，但在展开分析时被截断：`"…agent 的'内存'是分层异构的，包含上下文窗口（工作记忆）、长期记忆（向量库/知识图谱）、外部工具结果。设计原则：- **分层记忆架构**…"` |

模型这次尝试输出 JSON，但 max_tokens 仍然不够——JSON 在中间被截断，导致 `parse_json_value_from_model_text` 无法提取完整对象（可能缺少闭合 `}`）。`repair_attempted` 已为 `true`，触发兜底阻断。

**结果**：turn 终止，返回 `"模型的回复不符合本地协议，已拦截原始报文展示。请重试或换一个更具体的问题。"`。2 次 LLM 调用（1 正常 + 1 修复），无工具调用，session 无有效产出。

#### §2.3.3 T5b v1 失败逐轮分析

**Prompt（545 chars）：**
> 深入分析 agentOS 的进程模型设计：agent 的思考和行动是两个不同的相位，这对调度器设计意味着什么？现有操作系统的进程抽象（fork+exec）是否能满足 agent runtime 的需求？如果不能，需要什么样的新原语？

**第 1 轮（初始调用）：**

| 属性 | 值 |
|------|-----|
| `finish_reason` | `length`（命中 max_tokens=2048） |
| `content` 长度 | 2,275 字符 |
| JSON 合法性 | **不合法**——以 `{` 开头但截断 |
| 实际输出 | 包含 `"thought"` 字段的 JSON 前半部分，在展开分析时被截断：`"…传统fork+exec生成独立进程，地址空间隔离重，创建销毁开销大，且难以共享内存和持久化会话状态。agent runtime需要轻量级隔离、状态可挂起/恢复、高效消息传递、持久化上下文和续延能力。因此需要新原语，如：基于协程/绿色线程的轻量级执行上下文…"` |

与 T5a v1 不同，T5b v1 的模型确实尝试输出 JSON（以 `{` 开始，包含 `"thought"` 字段），但仍然在分析展开时命中 max_tokens 截断。

**第 2 轮（协议修复请求）：**

| 属性 | 值 |
|------|-----|
| 修复 prompt | `"Protocol repair request\nissue: invalid_json\nReturn exactly one valid JSON object with response_to_user. Do not use markdown fences."` |
| `finish_reason` | `length`（第三次命中 max_tokens） |
| `content` 长度 | 352 字符 |
| JSON 合法性 | **不合法**——截断 |
| 实际输出 | 包含 `"thought"` 和 `"response_to_user"` 的 JSON 片段，在 `"response_to_user"` 字段值中间截断：`"…因此需要新原语，如：基于协程/绿色线程的轻量级执行上下文、可挂起的轻量进程…需要结合memory/state管理。"` |

**结果**：同 T5a v1——2 次 LLM 调用后阻断，session 无有效产出。

#### §2.3.4 根因分析

T5a v1 和 T5b v1 的失败是**三个因素叠加**的结果：

**因素 1：DeepSeek v4-pro 的分析模式漂移。** 当 prompt 是开放式分析问题（"从第一性原理出发，分析…""深入分析…"），DeepSeek 倾向于进入 free-form 分析模式，输出结构化的中文分析段落而非 JSON envelope。对于 T5a v1，模型在第一轮完全忽略了 JSON 格式要求；对于 T5b v1，模型尝试了 JSON 但仍在其中嵌入了长篇分析。

**因素 2：max_tokens=2048 不足。** agentOS 设计原则这类深度分析问题，模型的自然输出长度远超 2,048 tokens。两个 session 的 4 次模型调用全部以 `finish_reason=length`（截断）结束。即使模型愿意遵守 JSON 格式，2048 tokens 的预算也不够它在 JSON 结构内完成四个维度的分析。截断后的 JSON 不可解析，触发协议修复，修复请求的响应同样被截断，形成死循环。

**因素 3：协议修复对模式漂移无效。** 修复 prompt（"Return exactly one valid JSON object…Do not use markdown fences."）是一个格式指令，但它无法把模型从"分析模式"切换到"JSON 输出模式"。模型在分析模式下收到格式指令后，要么继续输出分析文本（T5a），要么在 JSON 框架内继续长篇分析然后被截断（T5b）。修复机制能处理偶然的 JSON 格式错误（缺字段、markdown 包裹），但无法纠正根本性的模式违规。

```
死循环：
  prompt 触发分析模式 → 模型输出分析文本 → 超 max_tokens 截断
  → JSON 解析失败 → 修复请求 → 模型仍在分析模式
  → 再次超 max_tokens 截断 → 阻断
```

#### §2.3.5 v1→v2 修改的策略逻辑

v2 prompt 的修改不是随机的——每一项修改都针对上述根因：

| 修改 | 针对的根因 | 机制 |
|------|-----------|------|
| "从第一性原理出发，分析" → "设计…它的核心设计原则是什么？" | 因素1（分析模式漂移） | 将动词从"分析"改为"设计"，将 framing 从"第一性原理"改为具体问题——避免触发 free-form 分析模式 |
| 4 维度 → 3 维度（删除"内存管理"） | 因素2（max_tokens 不足） | 减少需要展开的维度数，降低模型的自然输出长度 |
| 加入"请阅读项目中的AGENTS.md文件" | 因素1（分析模式漂移） | 给出具体行动锚点——模型进入"执行工具调用"模式而非"分析输出"模式；一旦进入 JSON action 循环，就保持在协议内 |
| "深入分析 agentOS 的进程模型设计" → "agent的思考和行动是两个不同的相位…传统Unix的fork+exec能否满足" | 因素1（分析模式漂移） | 移除"深入分析"触发词，将抽象的"进程模型设计"替换为具体的"两个相位→调度含义→fork+exec 局限性"链式问题 |
| "agentOS"上下文 → 独立技术问题 | 因素1（分析模式漂移） | 去掉"agentOS"系统设计语境（避免触发系统设计推测），把问题定位为具体的技术分析 |

核心策略：**从"触发分析模式"转向"触发行动模式"**——让模型的第一步是工具调用（读文件）而非生成分析文本，从而在 JSON envelope 协议内启动认知循环。

#### §2.3.6 对比：T3 也触发了协议修复，但存活了

T3（7 次 LLM 调用）也频繁触发协议修复（7 次响应中仅 2 次是干净 JSON），但 session 成功完成了任务。原因：

- T3 的 prompt 是**行动导向**的（"添加 --version 参数"），模型从第一轮就在 JSON action 模式中
- 协议修复在 T3 中处理的是**格式问题**（markdown 包裹、chain-of-thought 泄漏、截断），而非**模式问题**（free-form 分析）
- JSON 提取层的容错能力（扫描 `{`、平衡括号提取）能恢复这些格式问题——即使 JSON 被 markdown 包裹或在 CoT 文本之后，只要有关键的 `{...}` 结构就能提取
- T3 中 3 次 `finish=length` 截断被后续轮次自然恢复（模型在下一轮继续工作）

T3 的存活说明协议修复机制的**设计意图**是正确的——处理偶然格式偏差。T5a/T5b v1 的失败说明它的**设计边界**——无法处理根本性的模式违规。

### §2.4 CC prompt 截断问题

T4、T5b、T5c 的 CC prompt 在 AgentSight 的 `audit_events.full_command` 中被截断（字段长度限制）。这意味着 CC 实际执行的任务完整描述不可知——缺失的部分可能包含额外的约束或指令，进一步影响可比性。

---

## §3 对对比结论的影响矩阵

| 对比维度 | T1 | T2 | T3 | T4 | T5a | T5b | T5c |
|---------|----|----|----|----|-----|-----|-----|
| **进程模型结构**（fork vs 线程池） | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **进程树深度** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **HTTP 实现方式**（curl vs 进程内） | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **进程存活时间双峰分布** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **资源占用**（RSS/CPU） | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **进程创建时序模式** | ✓ | ✓ | ⚠ | ✓ | ⚠ | ✗ | ⚠ |
| **进程数量** | ✓ | ⚠ | ✗ | ⚠ | ✗ | ✗ | ⚠ |
| **LLM 调用数** | ✓ | ⚠ | ✗ | ⚠ | ✗ | ✗ | ⚠ |
| **工具调用数** | ✓ | ⚠ | ✗ | ⚠ | ✗ | ✗ | ⚠ |
| **session 时长** | ✓ | ⚠ | ✗ | ⚠ | ✗ | ✗ | ⚠ |

> ✓ = prompt-independent（runtime 结构属性，可直接对比）
> ⚠ = 部分受 prompt 影响（可定性对比，定量值有偏差）
> ✗ = 严重受 prompt 影响（定量对比不可靠，应从对比中剔除或降级）

---

## §4 对 report-comparison.md 的建议

### §4.1 应添加方法学警告

比较报告开头应增加一节"方法学说明"，披露以下事实：

1. T1-T4 prompt 存在措辞差异（独立撰写而非复制），T3/T5a/T5b 的差异显著
2. T5a/T5b 的 TM prompt 因协议修复问题被应急修改，导致任务性质改变
3. CC 的 T4/T5b/T5c prompt 在 AgentSight 数据中存在截断
4. 本报告的**结构性结论**（fork vs 线程池、进程树形态、资源占用模式）不受 prompt 差异影响
5. 本报告的**定量结论**（进程数、调用数、时长的绝对值对比）受 prompt 差异影响，应视为参考值而非精确对比

### §4.2 应调整的具体内容

| 当前报告位置 | 问题 | 建议 |
|------------|------|------|
| 进程树可视化对比（T3） | TM 39 vs CC 14 的绝对值对比受探索开销影响 | 加注：TM 需自行发现文件路径，额外探索导致进程数偏高 |
| §2 进程创建时序 | T3 的 1 vs 7 集群对比部分来自探索轮次 | 加注：集群数差异反映任务探索需求+进程模型差异的叠加 |
| §5 资源与并发 | T3 并发 5 vs 7 的对比 | 加注：TM 并发峰值出现在文件探索阶段，task-dependent |
| §6 对比假设验证 | "TM 进程数与 LLM 调用正相关"的确认来自包含探索开销的数据 | 同样成立但定量系数受污染 |
| §7 协议修复 | T5a 的协议修复触发讨论 | 补充说明：T5a prompt 修改本身影响了 session 行为 |

---

## §5 后续改进建议

1. **重新录制 T3/T5a/T5b**：使用与 CC 完全相同的 prompt，确保定量对比的可靠性
2. **修复 AgentSight full_command 截断**：增大 `audit_events.details_json` 中 `full_command` 的存储上限，避免 CC prompt 截断问题
3. **建立 prompt 版本管理**：将每个任务的 prompt 写入 `README.md` 或独立的 `prompts.json`，作为实验协议的一部分版本化管理
4. **跨 runtime prompt 验证步骤**：在实验工作流中增加一步——录制前先在两个 runtime 上用相同 prompt 做一次快速验证（确认都能成功执行）

---

*Created: 2026-06-26*
*Based on audit log analysis of 14 sessions (7 CC + 7 TM)*
