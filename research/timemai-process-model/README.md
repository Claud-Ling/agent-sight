# TimemAi 进程模型研究

使用 AgentSight 观测 TimemAi 运行时进程行为，并与 Claude Code（`research/agentos-process-model/`）做对比分析，服务于 agentOS 进程抽象设计。

## 核心问题

1. TimemAi 运行时产生多少子进程、什么类型、什么层级关系？**与 Claude Code 的 Bun runtime 进程模型有何结构差异？**
2. 子进程的创建/销毁与 LLM 调用/工具调用之间的时序关联是什么？**TimemAi 的 `curl` 子进程 HTTP 模型 vs Claude Code 的进程内 HTTP，对进程创建模式有何影响？**
3. 两个 agent runtime 的进程模型差异对 agentOS 的进程抽象设计有何启示？

这三个问题对应 `research/agentos-process-model/` 的三问框架（结构→行为→设计），增加对比维度。每个问题同时考察 TimemAi 的内部特征和与 Claude Code 的差异。

### 对比假设（基于代码分析）

| 维度 | Claude Code（Bun runtime） | TimemAi（Rust + curl） |
|------|--------------------------|----------------------|
| HTTP 层 | 进程内（Bun HTTP client） | 子进程 `curl`，每次 LLM 调用创建 |
| 工具执行 | Bun worker 线程池，进程内 | `run_bash` → 子进程 |
| 预期进程数 | 低（启动 burst 后稳态零子进程） | 较高（每次 LLM 调用 + bash 工具都有子进程） |
| 预期层级 | 3 层（claude → bash → git） | 3-4 层（timem → sh → curl/git） |
| 子进程与 LLM 调用的关系 | 无（启动 burst 后零子进程） | 正相关（每次 LLM 调用对应 curl） |

> 核心假设：TimemAi 是 **per-request 子进程模型**，Claude Code 是 **启动 burst + 线程池稳态模型**。如果假设成立，agentOS 需要同时兼容这两种极端的进程模型。

## 成功标准

- 产出 5+ 个 TimemAi session 录制数据，覆盖 5 类任务（T1-T5）
- 每个 session 产出 16 维度分析 JSON + 进程树可视化
- TimemAi 观察报告（`report-timemai.md`），描述进程模型特征
- 对比报告（`report-comparison.md`），对比两种 agent runtime 的进程行为差异及对 agentOS 的设计启示

## 环境要求

- Linux（eBPF 需要）
- sudo（AgentSight eBPF 加载需要）
- TimemAi 已构建（`/home/lixiang/EMU/TimemAi/target/release/timem-native-rs`）
- TimemAi env 文件已配置（DeepSeek API）
- AgentSight 已构建（`/home/lixiang/EMU/agentsight/collector/target/release/agentsight`）
- Python 3.6+（分析脚本）
- 分析脚本复用 `research/agentos-process-model/scripts/`

## 实验任务

| 编号 | 任务类型 | 描述 | 预期进程特征 |
|------|---------|------|------------|
| T1 | 简单问答 | 单一知识问题，最简进程树基线 | curl × 1-2，最小树 |
| T2 | 多文件读取 | 阅读多个目录并总结，文件探索型 | curl × 3-5，find/ls/grep |
| T3 | 代码修改 | 修改单个源文件，产出型 | curl × 3-5，bash 子进程 |
| T4 | 工具密集 | 多次 git/文件扫描/tool 调用 | curl × 5-8，git/grep 高频 |
| T5 | 多轮深度 | agentOS 架构分析（3 个独立 session，复刻原研究 T5a/T5b/T5c） | curl × 5-15，持续 bash |

## 运行步骤

```bash
# 0. 前置条件
source /home/lixiang/EMU/TimemAi/env
AGENTSIGHT=/home/lixiang/EMU/agentsight/collector/target/release/agentsight
TIMEMAI=/home/lixiang/EMU/TimemAi/target/release/timem-native-rs
OUTDIR=research/timemai-process-model/sessions

# 通用 TimemAi 参数（回显在每条命令中）
TIMEM_ARGS="--gateway-provider custom --api-protocol openai-compatible \
  --base-url https://api.deepseek.com --model deepseek-v4-pro \
  --bash-approval approve"

# 1. 录制 T1（简单问答）
sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "请用3-5句话解释eBPF是什么，它和传统网络监控有什么区别" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t1
# 录制完成后，根据 AgentSight stdout 输出的 db 路径，cp 到 sessions/ 目录
# AgentSight 默认输出格式：data/<space>/<session>.db
# 示例：cp data/.test_mem/session_xxx.db $OUTDIR/T1-simple-qa.db

# 2. 录制 T2（多文件读取）
sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "阅读当前目录下的所有源代码文件，总结项目的整体架构和模块职责" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t2

# 3. 录制 T3（代码修改）
sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "在项目主入口文件中添加一个 --version 命令行参数，打印版本号 0.1.0 后退出" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t3

# 4. 录制 T4（工具密集）
sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "扫描当前目录中所有的 TODO/FIXME/HACK 注释，列出文件、行号和内容；然后用 git log 查看最近 5 次提交的作者和主题；最后输出一份代码质量简报" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t4

# 5. 录制 T5（3 个子 session，复刻 agentOS 研究结构）
sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "从第一性原理出发，分析一个 agentOS（面向AI agent的操作系统）应该具备哪些核心设计原则？请从进程模型、调度策略、内存管理、隔离机制四个维度展开" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t5a

sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "深入分析 agentOS 的进程模型设计：agent 的思考和行动是两个不同的相位，这对调度器设计意味着什么？现有操作系统的进程抽象（fork+exec）是否能满足 agent runtime 的需求？如果不能，需要什么样的新原语？" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t5b

sudo $AGENTSIGHT record -- \
  $TIMEMAI --once-json "综合前面的分析，给出一个 agentOS 的架构设计概要：核心组件、关键接口、与传统 OS 的差异、以及在 Linux 上的实现路径" \
    $TIMEM_ARGS --data-dir /tmp/timem-research-t5c

# 6. 运行分析
for db in $OUTDIR/T*.db; do
  label=$(basename $db .db)
  python3 research/agentos-process-model/scripts/analyze.py $db > $OUTDIR/../output/stats_${label}.json
  python3 research/agentos-process-model/scripts/process_tree.py $db $label $OUTDIR/../output/
done
```

## 非范围

- 不涉及 TimemAi 源码修改或功能扩展
- 不涉及 AgentSight eBPF 探针修改
- 不在本次实验中做交互式多轮录制（T5 沿用非交互独立 session 方案）
- 不做其他 agent runtime（如 Codex、Cursor）的对比

## 分析方法

复用 `research/agentos-process-model/scripts/analyze.py` 的 16 维度分析：

1. session_info — 时间跨度、LLM 调用数、进程节点数
2. process_tree_stats — 树拓扑、深度、orphan 数量
3. process_type_distribution — comm 类型分布
4. process_lifetime — p50/p95，INFRA vs ACTION 分类
5. exit_status_distribution — 退出码分布
6. llm_process_correlation — LLM↔进程 ±1s 窗口关联
7. parent_child_pairs — 父子对
8. startup_vs_action — 启动阶段 vs 稳态阶段
9. action_bursts — 进程创建爆发
10. token_flow — 输入/输出/cache token 模式
11. tool_analysis — 工具类型分布与进程映射
12. resource_profile — CPU/RSS 时间线
13. network_targets — 出站连接
14. concurrency — 最大并发进程数
15. event_timeline — 统一事件时间线
16. subprocess_vs_inprocess — 稳态工具执行是否产生子进程

## 对比框架

对比报告（`report-comparison.md`）使用以下结构：

1. **进程树拓扑对比**：深度、类型多样性、父子关系模式
2. **进程创建时序对比**：启动 burst vs 持续创建，与 LLM/工具调用的时序关系
3. **进程生命周期对比**：p50/p95 存活时间，INFRA/ACTION 分布
4. **工具执行模式对比**：子进程 vs 进程内，隔离粒度
5. **对 agentOS 设计启示**：两种模型的并存含义

## 已知风险

- **sudo 密码**：每次 `agentsight record` 需要 sudo，实验时需要手动输入密码
- **db 文件定位**：AgentSight 输出 db 的路径取决于 `--data-dir` 和 session 配置，录制完成后需根据日志确认
- **TimemAi 的 curl 子进程**：eBPF 探针是否能捕获极短命的 `curl` 子进程（存活可能 < 500ms），取决于竞态窗口

## 产出物

- `sessions/` — 5-7 个 session.db 文件
- `output/` — 统计 JSON + 进程树可视化（ASCII + DOT）
- `report-timemai.md` — TimemAi 16 维度观察报告
- `report-comparison.md` — 对比报告
