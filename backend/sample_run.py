"""Run the routing examples against the bundled sample map."""
## import json
## from pathlib import Path

## from routing import find_route


## DATA_PATH = Path(__file__).parent / "data" / "sample_map_graph.json"


## def main() -> None:
    ## map_data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    ## cases = [
        #(False, False, "No access filters"),
        #(True, False, "Wheelchair required"),
        #(False, True, "Authorised card available"),
        #(True, True, "Wheelchair and card"),
    #]

    ## for wheelchair_required, has_id_card, label in cases:
        #result = find_route(
         ##   map_data,
           # start="p1_d106b",
            #destination="p1_d107",
            #wheelchair_required=wheelchair_required,
            #has_id_card=has_id_card,
        #)
        #names = " -> ".join(point["name"] for point in result["points"])
        #print(f"{label}: {names} ({result['total_seconds']} seconds)")

#if __name__ == "__main__":
    #main()

import json
from pathlib import Path

from routing import find_route

backend_folder = Path(__file__).resolve().parent
map_path = backend_folder / "data" / "sample_map_graph.json"

print("Running script:", Path(__file__).resolve())
print("Using routing module:", __import__("routing").__file__)
print("Reading map:", map_path)

map_data = json.loads(map_path.read_text(encoding="utf-8"))
#ground_floor = all_map_data["pages"]["1"]

#print("First map node:", ground_floor["nodes"][0])

#map_data = {
    #"nodes": ground_floor["nodes"],
    #"edges": ground_floor["edges"]
#}
# Replace these with IDs from your updated JSON.
start = "p001_ground_floor_corridor_1"
destination = "p001_ground_floor_corridor_2"

print("Requested route:", start, "->", destination)

result = find_route(
    map_data,
    start=start,
    destination=destination,
    wheelchair_required=False,
    has_id_card=False,
)

if result["status"] == "ok":
    print("Route:")
    for point in result["points"]:
        print(
            f"  {point['name']} "
            f"(page {point.get('page', '?')}, "
            f"x={point['x']}, y={point['y']})"
        )
else:
    print(result["message"])