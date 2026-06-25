# agentOS 进程模型研究

使用 AgentSight 观测 Claude Code 运行时进程行为，服务于 agentOS 进程抽象设计。

## 核心问题

1. Claude Code 运行时产生多少子进程、什么类型、什么层级关系？
2. 子进程的创建/销毁与 LLM 调用/工具调用之间存在什么时序关联？
3. 进程存活时间分布和父子关系模式对 agentOS 的进程抽象设计有何启示？

这三个问题的递进逻辑是 **结构→行为→设计**，每个对应 agentOS 进程模型的一个核心设计决策：

**问题 1 的价值：决定 agentOS 的进程分类学。** 如果你要设计一个 agentOS，第一件事是定义"agent 进程到底是什么"。今天 Unix 只有一种进程抽象（fork+exec），但 agent 运行时产生的进程显然不止一种。观测结果直接回答：(a) agent 子进程可以分成几类——至少预期有 agent 基础设施进程（长期常驻，如 Node.js runtime、HTTP 连接池线程）和 agent 动作进程（短命、按需创建，如 bash/git/python），agentOS 是否需要为不同类别提供不同的调度策略和资源配额？(b) 层级深度是否有上界——agent 可以递归调用自己（agent spawn agent）吗？如果有，隔离模型需要改变。(c) 隔离粒度——agent 的每个"动作"对应一棵进程子树，沙箱边界画在子树根还是每个叶子上？

**问题 2 的价值：决定 agentOS 的调度模型。** 这是 agentOS 区别于传统 OS 最关键的地方——agent 的"思考"和"行动"是两个相位，而传统 OS 调度器完全不知道这个区别。时序关联回答：(a) LLM 调用（认知）和工具子进程（行动）是否有清晰的时序分离？如果有，agentOS 可以在 LLM 调用期间让 CPU 闲置，在返回前预暖进程池。(b) 进程生命周期是"创建→用完即毁"还是"创建→复用多次"？前者需要极轻量的进程创建原语；后者需要进程池和会话粘性。(c) LLM 调用间隔期的 idle 行为——进程回收还是驻留？这决定了 agentOS 应该用"请求驱动"还是"会话驱动"的进程模型。

**问题 3 的价值：决定是否需要新的 OS 原语。** 这是合成步骤，把观测翻译成设计。具体：(a) 存活时间分布决定进程创建的开销容忍度——如果 p50 存活时间 < 100ms（`git status` 级），传统 fork+exec 的开销（mmap 重建、页表拷贝）占比过高，agentOS 需要类似 `clone(CLONE_VFORK)` 或 Solaris 轻量进程的语义。(b) 父子关系模式揭示"意图链"——`claude → bash → git` 的 3 跳中，bash 只是通道节点，对 agent 意图无贡献。agentOS 是否应该在进程树中区分"意图节点"和"通道节点"，在调度和记账时跳过通道节点？(c) 综合判断 agent 进程模型更像 Web 服务器的请求-响应模型（per-request fork，短命，独立）还是数据库的连接池模型（长命 worker，复用，有状态）——这决定了 agentOS 进程抽象的根本取向。

## 成功标准

- 产出 3-5 个不同性质 Claude Code 任务的 session 录制数据
- 每个 session 能提取结构化的进程树拓扑和类型分布
- 形成一份观察报告，描述 Claude Code 的进程模型特征，并提出对 agentOS 进程抽象设计的初步启示

## 环境要求

- Linux（eBPF 需要）
- sudo 或 CAP_BPF + CAP_SYS_ADMIN
- Claude Code CLI（`claude -p` 非交互模式可用）
- AgentSight 已构建（`./collector/target/release/agentsight`）
- Python 3.6+（标准库 sqlite3 + json）
- Rust 工具链（AgentSight 构建依赖）

## 实验任务

| 编号 | 任务类型 | 描述 |
|------|---------|------|
| T1 | 简单问答 | 单一知识问题，最简进程树基线 |
| T2 | 多文件读取 | 阅读多个目录并总结，文件探索型 |
| T3 | 代码修改 | 修改单个源文件，产出型 |
| T4 | 工具密集 | 多次 git/文件扫描/tool 调用 |
| T5 | 多轮交互 | 交互式 5 轮 agentOS 架构讨论 |

## 运行步骤

```bash
# 1. 构建 AgentSight
make build

# 2. 录制 session（以 T1 为例）
sudo ./collector/target/release/agentsight record -- claude -p "请用3-5句话解释eBPF是什么"

# 3. 运行分析
python3 research/agentos-process-model/scripts/analyze.py sessions/T1-simple-qa.db

# 4. 生成可视化
python3 research/agentos-process-model/scripts/process_tree.py sessions/T1-simple-qa.db T1 output/
```

## 产出物

- `sessions/` — 5 个 session.db 文件
- `scripts/` — Python 分析脚本
- `output/` — 统计 JSON + 进程树可视化（ASCII/Graphviz）
- `report.md` — 观察报告
