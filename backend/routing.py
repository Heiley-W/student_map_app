"""Shortest-path routing over the graph produced by the PDF extractor."""

from datetime import datetime, time

import networkx as nx


def _floor_key(node):
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
        "segments": [],
        "steps": [],
        "floor_changes": 0,
    }


def _normalise_graph(map_data):
    """Flatten either the extractor's graph or the older paged format."""
    if "pages" in map_data:
        nodes = []
        edges = []
        for page_key, page_data in map_data["pages"].items():
            for original_node in page_data["nodes"]:
                node = dict(original_node)
                node.setdefault("page", page_data.get("page", page_key))
                node.setdefault("floor", page_data.get("floor"))
                nodes.append(node)
            edges.extend(page_data["edges"])
        return nodes, edges

    nodes = [dict(node) for node in map_data["nodes"]]
    edges = map_data["edges"]
    page_by_floor = {
        str(floor["name"]): floor["page"]
        for floor in map_data.get("floors", [])
        if floor.get("name") is not None and floor.get("page") is not None
    }
    for node in nodes:
        if node.get("page") is None and node.get("floor") is not None:
            node["page"] = page_by_floor.get(str(node["floor"]))
    return nodes, edges


def _build_graph(map_data, wheelchair_required, card_type, local_time, transfers):
    nodes, edges = _normalise_graph(map_data)
    graph = nx.Graph()

    for node in nodes:
        if node["id"] in graph:
            raise ValueError(f"Duplicate node ID: {node['id']}")
        _floor_key(node)
        graph.add_node(node["id"], **node)

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
        if wheelchair_required and (
            _is_stairs_node(source_node) or _is_stairs_node(target_node)
        ):
            continue
        if not _card_can_use_edge(edge, card_type, local_time):
            continue

        graph.add_edge(source, target, edge_id=edge.get("id"), transfer=None)

    for transfer in transfers:
        source = transfer["from"]
        target = transfer["to"]
        if source not in graph or target not in graph:
            raise ValueError(
                f"Floor transfer {source!r} -> {target!r} "
                "references an unknown node."
            )
        if _floor_key(graph.nodes[source]) == _floor_key(graph.nodes[target]):
            raise ValueError(
                f"Floor transfer {source!r} -> {target!r} "
                "must connect different floors."
            )
        if transfer.get("blocked") is True:
            continue
        kind = transfer["kind"]
        if wheelchair_required and kind != "lift":
            continue
        if not _card_can_use_edge(transfer, card_type, local_time):
            continue

        graph.add_edge(
            source,
            target,
            edge_id=transfer.get("id"),
            transfer={
                "group": transfer["group"],
                "kind": kind,
            },
        )

    return graph


def _is_stairs_node(node):
    return str(node.get("kind", "")).lower() in {"stairs", "staircase"}


def _card_can_use_edge(edge, card_type, local_time):
    if edge.get("requires_id_card") is not True:
        return True
    if card_type == "visitor":
        return False
    if card_type == "student" and local_time >= time(18, 0):
        return False
    return True


def _route_segments(graph, node_ids):
    segments = []
    steps = []
    floor_changes = 0

    first = graph.nodes[node_ids[0]]
    current = {
        "page": first.get("page"),
        "floor": first.get("floor"),
        "points": [_point(first)],
        "entry": None,
        "exit": None,
    }
    segments.append(current)

    for source, target in zip(node_ids, node_ids[1:]):
        source_node = graph.nodes[source]
        target_node = graph.nodes[target]
        edge = graph[source][target]
        transfer = edge.get("transfer")

        if transfer:
            direction = (
                "up"
                if int(target_node.get("page", 0)) > int(source_node.get("page", 0))
                else "down"
            )
            current["exit"] = {
                **transfer,
                "to_page": target_node.get("page"),
                "to_floor": target_node.get("floor"),
                "direction": direction,
            }
            current = {
                "page": target_node.get("page"),
                "floor": target_node.get("floor"),
                "points": [_point(target_node)],
                "entry": {
                    **transfer,
                    "from_page": source_node.get("page"),
                    "from_floor": source_node.get("floor"),
                },
                "exit": None,
            }
            segments.append(current)
            floor_changes += 1
            steps.append({
                "floor_change": True,
                "from_page": source_node.get("page"),
                "to_page": target_node.get("page"),
                "instruction": (
                    f"Take the {transfer['kind']} ({transfer['group']}) "
                    f"to {target_node.get('floor') or 'the next floor'}."
                ),
            })
        else:
            current["points"].append(_point(target_node))
            steps.append({
                "floor_change": False,
                "from": source,
                "to": target,
                "page": source_node.get("page"),
                "instruction": f"Continue to {target_node.get('name', target)}.",
            })

    return segments, steps, floor_changes


def find_route(
    map_data,
    start,
    destination,
    wheelchair_required=False,
    card_type="visitor",
    local_time=None,
    transfers=None,
):
    """Return the shortest route over walkable graph edges and explicit transfers."""
    if transfers is None:
        transfers = map_data.get("transfers", [])
    if local_time is None:
        local_time = datetime.now().time()
    if card_type not in {"student", "postgrad", "staff", "visitor"}:
        raise ValueError(f"Unsupported card type: {card_type}")

    graph = _build_graph(
        map_data,
        wheelchair_required=wheelchair_required,
        card_type=card_type,
        local_time=local_time,
        transfers=transfers,
    )
    if start not in graph or destination not in graph:
        raise ValueError("Unknown starting point or destination.")

    try:
        node_ids = nx.shortest_path(graph, start, destination)
    except nx.NetworkXNoPath:
        return _no_route(
            "No route meets the selected requirements between these locations."
        )

    segments, steps, floor_changes = _route_segments(graph, node_ids)
    return {
        "status": "ok",
        "message": None,
        "node_ids": node_ids,
        "edge_ids": [
            {
                "id": graph[source][target].get("edge_id"),
                "from": source,
                "to": target,
                "floor_change": graph[source][target].get("transfer") is not None,
            }
            for source, target in zip(node_ids, node_ids[1:])
        ],
        "points": [_point(graph.nodes[node_id]) for node_id in node_ids],
        "segments": segments,
        "steps": steps,
        "floor_changes": floor_changes,
    }
