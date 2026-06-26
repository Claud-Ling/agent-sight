# TimemAi 进程模型研究

使用 AgentSight 观测 TimemAi 运行时进程行为，并与 Claude Code（`research/agentos-process-model/`）做对比分析，服务于 agentOS 进程抽象设计。

**版本**：TimemAi v0.5, `TIMEM_MAX_LLM_OUTPUT=20480`。所有 prompt 与 Claude Code 实验逐字符一致。

## 核心问题

1. TimemAi 运行时产生多少子进程、什么类型、什么层级关系？**与 Claude Code 的 Bun runtime 进程模型有何结构差异？**
2. 子进程的创建/销毁与 LLM 调用/工具调用之间的时序关联是什么？**TimemAi 的 `curl` 子进程 HTTP 模型 vs Claude Code 的进程内 HTTP，对进程创建模式有何影响？**
3. 两个 agent runtime 的进程模型差异对 agentOS 的进程抽象设计有何启示？

## 环境要求

- Linux（eBPF 需要）+ sudo
- TimemAi v0.5 已构建：`/home/lixiang/EMU/TimemAi/target/release/timem-native-rs`
- TimemAi env 文件已配置（DeepSeek API key）
- AgentSight 已构建：`/home/lixiang/EMU/agentsight/collector/target/release/agentsight`
- Python 3.6+，分析脚本复用 `research/agentos-process-model/scripts/`

## 实验任务 & Prompts

所有 prompt 与 Claude Code 实验逐字符一致。

| 编号 | 任务类型 | Prompt |
|------|---------|--------|
| T1 | 简单问答 | `请用3-5句话解释eBPF是什么，以及它在Linux内核中的作用` |
| T2 | 多文件读取 | `阅读 bpf/ 目录和 collector/src/ 目录，用5句话总结 AgentSight 的整体架构` |
| T3 | 代码修改 | `在 collector/src/main.rs 中添加 --version CLI 参数，使其打印版本号后退出。只修改这一个文件，不要运行构建或测试。` |
| T4 | 工具密集 | `扫描项目中所有包含 TODO 或 FIXME 注释的文件，列出位置和内容。然后用 git log --oneline -5 查看最近提交。最后总结项目的技术债务情况。` |
| T5a | agentOS 基础 | `什么是 agentOS？它和传统操作系统（如 Linux）的根本区别是什么？从进程管理、资源调度、隔离模型三个维度分析。` |
| T5b | 进程模型 | `基于你刚对 agentOS 的分析，进一步思考：agentOS 的进程模型应该是什么样的？考虑以下方面：(1) agent 的进程分类学——哪些是长生命周期基础设施进程、哪些是短命动作进程；(2) agent 子进程的沙箱边界应该画在进程树哪一层；(3) 进程创建的开销对 agent 性能的影响。` |
| T5c | 架构设计 | `现在从 agentOS 架构设计角度，总结我们讨论的核心设计原则。从以下角度：(1) agent 进程的生命周期管理——何时创建、何时回收；(2) agent 间通信的 IPC 原语应该提供什么抽象；(3) agentOS 的资源记账应该以什么粒度（进程、会话、还是 agent）？(4) 与传统 OS 的兼容层是否需要、需要到什么程度？` |

## 运行步骤

```bash
# 0. 前置条件
AGENTSIGHT=/home/lixiang/EMU/agentsight/collector/target/release/agentsight
TIMEMAI=/home/lixiang/EMU/TimemAi/target/release/timem-native-rs
OUTDIR=research/timemai-process-model/sessions
API_KEY=sk-300c675ab6174a228b598889ab530ecc

# 通用 TimemAi 参数（v0.5）
TIMEM_ARGS="--gateway-provider custom --api-protocol openai-compatible \
  --base-url https://api.deepseek.com --model deepseek-v4-pro \
  --bash-approval approve"

# JSON 格式提示（用于 T3/T4/T5，缓解 DeepSeek chain-of-thought 泄漏）
JSON_HINT="IMPORTANT: Your entire response must be a single valid JSON object starting with {. \
  Do not include any text before the JSON. Do not add markdown code fences."

# 录制命令模板
record_and_copy() {
    local label=$1; local prompt=$2; local datadir=$3; local extra_args=$4
    sudo TIMEM_API_KEY=$API_KEY TIMEM_MAX_LLM_OUTPUT=20480 $AGENTSIGHT record -- \
      $TIMEMAI --once-json "$prompt" $TIMEM_ARGS \
      --data-dir $datadir $extra_args
    cp $(ls -t agentsight-*.db | head -1) $OUTDIR/${label}.db
}

# T1: 简单问答
record_and_copy "T1-simple-qa" \
  "请用3-5句话解释eBPF是什么，以及它在Linux内核中的作用" \
  /tmp/timem-research-t1

# T2: 多文件读取
record_and_copy "T2-multi-read" \
  "阅读 bpf/ 目录和 collector/src/ 目录，用5句话总结 AgentSight 的整体架构" \
  /tmp/timem-research-t2

# T3: 代码修改 (需要 JSON hint)
record_and_copy "T3-code-change" \
  "在 collector/src/main.rs 中添加 --version CLI 参数，使其打印版本号后退出。只修改这一个文件，不要运行构建或测试。" \
  /tmp/timem-research-t3 \
  "--supporting-context \"$JSON_HINT\""

# T4: 工具密集 (需要 JSON hint)
record_and_copy "T4-tool-intensive" \
  "扫描项目中所有包含 TODO 或 FIXME 注释的文件，列出位置和内容。然后用 git log --oneline -5 查看最近提交。最后总结项目的技术债务情况。" \
  /tmp/timem-research-t4 \
  "--supporting-context \"$JSON_HINT\""

# T5a: agentOS 基础 (需要 JSON hint)
record_and_copy "T5a-agentos-fundamentals" \
  "什么是 agentOS？它和传统操作系统（如 Linux）的根本区别是什么？从进程管理、资源调度、隔离模型三个维度分析。" \
  /tmp/timem-research-t5a \
  "--supporting-context \"$JSON_HINT\""

# T5b: 进程模型 (需要 JSON hint)
record_and_copy "T5b-process-model" \
  "基于你刚对 agentOS 的分析，进一步思考：agentOS 的进程模型应该是什么样的？考虑以下方面：(1) agent 的进程分类学——哪些是长生命周期基础设施进程、哪些是短命动作进程；(2) agent 子进程的沙箱边界应该画在进程树哪一层；(3) 进程创建的开销对 agent 性能的影响。" \
  /tmp/timem-research-t5b \
  "--supporting-context \"$JSON_HINT\""

# T5c: 架构设计 (需要 JSON hint)
record_and_copy "T5c-architecture" \
  "现在从 agentOS 架构设计角度，总结我们讨论的核心设计原则。从以下角度：(1) agent 进程的生命周期管理——何时创建、何时回收；(2) agent 间通信的 IPC 原语应该提供什么抽象；(3) agentOS 的资源记账应该以什么粒度（进程、会话、还是 agent）？(4) 与传统 OS 的兼容层是否需要、需要到什么程度？" \
  /tmp/timem-research-t5c \
  "--supporting-context \"$JSON_HINT\""
```

## 分析流程

```bash
SCRIPTS=research/agentos-process-model/scripts
OUTDIR=research/timemai-process-model/output
SESSIONS=research/timemai-process-model/sessions

for label in T1-simple-qa T2-multi-read T3-code-change T4-tool-intensive \
             T5a-agentos-fundamentals T5b-process-model T5c-architecture; do
    python3 $SCRIPTS/analyze.py $SESSIONS/${label}.db > $OUTDIR/stats_${label}.json
    python3 $SCRIPTS/process_tree.py $SESSIONS/${label}.db $label $OUTDIR/
done
# regenerate summary.json
python3 generate_summary.py
```

## 输出物

```
research/timemai-process-model/
├── README.md                          # 本文件
├── report-timemai.md                   # TimemAi 进程模型观察报告（16节）
├── report-comparison.md                # TimemAi vs Claude Code 对比报告（10节）
├── prompt-comparison.md                # Prompt 一致性审计（含 v1 历史 + v2 对齐状态）
├── sessions/                           # 7 个 AgentSight session .db
├── output/                             # 7×stats JSON + 7×process_tree TXT + 7×DOT + summary.json
└── _backup_20260626/                   # v0.4 实验数据备份
```

## 对比假设验证

| 假设 | v0.5 验证结果 |
|------|-------------|
| TimemAi 是 per-request 子进程模型 | ✓ 确认 — 每个 LLM 调用 = 1 个 curl 子进程 |
| Claude Code 是启动 burst + 线程池稳态模型 | ✓ 确认 — 启动 ~12 进程，稳态零子进程 |
| TM HTTP = curl 子进程 | ✓ 确认 |
| CC HTTP = 进程内 | ✓ 确认 |
| TM 进程数与 LLM 调用正相关 | ✓ 确认 |
| CC 进程数主要在启动阶段 | ✓ 确认 |

*基于 2026-06-26 使用 TimemAi v0.5 和 CC-identical prompts 的重新录制*
