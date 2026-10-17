import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
from routing import find_route


ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = ROOT / "gemini-map" / "map_graph.json"
TRANSFER_PATH = Path(__file__).resolve().parents[1] / "data" / "floor_transfers.json"
NODE_MAP_PATH = ROOT / "frontend" / "route-node-map.json"


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map_data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        cls.transfers = json.loads(TRANSFER_PATH.read_text(encoding="utf-8"))["transfers"]
        cls.node_ids = {node["id"] for node in cls.map_data["nodes"]}

    def test_same_floor_route_uses_extracted_graph(self):
        result = find_route(
            self.map_data,
            "p001_ground_floor_room_e20",
            "p001_ground_floor_room_e1",
            transfers=self.transfers,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["floor_changes"], 0)
        self.assertTrue(all(point["page"] == 1 for point in result["points"]))

    def test_cross_floor_route_uses_explicit_transfer(self):
        result = find_route(
            self.map_data,
            "p001_ground_floor_room_e20",
            "p002_first_floor_room_d14",
            transfers=self.transfers,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["floor_changes"], 1)
        self.assertTrue(any(edge["floor_change"] for edge in result["edge_ids"]))

    def test_step_free_mode_rejects_unknown_walkway_accessibility(self):
        result = find_route(
            self.map_data,
            "p001_ground_floor_room_e20",
            "p001_ground_floor_room_e1",
            wheelchair_required=True,
            transfers=self.transfers,
        )

        self.assertEqual(result["status"], "no_route")

    def test_frontend_room_and_connector_mappings_exist_in_graph(self):
        config = json.loads(NODE_MAP_PATH.read_text(encoding="utf-8"))
        mapped_ids = {
            node_id
            for floor in config["rooms"].values()
            for node_id in floor.values()
        }
        mapped_ids.update(
            node_id
            for floor in config["connectors"].values()
            for node_id in floor.values()
        )

        self.assertTrue(mapped_ids)
        self.assertEqual(mapped_ids - self.node_ids, set())
        self.assertEqual(
            {str(page) for page in config["floorPages"].values()},
            {str(floor["page"]) for floor in self.map_data["floors"]},
        )
        self.assertEqual(set(config["transforms"]), set(config["floorPages"]))

    def test_transfer_endpoints_exist_in_graph(self):
        for transfer in self.transfers:
            with self.subTest(transfer=transfer["id"]):
                self.assertIn(transfer["from"], self.node_ids)
                self.assertIn(transfer["to"], self.node_ids)

    def test_filters_require_confirmed_accessibility_and_card(self):
        graph = {
            "nodes": [
                {"id": "a", "name": "A", "kind": "room", "page": 1},
                {"id": "b", "name": "B", "kind": "room", "page": 1},
            ],
            "edges": [
                {
                    "from": "a",
                    "to": "b",
                    "wheelchair_accessible": True,
                    "requires_id_card": True,
                }
            ],
        }

        self.assertEqual(find_route(graph, "a", "b")["status"], "no_route")
        self.assertEqual(
            find_route(graph, "a", "b", has_id_card=True)["status"],
            "ok",
        )
        graph["edges"][0]["wheelchair_accessible"] = None
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                has_id_card=True,
                wheelchair_required=True,
            )["status"],
            "no_route",
        )

    def test_blocked_and_disconnected_edges_produce_no_route(self):
        graph = {
            "nodes": [
                {"id": "a", "name": "A", "kind": "room", "page": 1},
                {"id": "b", "name": "B", "kind": "room", "page": 1},
            ],
            "edges": [{"from": "a", "to": "b", "blocked": True}],
        }

        self.assertEqual(find_route(graph, "a", "b")["status"], "no_route")
        graph["edges"] = []
        self.assertEqual(find_route(graph, "a", "b")["status"], "no_route")


class RoutingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_map_catalog_exposes_node_ids_and_page_numbers(self):
        response = self.client.get("/map")

        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(len(catalog["nodes"]), 280)
        room = next(node for node in catalog["nodes"] if node["id"] == "p001_ground_floor_room_e20")
        self.assertEqual(room["page"], 1)

    def test_route_endpoint_returns_backend_route(self):
        response = self.client.post(
            "/route",
            json={
                "start": "p001_ground_floor_room_e20",
                "destination": "p002_first_floor_room_d14",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["floor_changes"], 1)

    def test_route_endpoint_rejects_unknown_node_ids(self):
        response = self.client.post(
            "/route",
            json={"start": "missing-node", "destination": "p001_ground_floor_room_e1"},
        )

        self.assertEqual(response.status_code, 404)

    def test_frontend_origin_is_allowed_by_cors(self):
        response = self.client.options(
            "/route",
            headers={
                "Origin": "http://localhost:5500",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://localhost:5500",
        )


if __name__ == "__main__":
    unittest.main()
