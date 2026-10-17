"""FastAPI endpoints for map data and indoor route calculation."""

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from routing import find_route


app = FastAPI(title="Indoor Wayfinder Routing API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8001",
        "http://localhost:5500",
    ],
    allow_methods=["GET", "POST"],
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


class RouteRequest(BaseModel):
    start: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    wheelchair_required: bool = False
    has_id_card: bool = False


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
    if not isinstance(map_data, dict) or not isinstance(map_data.get("nodes"), list) or not isinstance(map_data.get("edges"), list):
        raise HTTPException(
            status_code=500,
            detail="Map graph must contain top-level nodes and edges arrays.",
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


def _map_catalog(map_data):
    floors = map_data.get("floors", [])
    page_by_floor = {
        str(floor["name"]): floor["page"]
        for floor in floors
        if floor.get("name") is not None and floor.get("page") is not None
    }
    nodes = []
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


@app.get("/map")
def get_map():
    return _map_catalog(_load_map())


@app.post("/route")
def calculate_route(request: RouteRequest):
    map_data = _load_map()
    node_ids = {node.get("id") for node in map_data["nodes"]}
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
        return find_route(
            map_data,
            start=request.start,
            destination=request.destination,
            wheelchair_required=request.wheelchair_required,
            has_id_card=request.has_id_card,
            transfers=_load_transfers(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Map graph or transfer configuration is invalid: {error}",
        ) from error
