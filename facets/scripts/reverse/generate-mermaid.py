#!/usr/bin/env python3
"""Generate Mermaid graph syntax from a JSON imports/dependency description."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

VALID_DIRECTIONS = ("TD", "LR", "BT", "RL")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert JSON dependency graph to Mermaid syntax.")
    parser.add_argument("imports_json", help="Path to input JSON file, or '-' for stdin.")
    parser.add_argument(
        "--direction",
        default="TD",
        choices=VALID_DIRECTIONS,
        help="Graph direction (default: TD).",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=None,
        metavar="N",
        help="Limit to N nodes with highest degree.",
    )
    parser.add_argument(
        "--cluster",
        action="store_true",
        help="Group nodes by 'module' field into subgraph blocks.",
    )
    return parser.parse_args()


def load_json(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_label(node: dict[str, Any]) -> str:
    if "label" in node:
        return node["label"]
    return Path(node["id"]).stem


def escape_label(label: str) -> str:
    import re

    escaped = label.replace("\\", "\\\\").replace('"', '\\"')
    # Prevent Mermaid syntax injection via newlines/control chars breaking the
    # single-line node definition (untrusted module names, see EV-15).
    return re.sub(r"[\x00-\x1f\x7f]", "", escaped)


def sanitize_cluster_name(name: str) -> str:
    # Prevent Mermaid syntax injection via user-supplied module field values.
    import re

    stripped = re.sub(r"[\x00-\x1f\x7f]", "", name)
    sanitized = re.sub(r"[^A-Za-z0-9_\-\./ ]", "_", stripped).strip()
    return sanitized if sanitized else "uncategorized"


def compute_degrees(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    node_ids = {n["id"] for n in nodes}
    degrees: dict[str, int] = defaultdict(int)
    for edge in edges:
        src, dst = edge.get("from", ""), edge.get("to", "")
        if src in node_ids:
            degrees[src] += 1
        if dst in node_ids:
            degrees[dst] += 1
    return dict(degrees)


def filter_nodes(
    nodes: list[dict], edges: list[dict], max_nodes: int | None
) -> tuple[list[dict], list[dict]]:
    if max_nodes is None:
        return nodes, edges
    degrees = compute_degrees(nodes, edges)
    # Stable sort: highest degree first, ties by original order
    ranked = sorted(
        range(len(nodes)),
        key=lambda i: -degrees.get(nodes[i]["id"], 0),
    )
    kept_ids = {nodes[i]["id"] for i in ranked[:max_nodes]}
    filtered_nodes = [n for n in nodes if n["id"] in kept_ids]
    filtered_edges = [e for e in edges if e.get("from") in kept_ids and e.get("to") in kept_ids]
    return filtered_nodes, filtered_edges


def detect_cycles(node_ids: set[str], edges: list[dict]) -> set[tuple[str, str]]:
    """Return set of (from, to) pairs that form back-edges (cycle indicators) via DFS."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        src, dst = edge.get("from", ""), edge.get("to", "")
        if src in node_ids and dst in node_ids:
            adjacency[src].append(dst)

    visited: set[str] = set()
    in_stack: set[str] = set()
    cycle_edges: set[tuple[str, str]] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        in_stack.add(node)
        for neighbor in adjacency[node]:
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in in_stack:
                cycle_edges.add((node, neighbor))
        in_stack.discard(node)

    for nid in node_ids:
        if nid not in visited:
            dfs(nid)

    return cycle_edges


def mark_cycle_edges(edges: list[dict], cycle_set: set[tuple[str, str]]) -> list[dict]:
    result = []
    for edge in edges:
        pair = (edge.get("from", ""), edge.get("to", ""))
        if pair in cycle_set and "kind" not in edge:
            result.append({**edge, "kind": "cycle"})
        else:
            result.append(edge)
    return result


def build_node_index(nodes: list[dict]) -> dict[str, str]:
    return {node["id"]: f"N{i}" for i, node in enumerate(nodes)}


def _emit_flat_nodes(nodes: list[dict], node_index: dict[str, str], indent: str) -> list[str]:
    lines = []
    for node in nodes:
        nid = node_index[node["id"]]
        label = escape_label(resolve_label(node))
        lines.append(f'{indent}{nid}["{label}"]')
    return lines


def _emit_clustered_nodes(nodes: list[dict], node_index: dict[str, str], indent: str) -> list[str]:
    clusters: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        key = node.get("module") or "uncategorized"
        clusters[key].append(node)
    lines = []
    for cluster_name, cluster_nodes in clusters.items():
        safe_name = sanitize_cluster_name(cluster_name)
        lines.append(f'{indent}subgraph "{safe_name}"')
        lines.extend(_emit_flat_nodes(cluster_nodes, node_index, indent + "  "))
        lines.append(f"{indent}end")
    return lines


def emit_mermaid(
    direction: str,
    nodes: list[dict],
    edges: list[dict],
    node_index: dict[str, str],
    cluster: bool,
) -> str:
    lines = [f"graph {direction}"]
    indent = "  "

    if cluster:
        lines.extend(_emit_clustered_nodes(nodes, node_index, indent))
    else:
        lines.extend(_emit_flat_nodes(nodes, node_index, indent))

    valid_ids = set(node_index.keys())
    for edge in edges:
        src_id, dst_id = edge.get("from", ""), edge.get("to", "")
        if src_id not in valid_ids or dst_id not in valid_ids:
            print(
                f"Warning: dropping edge {src_id!r} -> {dst_id!r} (unknown node)", file=sys.stderr
            )
            continue
        src_n, dst_n = node_index[src_id], node_index[dst_id]
        arrow = "-.->" if edge.get("kind") == "cycle" else "-->"
        lines.append(f"{indent}{src_n} {arrow} {dst_n}")

    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    data = load_json(args.imports_json)

    nodes: list[dict] = data.get("nodes", [])
    edges: list[dict] = data.get("edges", [])

    nodes, edges = filter_nodes(nodes, edges, args.max_nodes)

    node_ids = {n["id"] for n in nodes}
    cycle_set = detect_cycles(node_ids, edges)
    edges = mark_cycle_edges(edges, cycle_set)

    node_index = build_node_index(nodes)
    output = emit_mermaid(args.direction, nodes, edges, node_index, args.cluster)
    print(output)


if __name__ == "__main__":
    main()
