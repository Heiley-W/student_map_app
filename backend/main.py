"""FastAPI endpoint for the route calculator."""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from routing import find_route

app = FastAPI(title="Hogwarts Navigator Routing API", version="1.0.0")

# Change/add your frontend development origin if it differs from these.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAP_PATH = Path(os.getenv("MAP_GRAPH_PATH", Path(__file__).parent / "data" / "sample_map_graph.json"))

class RouteRequest(BaseModel):
    start: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    wheelchair_required: bool = False
    has_id_card: bool = False

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/route")
def calculate_route(request: RouteRequest):
    try:
        map_data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        return find_route(
            map_data,
            start=request.start,
            destination=request.destination,
            wheelchair_required=request.wheelchair_required,
            has_id_card=request.has_id_card,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Map graph not found: {MAP_PATH}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Map graph is not valid JSON.")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
