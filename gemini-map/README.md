# Pythagoras — Map Extraction and Routing

## Overview

Pythagoras converts multi-floor building maps into a structured graph and uses graph algorithms to calculate routes between locations.

The system has two main components:

1. **Map extraction with Google Gemini** — analyses floor-plan PDFs and extracts rooms, corridors, doors, lifts, staircases, and their connections into JSON.
2. **Route calculation with NetworkX** — calculates shortest-time routes while considering floor changes, wheelchair accessibility, access-card restrictions, and blocked connections.

## 1. Map Extraction with Gemini

### How it works

The map extraction script, `pdf_to_json_map.py`, uses the Google Gemini API to interpret floor-plan images.

The extraction pipeline is:

1. Load the input PDF using PyMuPDF.
2. Render each PDF page into an image representing one floor.
3. Send each floor image and a structured extraction prompt to Gemini.
4. Ask Gemini to identify map features and their approximate coordinates.
5. Extract the features and connections into a structured graph using Pydantic schemas.
6. Validate the extracted graph and report potential connectivity issues.
7. Save the results to JSON and optionally generate visual previews.

### Why Gemini?

Floor plans contain visual information that is difficult to extract reliably using text extraction alone. For example, a room may have no label, a lift may be represented by a grey box, and corridors may connect rooms through narrow doorways.

Gemini's visual understanding allows the system to identify these features and infer their relationships from the floor-plan image.

The extraction prompt instructs Gemini to:

- Identify labelled and unlabelled rooms.
- Detect corridors, junctions, doors, lifts, and staircases.
- Represent rooms and other navigable locations as graph nodes.
- Connect rooms to corridors through doorways where visible.
- Identify unlabelled lift symbols, including dark-grey boxes.
- Record approximate coordinates and floor information.
- Avoid inventing connections through walls or between separate floors.

Gemini returns structured data conforming to the defined Pydantic schema. This makes the output easier to validate and convert into the graph format required by the routing algorithm.

**Important:** Gemini-generated coordinates and connections are estimates. Visual previews and graph validation should be used to identify extraction errors before deployment.

### Requirements

- Python 3.10 or later
- A Google Gemini API key
- The dependencies used by the extraction script

Install the required packages:

```bash
pip install google-genai pymupdf pydantic pillow matplotlib
```

Set the API key as an environment variable:

```bash
export GEMINI_API_KEY="YOUR_API_KEY"
```

On macOS or Linux, this makes the key available to the Python script without hardcoding it in the source code.

### Run the extraction

From the project directory:

```bash
python pdf_to_json_map.py "hogwarts_map.pdf" --no-resume
```

The script processes the PDF and generates the graph JSON. Depending on the script configuration, it can also produce intermediate checkpoint files and visual previews.

Common command-line options include:

| Option | Purpose |
|---|---|
| `--output` | Specify the output JSON path |
| `--model` | Select the Gemini model |
| `--dpi` | Set the PDF rendering resolution |
| `--previews` | Configure preview output |
| `--no-resume` | Ignore existing checkpoints and reprocess the pages |

Use `python pdf_to_json_map.py --help` to confirm the available options in your local version.

## 2. Graph JSON Structure

The extracted map is represented as an undirected graph.

### Nodes

Each node represents a location or navigational feature.

Example:

```json
{
  "id": "F1_room_01",
  "name": "Room 101",
  "kind": "room",
  "x": 120.5,
  "y": 240.0,
  "floor": "Floor 1"
}
```

Typical node types include:

- `room` — labelled or unlabelled rooms
- `corridor` — corridors and passageways
- `door` — doorway locations
- `lift` — lift locations
- `stairs` — staircases
- `junction` — corridor intersections

The exact values depend on the extraction schema and the prompt used.

### Edges

Each edge represents a traversable connection between two nodes. Edge IDs are not required; the endpoints identify the connection.

Example:

```json
{
  "from": "F1_room_01",
  "to": "F1_door_01",
  "time_seconds": 2,
  "wheelchair_accessible": true,
  "requires_id_card": false,
  "blocked": false
}
```

The fields have the following meanings:

| Field | Description |
|---|---|
| `from` | Source node ID |
| `to` | Destination node ID |
| `time_seconds` | Estimated traversal time |
| `wheelchair_accessible` | Whether wheelchair access is confirmed |
| `requires_id_card` | Whether the connection requires an access card |
| `blocked` | Whether the connection is unavailable |

The routing algorithm uses `time_seconds` as the edge weight. Missing edge times default to one second in the current implementation, so realistic traversal times should be supplied for meaningful route comparisons.

## 3. Route Calculation

The routing function, `find_route()`, uses NetworkX to calculate shortest-time paths through the extracted graph.

It accepts:

- `map_data` — the extracted graph JSON
- `start` — starting node ID
- `destination` — destination node ID
- `wheelchair_required` — whether wheelchair-accessible connections are required
- `has_id_card` — whether the user has an access card

### Same-floor routing

If the starting location and destination are on the same floor, the algorithm constructs a graph for that floor and finds the shortest-time path directly.

### Cross-floor routing

If the locations are on different floors, the algorithm follows three stages:

1. **Find a connector on the starting floor.** Calculate the shortest route from the start to each reachable lift or staircase.
2. **Match the connector on the destination floor.** Identify a lift or staircase of the same type at approximately the same coordinates.
3. **Complete the route.** Calculate the shortest route from the matched connector to the destination.

The algorithm evaluates valid connector pairs and selects the one with the lowest combined walking time.

The transfer between floors is represented separately rather than as an ordinary graph edge, because the floor plans do not necessarily contain explicit connections between corresponding lift or staircase nodes.

### Accessibility and access restrictions

The algorithm applies the following filters:

- **Blocked connections:** excluded from routing.
- **Wheelchair routing:** excludes stairs and connections whose wheelchair accessibility is not explicitly confirmed.
- **Access-card restrictions:** excludes connections explicitly requiring a card when the user does not have one.

Unknown access-card requirements are not automatically excluded. These should be verified if the building requires stricter access control.

### Example

```python
import json

with open("map_graph.json", "r", encoding="utf-8") as file:
    map_data = json.load(file)

route = find_route(
    map_data=map_data,
    start="F1_room_01",
    destination="F2_room_12",
    wheelchair_required=False,
    has_id_card=True,
)

if route["status"] == "ok":
    print("Route:", route["node_ids"])
    print("Transfers:", route["transfers"])
    print("Estimated time:", route["total_time_seconds"], "seconds")
else:
    print(route["message"])
```

Replace the example node IDs with IDs that actually exist in your JSON.

## 4. Limitations and Future Improvements

### Map extraction

- Gemini may miss small rooms, doors, or corridor connections.
- Unlabelled rooms and lift symbols may be ambiguous.
- Coordinates are approximate and should be checked against visual previews.
- Automatic graph validation can identify suspicious connectivity but cannot guarantee that every connection is physically valid.

### Cross-floor routing

- Corresponding connectors are currently matched using their type and approximate coordinates.
- Coordinate matching assumes that floor plans use a consistent coordinate system and scale.
- The algorithm currently supports a single transfer between the starting and destination floors.
- Lift and staircase traversal times are not included in the total estimated time.
- A missing or incorrectly classified connector can cause a valid route to be rejected.

### Recommended improvements

- Calibrate coordinates across floor plans.
- Manually review and correct extracted graph connections.
- Include lift waiting times and floor-transition times.
- Support routes through intermediate floors.
- Add explicit connector metadata instead of relying only on node names.
- Add tests for blocked edges, inaccessible routes, disconnected rooms, and missing connectors.

## 5. Summary

Gemini provides the visual interpretation needed to convert building floor plans into a structured graph. NetworkX then uses that graph to calculate routes.

Separating extraction from routing allows the map to be regenerated or corrected without rewriting the routing algorithm. Reliable navigation nevertheless depends on validating the extracted graph and ensuring that its connector coordinates and accessibility metadata accurately represent the building.

