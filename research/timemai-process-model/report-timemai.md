# TimemAi 进程模型观察报告

> 基于 AgentSight eBPF 录制的 7 个 session（T1-T5），从进程树、生命周期、并发、时序等维度描述 TimemAi（Rust + DeepSeek）的运行时进程行为。

## 实验数据总览

| Session | 任务类型 | 进程数 | curl(LLM代理) | sh(工具代理) | 耗时 | 树深度 | 唯一comm |
|---------|---------|--------|---------------|-------------|------|--------|---------|
| T1 | 简单问答 | 2 | 1 | 0 | 9.6s | 1 | 2 |
| T2 | 多文件读取 | 29 | 4 | 4 | 65.1s | 2 | 10 |
| T3 | 代码修改 | 39 | 7 | 3 | 158.2s | 2 | 10 |
| T4 | 工具密集 | 18 | 3 | 3 | 66.5s | 2 | 6 |
| T5a | agentOS基础概念 | 15 | 4 | 1 | 89.9s | 2 | 8 |
| T5b | 进程模型设计 | 3 | 2 | 0 | 72.0s | 1 | 2 |
| T5c | 架构设计概要 | 5 | 4 | 0 | 62.7s | 1 | 2 |

**数据说明**：AgentSight 的 `llm_calls`、`tool_calls`、`token_usage`、`network_targets` 表为空——SSL 探针未能将 curl 子进程的 TLS 流量解析为 LLM 调用/工具调用记录（可能是 HTTP 解析器不识别 DeepSeek API 响应格式，或 curl 子进程存活时间短于 eBPF 捕获窗口）。LLM 调用数以 `curl` 子进程数量为代理指标，工具调用数以 `sh` 子进程数量为代理指标。

---

## 1. 进程树拓扑：骨架-爆发模型

**核心形态**：

```
timem-native-rs              ← 唯一常驻根进程
├── curl                     ← LLM API 调用（每轮 1 个，存活取决于 API 延迟）
├── sh                        ← 工具执行入口（每轮 1-4 个）
│   ├── locale-check          ← 环境检测命令（每轮 run_bash 携带，区别于 CC 的一次性快照）
│   ├── grep                  ← 最高频文本搜索工具
│   ├── find / cat / head / ls ← 文件探索
│   └── sed                   ← 文本替换
├── curl                     ← 下一轮 LLM 调用
└── sh → {locale-check, grep, ...}  ← 下一轮工具执行
```

**特征**：
- 树深度 2 层（timem → {curl, sh → tools}），无中间基础设施层
- 只有 1 个根进程（`timem-native-rs`），无 worker 线程池
- `locale-check` 是 TimemAi 的进程行为特征——`run_bash` 每次执行都携带（Claude Code 中也出现，但仅为一次性环境快照）
- 文件探索主要用 find/grep/cat/ls，任务明确要求时也会使用 git（如 T4 的 `git log`）

## 2. 进程类型分布（T3 示例，39 进程）

| comm | 数量 | 占比 | 角色 |
|------|------|------|------|
| grep | 14 | 35.9% | 文本搜索 |
| curl | 7 | 17.9% | LLM API 调用代理 |
| locale-check | 5 | 12.8% | 环境检测（进程指纹） |
| sh | 3 | 7.7% | 工具执行器 |
| cat | 3 | 7.7% | 文件内容读取 |
| find | 2 | 5.1% | 文件扫描 |
| head | 2 | 5.1% | 输出截断 |
| timem-native-rs | 1 | 2.6% | 根进程 |
| ls | 1 | 2.6% | 目录列表 |
| sed | 1 | 2.6% | 文本替换 |

## 3. 进程生命周期：双峰分布

| 维度 | T3 值 |
|------|-------|
| p50 | 2ms |
| p95 | 37,457ms |
| min | 0ms |
| max | 158,207ms（timem 根进程 = session 全长） |
| mean | 8,112.7ms |

**按角色分类**：

| comm | p50 | 角色 | 解释 |
|------|-----|------|------|
| curl | 32,600ms | INFRA | 等待 LLM API 响应，30-40s |
| timem-native-rs | 158,207ms | INFRA | 持续整个 session |
| sh | 34ms | ACTION | 工具执行 < 100ms |
| grep | 2ms | ACTION | 瞬时文本搜索 |
| cat | 1ms | ACTION | 瞬时文件读取 |
| locale-check | 1ms | ACTION | 瞬时环境检测 |

curl 是唯一的 INFRA 级子进程（p50 > 1s），因为每次 LLM API 调用需要等待 DeepSeek 返回，存活 30-40s。

## 4. 进程创建时序：多集群交替爆发

T3 session 的 39 个进程分布在 **7 个创建集群**中，每个集群对应一轮操作：

```
集群1 (2):   curl                                  — LLM#1 调用
集群2 (6):   sh → locale-check + grep×3 + ls       — 第1轮文件探索
集群3 (1):   curl                                  — LLM#2 调用
集群4 (11):  sh → locale-check + grep×5 + cat +    — 第2轮文件修改
             find×2 + head
集群5 (8):   curl + sh → locale-check + grep×2 +   — 第3轮
             cat×2 + find
集群6 (5):   curl + head → locale-check + grep×2    — 第4轮
集群7 (6):   curl + sed → locale-check + grep×3     — 第5轮(实际代码修改)
```

**循环节奏**：curl (30-40s 等待) → sh-burst (< 100ms 工具执行) → curl → sh-burst → ...

- 每个 sh burst 内最多 10 个子进程并发
- action 之间严格串行（上一轮 sh burst 结束后才发出下一个 curl）
- 最后 36s（`post_burst_tail_ms` = 36,237ms）无新子进程——模型在做最终输出
- **T3 非 sh 入口点**：集群 6 和 7 中 `head`（PID 642140）和 `sed`（PID 642248）替代 `sh` 成为工具通道节点，各自携带 `locale-check` + `grep` 子树。这是 shell 在管道命令中优化掉 sh 包装层的结果——`run_bash` 传入的管道首命令被 exec 为入口进程

## 5. 并发与资源

| 维度 | T1 | T2 | T3 | T4 | T5a | T5b | T5c |
|------|----|----|----|----|-----|-----|-----|
| 最大并发进程 | 2 | 4 | 7 | 3 | 4 | 2 | 2 |
| CPU max (%) | 0.0 | 0.0 | 0.5 | 0.5 | 0.5 | 0.0 | 0.0 |
| RSS max (MB) | 16 | 17 | 17 | 17 | 17 | 17 | 17 |

并发峰值出现在 sh 工具执行阶段（grep×5 + find×2 等并行文件操作）。CPU 使用极低（max 0.5-1%），因为计算密集的 LLM 推理在远端。RSS 稳定在 17MB（Rust 编译的单体二进制）。

## 6. 协议修复（Protocol Repair）行为

在 T3（7 curl）和 T5a（4 curl）中，DeepSeek 模型的响应不符合 TimemAi 的 JSON envelope 协议（需包含 `response_to_user` + `next_actions` + `acceptance_check`），触发 `agent_core/src/lib.rs:317-344` 的两层防御：

1. **首次违规** → runtime 向模型发送修复请求，要求重新生成合法 JSON
2. **修复后仍不合法** → 阻断原始报文，返回兜底消息

这是 TimemAi 独有的可靠性设计——在 Rust runtime 层做显式协议验证，而非依赖模型自觉。对进程行为观测来说，协议修复不影响 eBPF 层面的进程创建/销毁事件捕获：curl 子进程的创建和退出仍被完整记录（但网络流量表 `network_targets` 因 AgentSight HTTP 解析器不识别 DeepSeek API 格式而保持为空）。

## 7. T5 深度 session 特征

T5a（15 进程）、T5b（3 进程）、T5c（5 进程）的进程数明显低于 T2-T4：

- T5b/T5c 是纯推理任务（概念分析、架构设计），几乎不涉及文件探索和代码修改
- 进程树简化为 `timem → curl` 二级结构，无 sh 工具链
- T5b 仅有 3 个进程（timem + 2 curl），但持续 98s——两个 curl 各存活约 40s，中间无工具调用，是 TimemAi 能达到的最简进程形态

## 8. 总结

| 特征 | 描述 |
|------|------|
| **进程模型** | 骨架-爆发（Skeleton-Burst）：单根进程 + 每轮 action 的叶子进程群 |
| **LLM 调用** | 子进程 `curl`，每次 API 调用 = 1 个存活取决于 API 延迟（9.6s~42s，复杂任务中典型 30-40s）的 curl 子进程 |
| **工具执行** | 子进程 `sh -c`，每次工具调用 fork 新 shell 及其子进程链 |
| **持久基础设施** | 无。无线程池、无 worker 进程、无守护进程 |
| **树深度** | 2 层（timem → {curl, sh → tools}） |
| **生命周期分布** | 双峰：curl 30-40s（INFRA），grep/cat/find 1-2ms（ACTION） |
| **资源占用** | RSS 17MB，CPU < 1% |
| **并发模式** | 每轮 action 内 sh 子进程并行 burst（max 7），action 间严格串行 |
| **进程指纹** | `locale-check` 每次 sh 调用携带；curl LLM 代理 |
| **可靠性机制** | Rust runtime 层 JSON envelope 验证 → 一次修复机会 → 兜底阻断 |
