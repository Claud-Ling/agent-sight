#!/usr/bin/env python3
"""Process tree reconstruction and visualization from session.db.

Usage: python3 process_tree.py <session.db> <label> <output_dir>

Outputs:
  <output_dir>/process_tree_<label>.txt   ASCII art tree
  <output_dir>/process_tree_<label>.dot   Graphviz DOT (color by comm type)
"""

from __future__ import annotations
import json
import sqlite3
import sys
from collections import defaultdict
from typing import List, Dict


COMM_COLORS = {
    "claude": "#4A90D9",
    "node": "#50B86C",
    "bash": "#E06C75",
    "sh": "#E5C07B",
    "git": "#F44747",
    "python": "#3572A5",
    "python3": "#3572A5",
    "grep": "#98C379",
    "find": "#61AFEF",
    "npm": "#CB3837",
    "npx": "#CB3837",
    "cargo": "#DEA584",
    "rustc": "#DEA584",
    "make": "#C678DD",
    "gcc": "#C678DD",
    "cat": "#ABB2BF",
    "ls": "#ABB2BF",
    "head": "#ABB2BF",
    "sed": "#ABB2BF",
    "awk": "#ABB2BF",
    "sort": "#ABB2BF",
    "chmod": "#D19A66",
    "mkdir": "#D19A66",
    "cp": "#D19A66",
    "mv": "#D19A66",
    "rm": "#D19A66",
    "curl": "#56B6C2",
    "docker": "#0DB7ED",
    "which": "#ABB2BF",
    "unknown": "#ABB2BF",
}
FALLBACK_COLOR = "#ABB2BF"


def load_db(path: str) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
    except Exception as e:
        print(f"Error: cannot open database: {e}", file=sys.stderr)
        sys.exit(1)


def build_tree(db: sqlite3.Connection) -> list[dict]:
    """Build forest of process trees. Returns list of root nodes."""
    rows = db.execute(
        "SELECT pid, ppid, comm, start_timestamp_ms FROM process_nodes"
    ).fetchall()

    if not rows:
        return []

    pids = {r["pid"] for r in rows}
    nodes = {}
    for r in rows:
        nodes[r["pid"]] = {
            "pid": r["pid"],
            "ppid": r["ppid"],
            "comm": r["comm"] or "?",
            "start_ms": r["start_timestamp_ms"],
            "children": [],
        }

    # Find root nodes: ppid not in our pid set
    roots = []
    parent_to_children = defaultdict(list)
    for r in rows:
        if r["ppid"] is not None and r["ppid"] in nodes:
            parent_to_children[r["ppid"]].append(r["pid"])
        if r["ppid"] is None or r["ppid"] not in pids:
            roots.append(r["pid"])

    # Build tree structure
    for pid, children in parent_to_children.items():
        nodes[pid]["children"] = [nodes[c] for c in children]
        nodes[pid]["children"].sort(key=lambda n: n.get("start_ms") or 0)

    return [nodes[pid] for pid in sorted(roots, key=lambda p: nodes[p].get("start_ms") or 0)]


def tree_to_ascii(roots: list[dict]) -> str:
    """Render forest as ASCII art using Unicode box-drawing characters."""
    if not roots:
        return "(empty process tree)"

    lines = []

    def render(node, prefix, is_last, depth):
        if depth > 10:  # safety limit
            lines.append(f"{prefix}(tree truncated at depth 10)")
            return
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{node['pid']}:{node['comm']}")
        extension = "    " if is_last else "│   "
        children = node.get("children", [])
        for i, child in enumerate(children):
            render(child, prefix + extension, i == len(children) - 1, depth + 1)

    for i, root in enumerate(roots):
        render(root, "", i == len(roots) - 1, 0)

    return "\n".join(lines)


def tree_to_dot(roots: list[dict]) -> str:
    """Render forest as Graphviz DOT with comm-type coloring."""
    lines = ["digraph process_tree {",
             "  rankdir=TB;",
             "  node [shape=box, style=rounded, fontname=monospace];",
             "  edge [fontname=monospace, fontsize=10];"]

    node_ids = set()

    def add_node(node):
        pid = node["pid"]
        if pid in node_ids:
            return
        node_ids.add(pid)
        comm = node.get("comm", "?")
        color = COMM_COLORS.get(comm, FALLBACK_COLOR)
        lines.append(f'  {pid} [label="{pid}\\n{comm}" fillcolor="{color}" '
                     f'style="filled,rounded" fontcolor="white"];')
        for child in node.get("children", []):
            add_node(child)
            lines.append(f"  {pid} -> {child['pid']};")

    for root in roots:
        add_node(root)

    lines.append("}")
    return "\n".join(lines)


def main(db_path: str, label: str, output_dir: str):
    db = load_db(db_path)
    roots = build_tree(db)
    db.close()

    ascii_text = tree_to_ascii(roots)
    dot_text = tree_to_dot(roots)

    txt_path = f"{output_dir}/process_tree_{label}.txt"
    dot_path = f"{output_dir}/process_tree_{label}.dot"

    with open(txt_path, "w") as f:
        f.write(f"# Process Tree: {label}\n")
        f.write(f"# Root processes: {len(roots)}\n")
        f.write(f"# Total processes in tree: {sum(len(r.get('children', [])) + 1 for r in roots)}\n\n")
        f.write(ascii_text)
        f.write("\n")

    with open(dot_path, "w") as f:
        f.write(dot_text)
        f.write("\n")

    print(f"Wrote {txt_path}")
    print(f"Wrote {dot_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <session.db> <label> <output_dir>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
