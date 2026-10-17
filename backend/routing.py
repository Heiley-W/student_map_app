"""Route within floors and match stairs/lifts across aligned floor maps."""

import math
import networkx as nx


def _connector_kind(node):
    kind = str(node.get("kind", "")).lower()

    if kind in ("lift", "elevator"):
        return "lift"
    if kind in ("stairs", "staircase"):
        return "stairs"

    return None


def _floor_key(node):
    # PDF page identifies the floor in the current map format.
    if node.get("page") is not None:
        return ("page", str(node["page"]))

    if node.get("floor") is not None:
        return ("floor", node["floor"])

    raise ValueError(f"Node {node['id']} has no page or floor.")


def _point(node):
    return {
        key: node[key]
        for key in ("id", "name", "kind", "x", "y", "page", "floor")
        if key in node
    }


def _no_route(message):
    return {
        "status": "no_route",
        "message": message,
        "node_ids": [],
        "edge_ids": [],
        "points": [],
        "steps": [],
    }


def find_route(
    map_data,
    start,
    destination,
    wheelchair_required=False,
    has_id_card=False,
    coordinate_tolerance=2.0,
):
    """
    coordinate_tolerance is measured in percentage points.

    For example, 2.0 allows a landing-coordinate difference of
    up to 2 percentage points, measured using Euclidean distance.
    """
    if coordinate_tolerance < 0 or not math.isfinite(coordinate_tolerance):
        raise ValueError("Coordinate tolerance must be finite and non-negative.")

    nodes = []
    edges = []

    # Support both multi-floor and flat JSON formats.
    if "pages" in map_data:
        for page_key, page_data in map_data["pages"].items():
            for original_node in page_data["nodes"]:
                node = dict(original_node)
                node.setdefault("page", page_data.get("page", page_key))
                node.setdefault("floor", page_data.get("floor"))
                nodes.append(node)

            edges.extend(page_data["edges"])
    else:
        nodes = map_data["nodes"]
        edges = map_data["edges"]

    graph = nx.Graph()

    for node in nodes:
        if node["id"] in graph:
            raise ValueError(f"Duplicate node ID: {node['id']}")

        graph.add_node(node["id"], **node)

    if start not in graph or destination not in graph:
        raise ValueError("Unknown starting point or destination.")

    # Build only the connections within each floor.
    for edge in edges:
        source = edge["from"]
        target = edge["to"]

        if source not in graph or target not in graph:
            raise ValueError(
                f"Connection {source!r} -> {target!r} "
                "references an unknown node."
            )

        source_node = graph.nodes[source]
        target_node = graph.nodes[target]

        if _floor_key(source_node) != _floor_key(target_node):
            continue

        if edge.get("blocked") is True:
            continue

        if wheelchair_required:
            if (
                #_connector_kind(source_node) == "stairs"
                #or _connector_kind(target_node) == "stairs"
                edge.get("wheelchair_accessible") is False
            ):
                continue

        if not has_id_card and edge.get("requires_id_card") is True:
            continue

        graph.add_edge(
            source,
            target,
            edge_id=edge.get("id"),
        )

    start_floor = _floor_key(graph.nodes[start])
    destination_floor = _floor_key(graph.nodes[destination])
    transfer = None

    if start_floor == destination_floor:
        try:
            node_ids = nx.shortest_path(graph, start, destination)
        except nx.NetworkXNoPath:
            return _no_route(
                "No route on this floor meets the selected requirements."
            )

    else:
        # Find everything reachable from the start on its own floor.
        start_paths = nx.single_source_shortest_path(graph, start)

        # Find everything reachable from the destination on its own floor.
        destination_paths = nx.single_source_shortest_path(
            graph, destination
        )

        candidates = []

        for source_id, path_to_connector in start_paths.items():
            source_node = graph.nodes[source_id]
            kind = _connector_kind(source_node)

            if kind is None:
                continue

            if wheelchair_required and kind != "lift":
                continue

            matches = []

            # Match by kind and coordinates on the destination floor.
            for target_id, target_node in graph.nodes(data=True):
                if _floor_key(target_node) != destination_floor:
                    continue

                if _connector_kind(target_node) != kind:
                    continue

                coordinate_difference = math.hypot(
                    float(source_node["x"]) - float(target_node["x"]),
                    float(source_node["y"]) - float(target_node["y"]),
                )

                if coordinate_difference <= coordinate_tolerance:
                    matches.append((coordinate_difference, target_id))

            if not matches:
                continue

            # Use the nearest coordinate match.
            matches.sort()
            difference, target_id = matches[0]

            # Avoid silently choosing between equally close matches.
            if (
                len(matches) > 1
                and math.isclose(
                    difference,
                    matches[1][0],
                    abs_tol=1e-9,
                )
            ):
                raise ValueError(
                    f"Ambiguous floor match for {source_id!r}. "
                    "Review the landing coordinates."
                )

            if target_id not in destination_paths:
                continue

            # Stored path runs destination -> landing; reverse it.
            path_from_connector = list(
                reversed(destination_paths[target_id])
            )

            candidates.append({
                "source": source_id,
                "target": target_id,
                "kind": kind,
                "start_path": path_to_connector,
                "end_path": path_from_connector,
                "start_hops": len(path_to_connector) - 1,
                "end_hops": len(path_from_connector) - 1,
                "coordinate_difference": difference,
            })

        if not candidates:
            return _no_route(
                "No reachable matching lift or staircase was found "
                "on the destination floor within the coordinate tolerance."
            )

        # Closest to start by graph connections.
        # Break ties using the remaining route, then coordinate match.
        transfer = min(
            candidates,
            key=lambda candidate: (
                candidate["start_hops"],
                candidate["end_hops"],
                candidate["coordinate_difference"],
                candidate["source"],
                candidate["target"],
            ),
        )

        node_ids = transfer["start_path"] + transfer["end_path"]

    segments = []
    steps = []

    for source, target in zip(node_ids, node_ids[1:]):
        source_node = graph.nodes[source]
        target_node = graph.nodes[target]

        is_transfer = (
            transfer is not None
            and source == transfer["source"]
            and target == transfer["target"]
        )

        if is_transfer:
            source_label = (
                source_node.get("floor")
                or f"page {source_node.get('page')}"
            )
            target_label = (
                target_node.get("floor")
                or f"page {target_node.get('page')}"
            )

            instruction = (
                f"Take the {transfer['kind']} at "
                f"{source_node.get('name', source)} from "
                f"{source_label} to {target_label}."
            )

            edge_id = None
        else:
            instruction = (
                f"Continue to {target_node.get('name', target)}."
            )
            edge_id = graph[source][target].get("edge_id")

        segments.append({
            "id": edge_id,
            "from": source,
            "to": target,
            "floor_change": is_transfer,
        })

        steps.append({
            "from": source,
            "to": target,
            "from_page": source_node.get("page"),
            "to_page": target_node.get("page"),
            "floor_change": is_transfer,
            "instruction": instruction,
        })

    return {
        "status": "ok",
        "node_ids": node_ids,
        "edge_ids": segments,
        "points": [_point(graph.nodes[node_id]) for node_id in node_ids],
        "steps": steps,
    }