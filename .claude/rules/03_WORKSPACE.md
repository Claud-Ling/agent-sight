# WORKSPACE.md - 目录路由速查

目标：让 AI 每轮 session 都能快速知道"去哪里找/放什么"。**找任何文件前先查这里。**

## §路由规则

### §项目与代码

- **eBPF 程序（C）**：`bpf/` — sslsniff、process、stdiocap 等内核态探针
- **Rust collector**：`collector/src/` — runners/、analyzers/、sources/、view/、sinks/、output/、server/、event.rs、model.rs
- **CLI 入口**：`collector/src/main.rs` — top、record、report、debug 等子命令
- **前端（Next.js/React/TypeScript）**：`frontend/`
- **构建系统**：`Makefile`（顶层，`make build`） / `collector/Cargo.toml`（`cargo build --release`） / `frontend/package.json`（`npm run build`）
- **测试**：`cd bpf && make test`（C 单测+运行时测试）/ `cd collector && cargo test`（Rust 测试）/ `cd frontend && npm run lint`（前端 lint）
- **脚本工具**：`script/` — SSL 分析、时间分布分析、信号关联等辅助脚本
- **文档**：`docs/` — 实验记录、flamegraph 示例、OTel 文档等
- **Agent 会话**：`agent-session/` — agent 原生会话文件
- **性能分析**：`agentpprof/` — flamegraph 生成与标记规则
- **CI/CD**：`.github/workflows/`
- **Docker**：`dev.dockerfile`、`dockerfile`
- **Nix**：`flake.nix`、`flake.lock`、`.envrc`

### §系统与规则

- 可复用技术方案 / Skill：`.claude/rules/skills/`（索引见 `.claude/rules/05_SKILLS_INDEX.md`）
- 核心公理（Axioms）：`.claude/rules/axioms/`（索引见 `.claude/rules/06_AXIOMS_INDEX.md`）
- 记忆系统：`.claude/memory/`
- Claude Code 仓库级自定义 skills：`.claude/skills/`
- Claude Code 仓库级自定义命令：`.claude/commands/`
- 设计文档：`.claude/docs/specs/`（`/brainstorming` skill 落盘，命名 `<YYYY-MM-DD>-<topic>-design.md`；已批准文档为冻结快照，一般不修改）
- 实施计划：`.claude/docs/plans/`（`/writing-plans` skill 落盘，命名 `<YYYY-MM-DD>-<feature>-implementation-plan.md`；已完成文档为冻结快照，一般不修改）
- 架构决策记录：`.claude/docs/adr/`

> **如何对待 `.claude/docs/` 历史文档（设计文档 + 实施计划）：**
> 1. **定位**：它们是**历史决策记录**，说明"当时为什么这么设计/计划"，不保证与当前代码一致。文档越旧，与现状漂移的概率越高。
> 2. **加载方式**：它们**不随 session 自动加载**。要访问时：先用 `ls` 列目录枚举文件，再用 `Read` 按路径主动读取；不要预先通读整个目录。
> 3. **事实优先级**：判断**当前实现或行为**时，以代码、配置、日志为准；`docs/` 只用于回溯设计意图和决策背景。

## §文件生命周期管理

知识文件（`.claude/rules/skills/`、`.claude/rules/axioms/`、`.claude/docs/`、`.claude/memory/`、`.claude/skills/`）的增删拆合，遵循以下规则。

### §创建阈值

| 条件 | 动作 |
|------|------|
| 新主题内容 >20 行 | 新建独立文件 + 更新对应索引（`05_SKILLS_INDEX.md` 或 `03_WORKSPACE.md`） |
| 新主题内容 ≤20 行 | 追加到现有最相关的文件中，不新建 |
| 新建 skill 文件 | 同时更新 `05_SKILLS_INDEX.md`，按 `<category>_<NN>-<name>.md` 命名 |
| 新建 axiom 文件 | 同时更新 `06_AXIOMS_INDEX.md`，按 `<domain><NN>_<slug>.md` 命名 |
| 新建设计文档 | 落盘到 `.claude/docs/specs/`，命名 `<YYYY-MM-DD>-<topic>-design.md` |
| 新建实施计划 | 落盘到 `.claude/docs/plans/`，命名 `<YYYY-MM-DD>-<feature>-implementation-plan.md` |

### §行数阈值

| 文件行数 | 状态 | 处理 |
|---------|------|------|
| <400 行 | 安全区 | 正常追加内容 |
| 400-600 行 | 警告区 | 文件顶部加 `<!-- WARNING: N lines, approaching 600-line archive threshold -->` 注释；下次新增内容时拆分低优先级章节到独立文件 |
| >600 行 | 归档区 | 必须归档：移动到 `archive/<name>_YYYYMMDD.md`，在原位置重建精简版（保留 §Quick Reference 和核心章节，其余外链指向归档文件） |

### §每次写入后

1. 更新文件顶部 `Updated` 时间戳（或底部 `*Updated: YYYY-MM-DD \| Reason: ...*`）
2. 检查本次变更是否需要更新对应索引文件
3. 若新增章节：检查是否需要在其他文件的关联章节添加 `Related:` 引用（维护知识图谱边，见 [04_COMMUNICATION.md §文件交叉引用规范]）
4. 若改名 §Section：全局搜索 `grep -r "§旧名"` → 更新所有引用

### §归档

- 归档目录：`.claude/rules/skills/archive/`、`.claude/rules/axioms/archive/`、`.claude/docs/specs/archive/`、`.claude/docs/plans/archive/`
- 归档时保留原始创建日期和归档日期
- 归档文件保留完整内容，不删改
- 归档后在对应索引中标注 `(archived YYYY-MM-DD)`

### §动态记忆（OBSERVATIONS.md）

- 追加模式下添加日期条目（见 `.claude/memory/OBSERVATIONS.md` 格式说明）
- 低优先级（🟢 Low）条目由 reflector 定期回收
- 高优先级（🔴 High）条目在 reflector 审视下考虑晋升为 axiom 或 skill 文件

## §命名规则

- 目录和文件名：小写 + 下划线 (snake_case)
- 临时一次性项目：`tmp_<name>/`

## §查找原则

- 先查本表，再搜索。
- 如果问题涉及外部源码树，在计划或上下文里明确源码根目录，不要假设它已经在本仓内。
- 禁止对仓库根做全局 glob/find/rg 扫描。路由到具体目录再搜索。

<!-- 随着你的项目增长，在这里添加活跃项目的快捷路由 -->
