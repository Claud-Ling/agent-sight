#!/usr/bin/env python3
"""AgentSight session analyzer — extracts 6 comparison dimensions from session.db.

Usage: python3 analyze.py <session.db>

Requires: Python 3.6+ (stdlib sqlite3 + json only)
Schema: collector/src/sinks/sqlite.rs
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


def session_info(db: sqlite3.Connection) -> dict:
    """Extract session-level metadata."""
    try:
        rows = db.execute(
            "SELECT agent_type, model, total_tokens, start_timestamp_ms, end_timestamp_ms FROM sessions"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"sessions": [], "warning": "sessions table not found"}

    sessions = []
    for r in rows:
        sessions.append({
            "agent_type": r["agent_type"],
            "model": r["model"],
            "total_tokens": r["total_tokens"],
            "duration_s": (
                round((r["end_timestamp_ms"] - r["start_timestamp_ms"]) / 1000.0, 1)
                if r["end_timestamp_ms"] and r["start_timestamp_ms"] else None
            ),
        })
    return {"sessions": sessions}


def process_tree_stats(db: sqlite3.Connection) -> dict:
    """Reconstruct process tree topology: total count, depth distribution, root candidates."""
    rows = db.execute(
        "SELECT pid, ppid, comm, command, start_timestamp_ms FROM process_nodes"
    ).fetchall()

    if not rows:
        return {"total_processes": 0, "warning": "process_nodes table is empty"}

    pids = {r["pid"] for r in rows}
    pid_info = {r["pid"]: dict(r) for r in rows}

    # Identify root candidates: ppid not in the pid set
    root_candidates = []
    for r in rows:
        if r["ppid"] is None or r["ppid"] not in pids:
            root_candidates.append({
                "pid": r["pid"],
                "comm": r["comm"],
                "command": r["command"],
                "ppid": r["ppid"],
            })

    # Compute depth for each process via BFS from root candidates
    depth_count = Counter()
    depths = {}
    bfs_pids = set()

    for rc in root_candidates:
        depths[rc["pid"]] = 0
        bfs_pids.add(rc["pid"])

    # BFS: expand children
    parent_to_children = defaultdict(list)
    for r in rows:
        if r["ppid"] is not None:
            parent_to_children[r["ppid"]].append(r["pid"])

    queue = [rc["pid"] for rc in root_candidates]
    visited = set(queue)
    while queue:
        pid = queue.pop(0)
        for child_pid in parent_to_children.get(pid, []):
            if child_pid not in visited:
                visited.add(child_pid)
                depths[child_pid] = depths.get(pid, 0) + 1
                queue.append(child_pid)

    for pid, depth in depths.items():
        depth_count[depth] += 1

    # Count processes not reached from roots
    unreachable = [pid for pid in pids if pid not in depths]

    return {
        "total_processes": len(rows),
        "depth_distribution": dict(sorted(depth_count.items())),
        "max_depth": max(depths.values()) if depths else 0,
        "root_candidates": root_candidates,
        "unreachable_pids": unreachable,
        "unreachable_count": len(unreachable),
    }


def process_type_distribution(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT comm FROM process_nodes").fetchall()
    counts = Counter(r["comm"] if r["comm"] else "unknown" for r in rows)
    total = sum(counts.values())
    dist = {comm: {"count": c, "pct": round(c / total * 100, 1)}
            for comm, c in counts.most_common()}
    return {"total": total, "unique_types": len(counts), "distribution": dist}


def process_lifetime(db: sqlite3.Connection) -> dict:
    """Compute process lifetime stats for processes with both start and end timestamps."""
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
        return {
            "computable": 0,
            "missing_start": missing_start,
            "missing_end": missing_end,
            "missing_both": missing_both,
            "total_rows": len(rows),
            "warning": "no computable lifetimes",
        }

    values = sorted(v[2] for v in lifetimes)

    def percentile(data, p):
        if not data:
            return 0
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]

    return {
        "computable": len(lifetimes),
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": round(sum(values) / len(values), 1),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "missing_start": missing_start,
        "missing_end": missing_end,
        "missing_both": missing_both,
        "total_rows": len(rows),
    }


def exit_status_distribution(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        "SELECT comm, exit_code FROM process_nodes"
    ).fetchall()

    by_comm = defaultdict(lambda: defaultdict(int))
    for r in rows:
        comm = r["comm"] if r["comm"] else "unknown"
        code = str(r["exit_code"]) if r["exit_code"] is not None else "still_running"
        by_comm[comm][code] += 1

    return {
        "by_comm": {comm: dict(codes) for comm, codes in sorted(by_comm.items())},
    }


def llm_process_correlation(db: sqlite3.Connection) -> dict:
    """Correlate LLM calls with process create/destroy events within ±1s window."""
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
            e = s  # fallback: use start as window center
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
        "per_call_detail": llm_call_details[:20],  # truncate for readability
    }


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
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <session.db>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
