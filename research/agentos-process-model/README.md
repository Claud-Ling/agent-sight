# agentOS 进程模型研究

使用 AgentSight 观测 Claude Code 运行时进程行为，服务于 agentOS 进程抽象设计。

## 核心问题

1. Claude Code 运行时产生多少子进程、什么类型、什么层级关系？
2. 子进程的创建/销毁与 LLM 调用/工具调用之间存在什么时序关联？
3. 进程存活时间分布和父子关系模式对 agentOS 的进程抽象设计有何启示？

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
