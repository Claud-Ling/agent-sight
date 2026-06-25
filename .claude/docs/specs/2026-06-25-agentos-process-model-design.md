# AgentSight 服务于 agentOS 进程模型研究 — 设计文档

## 背景与目标

### 背景

用户是操作系统架构师，正在研究 agentOS——面向 AI agent 的操作系统。agentOS 的核心问题之一是：**agent 运行时应该有什么样的进程模型？** 这包括进程拓扑、生命周期管理、父子关系模式、以及 agent 行为（LLM 调用、工具调用）与进程行为的关联。

AgentSight 是基于 eBPF 的 AI agent 观测框架，能无侵入地捕获 agent 的进程生命周期、SSL/TLS 流量、stdio 输出，并还原为结构化的 LLM 调用、审计事件和进程树。本项目的研究目标不是修改 AgentSight，而是**用它已有的观测能力服务于 agentOS 的进程模型设计**。

### 目标

通过观测 Claude Code（首要目标，后续扩展至自定义 agent）的运行时进程行为，建立对 agent 进程模型的系统性认知。具体要回答：

1. Claude Code 运行时产生多少子进程、什么类型、什么层级关系？
2. 子进程的创建/销毁与 LLM 调用/工具调用之间存在什么时序关联？
3. 进程存活时间分布和父子关系模式对 agentOS 的进程抽象设计有何启示？

这三个问题的递进逻辑是 **结构→行为→设计**，每个对应 agentOS 进程模型的一个核心设计决策：

**问题 1 的价值：决定 agentOS 的进程分类学。** 如果你要设计一个 agentOS，第一件事是定义"agent 进程到底是什么"。今天 Unix 只有一种进程抽象（fork+exec），但 agent 运行时产生的进程显然不止一种。观测结果直接回答：(a) agent 子进程可以分成几类——至少预期有 agent 基础设施进程（长期常驻，如 Node.js runtime、HTTP 连接池线程）和 agent 动作进程（短命、按需创建，如 bash/git/python），agentOS 是否需要为不同类别提供不同的调度策略和资源配额？(b) 层级深度是否有上界——agent 可以递归调用自己（agent spawn agent）吗？如果有，隔离模型需要改变。(c) 隔离粒度——agent 的每个"动作"对应一棵进程子树，沙箱边界画在子树根还是每个叶子上？

**问题 2 的价值：决定 agentOS 的调度模型。** 这是 agentOS 区别于传统 OS 最关键的地方——agent 的"思考"和"行动"是两个相位，而传统 OS 调度器完全不知道这个区别。时序关联回答：(a) LLM 调用（认知）和工具子进程（行动）是否有清晰的时序分离？如果有，agentOS 可以在 LLM 调用期间让 CPU 闲置，在返回前预暖进程池。(b) 进程生命周期是"创建→用完即毁"还是"创建→复用多次"？前者需要极轻量的进程创建原语；后者需要进程池和会话粘性。(c) LLM 调用间隔期的 idle 行为——进程回收还是驻留？这决定了 agentOS 应该用"请求驱动"还是"会话驱动"的进程模型。

**问题 3 的价值：决定是否需要新的 OS 原语。** 这是合成步骤，把观测翻译成设计。具体：(a) 存活时间分布决定进程创建的开销容忍度——如果 p50 存活时间 < 100ms（`git status` 级），传统 fork+exec 的开销（mmap 重建、页表拷贝）占比过高，agentOS 需要类似 `clone(CLONE_VFORK)` 或 Solaris 轻量进程的语义。(b) 父子关系模式揭示"意图链"——`claude → bash → git` 的 3 跳中，bash 只是通道节点，对 agent 意图无贡献。agentOS 是否应该在进程树中区分"意图节点"和"通道节点"，在调度和记账时跳过通道节点？(c) 综合判断 agent 进程模型更像 Web 服务器的请求-响应模型（per-request fork，短命，独立）还是数据库的连接池模型（长命 worker，复用，有状态）——这决定了 agentOS 进程抽象的根本取向。

### 成功标准

- 产出 3-5 个不同性质 Claude Code 任务的 session 录制数据
- 每个 session 能提取结构化的进程树拓扑和类型分布
- 形成一份观察报告，描述 Claude Code 的进程模型特征，并提出对 agentOS 进程抽象设计的初步启示

## 范围

### 范围内

- Claude Code 进程树拓扑分析（pid/ppid 关系、根进程识别、树深度）
- 子进程类型分类（bash、git、node、python 等，按 comm 和 argv 分类）
- 进程创建/销毁时序分析（存活时间分布、exec 频率）
- 进程行为与 LLM 调用/工具调用的时序关联（时间窗口模糊匹配）
- 利用 `agentsight record` 完成数据采集，Python 脚本通过 sqlite3 直接读取 session.db 进行分析
- 分析脚本从 SQLite 中提取进程特征——不经过 export JSON（Snapshot 结构体不含 `llm_calls` 字段）

### 非范围（本轮）

- 多 agent 横向对比（Claude Code vs Gemini CLI vs Codex）
- 长时间稳定性测试（方案 B，后续）
- 受控实验矩阵与定量统计（方案 C，后续）
- 自定义 agent 的观测（后续）
- CPU/内存资源消耗精细分析
- 修改 AgentSight 源码（本仓库需与 upstream 保持同步，长期维护）
- 线程（worker_threads）层面分析——AgentSight 在 eBPF 层过滤了线程事件
- 修改 AgentSight 的 Snapshot 结构体或数据摄入逻辑

## 方案比较

### 方案 A：单任务快照对比

- 核心思路：选 3-5 类代表性的 Claude Code 任务（简单问答、多文件读取、多文件重构、工具密集操作），每个任务用 `claude -p "<prompt>"` 执行并用 `agentsight record -- claude -p "<prompt>"` 录制独立 session，直接查询 SQLite 横向对比进程特征
- 优点：隔离干净，归因清晰；不需要长时间运行；快速产出第一手认知
- 缺点：只能看到单次任务的进程快照，看不到长时间演化（idle 行为、进程泄漏等）
- 适用条件：研究初期，建立基本认知

### 方案 B：长时间持续观测

- 核心思路：`debug trace --process --system --server` 持续运行一个真实开发 session，用 Web UI 观察实时进程树，关注进程长期演化规律
- 优点：看到真实全貌（常驻 vs 短命进程、idle 行为）
- 缺点：数据量大，变量控制弱，归因困难
- 适用条件：方案 A 之后，验证长尾行为

### 方案 C：受控实验矩阵

- 核心思路：设计标准化工作负载矩阵，每个负载跑 N 次，做统计分析和形式化描述
- 优点：变量可控，结论可复现，定量证据
- 缺点：前期投入大，真实复杂性可能被简化
- 适用条件：研究深入阶段

### 推荐方案

**递进式 A → B → C。** 本轮只执行方案 A，用 3-5 个代表性任务快速建立 Claude Code 进程模型的直觉。B 和 C 的启动时机取决于 A 的发现——如果快照对比已经足够回答核心问题，就不需要进到 B/C；如果发现意料之外的长尾行为或需要定量确认，再启动后续方案。

## 关键边界与组件职责

### 数据采集层

- **工具**：`sudo ./agentsight record -- claude -p "<prompt>"` — 录制完整 session
- **输出**：SQLite session 文件 + 嵌入式 eBPF 事件流
- **职责**：忠实记录 Claude Code 运行时的进程 exec/exit、SSL 流量、文件/网络事件
- **注意事项**：
  - Claude Code 需要 `--binary-path`（BoringSSL 静态链接，已通过 `record -- claude` 自动解析）
  - 带 `--binary-path` 时 sslsniff 的 `--comm` 过滤被跳过（SSL 流量跑在 "HTTP Client" 线程而非 "claude" 线程）

### 数据导出层

- **主路径**：Python `sqlite3` 直接读取 session.db。原因：`Snapshot` 结构体（`collector/src/model.rs:72-84`）不含 `llm_calls` 字段，export JSON 无法提供 LLM 调用时序数据，时序关联分析无法进行。SQLite 中 `llm_calls` 表包含 `start_timestamp_ms`/`end_timestamp_ms`，是时序关联的必要数据源。
- **SQLite 表结构**：`llm_calls`, `token_usages`, `audit_events`, `process_nodes`, `resource_samples`, `network_targets`, `tool_calls`（7 张表，定义于 `collector/src/sinks/sqlite.rs` SCHEMA 常量）
- **交互式 session 的补充数据**：对于 T5 多轮对话，可利用 `@agent-session` 目录的 agent-native session 数据（`~/.claude/` JSONL），它不需要 eBPF 权限即可读取，包含 prompt 文本和模型信息

### 分析层

- **进程树重建**：从 process_nodes 的 pid/ppid 构建邻接表，识别根进程（ppid 不在集合中），DFS 重建树形结构
- **进程类型分类**：按 comm 和 argv[0] 分类，预定义分类清单见附件
- **时序关联**：按时间窗口（LLM call 的 start/end timestamp）关联同窗口内的进程创建/退出事件
- **输出**：每个 session 的进程树拓扑图 + 类型分布统计 + 存活时间分布 + LLM-进程关联矩阵

### 分析脚本

- **工具**：Python 脚本（sqlite3 直连 session.db）
- **路径**：研究产物放在项目根 `research/` 目录（不在 `.claude/` 下，因为那是记忆系统目录）
- **职责**：可复现的分析管线，输入 session 文件，输出结构化分析结果

## 数据流

```
Claude Code 任务
    │
    ▼
sudo ./agentsight record -- claude -p "<prompt>"
    │  [eBPF: process.bpf.c + sslsniff.bpf.c + stdiocap.bpf.c]
    │  [Rust: MaterializingAnalyzer → SQLite ViewSink]
    ▼
./agentsight-{YYYYMMDD-HHMMSS}.db
    │  （生成于当前工作目录）
    │
    ▼
Python 分析脚本（sqlite3 直连）
    │  查询 llm_calls / process_nodes / audit_events / tool_calls 表
    │
    ▼
进程树拓扑 + 类型分布 + 存活时间 + 时序关联矩阵
```

## AgentSight 已有数据能力（利用率评估）

| 维度 | 行类型 | 本轮利用 | 说明 |
|------|--------|---------|------|
| 进程节点 | ProcessNodeRow | **主要** | pid/ppid/comm/command/argv/cwd/start_end_timestamps/exit_code |
| 审计事件 | AuditEventRow | **主要** | process exec/exit、file access、network events；含 target 字段可识别可执行体路径 |
| LLM 调用 | LlmCallRow | **辅助** | provider/model/request/response/timestamps；与进程事件的时序关联 |
| 会话 | SessionRow | **辅助** | agent_type/model/tokens；提供 session 级上下文 |
| 工具调用 | ToolCallRow | **辅助** | tool_name/input/output/duration；关联进程行为与 agent 决策 |
| 网络目标 | NetworkTargetRow | 本轮不用 | host/path 级别聚合，与进程模型直接关联弱 |
| 资源采样 | ResourceSampleRow | 本轮不用 | CPU/RSS 精细分析留到后续 |
| Token 用量 | TokenUsageRow | 本轮不用 | token 细分与进程模型直接关联弱 |

## 已知限制与工作假设

### 数据缺口

| 缺口 | 影响 | 应对 |
|------|------|------|
| `root_pid` 硬编码为 None | 进程树的根节点需要手工重建 | 在分析脚本中通过 pid/ppid 邻接表 + DFS 回溯识别根进程 |
| bash_readline 文本未被 Rust 摄入 | 无法从 SQLite 获取 bash 实际执行的命令内容 | 后续可检查 raw JSON 事件流是否包含；本轮通过 argv 推断命令 |
| fork/信号/进程组变更事件不可用 | 无法分析进程间信号交互、fork 速率和进程组编排 | eBPF 侧默认 `trace_signals=false`（不产出），Rust 侧也未启用 `--trace-signals`。后续如需此数据需同时修改 eBPF 和 Rust 两侧 |
| 线程（worker_threads）不可见 | Node.js worker_threads 生命周期完全盲区 | 设计文档记录；agentOS 的线程 vs 进程抽象决策需要额外数据源 |
| 进程↔LLM 调用无直接关联键 | 只能按时间戳模糊匹配 | 在分析脚本中使用时间窗口关联（LLM 调用 ±Δt 内的进程事件） |

### 工作假设

1. Claude Code 主进程识别需要多重启发式：`record -- claude` 通过 `/bin/sh -c 'kill -STOP $$; exec "$target"...'` 启动，进程树中会有一个 sh 壳层，需要跳过它定位真正的 Claude Code 进程。使用最小 pid + comm 匹配 + 父进程不在集合内的组合策略
2. 子进程的 ppid 可追溯到 Claude Code 主进程或中间进程（bash 等），trace_children 默认跟随子进程
3. 进程创建/销毁事件与 LLM 调用的时序关联在 ±1s 窗口内有意义
4. Claude Code 在不同任务类型下呈现相似的基础进程骨架（长期进程）和差异化的子进程模式（短期进程）

## 实验设计：方案 A 任务矩阵

### 任务分类

| 编号 | 任务类型 | 示例 prompt | 预期进程特征 |
|------|---------|------------|------------|
| T1 | 简单问答 | "什么是 eBPF？" | 最简进程树：主进程 + 单次 LLM 调用，极少的子进程 |
| T2 | 多文件读取 | "阅读 bpf/ 和 collector/src/ 目录，总结架构" | 多次文件访问，可能有 git ls-files 等子进程 |
| T3 | 代码修改 | "在 collector/src/main.rs 中添加 --version 参数" | 文件的读写操作、可能触发 build/test |
| T4 | 工具密集 | "检查项目中的 TODO 注释并创建 issue" | 多次 git 操作、可能涉及 gh CLI、文件扫描 |
| T5 | 多轮对话 | 交互式 session 连续 5 轮问答，逐步深入的架构讨论 | 进程跨轮复用模式、idle 行为。**注意**：`claude -p` 是单次问答模式，T5 需用 `record -c claude` 附着到已运行的交互式 Claude Code 进程，并用 `@agent-session` 目录的 agent-native session 数据作为 prompt 上下文补充 |

### 对比维度

每类任务提取以下维度：

- 进程树拓扑：根进程、树深度分布（各深度层级的进程数）、总进程数
- 进程类型分布：bash / git / node / python / 其他 的频次和占比
- 进程存活时间：min/max/mean/p50/p95（区分有退出事件的短期进程和未退出的长期进程）
- 进程退出状态分布：exit_code=0（成功） vs exit_code≠0（失败） vs 未退出，按进程类型分组
- 进程创建节奏：在 LLM 调用时间窗口内的进程 exec 密度
- 父子关系模式：常见的父→子进程对（如 claude → bash → git）

## 错误处理与回退

- 如果 `agentsight record -- claude` 无法捕获 SSL 流量（binary auto-discovery 失败）→ 回退到手动指定 `--binary-path`
- 如果 session 中 process_nodes 为空（eBPF process hook 加载失败）→ 检查内核版本和 CAP_BPF 权限
- 如果 session.db 过大导致 SQLite 查询过慢 → 为关键表（process_nodes、audit_events、llm_calls）创建时间索引
- 如果进程树重建中根进程识别错误 → 交叉验证 ppid 路径和进程 start_timestamp

## 测试策略

- 每个任务类型至少录制 1 个有效 session（共 5 个）
- 分析脚本在至少 2 个 session 上验证进程树重建逻辑
- 手工抽查 2 个 session 中的 5-10 个进程节点，确认 pid/ppid/comm/argv/cwd 字段完整性

## 产出物清单

| 产出 | 格式 | 说明 |
|------|------|------|
| Session 数据 | SQLite (.db) × 5 | 5 类任务的原始录制数据（生成于当前工作目录） |
| 分析脚本 | Python (.py) | sqlite3 直连，含进程树重建、类型分类、时序关联、统计汇总 |
| 进程树可视化 | ASCII art / Graphviz (.dot) | 每个 session 的 `pstree` 风格文本图 + 类型着色版 |
| 观察报告 | Markdown (.md) | 整合 5 个 session 的分析结果，提出对 agentOS 进程模型的启示 |
| 脚本与报告存放 | `research/agentos-process-model/` | 项目根下新建目录 |

## 未决事项

1. **root_pid 行为**：ProcessNodeRow.root_pid 被硬编码为 None，需要通过实验确认实际数据，后续设计进程树重建算法时调整
2. **bash_readline 数据可用性**：虽有 bash_readline uretprobe 但 eBPF 事件在 Rust 侧被标记为 Unknown 后丢弃（`canonical.rs` 不匹配 BASH_READLINE），`record` 模式下这些事件不可见。`debug process` 模式可看到 raw JSON，但 record session 中无法获取
3. **实验性 agent 的观测需求**：自定义 agent 上线后，是否需要为它单独设计观测策略，还是沿用 Claude Code 的采集参数
4. **fork/信号数据的获取策略**：当前 eBPF 侧 `trace_signals=false`（默认不产出）且 Rust 侧未传递 `--trace-signals`。如需这些数据，需要修改 AgentSight 源码（与本轮"不修改源码"约束冲突）。可选方案：单独用 `debug process --trace-signals` 运行 process 二进制获取，但会脱离 record 流程
5. **`claude -p` 行为验证**：需要在目标环境确认 Claude Code 单次非交互式执行的实际 CLI 参数（`-p` / `--print`）。不同版本可能有差异
6. **T5 多轮对话录制方案**：`record -- claude -p` 只支持单次执行后退出。交互式多轮有如下替代方案：(a) `sudo ./agentsight record -c claude` 附着到已运行进程 → 手动执行多轮 → 停止录制；(b) 利用 `agent-session/` 目录的 agent-native session 数据作为 T5 的 prompt 上下文补充
7. **进程树根节点识别的鲁棒性**：`record -- claude` 通过 `/bin/sh -c 'kill -STOP $$; exec ...'` 启动，进程树中多了一层 sh。分析脚本需要多重启发式策略跳过这个壳层。同时 psppid 路径经过 sh 中转可能导致误判父子关系
8. **audit_events 截断风险**：工具密集型任务（T4）的 audit_events 可能超过 SQLite ViewSink 的默认上限（10000 条），需在实验中确认是否存在截断

---

*Created: 2026-06-25 | Status: 待审批*
