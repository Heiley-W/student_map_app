# Route Mapping and Routing
Technical README for the IKU WIS Hackathon prototype.

This document describes the latest coordinate-matching router supplied for this project. It does not establish that a locally modified copy has the same behavior. The system uses a verified navigation graph for movement within a floor and matches lift/stair landings across aligned PDF pages to change floors.

## 1. Scope and architecture

| Component | Responsibility |
| --- | --- |
| PDF/map extraction | Produces floor images, nodes and candidate connections; maintained separately from routing |
| `data/sample_map_graph.json` | Stores map geometry, floor information and edge conditions |
| `routing.py` | Applies access filters, finds graph paths and matches floor connectors |
| `sample_run.py` | Loads the map and tests the routing function directly |
| `main.py` | Exposes routing through FastAPI |
| React/map frontend | Supplies node IDs and preferences; renders returned points and instructions |

No AI model or API credits are needed to execute the routing algorithm. Map extraction and natural-language features are separate from the router.

The router currently finds paths using unweighted graph connections. It does not calculate travel time or physical walking distance.

## 2. Suggested project structure

```text
backend/
├── data/
│   └── sample_map_graph.json
├── routing.py
├── sample_run.py
├── main.py
└── requirements.txt
frontend/
└── ...map interface
```

Use one shared map file for both the API and the standalone runner. A folder downloaded separately from the repository is a separate copy of the project.

## 3. Map JSON format

The current map groups data by PDF page:

```json
{
  "source_pdf": "map.pdf",
  "pages": {
    "1": {
      "page": 1,
      "floor": "Ground Floor",
      "nodes": [],
      "edges": []
    },
    "2": {
      "page": 2,
      "floor": "First Floor",
      "nodes": [],
      "edges": []
    }
  }
}
```

The latest router also accepts the older flat format with top-level `nodes` and `edges`. In the multi-floor format, it flattens all page nodes and same-floor edges internally.

### Nodes

Example based on the map's node naming convention:

```json
{
  "id": "p001_ground_floor_corridor_1",
  "name": "Corridor North",
  "kind": "junction",
  "x": 11.2,
  "y": 18.0,
  "floor": "Ground Floor",
  "page": 1,
  "confidence": 0.9,
  "notes": ""
}
```

| Field | Meaning |
| --- | --- |
| `id` | Globally unique identifier across every floor; requests use this value |
| `name` | Human-readable label; labels may repeat |
| `kind` | For example: room, door, junction, turning_point, stairs or lift |
| `x`, `y` | Percentage coordinates on that PDF page |
| `page` | PDF page identifier; used as the floor identity when present |
| `floor` | Floor label and fallback floor identity if page is absent |
| `confidence` | Extraction confidence; currently not used in routing |
| `notes` | Descriptive text; currently not interpreted as executable restrictions |

Coordinates use the top-left origin: x increases rightward and y increases downward. Both range from 0 to 100.

The implementation compares pages as strings, so page `1` and page `"1"` identify the same page. Each floor must be represented consistently. The current design assumes one PDF page corresponds to one floor.

### Edges

```json
{
  "id": "p001_ground_floor_edge_corridor_1_2",
  "from": "p001_ground_floor_corridor_1",
  "to": "p001_ground_floor_corridor_2",
  "time_seconds": null,
  "wheelchair_accessible": true,
  "requires_id_card": false,
  "blocked": false,
  "page": 1,
  "floor": "Ground Floor"
}
```

An edge represents an actual walkable connection. Two points being near each other, or appearing on the same line, does not create an edge.

| Field | Routing behavior |
| --- | --- |
| `from`, `to` | Must reference existing node IDs |
| `id` | Optional in the latest code; returned as a segment ID when available |
| `blocked` | Explicit true removes the edge |
| `wheelchair_accessible` | Must be explicit true when wheelchair mode is enabled |
| `requires_id_card` | Explicit true removes the edge if the user has no card |
| `time_seconds` | Ignored; may remain null |
| `page`, `floor` on the edge | Informational; endpoint nodes determine floor membership |

The graph is an undirected `networkx.Graph`: every retained connection is usable in either direction. Parallel edges between the same endpoints are not preserved separately; the later one overwrites the earlier edge attributes. Model separate doors as separate nodes or extend the implementation to a multigraph.

## 4. Access-filter policy

The latest implementation uses this exact policy:

| Condition | Edge retained? |
| --- | --- |
| blocked is true | No |
| blocked is false, null or missing | Yes, subject to other filters |
| Wheelchair requested; accessibility is true | Yes, subject to other filters |
| Wheelchair requested; accessibility is false, null or missing | No |
| Wheelchair not requested | Accessibility flag does not restrict the edge |
| Card required is true; user has no card | No |
| Card required is true; user has a card | Yes, subject to other filters |
| Card required is false, null or missing | Yes, subject to other filters |

This is more permissive about unknown blocked/card values than earlier versions of the prototype. Unknown values are not evidence that a connection is open or public. Review these fields before relying on a route.

The router also excludes same-floor edges touching a stair node in wheelchair mode. Cross-floor candidates in wheelchair mode are limited to lifts.

`has_id_card` describes the user. `requires_id_card` describes a map connection. A self-declared card checkbox does not authenticate actual door permissions.

## 5. Routing within one floor

1. Load every node into the graph.
2. Check that start and destination IDs exist.
3. Add same-floor edges that pass the access filters.
4. If both locations have the same floor identity, call `nx.shortest_path`.
5. Return the ordered path or `no_route`.

No edge weights are supplied. NetworkX therefore minimizes the number of edges. A corridor represented by many small segments can be disfavored compared with a longer corridor represented by fewer segments.

Junctions and corridor bends must appear as nodes if the frontend draws straight segments between route points. The algorithm does not discover walls or bends from an image.

## 6. Routing between floors

The current algorithm implements this sequence:

1. Find all nodes reachable from the start through filtered same-floor edges.
2. Find all nodes reachable from the destination through its filtered same-floor edges.
3. Consider reachable stair/lift nodes on the starting floor.
4. Find the nearest coordinate match of the same connector kind on the destination floor, within tolerance.
5. Keep that pair only if the destination landing can reach the destination.
6. Choose the starting connector with the fewest graph edges from the start.
7. Concatenate the start-to-connector path, the inferred floor transfer and the landing-to-destination path.

The router recognizes `lift` and `elevator` as lifts, and `stairs` and `staircase` as stairs. Other kind strings are not recognized as connectors.

### Coordinate matching

For landing positions (x1, y1) and (x2, y2):

```text
difference = sqrt((x1 - x2)^2 + (y1 - y2)^2)
match if difference <= coordinate_tolerance
```

The default tolerance is `2.0` percentage points. This is a Euclidean radius in percentage coordinates, not metres. It does not account for page aspect ratio.

Matching requires aligned floor plans: consistent orientation, scale, crop, page framing and placement. A larger tolerance can incorrectly associate different shafts.

For each starting connector, the code selects the closest coordinate match before checking whether that landing can reach the destination. It does not try a second-nearest landing if the closest one is disconnected. Equally close matches within the implementation's numeric tolerance raise a `ValueError` for review.

Candidates are ranked by:

1. Fewest edges from start to starting connector.
2. Fewest edges from destination landing to destination.
3. Smallest coordinate difference.
4. Node IDs, for deterministic tie breaking.

This follows the requested preference for the connector closest to the start. It does not minimize the total route length across both floors.

### Floor-transfer assumptions

The transition is inferred at route time; it is not an ordinary JSON edge. The router assumes the matched connector directly serves both selected floors.

It currently does not check shaft identity, intermediate floors, vertical travel time, connector opening hours, lift breakdowns or a separate accessibility attribute for the vertical transfer. Accessible approach edges alone do not prove that a lift itself is accessible.

The implementation uses one transfer between the selected floors. It cannot plan a route that changes connectors on an intermediate floor.

A landing must connect to its local corridor graph. An isolated lift node cannot be used unless it is itself the start or destination. In that special case, there is no approach edge on which to check access conditions.

Explicit verified connector IDs and transfer edges are a future improvement when they become available.

## 7. Function interface

```python
result = find_route(
    map_data,
    start="START_NODE_ID",
    destination="DESTINATION_NODE_ID",
    wheelchair_required=False,
    has_id_card=False,
    coordinate_tolerance=2.0,
)
```

| Parameter | Meaning |
| --- | --- |
| `map_data` | Parsed JSON dictionary containing all pages, or a flat graph |
| `start`, `destination` | Exact node IDs |
| `wheelchair_required` | Require confirmed accessible approach paths and use lifts for floor changes |
| `has_id_card` | Permit edges explicitly requiring a card |
| `coordinate_tolerance` | Maximum cross-floor landing-coordinate difference |

The code rejects duplicate node IDs, unknown endpoints and invalid negative/non-finite tolerance values. It does not perform comprehensive schema validation.

## 8. Result format

A successful response contains:

```json
{
  "status": "ok",
  "node_ids": ["START_NODE_ID", "LANDING_NODE_ID"],
  "edge_ids": [
    {
      "id": "EDGE_ID",
      "from": "START_NODE_ID",
      "to": "LANDING_NODE_ID",
      "floor_change": false
    }
  ],
  "points": [],
  "steps": []
}
```

The sample above illustrates the response shape; a real result also includes the corresponding point and step objects.

- `node_ids`: ordered route nodes.
- `edge_ids`: segment objects, despite the historical field name.
- `points`: ordered node details, including available page and floor information.
- `steps`: instructions and segment endpoints, with `from_page`, `to_page` and `floor_change`.
- Inferred floor-transfer segments have `id: null`.
- No travel-time value is returned.

A no-route result has `status: "no_route"`, a diagnostic message and empty lists for these route fields. It is a normal routing outcome, not an internal server error.

An identical start and destination produces a one-point route with no segments or steps.

## 9. Local setup and standalone test

From the repository's backend folder on macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The backend dependencies are `networkx`, `fastapi` and `uvicorn`. Use the activated environment's Python to avoid installing packages into a different interpreter.

Example `sample_run.py`:

```python
import json
from pathlib import Path

from routing import find_route

map_path = Path(__file__).resolve().parent / "data" / "sample_map_graph.json"
map_data = json.loads(map_path.read_text(encoding="utf-8"))

# Initial same-floor smoke test using IDs from the supplied map.
start = "p001_ground_floor_corridor_1"
destination = "p001_ground_floor_corridor_2"

result = find_route(
    map_data,
    start=start,
    destination=destination,
    wheelchair_required=False,
    has_id_card=False,
    coordinate_tolerance=2.0,
)

print("Map:", map_path.resolve())
print("Status:", result["status"])

if result["status"] == "ok":
    for step in result["steps"]:
        print(step["instruction"])
else:
    print(result["message"])
```

Run:

```bash
python sample_run.py
```

For a cross-floor test, replace start and destination with actual IDs from different pages. Pass the full map JSON; remove any code that selects only `pages["1"]`.

## 10. FastAPI integration

The API and standalone runner are separate entry points. Updating the test runner does not update the API.

Use a request model such as:

```python
class RouteRequest(BaseModel):
    start: str
    destination: str
    wheelchair_required: bool = False
    has_id_card: bool = False
```

Inside the existing route endpoint:

```python
map_data = json.loads(MAP_PATH.read_text(encoding="utf-8"))

return find_route(
    map_data,
    start=request.start,
    destination=request.destination,
    wheelchair_required=request.wheelchair_required,
    has_id_card=request.has_id_card,
    coordinate_tolerance=2.0,
)
```

The tolerance above is configured in Python. Including it in an API request will not change behavior unless the request model and endpoint are extended to accept and forward it.

Handle missing map files and invalid map JSON as server configuration errors. Convert user-facing `ValueError` exceptions to a suitable HTTP error; distinguish malformed map data from invalid location selections when refining the API.

Start from backend:

```bash
python -m uvicorn main:app --reload
```

Open [interactive API docs](http://localhost:8000/docs). An example request is:

```json
{
  "start": "p001_ground_floor_corridor_1",
  "destination": "p001_ground_floor_corridor_2",
  "wheelchair_required": false,
  "has_id_card": false
}
```

Send ordinary JSON with true/false literals. Do not include copied HTML entities or line-continuation backslashes.

Set CORS origins to the frontend's actual development address. A browser cannot directly import the Python function; it calls the API over HTTP.

## 11. Frontend map rendering

Render route points over the PDF page image using the same percentage coordinate system.

- Draw a line only for segments whose endpoints belong to the same page.
- Split the route into consecutive same-page runs; do not join unrelated runs merely because they have the same page ID.
- On a floor transfer, switch/highlight the destination page and display the transfer instruction.
- Mark the start, destination and selected lift/stair landings.
- Remove old travel-time displays.
- Clear or recalculate the route when locations or access preferences change.
- Use node IDs in dropdown values; show names and floors as labels.

The provided instructions identify the next node and floor transfer. They do not calculate left/right turns, indoor live position or spoken guidance. Speech synthesis can read the returned instructions as a separate feature.

## 12. Verification checklist

| Scenario | Expected behavior |
| --- | --- |
| Connected points on one floor | Ordered route through retained edges |
| Same start and destination | One point, no movement steps |
| Unknown location ID | ValueError/API error |
| Blocked edge | Excluded; alternative path or no_route |
| Wheelchair requested | Unknown/false accessibility excluded; stairs excluded |
| User lacks a card | Explicit card-controlled edges excluded |
| Matching connector on another floor | Transfer followed by destination-floor path |
| Connector outside tolerance | Not considered a match |
| Two equally close landing matches | ValueError requesting review |
| Landing disconnected from destination | Candidate rejected |
| No usable connector | no_route |
| Duplicate global node IDs | ValueError |

Inspect every floor transfer against the aligned map images. Synthetic graph tests can check algorithm behavior, but cannot establish real building connectivity or permissions.

This README documents the supplied implementation; the full installed project and real building routes have not been independently validated.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Old Hogwarts rooms still appear | Unsaved edits or another project copy; print script, routing module and map paths |
| JSONDecodeError at line 1 | Empty or invalid JSON; inspect the actual file being read |
| KeyError: nodes | Whole paged JSON passed to an older flat-only router; use latest router or extract one page for a local test |
| Invalid travel time | Old weighted routing code still running; latest code ignores time_seconds |
| Unknown starting point | Old node ID or the selected floor was removed from the supplied data |
| Every request says no_route | Reversed continue conditions, disconnected graph or access filters removing needed edges |
| Same-floor test works; API fails | main.py loads different data or still extracts only Ground Floor |
| Wrong lift/stair matched | Maps misaligned, tolerance too large or multiple nearby connector nodes |
| Internal Server Error | Read the Uvicorn terminal traceback for the exact failing line |

Print the active paths when diagnosing duplicate copies:

```python
import routing

print("Script:", Path(__file__).resolve())
print("Router:", routing.__file__)
print("Map:", map_path.resolve())
```

## 14. Current limitations and future work

Current limitations include unweighted route selection, assumed bidirectional edges, inferred direct floor transfers, permissive unknown blocked/card values, and no authentication of card permissions.

Conditions written only in notes, such as "closed after 6pm", are not enforced. Implement structured opening windows and evaluate them at the appropriate arrival time before claiming time-aware routing.

Admin edits, fault reporting, lift service status, persistent storage, QR/live positioning, spoken directions and AI-assisted requests require separate integration. A reported closure must update the affected routing data and trigger recalculation.

Useful next improvements are verified connector identities, explicit transfer permissions, automated schema/connectivity validation and route weighting based on measured distance or travel cost when required.

## 15. Git workflow

Work on the team's routing branch:

```bash
git switch route-calculator
git status
git add backend/routing.py backend/sample_run.py backend/main.py
git add backend/data/sample_map_graph.json
git commit -m "Add floor-aware routing with coordinate matching"
git push -u origin route-calculator
```

Only stage files belonging to the intended change. Keep virtual environments, caches, dependency folders and secrets out of Git:

```gitignore
.venv/
__pycache__/
node_modules/
.env
```

Commit extracted source files so teammates can review them. Use a pull request to integrate with the map-extraction and frontend branches.

