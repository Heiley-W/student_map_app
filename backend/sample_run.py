"""Run the routing examples against the bundled sample map."""
import json
from pathlib import Path

from routing import find_route


DATA_PATH = Path(__file__).parent / "data" / "sample_map_graph.json"


def main() -> None:
    map_data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    cases = [
        (False, False, "No access filters"),
        (True, False, "Wheelchair required"),
        (False, True, "Authorised card available"),
        (True, True, "Wheelchair and card"),
    ]

    for wheelchair_required, has_id_card, label in cases:
        result = find_route(
            map_data,
            start="great_hall",
            destination="library",
            wheelchair_required=wheelchair_required,
            has_id_card=has_id_card,
        )
        names = " -> ".join(point["name"] for point in result["points"])
        print(f"{label}: {names} ({result['total_seconds']} seconds)")


if __name__ == "__main__":
    main()
