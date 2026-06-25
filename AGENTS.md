# AGENTS.md - AgentSight

这个仓库是 AgentSight — 一个基于 eBPF 的 AI agent 行为观测框架，通过 SSL/TLS 流量拦截和进程监控来捕获未加密的请求/响应数据。把它当作 home。

## Every Session
Before doing anything else:
1. Read `.claude/rules/01_SOUL.md` — this is who you are
2. Read `.claude/rules/02_USER.md` — this is who you're helping
3. Read `.claude/rules/03_WORKSPACE.md` — file routing table, check before searching
4. Read `.claude/rules/04_COMMUNICATION.md` — how to think and communicate
5. Read `.claude/rules/05_SKILLS_INDEX.md` — understand available skills
Don't ask permission. Just do it.

## File Routing
**找文件时，先查 `.claude/rules/03_WORKSPACE.md`，再搜索。** 如果发现新目录或项目没被收录，顺手更新 WORKSPACE.md。

## Skills
**重要：遇到"怎么做 X"时，先查 skill 再查系统工具。** 搜索顺序：(1) `.claude/rules/05_SKILLS_INDEX.md` → (2) 系统工具。
**想添加新能力** → 参考 `.claude/rules/skills/bestpractice_01-skill_writing.md`，写完后更新 `.claude/rules/05_SKILLS_INDEX.md`

## Axioms（公理）
从个人经历提炼的决策原则。分类索引、使用指南和触发词见 `.claude/rules/06_AXIOMS_INDEX.md`。

## Working Mode
- 设计和计划：先边界，再方案，再验证。
- 实现和调试：先找根因，再做最小改动，再跑可执行验证。
- review：先给 findings，再给证据和建议。

构建/测试命令：
```bash
# Full build
make build
# Individual components
make build-bpf                          # eBPF C programs only
cd collector && cargo build --release   # Rust collector only
cd frontend && npm install && npm run build  # Frontend only
# Tests
cd bpf && make test              # C unit + runtime tests
cd collector && cargo test       # Rust tests
cd frontend && npm run lint      # Frontend linting
# Debug builds
cd bpf && make debug
cd bpf && make sslsniff-debug
```

## Memory System
三层记忆架构：
- **L3（全局约束）**：`.claude/rules/` 核心规则每次 session 启动读取；`.claude/rules/06_AXIOMS_INDEX.md` 及 `axioms/`、`skills/` 按需检索
- **L1/L2（动态记忆）**：`.claude/memory/OBSERVATIONS.md`，agent 主动检索
- **手动积累**：通过 `/ai-heartbeat` 手动触发 observer 和 reflector。

## Safety
- Don't exfiltrate private data. Ever.
- Don't run destructive commands without asking.
- When in doubt, ask.
