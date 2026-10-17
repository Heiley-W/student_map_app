
#!/usr/bin/env python3
"""Extract rooms, doors, corridor turning points and edges from a PDF map."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Literal

import fitz  # PyMuPDF
import matplotlib.pyplot as plt
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ---------- Structured Gemini output ----------

class Node(BaseModel):
    id: str = Field(description="Unique snake_case node ID")
    name: str = Field(description="Room, door, junction, or turning-point label")
    kind: Literal[
        "room", "door", "junction", "turning_point",
        "stairs", "entrance", "lift", "other"
    ]
    x: float = Field(ge=0, le=100, description="Horizontal position as image percentage")
    y: float = Field(ge=0, le=100, description="Vertical position as image percentage")
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


class Edge(BaseModel):
    id: str = Field(description="Unique snake_case edge ID")
    source: str = Field(description="ID of starting node")
    target: str = Field(description="ID of ending node")
    time_seconds: float | None = Field(
        description="Known or explicitly estimated traversal time; null if unknown"
    )
    wheelchair_accessible: bool | None = Field(
        description="True/false only when supported by map evidence; otherwise null"
    )
    requires_id_card: bool | None = Field(
        description="True/false only when supported by map evidence; otherwise null"
    )
    blocked: bool | None = Field(
        description="Whether explicitly shown to be blocked; null if unknown"
    )
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


class GraphExtraction(BaseModel):
    nodes: list[Node]
    edges: list[Edge]


PROMPT = r"""
Extract a navigation graph from this floor-plan page.

COORDINATES
- x and y must be percentages from 0 to 100 relative to THIS PAGE IMAGE.
- (0,0) is the top-left corner; (100,100) is the bottom-right.
- Put every node at its actual location on the map.

NODES
1. Create a node for each identifiable named room or destination.
2. Create nodes for doors/openings at the doorway itself.
3. Create nodes for corridor junctions, intersections, and branch points.
4. IMPORTANT: every significant corridor turn or bend must be a node.
   A continuous corridor that turns twice should have a node at each turn.
5. Add nodes at stairs, lifts, entrances, and floor-transition points.
6. Split corridors into edges between consecutive nodes. Do not represent a
   long winding corridor as a single straight edge.
7. Give each node a unique snake_case ID within this page.
8. Place room nodes inside the room, preferably near its centre.
9. Place corridor and turning-point nodes on the centreline of the walkable path.

EDGES
1. Each edge represents one directly walkable segment between two nodes.
2. Connect only consecutive nodes along the same visible passage.
3. Do not connect nodes merely because they are close together.
4. Never draw an edge through a wall.
5. Connect rooms through actual visible doorways, not through the middle of walls.
6. Include all intermediate turning-point nodes so the edge sequence follows
   the corridor's actual shape.
7. Do not create connections to another PDF page unless the connection is
   explicitly represented on this page. Different pages may show different floors.
8. Use unique snake_case edge IDs. Edges are undirected for navigation.
9. Do not include geometry waypoints in an edge: represent turns as nodes.

ACCESS AND TIMING
- time_seconds: use null unless a traversal time is explicitly provided or a
  clearly labelled, defensible estimate can be made from a visible scale.
  Do not invent precise walking times from an unscaled image.
- wheelchair_accessible: use null unless accessibility is explicitly indicated
  or unambiguously established by the map. Stairs alone are not accessible.
- requires_id_card: true only if an ID-card reader or explicit access restriction
  is visible; false only when the map establishes no such restriction; otherwise null.
- blocked: true only for a passage explicitly shown as blocked; false if visibly
  open and traversable; otherwise null.
- Put uncertainty in notes and use a lower confidence score when appropriate.

QUALITY CONTROL
- Every edge's source and target must refer to nodes in this output.
- Do not invent unseen doors, corridors, rooms, or connections.
- Prefer an incomplete but evidence-based graph over guessed connectivity.
- Return only the requested structured data.
"""


def safe_id(text):
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "node"


def extract_page(client, model, page_png, page_number):
    response = client.models.generate_content(
        model=model,
        contents=[
            PROMPT,
            types.Part.from_bytes(
                data=page_png.read_bytes(),
                mime_type="image/png",
            ),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GraphExtraction,
            temperature=0.1,
        ),
    )

    if not response.text:
        raise RuntimeError(f"Gemini returned no result for PDF page {page_number}.")

    extraction = GraphExtraction.model_validate_json(response.text)

    # Prefix IDs by page to guarantee uniqueness across PDF pages.
    id_map = {}
    nodes = []

    for node in extraction.nodes:
        old_id = node.id
        new_id = f"p{page_number}_{safe_id(old_id)}"

        if old_id in id_map:
            raise ValueError(
                f"Duplicate node ID {old_id!r} on PDF page {page_number}."
            )
        id_map[old_id] = new_id

        item = node.model_dump()
        item["id"] = new_id
        item["page"] = page_number
        nodes.append(item)

    edges = []
    used_edge_ids = set()
    node_ids = {node["id"] for node in nodes}

    for edge in extraction.edges:
        if edge.source not in id_map or edge.target not in id_map:
            print(
                f"Warning: skipping edge with unknown endpoint on page "
                f"{page_number}: {edge.source} -> {edge.target}"
            )
            continue

        source = id_map[edge.source]
        target = id_map[edge.target]

        if source == target:
            continue

        edge_id = f"p{page_number}_{safe_id(edge.id)}"
        if edge_id in used_edge_ids:
            suffix = 2
            while f"{edge_id}_{suffix}" in used_edge_ids:
                suffix += 1
            edge_id = f"{edge_id}_{suffix}"
        used_edge_ids.add(edge_id)

        item = edge.model_dump()
        item["id"] = edge_id
        item["from"] = source
        item["to"] = target
        item["page"] = page_number
        del item["source"]
        del item["target"]

        edges.append(item)

    # Final endpoint check.
    edges = [
        e for e in edges
        if e["from"] in node_ids and e["to"] in node_ids
    ]
    return nodes, edges


def draw_preview(page_image, nodes, edges, output_path, page_number):
    """Draw extracted edges and nodes over the corresponding PDF page."""
    image = Image.open(page_image).convert("RGB")
    width, height = image.size

    fig, ax = plt.subplots(figsize=(14, 16))
    ax.imshow(image)
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)

    page_nodes = {n["id"]: n for n in nodes if n["page"] == page_number}
    page_edges = [e for e in edges if e["page"] == page_number]

    def pixel_xy(node):
        return node["x"] / 100 * width, node["y"] / 100 * height

    # Draw edges first so the nodes remain visible.
    for edge in page_edges:
        a = page_nodes.get(edge["from"])
        b = page_nodes.get(edge["to"])
        if a is None or b is None:
            continue

        x1, y1 = pixel_xy(a)
        x2, y2 = pixel_xy(b)
        ax.plot(
            [x1, x2], [y1, y2],
            color="deepskyblue",
            linewidth=2,
            alpha=0.9,
            zorder=3,
        )

        # Label edge ID near the midpoint.
        ax.text(
            (x1 + x2) / 2,
            (y1 + y2) / 2,
            edge["id"],
            fontsize=5,
            color="white",
            bbox=dict(facecolor="blue", alpha=0.65, pad=1, edgecolor="none"),
            zorder=5,
        )

    colors = {
        "room": "lime",
        "door": "orange",
        "junction": "cyan",
        "turning_point": "magenta",
        "stairs": "red",
        "entrance": "yellow",
        "lift": "white",
        "other": "white",
    }

    for node in page_nodes.values():
        x, y = pixel_xy(node)
        ax.scatter(
            x, y,
            s=40,
            c=colors.get(node["kind"], "white"),
            edgecolors="black",
            linewidths=0.7,
            zorder=6,
        )
        ax.annotate(
            f'{node["name"]}\n{node["id"]}',
            (x, y),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=6,
            color="white",
            bbox=dict(facecolor="black", alpha=0.65, pad=1, edgecolor="none"),
            zorder=7,
        )

    ax.set_title(
        f"PDF page {page_number}: navigation graph\n"
        "Blue = edges; node colours indicate node type"
    )
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Convert a PDF floor plan into nodes, edges and preview images."
    )
    parser.add_argument("pdf", help="Input PDF map")
    parser.add_argument("--out", default="map_graph.json", help="Output JSON path")
    parser.add_argument(
        "--preview-dir",
        default="map_previews",
        help="Directory for page preview images",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.8-flash",
        help="Gemini model ID",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=160,
        help="PDF rendering resolution",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError('Set GEMINI_API_KEY first: export GEMINI_API_KEY="YOUR_KEY"')

    preview_dir = Path(args.preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)
    all_nodes = []
    all_edges = []

    with fitz.open(pdf_path) as pdf:
        if len(pdf) == 0:
            raise ValueError("The PDF contains no pages.")

        print(f"PDF: {pdf_path} ({len(pdf)} page(s))")

        for page_index, page in enumerate(pdf):
            page_number = page_index + 1
            print(f"\nProcessing page {page_number}/{len(pdf)}...")

            pix = page.get_pixmap(
                matrix=fitz.Matrix(args.dpi / 72, args.dpi / 72),
                alpha=False,
            )
            page_png = preview_dir / f"page_{page_number:03d}.png"
            pix.save(page_png)

            nodes, edges = extract_page(
                client, args.model, page_png, page_number
            )

            all_nodes.extend(nodes)
            all_edges.extend(edges)

            preview_path = preview_dir / f"graph_page_{page_number:03d}.png"
            draw_preview(
                page_png, all_nodes, all_edges, preview_path, page_number
            )

            print(f"  Nodes: {len(nodes)}")
            print(f"  Edges: {len(edges)}")
            print(f"  Preview: {preview_path}")

    result = {
        "metadata": {
            "source_pdf": str(pdf_path.resolve()),
            "coordinate_system": (
                "x and y are percentages of the corresponding PDF page; "
                "(0,0) is top-left."
            ),
            "node_count": len(all_nodes),
            "edge_count": len(all_edges),
            "notes": (
                "Null means unknown. Review graph preview images and verify "
                "connectivity before using for navigation."
            ),
        },
        "nodes": all_nodes,
        "edges": all_edges,
    }

    output_path = Path(args.out)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\nDone.")
    print(f"JSON graph: {output_path.resolve()}")
    print(f"Preview images: {preview_dir.resolve()}")
    print(f"Total nodes: {len(all_nodes)}")
    print(f"Total edges: {len(all_edges)}")


if __name__ == "__main__":
    main()

