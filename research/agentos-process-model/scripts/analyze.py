#!/usr/bin/env python3
"""AgentSight session analyzer — extracts comparison dimensions from session.db.

Usage: python3 analyze.py <session.db>

Requires: Python 3.6+ (stdlib sqlite3 + json only)
Schema: collector/src/sinks/sqlite.rs  (NOTE: there is no `sessions` table;
        session metadata lives implicitly in llm_calls/process_nodes timestamps.)

Dimensions:
  1. session_info           — derived session span (no `sessions` table exists)
  2. process_tree_stats     — tree topology, depth, root vs missing-parent orphans
  3. process_type_distribution — comm type counts
  4. process_lifetime       — p50/p95/min/max + lifetime-based INFRA/ACTION split
  5. exit_status_distribution — exit_code by comm (incl. non-zero failures)
  6. llm_process_correlation — LLM↔process ±1s window correlation (kept for ref)
  7. parent_child_pairs     — top parent→child pairs
  8. startup_vs_action       — Phase 1 (bootstrap) vs Phase 2 (action) separation
  9. action_bursts           — post-LLM process creation bursts (one-time?)
  10. token_flow             — input/output/cache token pattern
  11. tool_analysis          — tool type distribution and process mapping
  12. resource_profile       — CPU/RSS timeline
  13. network_targets        — outbound connections
  14. concurrency            — max simultaneous processes
  15. event_timeline         — unified proc/llm/tool/in-process-IO timeline + key ratios
  16. subprocess_vs_inprocess — does steady-state tool work spawn subprocesses?
"""

import json
import sqlite3
import sys
from collections import Counter, defaultdict


def load_db(path: str) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
    except Exception as e:
        print(json.dumps({"error": f"cannot open database: {e}"}))
        sys.exit(1)


# ── D1: Session info (derived — no `sessions` table in schema) ───

def session_info(db: sqlite3.Connection) -> dict:
    """The SQLite schema has no `sessions` table. Derive session-level
    metadata from llm_calls + process_nodes timestamp spans instead."""
    def span(table, scol, ecol):
        try:
            r = db.execute(
                f"SELECT MIN({scol}) AS s, MAX({ecol}) AS e FROM {table}"
            ).fetchone()
            return r["s"], r["e"]
        except sqlite3.OperationalError:
            return None, None

    p_s, p_e = span("process_nodes", "start_timestamp_ms", "end_timestamp_ms")
    l_s, l_e = span("llm_calls", "start_timestamp_ms", "end_timestamp_ms")

    starts = [x for x in (p_s, l_s) if x is not None]
    ends = [x for x in (p_e, l_e) if x is not None]
    duration_s = None
    if starts and ends:
        duration_s = round((max(ends) - min(starts)) / 1000.0, 1)

    models = db.execute(
        "SELECT DISTINCT model FROM llm_calls WHERE model IS NOT NULL"
    ).fetchall()
    n_llm = db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    n_proc = db.execute("SELECT COUNT(*) FROM process_nodes").fetchone()[0]

    return {
        "schema_note": "no `sessions` table; metadata derived from timestamps",
        "duration_s": duration_s,
        "llm_calls": n_llm,
        "process_nodes": n_proc,
        "models": [m["model"] for m in models],
    }


# ── D2: Process tree stats ──────────────────────────────────────

def process_tree_stats(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT pid, ppid, comm, command, start_timestamp_ms FROM process_nodes"
    ).fetchall()

    if not rows:
        return {"total_processes": 0, "warning": "process_nodes table is empty"}

    pids = {r["pid"] for r in rows}

    # Classify every node whose ppid is not in the captured set.
    #   - the agent root (comm == claude) has a genuinely external ppid (the
    #     recorder / shell that launched it)
    #   - everything else (e.g. base64) is a MISSING-PARENT orphan: its ppid
    #     points at an unobserved intermediate that exited inside the eBPF
    #     race window. These are NOT additional roots of the agent tree.
    root_candidates = []
    missing_parent_orphans = []
    for r in rows:
        if r["ppid"] is None or r["ppid"] not in pids:
            entry = {
                "pid": r["pid"], "comm": r["comm"],
                "command": r["command"], "ppid": r["ppid"],
            }
            if r["comm"] == "claude":
                root_candidates.append(entry)
            else:
                missing_parent_orphans.append(entry)

    # Depth is computed from the agent root(s) only.
    seed = root_candidates if root_candidates else (
        # degenerate fallback: if no claude root captured, use all orphans
        [{"pid": r["pid"], "comm": r["comm"], "command": r["command"],
          "ppid": r["ppid"]} for r in rows
         if r["ppid"] is None or r["ppid"] not in pids]
    )

    parent_to_children = defaultdict(list)
    for r in rows:
        if r["ppid"] is not None:
            parent_to_children[r["ppid"]].append(r["pid"])

    depths = {}
    for rc in seed:
        depths[rc["pid"]] = 0

    queue = [rc["pid"] for rc in seed]
    visited = set(queue)
    while queue:
        pid = queue.pop(0)
        for child_pid in parent_to_children.get(pid, []):
            if child_pid not in visited:
                visited.add(child_pid)
                depths[child_pid] = depths.get(pid, 0) + 1
                queue.append(child_pid)

    depth_count = Counter(depths.values())
    unreachable = [pid for pid in pids if pid not in depths]

    orphan_comms = Counter(o["comm"] for o in missing_parent_orphans)

    return {
        "total_processes": len(rows),
        "depth_distribution": dict(sorted(depth_count.items())),
        "max_depth": max(depths.values()) if depths else 0,
        "agent_roots": root_candidates,
        "agent_root_count": len(root_candidates),
        "missing_parent_orphans": dict(orphan_comms),
        "missing_parent_orphan_count": len(missing_parent_orphans),
        "unreachable_pids": unreachable,
        "unreachable_count": len(unreachable),
    }


# ── D3: Process type distribution ───────────────────────────────

def process_type_distribution(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT comm FROM process_nodes").fetchall()
    counts = Counter(r["comm"] if r["comm"] else "unknown" for r in rows)
    total = sum(counts.values())
    dist = {comm: {"count": c, "pct": round(c / total * 100, 1)}
            for comm, c in counts.most_common()}
    return {"total": total, "unique_types": len(counts), "distribution": dist}


# ── D4: Process lifetime ────────────────────────────────────────

def process_lifetime(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT pid, comm, start_timestamp_ms, end_timestamp_ms FROM process_nodes"
    ).fetchall()

    lifetimes = []
    missing_start = 0
    missing_end = 0
    missing_both = 0

    for r in rows:
        s = r["start_timestamp_ms"]
        e = r["end_timestamp_ms"]
        if s is None and e is None:
            missing_both += 1
            continue
        if s is None:
            missing_start += 1
            continue
        if e is None:
            missing_end += 1
            continue
        lifetimes.append((r["pid"], r["comm"], e - s))

    if not lifetimes:
        return {"computable": 0, "missing_start": missing_start,
                "missing_end": missing_end, "missing_both": missing_both,
                "total_rows": len(rows), "warning": "no computable lifetimes"}

    values = sorted(v[2] for v in lifetimes)

    def percentile(data, p):
        if not data: return 0
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]

    # Per-comm lifetime distribution
    by_comm = defaultdict(list)
    for pid, comm, dur in lifetimes:
        by_comm[comm or "unknown"].append(dur)

    comm_stats = {}
    for comm, durs in sorted(by_comm.items()):
        sv = sorted(durs)
        p50 = sv[len(sv)//2]
        # Classify by MEASURED lifetime, not comm name. A process is INFRA
        # only if it actually lives long (>1s). NOTE: claude.exe lives 1-7ms
        # and is therefore ACTION, not INFRA — only `claude` itself is INFRA.
        role = "INFRA" if p50 > 1000 else "ACTION"
        comm_stats[comm] = {
            "count": len(sv), "p50_ms": p50,
            "max_ms": max(sv), "role": role,
        }

    return {
        "computable": len(lifetimes),
        "min_ms": min(values), "max_ms": max(values),
        "mean_ms": round(sum(values) / len(values), 1),
        "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95),
        "missing_start": missing_start, "missing_end": missing_end,
        "missing_both": missing_both, "total_rows": len(rows),
        "by_comm": comm_stats,
    }


# ── D5: Exit status distribution ────────────────────────────────

def exit_status_distribution(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT comm, exit_code FROM process_nodes").fetchall()
    by_comm = defaultdict(lambda: defaultdict(int))
    failures = Counter()
    n_exited = 0
    n_nonzero = 0
    for r in rows:
        comm = r["comm"] if r["comm"] else "unknown"
        if r["exit_code"] is None:
            code = "still_running"
        else:
            code = str(r["exit_code"])
            n_exited += 1
            if r["exit_code"] != 0:
                n_nonzero += 1
                failures[f"{comm}={r['exit_code']}"] += 1
        by_comm[comm][code] += 1
    return {
        "by_comm": {comm: dict(codes) for comm, codes in sorted(by_comm.items())},
        "exited_count": n_exited,
        "nonzero_exit_count": n_nonzero,
        "nonzero_exits": dict(failures),
        "failure_rate_pct": round(n_nonzero / n_exited * 100, 1) if n_exited else None,
    }


# ── D6: LLM↔process correlation ─────────────────────────────────

def llm_process_correlation(db: sqlite3.Connection) -> dict:
    llm_rows = db.execute(
        "SELECT id, start_timestamp_ms, end_timestamp_ms FROM llm_calls"
    ).fetchall()

    proc_rows = db.execute(
        "SELECT pid, start_timestamp_ms, end_timestamp_ms FROM process_nodes"
    ).fetchall()

    if not llm_rows:
        return {"llm_calls": 0, "warning": "llm_calls table is empty"}

    total_associated = 0
    llm_call_details = []
    null_end_fallback = 0

    for lr in llm_rows:
        s = lr["start_timestamp_ms"]
        e = lr["end_timestamp_ms"]
        if e is None:
            e = s
            null_end_fallback += 1

        window_start = s - 1000
        window_end = e + 1000
        in_window = [
            pr["pid"] for pr in proc_rows
            if pr["start_timestamp_ms"] is not None
            and window_start <= pr["start_timestamp_ms"] <= window_end
        ]
        total_associated += len(in_window)
        llm_call_details.append({
            "llm_call_id": lr["id"],
            "window_start_ms": window_start,
            "window_end_ms": window_end,
            "processes_in_window": len(in_window),
        })

    return {
        "llm_calls": len(llm_rows),
        "null_end_fallback_count": null_end_fallback,
        "total_associated_processes": total_associated,
        "avg_processes_per_call": round(total_associated / len(llm_rows), 1),
        "per_call_detail": llm_call_details[:20],
    }


# ── D7: Parent-child pairs ──────────────────────────────────────

def parent_child_pairs(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT c.pid AS child_pid, c.comm AS child_comm, "
        "p.pid AS parent_pid, p.comm AS parent_comm "
        "FROM process_nodes c "
        "JOIN process_nodes p ON c.ppid = p.pid"
    ).fetchall()

    pair_counts = Counter()
    for r in rows:
        pair = f"{r['parent_comm'] or '?'} → {r['child_comm'] or '?'}"
        pair_counts[pair] += 1

    top = [{"pair": pair, "count": c} for pair, c in pair_counts.most_common(10)]
    return {"total_pairs": sum(pair_counts.values()), "top_10": top}


# ── D8: Startup vs Action phase separation ──────────────────────

def startup_vs_action(db: sqlite3.Connection) -> dict:
    """Separate processes into Phase 1 (bootstrap, before first LLM call)
    and Phase 2 (action, after first LLM call completes)."""
    first_llm_row = db.execute(
        "SELECT start_timestamp_ms, end_timestamp_ms FROM llm_calls ORDER BY start_timestamp_ms LIMIT 1"
    ).fetchone()

    if not first_llm_row:
        return {"warning": "no LLM calls"}

    first_llm_start = first_llm_row["start_timestamp_ms"]
    first_llm_end = first_llm_row["end_timestamp_ms"] or first_llm_start

    procs = db.execute(
        "SELECT pid, comm, start_timestamp_ms FROM process_nodes WHERE start_timestamp_ms IS NOT NULL ORDER BY start_timestamp_ms"
    ).fetchall()

    before = [p for p in procs if p["start_timestamp_ms"] < first_llm_start]
    during = [p for p in procs if first_llm_start <= p["start_timestamp_ms"] <= first_llm_end]
    after = [p for p in procs if p["start_timestamp_ms"] > first_llm_end]

    def phase_summary(proc_list):
        comms = Counter(p["comm"] for p in proc_list)
        timing = {"first_ms": proc_list[0]["start_timestamp_ms"] if proc_list else None,
                  "last_ms": proc_list[-1]["start_timestamp_ms"] if proc_list else None}
        if proc_list and timing["first_ms"]:
            timing["span_ms"] = timing["last_ms"] - timing["first_ms"]
        return {"count": len(proc_list), "comms": dict(comms.most_common()),
                "timing": timing}

    return {
        "first_llm_start_ms": first_llm_start,
        "first_llm_end_ms": first_llm_end,
        "first_llm_dur_ms": first_llm_end - first_llm_start,
        "phase_startup": phase_summary(before),
        "phase_llm_during": phase_summary(during),
        "phase_action": phase_summary(after),
    }


# ── D9: Action bursts ───────────────────────────────────────────

def action_bursts(db: sqlite3.Connection) -> dict:
    """Identify process creation bursts in the action phase.
    A burst = processes within 100ms of each other."""
    first_llm_row = db.execute(
        "SELECT start_timestamp_ms, end_timestamp_ms FROM llm_calls ORDER BY start_timestamp_ms LIMIT 1"
    ).fetchone()

    if not first_llm_row:
        return {"warning": "no LLM calls"}

    first_llm_end = first_llm_row["end_timestamp_ms"] or first_llm_row["start_timestamp_ms"]

    procs = db.execute(
        "SELECT pid, comm, start_timestamp_ms FROM process_nodes "
        "WHERE start_timestamp_ms IS NOT NULL AND start_timestamp_ms > ? "
        "ORDER BY start_timestamp_ms",
        (first_llm_end,)
    ).fetchall()

    if not procs:
        return {"bursts": [], "total_action_procs": 0}

    # Cluster by 100ms proximity
    bursts = []
    current = [procs[0]]
    for i in range(1, len(procs)):
        if procs[i]["start_timestamp_ms"] - current[-1]["start_timestamp_ms"] < 100:
            current.append(procs[i])
        else:
            bursts.append(current)
            current = [procs[i]]
    bursts.append(current)

    burst_summaries = []
    for i, b in enumerate(bursts):
        comms = Counter(p["comm"] for p in b)
        t0 = b[0]["start_timestamp_ms"] - first_llm_end
        span = b[-1]["start_timestamp_ms"] - b[0]["start_timestamp_ms"]
        burst_summaries.append({
            "index": i, "count": len(b),
            "offset_from_first_llm_end_ms": t0,
            "span_ms": span,
            "comms": dict(comms.most_common()),
        })

    return {"total_action_procs": len(procs), "num_bursts": len(bursts),
            "bursts": burst_summaries}


# ── D10: Token flow ─────────────────────────────────────────────

def token_flow(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT model, input_tokens, output_tokens, cache_creation_tokens, "
        "cache_read_tokens, total_tokens FROM token_usage ORDER BY timestamp_ms"
    ).fetchall()

    if not rows:
        return {"token_records": 0, "warning": "token_usage table is empty"}

    total_in = sum(r["input_tokens"] or 0 for r in rows)
    total_out = sum(r["output_tokens"] or 0 for r in rows)
    total_cache_create = sum(r["cache_creation_tokens"] or 0 for r in rows)
    total_cache_read = sum(r["cache_read_tokens"] or 0 for r in rows)
    models = Counter(r["model"] for r in rows)

    per_call = []
    for i, r in enumerate(rows):
        per_call.append({
            "index": i, "model": r["model"],
            "input": r["input_tokens"], "output": r["output_tokens"],
            "total": r["total_tokens"],
            "cache_read": r["cache_read_tokens"],
            "cache_create": r["cache_creation_tokens"],
        })

    return {
        "token_records": len(rows),
        "models": dict(models),
        "total_input": total_in, "total_output": total_out,
        "output_ratio": round(total_out / total_in, 3) if total_in else 0,
        "total_cache_create": total_cache_create,
        "total_cache_read": total_cache_read,
        "cache_hit_rate": round(total_cache_read / (total_cache_create + total_cache_read) * 100, 1)
        if (total_cache_create + total_cache_read) > 0 else None,
        "per_call": per_call,
    }


# ── D11: Tool analysis ──────────────────────────────────────────

def tool_analysis(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT tool_name, duration_ms, related_pid, status FROM tool_calls"
    ).fetchall()

    if not rows:
        return {"tool_calls": 0, "warning": "tool_calls table is empty"}

    tools = Counter(r["tool_name"] or "unknown" for r in rows)
    statuses = Counter(r["status"] or "unknown" for r in rows)

    # Tool→process mapping
    with_pid = sum(1 for r in rows if r["related_pid"] is not None)
    without_pid = sum(1 for r in rows if r["related_pid"] is None)

    # Join to get comm of related process
    linked = db.execute(
        "SELECT t.tool_name, p.comm as proc_comm FROM tool_calls t "
        "JOIN process_nodes p ON t.related_pid = p.pid"
    ).fetchall()
    tool_proc_pairs = Counter(f"{r['tool_name']} → {r['proc_comm']}" for r in linked)

    # Duration by tool type
    durations = defaultdict(list)
    for r in rows:
        if r["duration_ms"] is not None:
            durations[r["tool_name"] or "unknown"].append(r["duration_ms"])

    dur_stats = {}
    for t, ds in durations.items():
        sv = sorted(ds)
        dur_stats[t] = {"p50_ms": sv[len(sv)//2], "max_ms": max(sv), "count": len(sv)}

    return {
        "tool_calls": len(rows),
        "unique_tools": len(tools),
        "distribution": dict(tools.most_common()),
        "statuses": dict(statuses),
        "with_related_pid": with_pid,
        "without_related_pid": without_pid,
        "tool_proc_pairs": dict(tool_proc_pairs.most_common(10)),
        "duration_by_tool": dur_stats,
    }


# ── D12: Resource profile ───────────────────────────────────────

def resource_profile(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT timestamp_ms, cpu_percent, rss_mb, comm FROM resource_samples ORDER BY timestamp_ms"
    ).fetchall()

    if not rows:
        return {"samples": 0, "warning": "resource_samples table is empty"}

    cpu_vals = [r["cpu_percent"] for r in rows if r["cpu_percent"] is not None]
    rss_vals = [r["rss_mb"] for r in rows if r["rss_mb"] is not None]
    comms = Counter(r["comm"] or "?" for r in rows)

    cpu_sorted = sorted(cpu_vals)
    rss_sorted = sorted(rss_vals)

    return {
        "samples": len(rows),
        "comms": dict(comms),
        "cpu": {"p50": cpu_sorted[len(cpu_sorted)//2] if cpu_sorted else None,
                "max": max(cpu_vals) if cpu_vals else None},
        "rss": {"first_mb": rss_vals[0] if rss_vals else None,
                "last_mb": rss_vals[-1] if rss_vals else None,
                "max_mb": max(rss_vals) if rss_vals else None,
                "delta_mb": rss_vals[-1] - rss_vals[0] if len(rss_vals) >= 2 else None},
    }


# ── D13: Network targets ────────────────────────────────────────

def network_targets(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT host, path, count, error_count, comm FROM network_targets"
    ).fetchall()

    if not rows:
        return {"targets": 0, "warning": "network_targets table is empty"}

    targets = []
    for r in rows:
        targets.append({
            "host": r["host"], "path": r["path"],
            "count": r["count"], "error_count": r["error_count"],
            "comm": r["comm"],
        })

    # Group by category
    categories = defaultdict(int)
    for r in rows:
        host = r["host"] or ""
        if "deepseek" in host:
            categories["llm_api"] += r["count"]
        elif "mcp-registry" in (r["path"] or ""):
            categories["mcp_registry"] += r["count"]
        elif "event_logging" in (r["path"] or ""):
            categories["telemetry"] += r["count"]
        elif "downloads" in host or "plugins" in (r["path"] or ""):
            categories["plugins"] += r["count"]
        else:
            categories["other"] += r["count"]

    return {"targets": len(rows), "categories": dict(categories), "detail": targets}


# ── D14: Concurrency ────────────────────────────────────────────

def concurrency(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT pid, start_timestamp_ms, end_timestamp_ms FROM process_nodes "
        "WHERE start_timestamp_ms IS NOT NULL"
    ).fetchall()

    if not rows:
        return {"warning": "no process timestamps"}

    events = []
    for r in rows:
        s = r["start_timestamp_ms"]
        e = r["end_timestamp_ms"] if r["end_timestamp_ms"] is not None else s + 1
        events.append((s, 1))
        events.append((e, -1))
    events.sort()

    concurrent = 0
    max_c = 0
    for t, delta in events:
        concurrent += delta
        if concurrent > max_c:
            max_c = concurrent

    return {"max_concurrent": max_c, "total_processes": len(rows)}


# ── D15: Unified event timeline ─────────────────────────────────

def event_timeline(db: sqlite3.Connection) -> dict:
    """Merge process-exec / llm-call / tool-call / in-process file-IO events
    onto one timeline. Answers the core question: after the one-time process
    burst stops, does the agent keep doing work (LLM calls, tool calls, file
    writes) WITHOUT spawning new subprocesses?"""
    base_row = db.execute(
        "SELECT MIN(start_timestamp_ms) AS b FROM process_nodes"
    ).fetchone()
    if not base_row or base_row["b"] is None:
        return {"warning": "no process timestamps"}
    base = base_row["b"]

    # last subprocess exec (process_nodes start is the exec moment)
    last_proc = db.execute(
        "SELECT MAX(start_timestamp_ms) AS m FROM process_nodes"
    ).fetchone()["m"]

    last_llm = db.execute(
        "SELECT MAX(start_timestamp_ms) AS m FROM llm_calls"
    ).fetchone()["m"]

    last_tool = db.execute(
        "SELECT MAX(start_timestamp_ms) AS m FROM tool_calls"
    ).fetchone()
    last_tool = last_tool["m"] if last_tool else None

    # in-process IO actors: file audit events whose comm is the agent itself
    # or a "Bun Pool N" worker thread (Claude Code = Bun runtime thread pool)
    file_rows = db.execute(
        "SELECT timestamp_ms, comm FROM audit_events WHERE audit_type='file' "
        "ORDER BY timestamp_ms"
    ).fetchall()
    inproc_comms = Counter()
    file_after_last_proc = 0
    last_file = None
    for r in file_rows:
        comm = r["comm"] or "?"
        inproc_comms[comm] += 1
        last_file = r["timestamp_ms"]
        if last_proc is not None and r["timestamp_ms"] > last_proc + 500:
            file_after_last_proc += 1

    session_end = max(x for x in (last_proc, last_llm, last_tool, last_file)
                      if x is not None)

    # "post-burst tail" = time the session keeps working after the last
    # subprocess was spawned. If large, steady-state work is in-process.
    post_burst_tail_ms = session_end - last_proc if last_proc else None

    return {
        "base_ms": base,
        "last_subprocess_exec_offset_ms": (last_proc - base) if last_proc else None,
        "last_llm_call_offset_ms": (last_llm - base) if last_llm else None,
        "last_tool_call_offset_ms": (last_tool - base) if last_tool else None,
        "session_end_offset_ms": session_end - base,
        "post_burst_tail_ms": post_burst_tail_ms,
        "in_process_io_actors": dict(inproc_comms.most_common()),
        "file_writes_after_last_subprocess": file_after_last_proc,
    }


# ── D16: Subprocess vs in-process tool execution ────────────────

def subprocess_vs_inprocess(db: sqlite3.Connection) -> dict:
    """Test whether tool execution maps 1:1 to subprocess spawning, or whether
    the bulk of tool calls execute in-process (no fork). Compares the count of
    tool calls / LLM calls AFTER the process burst ends vs subprocesses spawned
    after it."""
    n_tools = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    n_llm = db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]

    # The burst window: cluster process starts; the burst is the last cluster.
    procs = db.execute(
        "SELECT start_timestamp_ms FROM process_nodes "
        "WHERE start_timestamp_ms IS NOT NULL ORDER BY start_timestamp_ms"
    ).fetchall()
    if not procs:
        return {"warning": "no processes"}

    starts = [p["start_timestamp_ms"] for p in procs]
    # find clusters separated by >1s gaps
    clusters = [[starts[0]]]
    for s in starts[1:]:
        if s - clusters[-1][-1] > 1000:
            clusters.append([s])
        else:
            clusters[-1].append(s)
    last_cluster_end = clusters[-1][-1]

    # tool/llm calls strictly after the last subprocess cluster
    tools_after = db.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE start_timestamp_ms > ?",
        (last_cluster_end + 500,)
    ).fetchone()[0]
    llm_after = db.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE start_timestamp_ms > ?",
        (last_cluster_end + 500,)
    ).fetchone()[0]
    procs_after = db.execute(
        "SELECT COUNT(*) FROM process_nodes WHERE start_timestamp_ms > ?",
        (last_cluster_end + 500,)
    ).fetchone()[0]

    return {
        "num_process_clusters": len(clusters),
        "cluster_sizes": [len(c) for c in clusters],
        "total_tool_calls": n_tools,
        "total_llm_calls": n_llm,
        "tool_calls_after_last_cluster": tools_after,
        "llm_calls_after_last_cluster": llm_after,
        "subprocesses_after_last_cluster": procs_after,
        "interpretation": (
            "steady-state work is IN-PROCESS (tool/llm calls continue with no "
            "new subprocesses)" if procs_after == 0 and (tools_after + llm_after) > 0
            else "subprocesses continue past the burst"
        ),
    }


# ── Main ─────────────────────────────────────────────────────────

def main(db_path: str):
    db = load_db(db_path)

    result = {
        "session_info": session_info(db),
        "process_tree_stats": process_tree_stats(db),
        "process_type_distribution": process_type_distribution(db),
        "process_lifetime": process_lifetime(db),
        "exit_status_distribution": exit_status_distribution(db),
        "llm_process_correlation": llm_process_correlation(db),
        "parent_child_pairs": parent_child_pairs(db),
        "startup_vs_action": startup_vs_action(db),
        "action_bursts": action_bursts(db),
        "token_flow": token_flow(db),
        "tool_analysis": tool_analysis(db),
        "resource_profile": resource_profile(db),
        "network_targets": network_targets(db),
        "concurrency": concurrency(db),
        "event_timeline": event_timeline(db),
        "subprocess_vs_inprocess": subprocess_vs_inprocess(db),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <session.db>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
