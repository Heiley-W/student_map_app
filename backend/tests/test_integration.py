import json
import tempfile
import unittest
from datetime import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from routing import find_route


ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = ROOT / "gemini-map" / "map_graph.json"
TRANSFER_PATH = Path(__file__).resolve().parents[1] / "data" / "floor_transfers.json"
NODE_MAP_PATH = ROOT / "frontend" / "route-node-map.json"
CHECKPOINT_MAP_PATH = ROOT / "gemini-map" / "map_graph.checkpoint.json"


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

    def test_step_free_mode_allows_unknown_walkway_accessibility(self):
        result = find_route(
            self.map_data,
            "p001_ground_floor_room_e20",
            "p001_ground_floor_room_e1",
            wheelchair_required=True,
            transfers=self.transfers,
        )

        self.assertEqual(result["status"], "ok")

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
            find_route(graph, "a", "b", card_type="staff")["status"],
            "ok",
        )
        self.assertEqual(
            find_route(graph, "a", "b", card_type="visitor")["status"],
            "no_route",
        )
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                card_type="student",
                local_time=time(17, 59),
            )["status"],
            "ok",
        )
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                card_type="student",
                local_time=time(18, 0),
            )["status"],
            "no_route",
        )
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                card_type="postgrad",
                local_time=time(22, 0),
            )["status"],
            "ok",
        )
        graph["edges"][0]["wheelchair_accessible"] = None
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                card_type="staff",
                wheelchair_required=True,
            )["status"],
            "ok",
        )
        graph["edges"][0]["wheelchair_accessible"] = False
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                card_type="staff",
                wheelchair_required=True,
            )["status"],
            "no_route",
        )

    def test_step_free_mode_treats_null_transfer_accessibility_as_accessible(self):
        graph = {
            "nodes": [
                {"id": "a", "name": "A", "kind": "lift", "page": 1},
                {"id": "b", "name": "B", "kind": "lift", "page": 2},
            ],
            "edges": [],
        }
        transfer = {
            "from": "a",
            "to": "b",
            "group": "Lift",
            "kind": "lift",
            "wheelchair_accessible": None,
        }

        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                wheelchair_required=True,
                transfers=[transfer],
            )["status"],
            "ok",
        )
        transfer["wheelchair_accessible"] = False
        self.assertEqual(
            find_route(
                graph,
                "a",
                "b",
                wheelchair_required=True,
                transfers=[transfer],
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

    def test_admin_node_status_put_is_allowed_by_cors(self):
        response = self.client.options(
            "/admin/node-status",
            headers={
                "Origin": "http://localhost:5500",
                "Access-Control-Request-Method": "PUT",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "PUT",
            response.headers["access-control-allow-methods"],
        )

    def test_paged_checkpoint_map_is_served_and_routable(self):
        with patch.object(main, "MAP_PATH", CHECKPOINT_MAP_PATH):
            catalog = self.client.get("/map")
            route = self.client.post(
                "/route",
                json={
                    "start": "p001_ground_floor_room_e20",
                    "destination": "p002_first_floor_room_d14",
                },
            )

        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(len(catalog.json()["nodes"]), 280)
        self.assertEqual(catalog.json()["nodes"][0]["page"], 1)
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["status"], "ok")
        self.assertEqual(route.json()["floor_changes"], 1)

    def test_closed_node_blocks_incident_edges_until_reopened(self):
        graph = {
            "nodes": [
                {"id": "a", "name": "A", "kind": "room", "page": 1},
                {"id": "b", "name": "B", "kind": "room", "page": 1},
            ],
            "edges": [{"from": "a", "to": "b", "blocked": False}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "map.json"
            transfer_path = root / "transfers.json"
            status_path = root / "node_status.json"
            map_path.write_text(json.dumps(graph), encoding="utf-8")
            transfer_path.write_text('{"transfers":[]}', encoding="utf-8")
            status_path.write_text('{"blocked_nodes":[]}', encoding="utf-8")

            with (
                patch.object(main, "MAP_PATH", map_path),
                patch.object(main, "TRANSFER_PATH", transfer_path),
                patch.object(main, "NODE_STATUS_PATH", status_path),
            ):
                blocked = self.client.put(
                    "/admin/node-status",
                    json={"node_id": "a", "out_of_order": True},
                )
                blocked_route = self.client.post(
                    "/route",
                    json={"start": "a", "destination": "b", "card_type": "staff"},
                )
                reopened = self.client.put(
                    "/admin/node-status",
                    json={"node_id": "a", "out_of_order": False},
                )
                open_route = self.client.post(
                    "/route",
                    json={"start": "a", "destination": "b", "card_type": "staff"},
                )

        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked_route.json()["status"], "no_route")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(open_route.json()["status"], "ok")

    def test_closed_node_blocks_floor_transfer(self):
        map_data = {
            "nodes": [
                {"id": "a", "name": "A", "kind": "lift", "page": 1},
                {"id": "b", "name": "B", "kind": "lift", "page": 2},
            ],
            "edges": [],
        }
        transfers = [
            {"from": "a", "to": "b", "kind": "lift", "group": "Lift"}
        ]

        main._apply_node_blocks(map_data, transfers, {"a"})

        self.assertTrue(transfers[0]["blocked"])


if __name__ == "__main__":
    unittest.main()
