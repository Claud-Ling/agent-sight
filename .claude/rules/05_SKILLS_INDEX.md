# Skills Index

本索引指向可复用的 Skills（技能）—— AI 可以调用的工作流程和最佳实践。

- **想使用某个能力** → 浏览下方分类，找到对应的 skill 文件
- **想添加新 skill** → 见底部[「如何添加你自己的 Skill」](#如何添加你自己的-skill)

---

## §分类索引

### §Workflow（工作流）

特定任务的完整工作流程。

（暂无项目特定 workflow）

### §BestPractice（最佳实践）

通用的最佳实践和经验教训。

- [Skill 写作指南（Meta-Skill）](skills/bestpractice_01-skill_writing.md) — 创建或重写任何 skill 时使用。
- [AI 编程核心方法论](skills/bestpractice_02-ai_programming_mindset.md) — 启动新功能或新项目前，确认问题定义、成功标准和验证方式。
- [AI 辅助调试诊断](skills/bestpractice_03-ai_debugging_diagnosis.md) — 遇到构建失败、运行异常或接口报错时优先参考。
- [时间敏感信息验证](skills/bestpractice_04-temporal_info_verification.md) — 涉及版本号、spec 引用、发布时间等可能过时的信息时使用。
- [Bash strict mode 管道退出码陷阱](skills/bestpractice_07-bash_strict_mode_pipes.md) — 在 `set -euo pipefail` 下写 `cmd | grep/awk/head` 管道时，避免下游非零退出码被 pipefail 当硬错误中止脚本。

Related: [03_WORKSPACE.md §系统与规则] [06_AXIOMS_INDEX.md §如何使用]

---

## §如何添加你自己的 Skill

创建或重写 skill 前，先读 [`bestpractice_01-skill_writing.md`](skills/bestpractice_01-skill_writing.md)。它说明如何用目标、验收标准、可用资源和输出规格定义一个 skill，而不是把 skill 写成机械步骤清单。

文件命名建议采用 `<category>_<NN>-<name>.md`，例如 `workflow_01-my_process.md`、`bestpractice_01-my_insight.md`。写完后在本 INDEX 的对应分类下添加入口，确保后续 agent 能找到。

Related: [bestpractice_01-skill_writing.md §核心原则] [03_WORKSPACE.md §创建阈值]

## §Progressive Disclosure

Skills 采用渐进式披露原则：
- **05_SKILLS_INDEX.md** 提供概览，快速定位
- **具体 skill 文件** 包含完整的操作步骤和示例

Related: [03_WORKSPACE.md §查找原则] [04_COMMUNICATION.md §文件交叉引用规范]
