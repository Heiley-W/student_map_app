
#!/usr/bin/env python3
"""
Extract a navigable, multi-floor graph from a floor-plan PDF using Gemini.

Features
--------
- Detects labelled and unlabelled rooms.
- Specifically looks for dark-grey, unlabelled lift symbols.
- Extracts room doorways, corridors, junctions, stairs and lifts.
- Validates room/lift connectivity and edge endpoints.
- Generates a graph preview for every page.
- Saves a checkpoint after each page and supports resuming.
- Produces JSON suitable for a React navigation app.

Usage
-----
python pdf_to_json_map.py "hogwarts_map.pdf"
python pdf_to_json_map.py "hogwarts_map.pdf" --no-resume
python pdf_to_json_map.py "hogwarts_map.pdf" --output map_graph.json
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal, Optional

import fitz  # PyMuPDF
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ============================================================
# Configuration
# ============================================================

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_ATTEMPTS = 6
INITIAL_RETRY_DELAY = 3

NODE_KINDS = Literal[
    "room",
    "door",
    "corridor",
    "junction",
    "turning_point",
    "stairs",
    "lift",
    "entrance",
    "other",
]


# ============================================================
# Structured output schema
# ============================================================

class Node(BaseModel):
    id: str = Field(description="Unique node ID within this page")
    name: str = Field(
        description="Room name, corridor label, or descriptive name"
    )
    kind: NODE_KINDS
    x: float = Field(
        description="Horizontal coordinate as percentage, 0 to 100"
    )
    y: float = Field(
        description="Vertical coordinate as percentage, 0 to 100"
    )
    floor: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)
    notes: str = ""


class Edge(BaseModel):
    source: str = Field(description="ID of the starting node")
    target: str = Field(description="ID of the destination node")
    time_seconds: Optional[float] = None
    wheelchair_accessible: Optional[bool] = None
    requires_id_card: Optional[bool] = None
    blocked: bool = False
    confidence: float = Field(default=0.8, ge=0, le=1)
    notes: str = ""


class ArrowNote(BaseModel):
    text: str
    x: float = Field(description="Arrow target x, percentage 0 to 100")
    y: float = Field(description="Arrow target y, percentage 0 to 100")
    label_x: Optional[float] = None
    label_y: Optional[float] = None
    direction: Optional[str] = None
    target_node_id: Optional[str] = None
    floor: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)


class GraphExtraction(BaseModel):
    floor: str
    nodes: list[Node]
    edges: list[Edge]
    arrow_notes: list[ArrowNote] = []


# ============================================================
# Extraction prompt
# ============================================================

PROMPT = r"""
You are extracting a real, walkable indoor navigation graph from a
floor-plan image. Accuracy of geometry and connectivity is essential.

The page is one floor of a multi-floor building. The floor name is
usually printed near the top-left of the page.

Return structured data using the supplied schema.

COORDINATES
- Every node coordinate must be a percentage of the page image:
  x = 0 at the left and 100 at the right.
  y = 0 at the top and 100 at the bottom.
- Place nodes at meaningful physical locations, not arbitrary points.
- Place room nodes near the centre of each room.
- Place door nodes at the actual door opening.
- Place lift nodes at the centre of the lift symbol.
- Place corridor and junction nodes on the walkable corridor space.

1. DETECT EVERY ROOM
- Identify every enclosed room, including rooms with no text,
  room number, label, furniture, or other contents.
- Use wall boundaries, room outlines, and door openings, not text alone.
- A blank enclosed space is still a room.
- Give labelled rooms their printed names.
- Name unlabelled rooms uniquely, for example:
  "Unlabelled Room 1", "Unlabelled Room 2".
- Number unlabelled rooms separately on each floor.
- Include small rooms, storage rooms, toilets, offices and utility rooms
  where their outlines are visible.
- Do not classify corridors, open halls, stairs or lifts as rooms.
- Do not create duplicate nodes for the same physical room.
- If a boundary is ambiguous, record the uncertainty in notes.

2. ROOM DOORS AND CORRIDOR CONNECTIONS
For EACH room:
- Find its actual entrance or doorway in the wall boundary.
- Create a distinct "door" node at that opening unless a suitable
  doorway node already exists.
- Add an edge between the room node and its doorway node.
- Add an edge between that doorway and the corridor or junction
  immediately outside the door.
- Ensure the doorway is connected to the wider corridor network.
- If a door opens into a small connecting passage, represent that
  passage with a corridor or junction node when necessary.
- A room must not be considered connected merely because its node
  is close to a corridor node in Euclidean distance.
- Never connect across a solid wall or through another room.
- Do not connect a room to a corridor that is not reachable through
  its actual opening.
- If you cannot determine the entrance confidently, still create the
  room node and explain the uncertain connection in its notes.
- Do not omit edges simply because a room has no printed name.

3. IMPORTANT LIFT DETECTION
- There is a lift represented by a dark-grey filled square or rectangle
  that may have NO text, letter, icon or label.
- Actively inspect dark-grey enclosed boxes and their surrounding walls.
- Do not require the word "LIFT" to appear.
- Classify the known dark-grey lift box as kind "lift".
- Give it the name "Lift" or "Lift (unlabelled)".
- Place its node at the centre of the box.
- Connect the lift to the corridor/junction at its real entrance.
- Do not mistake the lift for an ordinary room, wall, or solid obstruction.
- If there are multiple lifts, represent each separately.
- Do not infer an entrance through a solid wall.

4. CORRIDORS AND JUNCTIONS
- Represent the walkable corridor network with corridor and junction
  nodes, including bends, intersections and dead ends.
- Add edges along physically continuous walkable passages.
- Split a corridor at meaningful intersections, doors, stairs and lifts
  where this helps route finding.
- Connect doorways to the correct corridor segment.
- Do not connect corridors across walls or closed, impassable spaces.
- Avoid excessive redundant nodes in a straight corridor.

5. STAIRS AND FLOOR CHANGES
- Detect staircases and label them "stairs".
- Detect lifts even when their symbols are unlabelled.
- Connect stairs and lifts to the corridor network on this floor.
- Do NOT create cross-floor edges in this extraction.
- Cross-floor links will be matched separately by the application.

6. ARROWS AND ANNOTATIONS
- Record meaningful arrows, directional annotations and route notes.
- Preserve the text as written.
- Record the arrow target coordinates and, where possible, its target node.
- Do not interpret decorative arrows as passages.

7. EDGE RULES
- Every edge endpoint must reference an ID of an existing node.
- Every edge must represent a physically plausible walking connection.
- Edges are undirected unless the floor plan explicitly indicates
  one-way movement.
- Do not invent distances, accessibility or restrictions that are not
  shown. Leave unknown optional properties null.
- Do not create edges between nodes on different floors.
- Use unique node IDs within this page.
- Avoid duplicate edges.

8. FINAL SELF-CHECK
Before returning the graph, inspect every room individually:
- Is the room node present?
- Is its doorway represented?
- Is the room connected to that doorway?
- Is the doorway connected to the corridor immediately outside?
- Can the room reach the wider corridor network?
Also check the dark-grey unlabelled lift:
- Is there a lift node at its centre?
- Is it connected to the corridor at its actual entrance?

Do not falsely claim a connection when the floor plan does not establish it.
Use notes and confidence scores to communicate uncertainty.
"""


# ============================================================
# Gemini client and retry logic
# ============================================================

def make_client():
    """Create the Gemini client using the configured API key."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY is not set.\n"
            "Set it in your terminal before running this script."
        )

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def generate_with_retry(client, model, prompt, image):
    """Retry transient Gemini API failures with exponential backoff."""
    transient_markers = (
        "429",
        "500",
        "502",
        "503",
        "504",
        "RESOURCE_EXHAUSTED",
        "UNAVAILABLE",
        "INTERNAL",
        "DEADLINE_EXCEEDED",
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(
                model=model,
                contents=[prompt, image],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GraphExtraction,
                    temperature=0.1,
                ),
            )

        except Exception as exc:
            message = str(exc)
            is_transient = any(
                marker.lower() in message.lower()
                for marker in transient_markers
            )

            if not is_transient or attempt == MAX_ATTEMPTS:
                raise

            delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
            print(
                f"  Gemini request failed "
                f"({attempt}/{MAX_ATTEMPTS}): {message[:200]}"
            )
            print(f"  Retrying in {delay}s...")
            time.sleep(delay)


# ============================================================
# PDF rendering
# ============================================================

def render_page(page, dpi):
    """Render a PyMuPDF page as an RGB PIL image."""
    scale = dpi / 72
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
    )

    image = Image.frombytes(
        "RGB",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )

    return image


# ============================================================
# Helpers
# ============================================================

def slugify(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return slug.strip("_") or "floor"


def clamp_percentage(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0

    return max(0.0, min(100.0, value))


def normalise_floor_name(name, page_number):
    name = str(name or "").strip()
    if not name:
        return f"Page {page_number}"
    return name


def edge_key(source, target):
    """Undirected edge key for duplicate detection."""
    return tuple(sorted((source, target)))


# ============================================================
# Graph validation
# ============================================================

def validate_graph(nodes, edges):
    """
    Find invalid edges, duplicate edges, isolated rooms/lifts, and rooms
    or lifts that cannot reach a corridor-network node.

    Returns a list of human-readable warnings.
    Does not fabricate connections through walls.
    """
    warnings = []
    node_by_id = {node["id"]: node for node in nodes}
    adjacency = {node_id: set() for node_id in node_by_id}
    seen_edges = set()

    for edge in edges:
        source = edge.get("from", edge.get("source"))
        target = edge.get("to", edge.get("target"))

        if source not in node_by_id:
            warnings.append(
                f"Edge references missing source node: {source}"
            )
            continue

        if target not in node_by_id:
            warnings.append(
                f"Edge references missing target node: {target}"
            )
            continue

        if source == target:
            warnings.append(f"Self-loop edge on node {source}")
            continue

        key = edge_key(source, target)
        if key in seen_edges:
            warnings.append(f"Duplicate edge: {source} <-> {target}")
            continue

        seen_edges.add(key)
        adjacency[source].add(target)
        adjacency[target].add(source)

    # Corridor-network targets for reachability.
    corridor_kinds = {"corridor", "junction", "turning_point"}
    corridor_ids = {
        node_id
        for node_id, node in node_by_id.items()
        if node.get("kind") in corridor_kinds
    }

    # Find all nodes reachable from any corridor/junction.
    reachable_from_corridor = set(corridor_ids)
    stack = list(corridor_ids)

    while stack:
        current = stack.pop()
        for neighbour in adjacency.get(current, ()):
            if neighbour not in reachable_from_corridor:
                reachable_from_corridor.add(neighbour)
                stack.append(neighbour)

    # Check every room and lift.
    for node_id, node in node_by_id.items():
        kind = node.get("kind")
        name = node.get("name", node_id)

        if kind not in {"room", "lift"}:
            continue

        if not adjacency[node_id]:
            warnings.append(
                f"{kind.upper()} '{name}' ({node_id}) has NO edges."
            )
            continue

        if not corridor_ids:
            warnings.append(
                f"No corridor/junction nodes were extracted on this floor."
            )
            break

        if node_id not in reachable_from_corridor:
            warnings.append(
                f"{kind.upper()} '{name}' ({node_id}) cannot reach "
                f"the corridor network."
            )

        if kind == "room":
            neighbours = [
                node_by_id[n]
                for n in adjacency[node_id]
                if n in node_by_id
            ]
            if not any(n.get("kind") == "door" for n in neighbours):
                warnings.append(
                    f"ROOM '{name}' ({node_id}) has no directly connected "
                    f"door node. Check its entrance."
                )

    return warnings


# ============================================================
# Extraction and stable IDs
# ============================================================

def extract_page(client, model, image, page_number):
    """Extract one page and create page-prefixed node IDs."""
    print("  Sending page to Gemini...")

    response = generate_with_retry(client, model, PROMPT, image)

    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")

    try:
        parsed = GraphExtraction.model_validate_json(response.text)
    except Exception as exc:
        raise RuntimeError(
            f"Could not parse Gemini's structured response: {exc}"
        ) from exc

    floor_name = normalise_floor_name(parsed.floor, page_number)
    floor_slug = slugify(floor_name)
    prefix = f"p{page_number:03d}_{floor_slug}"

    # Remap IDs so IDs remain unique across all pages.
    id_mapping = {}

    for index, node in enumerate(parsed.nodes, start=1):
        old_id = node.id or f"node_{index}"
        id_mapping[old_id] = f"{prefix}_{slugify(old_id)}"

    nodes = []
    for node in parsed.nodes:
        item = node.model_dump()
        old_id = item["id"]
        item["id"] = id_mapping[old_id]
        item["floor"] = floor_name
        item["x"] = clamp_percentage(item["x"])
        item["y"] = clamp_percentage(item["y"])
        item["confidence"] = max(
            0.0, min(1.0, float(item.get("confidence", 0.8)))
        )
        nodes.append(item)

    edges = []
    for edge in parsed.edges:
        item = edge.model_dump()

        source = id_mapping.get(item.pop("source"), None)
        target = id_mapping.get(item.pop("target"), None)

        if source is None or target is None:
            print(
                "  WARNING: dropping edge with unknown endpoint: "
                f"{source} -> {target}"
            )
            continue

        item["from"] = source
        item["to"] = target
        item["floor"] = floor_name
        edges.append(item)

    arrow_notes = []
    for note in parsed.arrow_notes:
        item = note.model_dump()
        old_target = item.get("target_node_id")
        item["target_node_id"] = id_mapping.get(old_target, old_target)
        item["floor"] = floor_name
        item["x"] = clamp_percentage(item["x"])
        item["y"] = clamp_percentage(item["y"])

        if item.get("label_x") is not None:
            item["label_x"] = clamp_percentage(item["label_x"])
        if item.get("label_y") is not None:
            item["label_y"] = clamp_percentage(item["label_y"])

        arrow_notes.append(item)

    graph = {
        "floor": floor_name,
        "page": page_number,
        "nodes": nodes,
        "edges": edges,
        "arrow_notes": arrow_notes,
    }

    warnings = validate_graph(nodes, edges)
    graph["validation_warnings"] = warnings

    print(f"  Floor: {floor_name}")
    print(
        f"  Nodes: {len(nodes)} | Edges: {len(edges)} | "
        f"Arrow notes: {len(arrow_notes)}"
    )

    if warnings:
        print(f"  Connectivity warnings: {len(warnings)}")
        for warning in warnings:
            print(f"    - {warning}")
    else:
        print("  Graph validation: no connectivity issues detected.")

    return graph


# ============================================================
# Preview image
# ============================================================

def draw_preview(image, graph, output_path):
    """Draw nodes, graph edges and arrow annotations over the page."""
    width, height = image.size
    fig, ax = plt.subplots(figsize=(14, 14 * height / width))

    ax.imshow(image)
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")

    node_by_id = {node["id"]: node for node in graph["nodes"]}

    def pixel_position(node):
        return (
            node["x"] / 100.0 * width,
            node["y"] / 100.0 * height,
        )

    # Draw edges behind nodes.
    for edge in graph["edges"]:
        source_id = edge["from"]
        target_id = edge["to"]

        if source_id not in node_by_id or target_id not in node_by_id:
            continue

        x1, y1 = pixel_position(node_by_id[source_id])
        x2, y2 = pixel_position(node_by_id[target_id])

        ax.plot(
            [x1, x2],
            [y1, y2],
            color="cyan",
            linewidth=1.3,
            alpha=0.85,
            zorder=2,
        )

    colours = {
        "room": "lime",
        "door": "orange",
        "corridor": "deepskyblue",
        "junction": "magenta",
        "turning_point": "yellow",
        "stairs": "red",
        "lift": "white",
        "entrance": "blue",
        "other": "grey",
    }

    # Draw nodes and labels.
    for node in graph["nodes"]:
        x, y = pixel_position(node)
        kind = node.get("kind", "other")
        colour = colours.get(kind, "grey")

        if kind == "lift":
            ax.scatter(
                x, y, s=110, marker="s",
                c=colour, edgecolors="black", linewidths=1.5,
                zorder=5,
            )
        elif kind == "room":
            ax.scatter(
                x, y, s=65, marker="o",
                c=colour, edgecolors="black", linewidths=0.7,
                zorder=5,
            )
        else:
            ax.scatter(
                x, y, s=40, marker="o",
                c=colour, edgecolors="black", linewidths=0.5,
                zorder=5,
            )

        label = node.get("name", node["id"])
        if len(label) > 32:
            label = label[:29] + "..."

        ax.annotate(
            label,
            (x, y),
            xytext=(4, -5),
            textcoords="offset points",
            fontsize=6,
            color="black",
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "alpha": 0.75,
                "edgecolor": "none",
            },
            zorder=6,
        )

    # Draw arrow-note targets.
    for note in graph["arrow_notes"]:
        x = note["x"] / 100.0 * width
        y = note["y"] / 100.0 * height

        ax.scatter(
            x, y, s=100, marker="x",
            c="red", linewidths=2, zorder=7,
        )

        text = note.get("text", "Arrow")
        ax.annotate(
            text,
            (x, y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=6,
            color="red",
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "alpha": 0.8,
                "edgecolor": "red",
            },
            zorder=8,
        )

    # Display validation issues in the image title.
    warnings = graph.get("validation_warnings", [])
    status = (
        f"{len(warnings)} validation warning(s)"
        if warnings else "validation passed"
    )

    ax.set_title(
        f"{graph['floor']} — {len(graph['nodes'])} nodes, "
        f"{len(graph['edges'])} edges — {status}",
        fontsize=11,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
        pad_inches=0.1,
    )
    plt.close(fig)


# ============================================================
# Checkpoint and output
# ============================================================

def save_json(data, path):
    """Atomically write JSON to avoid leaving a partially written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    temporary_path.replace(path)


def load_checkpoint(path):
    path = Path(path)
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data.get("pages", {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: cannot read checkpoint: {exc}")
        return {}


def build_final_output(page_graphs, pdf_path):
    nodes = []
    edges = []
    arrow_notes = []
    floors = []

    for page_number in sorted(page_graphs, key=lambda x: int(x)):
        graph = page_graphs[page_number]

        floors.append({
            "name": graph["floor"],
            "page": graph["page"],
        })
        nodes.extend(graph["nodes"])
        edges.extend(graph["edges"])
        arrow_notes.extend(graph["arrow_notes"])

    all_warnings = []
    for page_number in sorted(page_graphs, key=lambda x: int(x)):
        graph = page_graphs[page_number]
        for warning in graph.get("validation_warnings", []):
            all_warnings.append({
                "floor": graph["floor"],
                "page": graph["page"],
                "warning": warning,
            })

    return {
        "metadata": {
            "source_pdf": str(pdf_path),
            "page_count": len(page_graphs),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "arrow_note_count": len(arrow_notes),
            "validation_warning_count": len(all_warnings),
            "coordinate_system": (
                "x and y are percentages from 0 to 100 "
                "relative to each PDF page"
            ),
            "cross_floor_edges_created": False,
        },
        "floors": floors,
        "nodes": nodes,
        "edges": edges,
        "arrow_notes": arrow_notes,
        "validation_warnings": all_warnings,
    }


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract a multi-floor navigation graph from a PDF."
    )
    parser.add_argument("pdf", help="Path to the floor-plan PDF")
    parser.add_argument(
        "--output",
        default="map_graph.json",
        help="Output JSON path (default: map_graph.json)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=160,
        help="PDF rendering resolution (default: 160)",
    )
    parser.add_argument(
        "--previews",
        default="map_previews",
        help="Preview output directory (default: map_previews)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Reprocess all pages instead of reusing checkpoint data",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    preview_dir = Path(args.previews).expanduser().resolve()

    if not pdf_path.exists():
        sys.exit(f"ERROR: PDF not found: {pdf_path}")

    if args.dpi < 72:
        sys.exit("ERROR: --dpi must be at least 72.")

    checkpoint_path = output_path.with_name(
        output_path.stem + ".checkpoint.json"
    )

    print(f"PDF:        {pdf_path}")
    print(f"Output:     {output_path}")
    print(f"Checkpoint: {checkpoint_path}")
    print(f"Previews:   {preview_dir}")
    print(f"Model:      {args.model}")

    try:
        client = make_client()
    except Exception as exc:
        sys.exit(f"ERROR: {exc}")

    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:
        sys.exit(f"ERROR: could not open PDF: {exc}")

    if len(pdf) == 0:
        pdf.close()
        sys.exit("ERROR: PDF contains no pages.")

    if args.no_resume:
        page_graphs = {}
        print("Resume disabled: all pages will be reprocessed.")
    else:
        page_graphs = load_checkpoint(checkpoint_path)
        if page_graphs:
            print(
                f"Loaded checkpoint with {len(page_graphs)} "
                f"previously extracted page(s)."
            )

    try:
        for page_index in range(len(pdf)):
            page_number = page_index + 1
            key = str(page_number)

            print(f"\nPage {page_number}/{len(pdf)}")

            if key in page_graphs and not args.no_resume:
                graph = page_graphs[key]
                print(f"  Reusing saved extraction: {graph['floor']}")
                print(
                    f"  Nodes: {len(graph['nodes'])} | "
                    f"Edges: {len(graph['edges'])} | "
                    f"Arrow notes: {len(graph['arrow_notes'])}"
                )

                # Regenerate preview even when reusing the extraction.
                image = render_page(pdf[page_index], args.dpi)
                preview_path = preview_dir / (
                    f"graph_page_{page_number:03d}.png"
                )
                draw_preview(image, graph, preview_path)
                print(f"  Preview: {preview_path}")
                continue

            image = render_page(pdf[page_index], args.dpi)

            try:
                graph = extract_page(
                    client=client,
                    model=args.model,
                    image=image,
                    page_number=page_number,
                )
            except Exception as exc:
                print(
                    f"\nERROR extracting page {page_number}: {exc}",
                    file=sys.stderr,
                )
                print(
                    "Previously completed pages remain in the checkpoint.",
                    file=sys.stderr,
                )
                raise

            page_graphs[key] = graph

            preview_path = preview_dir / (
                f"graph_page_{page_number:03d}.png"
            )
            draw_preview(image, graph, preview_path)
            print(f"  Preview: {preview_path}")

            # Save after each successful page.
            save_json(
                {"pages": page_graphs},
                checkpoint_path,
            )
            print(f"  Checkpoint saved: {checkpoint_path}")

    finally:
        pdf.close()

    final_output = build_final_output(page_graphs, pdf_path)
    save_json(final_output, output_path)

    print("\n" + "=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Output JSON: {output_path}")
    print(f"Preview directory: {preview_dir}")
    print(f"Floors: {len(final_output['floors'])}")
    print(f"Nodes: {final_output['metadata']['node_count']}")
    print(f"Edges: {final_output['metadata']['edge_count']}")
    print(
        "Validation warnings: "
        f"{final_output['metadata']['validation_warning_count']}"
    )

    if final_output["validation_warnings"]:
        print("\nReview these issues in the previews and JSON:")
        for warning in final_output["validation_warnings"]:
            print(
                f"  Page {warning['page']} "
                f"({warning['floor']}): {warning['warning']}"
            )

    print(
        "\nNote: validation can detect disconnected nodes, but it cannot "
        "prove that every detected connection passes through a real door. "
        "Inspect the preview images before using the graph for navigation."
    )


if __name__ == "__main__":
    main()

