
#!/usr/bin/env python3

"""
Extract a multi-floor navigation graph from a PDF using Gemini.

Features:
- Detects the floor name from the top-left of each PDF page.
- Identifies rooms, doors, corridors, junctions, turns, stairs and lifts.
- Extracts arrow annotations and their target locations.
- Retries temporary Gemini API failures (429, 500, 502, 503, 504).
- Saves a checkpoint after each successfully processed page.
- Resumes from saved pages on subsequent runs.
- Generates annotated preview images for every page.

Install:
    pip install google-genai pymupdf pydantic pillow matplotlib

Set API key:
    export GEMINI_API_KEY="YOUR_API_KEY"

Run:
    python pdf_to_json_map.py "hogwarts_map.pdf"

Options:
    --output map_graph.json
    --model gemini-3.8-flash
    --dpi 180
    --no-resume
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Literal

import fitz
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Circle
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ============================================================
# 1. Structured output schemas
# ============================================================

NodeKind = Literal[
    "room",
    "door",
    "junction",
    "turning_point",
    "stairs",
    "lift",
    "entrance",
    "other",
]


class Node(BaseModel):
    id: str
    name: str
    kind: NodeKind

    # Percentages relative to the individual PDF page.
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)

    floor: str
    confidence: float = Field(ge=0, le=1)
    notes: str = ""


class Edge(BaseModel):
    id: str
    source: str
    target: str

    time_seconds: float | None = None
    wheelchair_accessible: bool | None = None
    requires_id_card: bool | None = None
    blocked: bool | None = None

    confidence: float = Field(ge=0, le=1)
    notes: str = ""


class ArrowNote(BaseModel):
    id: str
    text: str

    # Arrow target position, as page percentages.
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)

    # Text label position, when identifiable.
    label_x: float | None = Field(default=None, ge=0, le=100)
    label_y: float | None = Field(default=None, ge=0, le=100)

    direction: str = "unknown"
    target_node_id: str | None = None
    floor: str
    confidence: float = Field(ge=0, le=1)


class GraphExtraction(BaseModel):
    floor: str
    nodes: list[Node]
    edges: list[Edge]
    arrow_notes: list[ArrowNote] = Field(default_factory=list)


# ============================================================
# 2. Gemini prompt
# ============================================================

PROMPT = """
Extract a complete indoor navigation graph from this floor-plan image.

This image represents ONE FLOOR of a multi-floor building.

FLOOR NAME
- Read the floor name printed in the TOP-LEFT corner.
- Preserve the wording as printed.
- Return the name in the top-level floor field and every node and arrow note.
- If unreadable, use "Unknown floor".
- Do not infer the floor name from other pages.

COORDINATES
- x and y must be percentages from 0 to 100.
- (0, 0) is the top-left corner.
- (100, 100) is the bottom-right corner.
- Node coordinates indicate the actual feature location.
- Arrow-note coordinates indicate the point the arrow points to.
- label_x and label_y indicate the annotation text location when identifiable.

NODES
Identify:
- Rooms and named destinations.
- Doors and corridor openings.
- Corridor junctions.
- Every significant corridor bend and turning point.
- Entrances and exits.
- Staircases, stairwells and stair access points.
- Lifts, elevators, lift shafts and lift access points.
- Other significant navigation landmarks.

STAIRS
- Use kind="stairs".
- Identify stair symbols and visible labels such as "Stairs",
  "Staircase", "Fire Escape" and similar wording.
- Place nodes at actual stair access locations.
- If a separate stair lobby or entrance is shown, create a separate node
  if needed for routing.
- Connect stairs to corridors only where a walkable connection is visible.
- Do not invent stair connections between floors.

LIFTS
- Use kind="lift".
- Identify lift symbols, elevator symbols, shafts and visible labels.
- Preserve labels such as "Lift", "Elevator" or lift numbers.
- Place nodes at actual lift access locations.
- If a separate lift lobby or entrance is shown, create a separate node
  if needed for routing.
- Connect lifts to corridors only where a walkable connection is visible.
- Do not invent cross-floor connections.

WALKABLE EDGES
- Each edge represents a directly walkable connection on THIS floor.
- Follow visible corridors, doorways and openings.
- Every significant corridor bend must be a node.
- Connect consecutive nodes along corridors.
- Do not connect nodes through walls.
- Do not connect rooms without evidence of a doorway or opening.
- Do not treat annotation arrows as walkable paths.
- Do not create edges to another floor.
- Do not assume nearby nodes are connected just because they are close.
- If traversal time is unknown, use null.
- Use null for unknown wheelchair accessibility, access-card requirements
  and blocked status.

ARROW ANNOTATIONS
- Read all visible notes and labels associated with arrows.
- Preserve the text as accurately as possible.
- Record each annotation in arrow_notes.
- Include arrow target coordinates and text position when identifiable.
- Describe arrow direction when identifiable.
- If an arrow points to a room, lift, staircase, door or corridor,
  associate it with that node if the target is clear.
- Also add relevant annotation information to the node's notes.
- Do not create navigation edges from annotation arrows.
- If a target is unclear, state that in the notes.

OUTPUT RULES
- Return floor, nodes, edges and arrow_notes.
- Do not invent locations, labels, floor names or connections.
- Use unique node and edge IDs within this page.
- Use snake_case IDs.
- Include all significant corridor turns.
- Return coordinates relative to this page, not the whole PDF.
"""


# ============================================================
# 3. Helpers
# ============================================================

def safe_id(value: str) -> str:
    """Convert text into a simple snake_case identifier."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "unnamed"


def unique_id(base: str, used: set[str]) -> str:
    """Generate a unique identifier."""
    candidate = base
    suffix = 2

    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1

    used.add(candidate)
    return candidate


def normalise_floor_name(name: str | None) -> str:
    if not name or not name.strip():
        return "Unknown floor"
    return name.strip()


def render_pdf_page(page: fitz.Page, dpi: int) -> Image.Image:
    """Render a PDF page as a PIL image."""
    scale = dpi / 72
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
    )

    return Image.frombytes(
        "RGB",
        [pix.width, pix.height],
        pix.samples,
    )


def save_json(path: Path, data: dict) -> None:
    """Write JSON safely via a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_path.replace(path)


# ============================================================
# 4. Retry temporary Gemini API errors
# ============================================================

def get_status_code(exc: Exception) -> int | None:
    """Extract an HTTP status code from a Google SDK exception."""
    code = getattr(exc, "code", None)

    if code is None:
        code = getattr(exc, "status_code", None)

    try:
        if code is not None:
            return int(code)
    except (TypeError, ValueError):
        pass

    match = re.search(
        r"\b(429|500|502|503|504)\b",
        str(exc),
    )

    return int(match.group(1)) if match else None


def generate_content_with_retry(
    client,
    *,
    model: str,
    contents,
    config,
    max_attempts: int = 6,
):
    """
    Retry temporary service errors with exponential backoff.

    Other errors are raised immediately.
    """

    retryable_codes = {429, 500, 502, 503, 504}

    for attempt in range(max_attempts):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        except Exception as exc:
            status_code = get_status_code(exc)

            if status_code not in retryable_codes:
                raise

            if attempt == max_attempts - 1:
                raise

            delay = min(2 ** (attempt + 1), 60)
            delay += random.uniform(0, 1)

            print(
                f"  Gemini HTTP {status_code}: {exc}",
                file=sys.stderr,
            )
            print(
                f"  Retrying in {delay:.1f}s "
                f"(attempt {attempt + 2}/{max_attempts})...",
                flush=True,
            )

            time.sleep(delay)


# ============================================================
# 5. Extract one page
# ============================================================

def extract_page(
    client,
    model: str,
    page_image: Image.Image,
    page_number: int,
) -> dict:

    response = generate_content_with_retry(
        client,
        model=model,
        contents=[PROMPT, page_image],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GraphExtraction,
            temperature=0,
        ),
    )

    extraction = response.parsed

    if extraction is None:
        raise RuntimeError(
            f"Gemini returned no valid structured result for page "
            f"{page_number}. Response: {response.text}"
        )

    floor_name = normalise_floor_name(extraction.floor)
    prefix = f"p{page_number:03d}_{safe_id(floor_name)}"

    # ----- Nodes -----

    id_map: dict[str, str] = {}
    used_node_ids: set[str] = set()
    nodes: list[dict] = []

    for node in extraction.nodes:
        new_id = unique_id(
            f"{prefix}_{safe_id(node.id)}",
            used_node_ids,
        )

        id_map[node.id] = new_id

        item = node.model_dump()
        item["id"] = new_id
        item["floor"] = floor_name
        item["page"] = page_number

        nodes.append(item)

    # ----- Edges -----

    used_edge_ids: set[str] = set()
    edges: list[dict] = []

    for edge in extraction.edges:
        source_id = id_map.get(edge.source)
        target_id = id_map.get(edge.target)

        if source_id is None or target_id is None:
            print(
                f"Warning: skipped edge {edge.id!r} on page "
                f"{page_number}: missing endpoint.",
                file=sys.stderr,
            )
            continue

        if source_id == target_id:
            continue

        item = edge.model_dump()
        item["id"] = unique_id(
            f"{prefix}_{safe_id(edge.id)}",
            used_edge_ids,
        )

        # These names match the React graph format.
        item["from"] = source_id
        item["to"] = target_id

        item.pop("source", None)
        item.pop("target", None)

        item["floor"] = floor_name
        item["page"] = page_number

        edges.append(item)

    # ----- Arrow notes -----

    arrow_notes: list[dict] = []

    for index, note in enumerate(extraction.arrow_notes, start=1):
        item = note.model_dump()

        item["id"] = f"{prefix}_arrow_{index:03d}"
        item["floor"] = floor_name
        item["page"] = page_number

        old_target = item.get("target_node_id")
        if old_target:
            item["target_node_id"] = id_map.get(old_target)

        arrow_notes.append(item)

    return {
        "page": page_number,
        "floor": floor_name,
        "nodes": nodes,
        "edges": edges,
        "arrow_notes": arrow_notes,
    }


# ============================================================
# 6. Annotated preview
# ============================================================

NODE_COLORS = {
    "room": "lime",
    "door": "orange",
    "junction": "cyan",
    "turning_point": "magenta",
    "stairs": "red",
    "lift": "deepskyblue",
    "entrance": "yellow",
    "other": "white",
}


def draw_preview(
    page_image: Image.Image,
    page_graph: dict,
    output_path: Path,
) -> None:

    width, height = page_image.size

    fig, ax = plt.subplots(figsize=(16, 12))
    ax.imshow(page_image)
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")

    nodes = page_graph["nodes"]
    edges = page_graph["edges"]
    notes = page_graph["arrow_notes"]

    node_by_id = {node["id"]: node for node in nodes}

    def pixel_xy(item: dict) -> tuple[float, float]:
        return (
            float(item["x"]) * width / 100,
            float(item["y"]) * height / 100,
        )

    # Walkable edges.
    for edge in edges:
        source = node_by_id.get(edge["from"])
        target = node_by_id.get(edge["to"])

        if source is None or target is None:
            continue

        x1, y1 = pixel_xy(source)
        x2, y2 = pixel_xy(target)

        ax.plot(
            [x1, x2],
            [y1, y2],
            color="deepskyblue",
            linewidth=1.5,
            alpha=0.85,
            zorder=3,
        )

    # Nodes and labels.
    for node in nodes:
        x, y = pixel_xy(node)
        kind = node.get("kind", "other")
        color = NODE_COLORS.get(kind, "white")

        marker = "s" if kind in ("stairs", "lift") else "o"
        size = 85 if kind in ("stairs", "lift") else 42

        ax.scatter(
            x,
            y,
            c=color,
            marker=marker,
            s=size,
            edgecolors="black",
            linewidths=0.7,
            zorder=5,
        )

        ax.annotate(
            f'{node["name"]} [{kind}]',
            (x, y),
            xytext=(5, -8),
            textcoords="offset points",
            fontsize=7,
            color="white",
            bbox={
                "facecolor": "black",
                "alpha": 0.7,
                "pad": 1.5,
            },
            zorder=6,
        )

    # Arrow notes and targets.
    for note in notes:
        x = float(note["x"]) * width / 100
        y = float(note["y"]) * height / 100

        ax.scatter(
            x,
            y,
            marker="x",
            color="orange",
            s=90,
            linewidths=2,
            zorder=8,
        )

        label_x = note.get("label_x")
        label_y = note.get("label_y")

        if label_x is not None and label_y is not None:
            lx = float(label_x) * width / 100
            ly = float(label_y) * height / 100

            ax.annotate(
                "",
                xy=(x, y),
                xytext=(lx, ly),
                arrowprops={
                    "arrowstyle": "->",
                    "color": "orange",
                    "linewidth": 1.5,
                },
                zorder=7,
            )

            text_xy = (lx, ly)
        else:
            text_xy = (x, y)

        ax.annotate(
            f'NOTE: {note["text"]}',
            text_xy,
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=7,
            color="white",
            bbox={
                "facecolor": "darkorange",
                "alpha": 0.85,
                "pad": 2,
            },
            zorder=9,
        )

    ax.set_title(
        f'Page {page_graph["page"]} — {page_graph["floor"]}\n'
        f'{len(nodes)} nodes | {len(edges)} edges | '
        f'{len(notes)} arrow notes',
        fontsize=13,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig.savefig(
        output_path,
        dpi=160,
        bbox_inches="tight",
        pad_inches=0.1,
    )
    plt.close(fig)


# ============================================================
# 7. Main and checkpoint/resume
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a multi-floor navigation graph from a PDF."
    )

    parser.add_argument("pdf", help="Input PDF floor plan")
    parser.add_argument(
        "--output",
        default="map_graph.json",
        help="Output graph JSON file",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.8-flash",
        help="Gemini model name",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,
        help="PDF rendering resolution",
    )
    parser.add_argument(
        "--previews",
        default="map_previews",
        help="Preview output directory",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the existing checkpoint and process every page again",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    preview_dir = Path(args.previews).expanduser().resolve()

    # Keep checkpoint next to the output JSON.
    checkpoint_path = output_path.with_name(
        output_path.stem + ".checkpoint.json"
    )

    if not pdf_path.is_file():
        parser.error(f"PDF not found: {pdf_path}")

    if args.dpi <= 0:
        parser.error("--dpi must be greater than zero")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        parser.error("GEMINI_API_KEY environment variable is not set")

    # Load checkpoint only when it belongs to this PDF.
    completed_pages: dict[str, dict] = {}

    if checkpoint_path.exists() and not args.no_resume:
        try:
            saved = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )

            if saved.get("source_pdf") == str(pdf_path):
                completed_pages = saved.get("pages", {})
                print(
                    f"Loaded checkpoint: {len(completed_pages)} "
                    "previously completed page(s)."
                )
            else:
                print(
                    "Checkpoint belongs to a different PDF; "
                    "starting fresh."
                )

        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Could not read checkpoint ({exc}); starting fresh.",
                file=sys.stderr,
            )

    if args.no_resume:
        completed_pages = {}

    client = genai.Client(api_key=api_key)

    with fitz.open(pdf_path) as pdf:
        total_pages = len(pdf)

        print(f"PDF: {pdf_path}")
        print(f"Pages: {total_pages}")
        print(f"Model: {args.model}")
        print(f"Checkpoint: {checkpoint_path}")

        for page_index, page in enumerate(pdf):
            page_number = page_index + 1
            page_key = str(page_number)

            print(
                f"\nPage {page_number}/{total_pages}",
                flush=True,
            )

            # Render the page even when the graph is cached, so previews
            # can be regenerated if needed.
            page_image = render_pdf_page(page, args.dpi)

            if page_key in completed_pages:
                page_graph = completed_pages[page_key]
                print(
                    f"  Reusing saved extraction: "
                    f'{page_graph.get("floor", "Unknown floor")}'
                )
            else:
                print("  Calling Gemini...")

                # If this fails after retries, earlier pages remain saved.
                page_graph = extract_page(
                    client=client,
                    model=args.model,
                    page_image=page_image,
                    page_number=page_number,
                )

                completed_pages[page_key] = page_graph

                # Save immediately after each successful page.
                save_json(
                    checkpoint_path,
                    {
                        "source_pdf": str(pdf_path),
                        "pages": completed_pages,
                    },
                )

                print(
                    f'  Extracted floor: {page_graph["floor"]}'
                )

            preview_path = (
                preview_dir / f"graph_page_{page_number:03d}.png"
            )

            draw_preview(
                page_image,
                page_graph,
                preview_path,
            )

            print(
                f'  Nodes: {len(page_graph["nodes"])} | '
                f'Edges: {len(page_graph["edges"])} | '
                f'Arrow notes: {len(page_graph["arrow_notes"])}'
            )
            print(f"  Preview: {preview_path}")

    # Assemble final graph in PDF page order.
    page_graphs = [
        completed_pages[str(page_number)]
        for page_number in range(1, total_pages + 1)
        if str(page_number) in completed_pages
    ]

    if len(page_graphs) != total_pages:
        raise RuntimeError(
            "Not all pages were extracted. The checkpoint has been "
            "preserved; run the command again to retry missing pages."
        )

    all_nodes = [
        node
        for graph in page_graphs
        for node in graph["nodes"]
    ]

    all_edges = [
        edge
        for graph in page_graphs
        for edge in graph["edges"]
    ]

    all_arrow_notes = [
        note
        for graph in page_graphs
        for note in graph["arrow_notes"]
    ]

    floors = [
        {
            "page": graph["page"],
            "floor": graph["floor"],
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "arrow_note_count": len(graph["arrow_notes"]),
        }
        for graph in page_graphs
    ]

    result = {
        "metadata": {
            "source_pdf": str(pdf_path),
            "page_count": total_pages,
            "floor_count": len(floors),
            "node_count": len(all_nodes),
            "edge_count": len(all_edges),
            "arrow_note_count": len(all_arrow_notes),
            "coordinate_system": (
                "x and y are percentages from 0 to 100 relative "
                "to each individual PDF page"
            ),
        },
        "floors": floors,
        "nodes": all_nodes,
        "edges": all_edges,
        "arrow_notes": all_arrow_notes,
    }

    save_json(output_path, result)

    print("\nExtraction complete.")
    print(f"JSON: {output_path}")
    print(f"Previews: {preview_dir}")
    print(f"Floors: {len(floors)}")
    print(f"Nodes: {len(all_nodes)}")
    print(f"Edges: {len(all_edges)}")
    print(f"Arrow notes: {len(all_arrow_notes)}")
    print(f"Checkpoint: {checkpoint_path}")

    print(
        "\nReview the preview images for missed labels, stairs, lifts, "
        "arrows, and corridor connections. Cross-floor connections must "
        "be verified before using the graph for multi-floor routing."
    )


if __name__ == "__main__":
    main()


