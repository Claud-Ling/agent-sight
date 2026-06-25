# agentOS 进程模型研究 — 实施计划

## 目标

执行设计文档方案 A：录制 5 类 Claude Code 任务的 session 数据，用 Python sqlite3 直连分析进程模型特征，产出分析脚本、可视化和观察报告。

## 输入工件

- 设计文档：`.claude/docs/specs/2026-06-25-agentos-process-model-design.md`
- 环境确认：Claude Code v2.1.191（`/home/lixiang/.local/share/fnm/node-versions/v20.20.1/installation/bin/claude`），`claude -p` 支持单次非交互式执行
- AgentSight：已构建（`make build`），`sudo ./agentsight record -- claude -p` 可用

## 架构快照

本次是纯研究任务，不修改 AgentSight 源码。数据管线：

```
claude -p "<prompt>" → agentsight record -- claude -p "..." → ./agentsight-{ts}.db → Python sqlite3 → 统计 + 可视化 → report.md
```

T5（多轮交互）特殊路径：`sudo ./agentsight record -c claude --binary-path <path>` 附着到已运行的交互式 Claude Code 进程，手动执行多轮后停止录制。

## 文件结构与职责

```
research/agentos-process-model/           ← 新建，研究产物根目录
├── README.md                             ← 实验说明、环境要求、运行步骤
├── sessions/                             ← 录制产物
│   ├── .gitkeep
│   ├── T1-simple-qa.db                   ← T1 session（录制后移入）
│   ├── T2-multi-read.db                  ← T2 session
│   ├── T3-code-change.db                 ← T3 session
│   ├── T4-tool-intensive.db              ← T4 session
│   └── T5-multi-turn.db                  ← T5 session
├── scripts/
│   ├── analyze.py                        ← 主分析脚本：读 SQLite，输出 6 维度统计
│   └── process_tree.py                   ← 进程树重建 + ASCII/Graphviz 可视化
├── output/
│   ├── .gitkeep
│   ├── process_tree_T1.txt               ← T1 进程树 ASCII
│   ├── process_tree_T1.dot               ← T1 进程树 Graphviz
│   ├── ...                               ← T2-T5 同理
│   └── summary_stats.json                ← 汇总统计（所有 session 维度对比）
└── report.md                             ← 观察报告：整合分析结果，回答设计文档中 3 个核心问题
```

## 任务清单

### Task 1: 创建研究目录结构和 README

- 目标：建立 `research/agentos-process-model/` 目录树，编写实验说明
- Files: Create `research/agentos-process-model/README.md`, `research/agentos-process-model/sessions/.gitkeep`, `research/agentos-process-model/output/.gitkeep`
- 验证范围：目录结构存在，README 包含环境要求、运行步骤和预期产出

- [ ] Step 1: 确认当前不存在 research/agentos-process-model/ 目录
- Run: `ls research/agentos-process-model/ 2>&1`
- Expected: `No such file or directory`

- [ ] Step 2: 验证 AgentSight 已构建
- Run: `ls -la ./agentsight && file ./agentsight`
- Expected: 文件存在，类型为 ELF 可执行文件

- [ ] Step 3: 验证 `claude -p` 非交互模式可用
- Run: `claude -p "hello" 2>&1 | head -5`
- Expected: 输出 Claude 回复文本，无连接错误

- [ ] Step 4: 创建完整目录结构
- Run: `mkdir -p research/agentos-process-model/{sessions,scripts,output} && touch research/agentos-process-model/sessions/.gitkeep research/agentos-process-model/output/.gitkeep`
- Expected: 目录创建成功，exit code 0

- [ ] Step 5: 编写 README.md，包含：实验目标（3 个核心问题）、环境要求（sudo、Claude Code、AgentSight 已构建、Python 3.6+）、T1-T5 任务说明、运行步骤、产出物清单
- Change: 写 `research/agentos-process-model/README.md`
- [ ] Step 6: 验证 README 存在且非空
- Run: `wc -l research/agentos-process-model/README.md`
- Expected: >20 行

### Task 2: 录制 T1 — 简单问答

- 目标：录制 Claude Code 回答单一知识性问题的 session，获取最简进程树基线
- Files: Create `research/agentos-process-model/sessions/T1-simple-qa.db`（通过移动）
- 验证范围：session.db 存在，llm_calls 表非空，process_nodes 表非空

- [ ] Step 1: 确认当前目录无残留 agentsight-*.db 文件
- Run: `ls agentsight-*.db 2>&1`
- Expected: `No such file or directory`

- [ ] Step 2: 录制 session — Claude Code 回答 eBPF 知识问题
- Run: `sudo ./agentsight record -- claude -p "请用3-5句话解释eBPF是什么，以及它在Linux内核中的作用"`
- Expected: 命令正常退出（exit code 0），当前目录生成 `agentsight-*.db` 文件

- [ ] Step 3: 验证 session.db 包含 llm_calls 和 process_nodes 数据
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  sqlite3 "$DB" "SELECT COUNT(*) FROM llm_calls;" && sqlite3 "$DB" "SELECT COUNT(*) FROM process_nodes;"
  ```
- Expected: llm_calls >= 1, process_nodes >= 1

- [ ] Step 4: 将 session.db 移入研究目录并重命名
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  mv "$DB" research/agentos-process-model/sessions/T1-simple-qa.db
  ```
- Expected: 文件移动成功，原路径无残留

### Task 3: 录制 T2 — 多文件读取

- 目标：录制 Claude Code 阅读多文件并总结的 session，观察文件探索型任务的进程模式
- Files: Create `research/agentos-process-model/sessions/T2-multi-read.db`
- 验证范围：llm_calls >= 2（多次工具调用），audit_events 包含 file 类型事件

- [ ] Step 1: 录制 session — 让 Claude Code 阅读项目架构
- Run: `sudo ./agentsight record -- claude -p "阅读 bpf/ 目录和 collector/src/ 目录，用5句话总结 AgentSight 的整体架构"`
- Expected: exit code 0，生成 agentsight-*.db

- [ ] Step 2: 验证数据完整性
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  sqlite3 "$DB" "SELECT COUNT(*) FROM llm_calls;" && sqlite3 "$DB" "SELECT COUNT(*) FROM audit_events WHERE audit_type='file';" && sqlite3 "$DB" "SELECT COUNT(*) FROM process_nodes;"
  ```
- Expected: llm_calls >= 1, file audit_events >= 1, process_nodes >= 1

- [ ] Step 3: 移入研究目录
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  mv "$DB" research/agentos-process-model/sessions/T2-multi-read.db
  ```

### Task 4: 录制 T3 — 代码修改

- 目标：录制 Claude Code 执行代码修改的 session，观察产出型任务的进程模式（文件写入、可能的构建工具调用）
- Files: Create `research/agentos-process-model/sessions/T3-code-change.db`
- 验证范围：llm_calls >= 1，audit_events 包含 file 类型且 action 含 write/open

- [ ] Step 1: 录制 session — 让 Claude Code 添加 `--version` 参数
- Run: `sudo ./agentsight record -- claude -p "在 collector/src/main.rs 中添加 --version CLI 参数，使其打印版本号后退出。不要修改其他文件，不要运行构建或测试。"`
- Expected: exit code 0，生成 agentsight-*.db

- [ ] Step 2: 验证数据完整性 — 确认有文件写入事件
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  sqlite3 "$DB" "SELECT COUNT(*) FROM llm_calls;" && sqlite3 "$DB" "SELECT COUNT(*) FROM audit_events WHERE audit_type='file' AND (action='write' OR action='open');" && sqlite3 "$DB" "SELECT COUNT(*) FROM process_nodes;"
  ```
- Expected: llm_calls >= 1, file write/open audit_events >= 1, process_nodes >= 1

- [ ] Step 3: 移入研究目录
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  mv "$DB" research/agentos-process-model/sessions/T3-code-change.db
  ```

### Task 5: 录制 T4 — 工具密集

- 目标：录制 Claude Code 执行多次工具调用（git、文件扫描、gh CLI）的 session，观察工具密集型任务的进程爆炸模式
- Files: Create `research/agentos-process-model/sessions/T4-tool-intensive.db`
- 验证范围：llm_calls >= 2，process_nodes >= 5（预期多个子进程），tool_calls 表非空

- [ ] Step 1: 录制 session — 让 Claude Code 扫描 TODO 并尝试创建 issue
- Run: `sudo ./agentsight record -- claude -p "扫描项目中所有包含 TODO 或 FIXME 注释的文件，列出位置和内容。然后用 git log --oneline -5 查看最近提交。最后总结项目的技术债务情况。"`
- Expected: exit code 0，生成 agentsight-*.db

- [ ] Step 2: 验证数据完整性 — 确认多种进程类型，检查 audit 截断风险
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  sqlite3 "$DB" "SELECT COUNT(*) FROM llm_calls;" && sqlite3 "$DB" "SELECT COUNT(DISTINCT comm) FROM process_nodes;" && sqlite3 "$DB" "SELECT COUNT(*) FROM tool_calls;" && sqlite3 "$DB" "SELECT COUNT(*) FROM audit_events;"
  ```
- Expected: llm_calls >= 2, distinct comm values >= 2, tool_calls >= 1。audit_events 总数若等于 10000（默认上限），记录此截断警告

- [ ] Step 3: 移入研究目录
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  mv "$DB" research/agentos-process-model/sessions/T4-tool-intensive.db
  ```

### Task 6: 录制 T5 — 多轮交互

- 目标：录制 Claude Code 交互式多轮对话的 session，观察跨轮进程复用模式和 idle 行为
- Files: Create `research/agentos-process-model/sessions/T5-multi-turn.db`
- 注意：`claude -p` 是单次执行模式，T5 需要用 `sudo ./agentsight record -c claude --binary-path <path>` 附着到已运行的交互式 Claude Code
- 验证范围：llm_calls >= 5（5 轮），process_nodes 非空

- [ ] Step 1: 获取 Claude Code 二进制路径
- Run: `which claude`
- Expected: 输出 Claude Code 可执行文件路径（如 `/home/lixiang/.local/share/fnm/node-versions/v20.20.1/installation/bin/claude`）

- [ ] Step 2: 启动 Claude Code 交互式 session（在后台终端中）
- 手动操作：在新终端窗口中运行 `claude`，进入交互式 REPL
- Expected: Claude Code 交互式 prompt 就绪

- [ ] Step 3: 确认 Claude Code 进程正在运行
- Run: `pgrep -f claude | head -3`
- Expected: 至少输出 1 个 PID

- [ ] Step 4: 附着 AgentSight 录制
- Run: `sudo ./agentsight record -c claude --binary-path $(which claude)`
- Expected: AgentSight 开始录制，显示 session ID

- [ ] Step 5: 在 Claude Code 交互式窗口中执行 5 轮对话
- 话题：逐步深入的 agentOS 架构讨论
  1. "什么是 agentOS？它和传统 OS 有什么区别？"
  2. "agentOS 的进程模型应该是什么样的？"
  3. "agent 的子进程应该如何隔离和调度？"
  4. "agentOS 需要什么样的 IPC 机制来支持 agent 间通信？"
  5. "总结我们讨论的 agentOS 架构，列出核心设计原则"
- Expected: 5 轮对话全部完成

- [ ] Step 6: 停止录制，验证数据
- 在 AgentSight 终端按 Ctrl+C 停止录制
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  sqlite3 "$DB" "SELECT COUNT(*) FROM llm_calls;" && sqlite3 "$DB" "SELECT COUNT(*) FROM process_nodes;"
  ```
- Expected: llm_calls >= 2, process_nodes >= 1。注意：交互式的 5 轮对话可能被流式 API 合并为更少的 `llm_calls` 行——如果 llm_calls < 5 但 >= 2，仍算通过。从 `~/.claude/` JSONL 交叉验证对话轮数

- [ ] Step 7: 移入研究目录
- Run:
  ```
  DB=$(ls -t agentsight-*.db | head -1)
  mv "$DB" research/agentos-process-model/sessions/T5-multi-turn.db
  ```

### Task 7: 编写分析脚本（analyze.py + process_tree.py）

- 目标：编写 Python 脚本，从 SQLite session.db 中提取 6 个对比维度的统计数据
- Files: Create `research/agentos-process-model/scripts/analyze.py`, `research/agentos-process-model/scripts/process_tree.py`
- 验证范围：脚本在任意一个已录制的 session.db 上运行不报错，输出合法 JSON

- [ ] Step 1: 确认 Python3 和 sqlite3 模块可用
- Run: `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`
- Expected: 输出版本号，无错误

- [ ] Step 2: 编写 analyze.py
- Change: 实现以下功能模块。注意：SCHEMA 中 `argv_json`、`input_json`/`output_json`（tool_calls）、`details_json`（audit_events）存储为 JSON TEXT 字符串——使用 `json.loads()` 解析。Python 版本 >= 3.6（标准库 sqlite3 + json 即可，无额外依赖）：
  - `load_db(path)` — 打开 SQLite 连接，设置 `row_factory = sqlite3.Row`。若文件不存在则打印错误并 `sys.exit(1)`
  - `session_info(db)` — 查询 `sessions` 表（如有），提取 agent_type、model、total_tokens。为报告提供 session 级元数据
  - `process_tree_stats(db)` — 查询 process_nodes，输出：总进程数、树深度分布（按 ppid 回溯深度，各级深度进程数）、候选根进程列表（pid + comm + command + ppid，其中 ppid 不在进程集合中）。注意 `start_timestamp_ms` 可空，NULL 值排在末尾处理
  - `process_type_distribution(db)` — 按 comm 分组统计（NULL comm 标记为 "unknown"），输出 `{comm: count}` + 占比
  - `process_lifetime(db)` — 查询 **同时有** `start_timestamp_ms` 和 `end_timestamp_ms` 且均非 NULL 的进程，计算 `(end - start)` 为存活时间 ms。输出 min/max/mean/p50/p95。同时统计缺少时间戳的进程数，标记为"未计算存活时间"
  - `exit_status_distribution(db)` — 按 exit_code 和 comm 分组。NULL exit_code 标记为 "still_running"。输出 `{comm: {exit_code: count}}`
  - `llm_process_correlation(db)` — 查询每个 llm_call 的 `start_timestamp_ms` 和 `end_timestamp_ms`。若 `end_timestamp_ms IS NULL`，fallback 使用 `start_timestamp_ms` 作为窗口中心。时间窗口：`[start - 1000ms, end + 1000ms]`（±1s）。统计窗口内的 process_node 数量。输出关联的 llm_call 数量、关联进程总数、每 llm_call 平均关联进程数。同时统计因 NULL 时间戳而跳过/fallback 的 llm_call 数量
  - `parent_child_pairs(db)` — 查询最常见的父子进程对（按 ppid 关联父进程 pid），输出 top 10 `(parent_comm → child_comm, count)`
  - `main(db_path)` — 依次调用以上函数，输出 JSON 到 stdout。成功时 exit code 0；数据表为空时仍在 JSON 中标注 `"warning": "table X is empty"` 但 exit code 0；数据库文件不存在时 `sys.exit(1)`
- [ ] Step 3: 编写 process_tree.py
- Change: 实现以下功能：
  - `build_tree(db)` — 从 process_nodes 构建邻接表，识别根进程（ppid 不在集合中 + start_timestamp_ms 最早），DFS 重建树形结构
  - `tree_to_ascii(root, prefix)` — 将树转为 ASCII art 文本（类 `pstree` 格式，节点显示 pid:comm）
  - `tree_to_dot(root)` — 将树转为 Graphviz DOT 格式（节点着色按 comm 类型）
  - `main(db_path, output_dir)` — 读 session.db，输出 `<output_dir>/process_tree_<label>.txt` 和 `<output_dir>/process_tree_<label>.dot`
- [ ] Step 4: 在 T1 session 上验证分析脚本
- Run: `python3 research/agentos-process-model/scripts/analyze.py research/agentos-process-model/sessions/T1-simple-qa.db`
- Expected: 输出合法 JSON，包含所有 6 个维度的键，exit code 0

- [ ] Step 5: 在 T1 session 上验证进程树脚本
- Run: `python3 research/agentos-process-model/scripts/process_tree.py research/agentos-process-model/sessions/T1-simple-qa.db T1 research/agentos-process-model/output`
- Expected: exit code 0，生成 `output/process_tree_T1.txt` 和 `output/process_tree_T1.dot`

### Task 8: 运行全量分析

- 目标：在所有 5 个 session 上运行分析脚本，产出汇总统计
- Files: Create `research/agentos-process-model/output/summary_stats.json`
- 验证范围：所有 session 分析通过，summary_stats.json 包含 T1-T5 的对比数据

- [ ] Step 1: 对所有 session 运行 analyze.py
- Run:
  ```
  for label in T1-simple-qa T2-multi-read T3-code-change T4-tool-intensive T5-multi-turn; do
    echo "=== $label ==="
    python3 research/agentos-process-model/scripts/analyze.py research/agentos-process-model/sessions/$label.db > research/agentos-process-model/output/stats_$label.json
  done
  ```
- Expected: 5 个 stats_*.json 文件生成，每个 exit code 0

- [ ] Step 2: 对所有 session 运行 process_tree.py
- Run:
  ```
  for label in T1-simple-qa T2-multi-read T3-code-change T4-tool-intensive T5-multi-turn; do
    python3 research/agentos-process-model/scripts/process_tree.py research/agentos-process-model/sessions/$label.db $label research/agentos-process-model/output
  done
  ```
- Expected: 10 个文件生成（5 个 .txt + 5 个 .dot），exit code 0

- [ ] Step 3: 生成汇总统计
- 用 analyze.py 的 `main()` 输出基础，手写一个 `summary_stats.json` 包含 T1-T5 的横向对比矩阵：
  ```json
  {
    "tasks": {
      "T1": {"total_processes": N, "tree_depth_max": N, "comm_types": N, "p50_lifetime_ms": N, ...},
      "T2": {...}, "T3": {...}, "T4": {...}, "T5": {...}
    }
  }
  ```
- Change: 手动汇总 5 个 stats_*.json 的关键指标，写入 `output/summary_stats.json`

- [ ] Step 4: 抽查验证 — 随机选 1 个 session，手工数 process_nodes 行数 vs 分析输出
- Run: `sqlite3 research/agentos-process-model/sessions/T1-simple-qa.db "SELECT COUNT(*) FROM process_nodes;"`
- Expected: 结果与 `stats_T1-simple-qa.json` 中的 total_processes 一致

### Task 9: 写观察报告

- 目标：整合全量分析结果，回答设计文档中的 3 个核心问题，提出对 agentOS 进程模型设计的初步启示
- Files: Create `research/agentos-process-model/report.md`
- 验证范围：报告覆盖 6 个对比维度，每个维度有数据支撑，对 3 个核心问题有明确回答或阶段性结论

- [ ] Step 1: 确认所有分析数据就绪
- Run: `ls research/agentos-process-model/output/stats_*.json research/agentos-process-model/output/summary_stats.json research/agentos-process-model/output/process_tree_*.txt`
- Expected: 5 个 stats_*.json + 1 个 summary_stats.json + 5 个 process_tree_*.txt

- [ ] Step 2: 写观察报告
- Change: 写入 `research/agentos-process-model/report.md`，结构如下：
  - **§1 实验概览**：T1-T5 任务说明、录制环境、总览数据
  - **§2 进程树拓扑**：各任务的进程树深度分布、根进程识别结果、T1-T5 横向对比
  - **§3 进程类型分布**：各任务的 comm 分布、跨任务共同类型、任务特定类型
  - **§4 进程存活时间**：各任务 p50/p95/max、短期 vs 长期进程的区分阈值、跨任务模式
  - **§5 退出状态分布**：各类型进程的 exit_code 分布、失败模式
  - **§6 进程-LLM 时序关联**：每 LLM 调用的进程密度、认知-行动相位的时序分离证据
  - **§7 父子关系模式**：top 10 父子对、意图节点 vs 通道节点的区分
  - **§8 对 agentOS 进程模型的启示**：回扣设计文档 3 个核心问题，给出阶段性结论和后续研究建议
  - **§9 已知限制**：引用设计文档中的限制，说明哪些结论受限于数据缺口

- [ ] Step 3: 自我验证 — 检查报告每个 § 是否有对应的量化数据支撑
- Run: 逐节核对报告中的数字与 `output/summary_stats.json` 一致
- Expected: 所有数字可追溯到 stats_*.json

### Task 10: 最终验证

- 目标：确认所有产出物完整、一致，可交付
- 验证范围：目录结构完整，文件清单与设计文档产出物清单匹配

- [ ] Step 1: 验证完整文件清单
- Run:
  ```
  find research/agentos-process-model -type f | sort
  ```
- Expected: 至少包含以下文件：
  - README.md
  - sessions/T1-simple-qa.db ~ T5-multi-turn.db（5 个）
  - scripts/analyze.py, scripts/process_tree.py（2 个）
  - output/stats_T1-simple-qa.json ~ stats_T5-multi-turn.json（5 个）、summary_stats.json（1 个）、process_tree_T1.txt ~ T5.txt（5 个）、process_tree_T1.dot ~ T5.dot（5 个）
  - report.md

- [ ] Step 2: 验证 report.md 覆盖 3 个核心问题
- Run: `grep -c "§" research/agentos-process-model/report.md`
- Expected: >= 9（报告各节 + 子节）

- [ ] Step 3: 验证分析脚本可在单个命令中复现
- Run: `python3 research/agentos-process-model/scripts/analyze.py research/agentos-process-model/sessions/T1-simple-qa.db | python3 -m json.tool > /dev/null`
- Expected: exit code 0

## 执行纪律

- 开始实现前，先批判性复查整份计划；如果发现缺项、矛盾、命名不一致或验证命令无效，先修计划
- 按任务顺序执行，不要无声跳步、合并步或改变任务目标
- 每完成一个任务，都运行该任务定义的验证
- T5 录制策略（`record -c claude` 附着）如果不可行 → 回退到多个 `record -- claude -p` session 模拟多轮（每轮独立 session，标记为 T5a-T5e），并在报告中说明方法学差异
- 如果 `claude -p` 在当前环境不可用或行为异常 → 确认 Claude Code 版本和 CLI 文档后再继续
- 如果某个 session 的 process_nodes 为空（eBPF 加载失败）→ 检查 `sudo` 权限和内核版本，回退到 `debug process` 单独验证
- 全部任务完成后，运行最终验证并输出修改摘要

## 最终验证

- Run: `find research/agentos-process-model -type f | sort && echo "---" && python3 -c "import json; [json.load(open(f'research/agentos-process-model/output/stats_{t}.json')) for t in ['T1-simple-qa','T2-multi-read','T3-code-change','T4-tool-intensive','T5-multi-turn']]" && echo "---" && wc -l research/agentos-process-model/report.md`
- Expected: 所有文件存在，5 个 stats JSON 均合法，report.md >100 行

## 审阅 Checkpoint

实施计划已写好并保存到 `.claude/docs/plans/2026-06-25-agentos-process-model-plan.md`。请先确认这份计划；如果没问题，下一步可以按计划逐任务执行。
