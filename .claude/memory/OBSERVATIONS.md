# Memory Observations

这是三层记忆系统的动态记忆日志。observer 会把当天观测追加到这里；reflector 会回看这里的近期内容，清理低价值项，并据此产出规则晋升与报告。

## 格式说明

每个日期条目格式如下：

```raw
Date: YYYY-MM-DD

🔴 High: [方法论/约束] 描述
🟡 Medium: [项目状态/决策] 描述
🟢 Low: [任务流水] 描述
```

### 优先级定义

- **🔴 High**：跨项目通用的经验教训、硬性约束、影响系统架构的重大决策。永久保留，候选晋升为 axiom 或 skill。
- **🟡 Medium**：活跃项目的关键进展、技术决策背景、未来几周仍需参考的信息。
- **🟢 Low**：日常任务流水、瞬时 debug 记录、临时上下文。定期垃圾回收。

## 如何加载记忆

不要全文加载这个文件（可能很大）。按需检索：

```bash
# 搜索特定主题
grep -n "关键词" .claude/memory/OBSERVATIONS.md

# 搜索最近 N 天
grep -A 20 "Date: $(date -v-7d +%Y-%m-%d)" .claude/memory/OBSERVATIONS.md
```

或使用 `grep`（正则搜关键词）做跨日期检索。

---

<!-- 以下是记录区域，由 observer 追加，reflector 定期整理 -->

Date: 2026-06-25

🔴 High: [方法论] AgentSight 实验完成——7 个 session 录制了 Claude Code v2.1.191 的进程行为。核心发现：进程树深度恒定 3 层、p50 存活 1-7ms、bash 是应被调度器透明化的通道节点、采用请求驱动（per-request fork）而非连接池生命周期模型。实验设计/计划/脚本/报告在 research/agentos-process-model/。
🟡 Medium: [工具链] AgentSight 编译需要 Rust 1.96+（edition2024）、libbpf 子模块初始化。sudo NOPASSWD 只配了 agentsight 二进制路径（/etc/sudoers.d/agentsight）。非交互式录制 `record -- claude -p` 可用，T5 交互式多轮需 `record -c claude` 附着但本次因环境限制用了回退方案。
🟡 Medium: [维护] collector/vendor/ 已加入 .git/info/exclude 防止构建变更污染 git。AGENTS.md 从 symlink 改为独立文件（deploy-context 部署）。
🟢 Low: [环境] Rust 1.75→1.96、git submodule update、make build 全量通过。

