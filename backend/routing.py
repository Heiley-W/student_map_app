"""Shortest-time route calculation with accessibility and access filters."""
from __future__ import annotations

import math
import networkx as nx


def find_route(
    map_data: dict,
    start: str,
    destination: str,
    wheelchair_required: bool = False,
    has_id_card: bool = False,
) -> dict:
    """Return the fastest allowed route, or a clear no-route result.

    Unknown accessibility is treated as inaccessible when wheelchair access is
    required. Card-controlled edges are available only when has_id_card is true.
    """
    graph = nx.Graph()
    nodes = map_data.get("nodes", [])
    edges = map_data.get("edges", [])
    for node in nodes:
        if not all(key in node for key in ("id", "name", "x", "y")):
            raise ValueError("Each node needs id, name, x, and y fields.")
        if node["id"] in graph:
            raise ValueError(f"Duplicate node id: {node['id']}")
        graph.add_node(node["id"], **node)

    if start not in graph or destination not in graph:
        raise ValueError("Unknown starting point or destination.")
    if start == destination:
        return {
            "status": "ok", "node_ids": [start], "edge_ids": [],
            "points": [_point(graph, start)], "total_seconds": 0,
        }

    for edge in edges:
        required = ("id", "from", "to", "time_seconds", "wheelchair_accessible",
                    "requires_id_card", "blocked")
        if not all(key in edge for key in required):
            raise ValueError("Each edge must include id, from, to, time_seconds, "
                             "wheelchair_accessible, requires_id_card, and blocked.")
        if edge["from"] not in graph or edge["to"] not in graph:
            raise ValueError(f"Edge {edge['id']} references an unknown node.")
        try:
            travel_time = float(edge["time_seconds"])
        except (TypeError, ValueError):
            raise ValueError(f"Edge {edge['id']} has an invalid travel time.")
        if not math.isfinite(travel_time) or travel_time < 0:
            raise ValueError(f"Edge {edge['id']} must have a finite, non-negative time.")
        if edge["blocked"]:
            continue
        if wheelchair_required and edge["wheelchair_accessible"] is not True:
            continue
        if edge["requires_id_card"] and not has_id_card:
            continue
        # This starter assumes one bidirectional connection per node pair.
        graph.add_edge(edge["from"], edge["to"],
                       time_seconds=travel_time, edge_id=edge["id"])

    try:
        total_seconds, node_ids = nx.single_source_dijkstra(
            graph, source=start, target=destination, weight="time_seconds")
    except nx.NetworkXNoPath:
        return {
            "status": "no_route",
            "message": "No route meets the selected access requirements.",
            "node_ids": [], "edge_ids": [], "points": [], "total_seconds": None,
        }

    edge_ids = [graph[a][b]["edge_id"] for a, b in zip(node_ids, node_ids[1:])]
    return {
        "status": "ok",
        "node_ids": node_ids,
        "edge_ids": edge_ids,
        "points": [_point(graph, node_id) for node_id in node_ids],
        "total_seconds": total_seconds,
    }


def _point(graph: nx.Graph, node_id: str) -> dict:
    node = graph.nodes[node_id]
    return {key: node[key] for key in ("id", "name", "x", "y")}
