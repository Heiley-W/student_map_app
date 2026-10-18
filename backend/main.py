"""FastAPI endpoints for map data and indoor route calculation."""

import json
import os
from datetime import datetime, time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from routing import find_route


load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="Indoor Wayfinder Routing API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8001",
        "http://localhost:5500",
    ],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = Path(
    os.getenv("MAP_GRAPH_PATH", REPO_ROOT / "gemini-map" / "map_graph.json")
)
TRANSFER_PATH = Path(
    os.getenv(
        "FLOOR_TRANSFERS_PATH",
        Path(__file__).resolve().parent / "data" / "floor_transfers.json",
    )
)
NODE_STATUS_PATH = Path(
    os.getenv(
        "NODE_STATUS_PATH",
        Path(__file__).resolve().parent / "data" / "node_status.json",
    )
)


class RouteRequest(BaseModel):
    start: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    wheelchair_required: bool = False
    card_type: Literal["student", "postgrad", "staff", "visitor"] | None = None
    has_id_card: bool | None = None
    local_time: time = Field(default_factory=lambda: datetime.now().time())


class NodeStatusRequest(BaseModel):
    node_id: str = Field(min_length=1)
    out_of_order: bool


def _read_json(path: Path, description: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=500,
            detail=f"{description} not found: {path}",
        ) from error
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail=f"{description} is not valid JSON.",
        ) from error


def _load_map():
    map_data = _read_json(MAP_PATH, "Map graph")
    flat_graph = (
        isinstance(map_data, dict)
        and isinstance(map_data.get("nodes"), list)
        and isinstance(map_data.get("edges"), list)
    )
    paged_graph = (
        isinstance(map_data, dict)
        and isinstance(map_data.get("pages"), dict)
        and all(
            isinstance(page_data, dict)
            and isinstance(page_data.get("nodes"), list)
            and isinstance(page_data.get("edges"), list)
            for page_data in map_data["pages"].values()
        )
    )
    if not (flat_graph or paged_graph):
        raise HTTPException(
            status_code=500,
            detail="Map graph must contain top-level nodes and edges arrays, or a pages object with nodes and edges arrays.",
        )
    return map_data


def _load_transfers():
    data = _read_json(TRANSFER_PATH, "Floor transfer configuration")
    if not isinstance(data, dict) or not isinstance(data.get("transfers"), list):
        raise HTTPException(
            status_code=500,
            detail="Floor transfer configuration must contain a transfers array.",
        )
    return data["transfers"]


def _graph_nodes(map_data):
    if "pages" in map_data:
        return [
            node
            for page_data in map_data["pages"].values()
            for node in page_data["nodes"]
        ]
    return map_data["nodes"]


def _load_blocked_nodes():
    data = _read_json(NODE_STATUS_PATH, "Node status")
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("blocked_nodes"), list)
        or not all(isinstance(node_id, str) for node_id in data["blocked_nodes"])
    ):
        raise HTTPException(
            status_code=500,
            detail="Node status must contain a blocked_nodes array of node IDs.",
        )
    return set(data["blocked_nodes"])


def _save_blocked_nodes(blocked_nodes):
    NODE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=NODE_STATUS_PATH.parent,
            delete=False,
        ) as temp_file:
            json.dump({"blocked_nodes": sorted(blocked_nodes)}, temp_file, indent=2)
            temp_file.write("\n")
            temporary_path = Path(temp_file.name)
        temporary_path.replace(NODE_STATUS_PATH)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not save node status: {error}",
        ) from error


def _apply_node_blocks(map_data, transfers, blocked_nodes):
    if "pages" in map_data:
        edges = (
            edge
            for page_data in map_data["pages"].values()
            for edge in page_data["edges"]
        )
    else:
        edges = iter(map_data["edges"])
    for edge in edges:
        if edge.get("from") in blocked_nodes or edge.get("to") in blocked_nodes:
            edge["blocked"] = True
    for transfer in transfers:
        if transfer.get("from") in blocked_nodes or transfer.get("to") in blocked_nodes:
            transfer["blocked"] = True


def _map_catalog(map_data):
    floors = map_data.get("floors")
    if not isinstance(floors, list):
        floors = [
            {
                "name": page_data.get("floor"),
                "page": page_data.get("page", page_key),
            }
            for page_key, page_data in map_data.get("pages", {}).items()
        ]
    page_by_floor = {
        str(floor["name"]): floor["page"]
        for floor in floors
        if floor.get("name") is not None and floor.get("page") is not None
    }
    nodes = []
    if "pages" in map_data:
        source_nodes = (
            (node, page_data)
            for page_key, page_data in map_data["pages"].items()
            for node in page_data["nodes"]
        )
        for node, page_data in source_nodes:
            item = {
                key: node[key]
                for key in ("id", "name", "kind", "x", "y", "floor", "page")
                if key in node
            }
            item.setdefault("page", page_data.get("page"))
            item.setdefault("page", page_by_floor.get(str(item.get("floor"))))
            nodes.append(item)
    else:
        for node in map_data["nodes"]:
            item = {
                key: node[key]
                for key in ("id", "name", "kind", "x", "y", "floor", "page")
                if key in node
            }
            if item.get("page") is None and item.get("floor") is not None:
                item["page"] = page_by_floor.get(str(item["floor"]))
            nodes.append(item)
    return {
        "metadata": map_data.get("metadata", {}),
        "floors": floors,
        "nodes": nodes,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def get_public_config():
    return {
        "google_maps_api_key": os.getenv("GOOGLE_MAPS_API_KEY", ""),
    }


@app.get("/map")
def get_map():
    return _map_catalog(_load_map())


@app.put("/admin/node-status")
def update_node_status(request: NodeStatusRequest):
    node_ids = {node.get("id") for node in _graph_nodes(_load_map())}
    if request.node_id not in node_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown route node ID: {request.node_id}",
        )

    blocked_nodes = _load_blocked_nodes()
    if request.out_of_order:
        blocked_nodes.add(request.node_id)
    else:
        blocked_nodes.discard(request.node_id)
    _save_blocked_nodes(blocked_nodes)
    return {
        "node_id": request.node_id,
        "out_of_order": request.out_of_order,
    }


@app.post("/route")
def calculate_route(request: RouteRequest):
    map_data = _load_map()
    node_ids = {node.get("id") for node in _graph_nodes(map_data)}
    unknown = [
        node_id
        for node_id in (request.start, request.destination)
        if node_id not in node_ids
    ]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown route node ID: {', '.join(unknown)}",
        )

    try:
        transfers = _load_transfers()
        _apply_node_blocks(map_data, transfers, _load_blocked_nodes())
        return find_route(
            map_data,
            start=request.start,
            destination=request.destination,
            wheelchair_required=request.wheelchair_required,
            card_type=request.card_type or ("staff" if request.has_id_card else "visitor"),
            local_time=request.local_time,
            transfers=transfers,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Map graph or transfer configuration is invalid: {error}",
        ) from error
