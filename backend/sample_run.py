"""Run representative routes against the extracted map graph."""

import json
from pathlib import Path

from routing import find_route


REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = REPO_ROOT / "gemini-map" / "map_graph.json"
TRANSFER_PATH = Path(__file__).resolve().parent / "data" / "floor_transfers.json"


def main():
    map_data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    transfers = json.loads(TRANSFER_PATH.read_text(encoding="utf-8"))["transfers"]

    examples = [
        ("p001_ground_floor_room_e20", "p001_ground_floor_room_e1"),
        ("p001_ground_floor_room_e20", "p002_first_floor_room_d14"),
    ]
    for start, destination in examples:
        result = find_route(
            map_data,
            start=start,
            destination=destination,
            transfers=transfers,
        )
        print(f"{start} -> {destination}: {result['status']}")
        if result["status"] == "ok":
            print("  nodes:", " -> ".join(result["node_ids"]))
            print("  floor changes:", result["floor_changes"])
        else:
            print(" ", result["message"])


if __name__ == "__main__":
    main()
