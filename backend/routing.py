"""Shortest-time route calculation with accessibility and access filters."""
from __future__ import annotations

import math
import networkx as nx

def _point(graph: nx.Graph, node_id: str) -> dict:
    node = graph.nodes[node_id]
    return {key: node[key] for key in ("id", "name", "kind","x", "y", "page")}

def find_route(
    map_data,
    start,
    destination,
    wheelchair_required=False,
    has_id_card=False,
):
    graph = nx.Graph()

    for node in map_data["nodes"]:
        graph.add_node(node["id"], **node)

    if start not in graph or destination not in graph:
        raise ValueError("Unknown starting point or destination.")

    for edge in map_data["edges"]:
        source = edge["from"]
        target = edge["to"]

        if source not in graph or target not in graph:
            raise ValueError(
                f"Edge {edge['id']} references an unknown node."
            )

        # Only confirmed open connections are allowed.
        if edge.get("blocked") is not False:
            continue

        # Unknown accessibility is excluded when wheelchair access is needed.
        if (
            wheelchair_required
            and edge.get("wheelchair_accessible") is not True
        ):
            continue

        # Unknown card requirements are excluded until verified.
        card_required = edge.get("requires_id_card")

        if card_required is not True:
            continue

        if card_required is True and not has_id_card:
            continue

        graph.add_edge(source, target, edge_id=edge["id"])

    try:
        node_ids = nx.shortest_path(
            graph,
            source=start,
            target=destination,
            weight=None,
        )
    except nx.NetworkXNoPath:
        return {
            "status": "no_route",
            "message": "No route meets the selected access requirements.",
            "node_ids": [],
            "edge_ids": [],
            "points": [],
        }

    return {
        "status": "ok",
        "node_ids": node_ids,
        "edge_ids": [
            graph[a][b]["edge_id"]
            for a, b in zip(node_ids, node_ids[1:])
        ],
        "points": [
            {
                key: graph.nodes[node_id][key]
                for key in ("id", "name", "kind", "x", "y", "page")
                if key in graph.nodes[node_id]
            }
            for node_id in node_ids
        ],
    }
