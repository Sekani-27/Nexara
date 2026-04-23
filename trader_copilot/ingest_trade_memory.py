"""
ingest_trade_memory.py
======================
Reads trade_memory.json, generates sentence-transformer embeddings for each
record, and upserts them into a local Qdrant collection called "trade_memory".

Dependencies:
    pip install qdrant-client sentence-transformers

Usage:
    python ingest_trade_memory.py
    python ingest_trade_memory.py --file /path/to/trade_memory.json
    python ingest_trade_memory.py --host localhost --port 6333
"""

import argparse
import json
import sys
from pathlib import Path

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        VectorParams,
        PointStruct,
    )
except ImportError:
    sys.exit("Missing dependency: pip install qdrant-client")

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    sys.exit("Missing dependency: pip install sentence-transformers")

# ── Constants ─────────────────────────────────────────────────────────────────
COLLECTION_NAME = "trade_memory"
MODEL_NAME      = "all-MiniLM-L6-v2"
VECTOR_SIZE     = 384
DISTANCE        = Distance.COSINE

DEFAULT_JSON    = Path(__file__).parent / "trade_memory.json"
DEFAULT_HOST    = "localhost"
DEFAULT_PORT    = 6333


# ── Text representation ───────────────────────────────────────────────────────

def build_text(record: dict) -> str:
    """
    Combine key semantic fields into a single sentence that the embedding model
    can encode meaningfully.

    Example output:
        "EURCAD sell in london session. Pattern: breakout_retest. Outcome: win.
         Confluences: bos_choch, fvg, order_block, wedge_pattern,
         supply_demand_zone, ninety_percent_rule.
         Notes: Descending bearish channel with rising corrective wedge..."
    """
    instrument   = record.get("instrument", "unknown").upper()
    direction    = record.get("direction", "unknown")
    session      = record.get("session", "unknown")
    pattern_type = record.get("pattern_type", "unknown").replace("_", " ")
    outcome      = record.get("outcome", "unknown")
    timeframe    = record.get("timeframe", "unknown")
    quality      = record.get("quality_grade", "")
    vision_notes = record.get("vision_notes", "")

    # Flatten confluences — list only the True ones
    confluences_raw = record.get("confluences") or {}
    active_confluences = [
        k.replace("_", " ")
        for k, v in confluences_raw.items()
        if v is True
    ]
    confluence_str = ", ".join(active_confluences) if active_confluences else "none"

    # Sniper flag
    is_sniper = record.get("is_sniper", False)
    sniper_str = "sniper entry" if is_sniper else "standard entry"

    text = (
        f"{instrument} {direction} on {timeframe} in {session} session. "
        f"Pattern: {pattern_type}. "
        f"Entry type: {sniper_str}. "
        f"Outcome: {outcome}. "
        f"Quality grade: {quality}. "
        f"Confluences: {confluence_str}. "
        f"Notes: {vision_notes}"
    )
    return text.strip()


# ── Collection bootstrap ───────────────────────────────────────────────────────

def ensure_collection(client: QdrantClient) -> None:
    """Create the collection if it doesn't already exist."""
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
        )
        print(f"[setup] Created collection '{COLLECTION_NAME}' "
              f"(size={VECTOR_SIZE}, distance=cosine)")
    else:
        print(f"[setup] Collection '{COLLECTION_NAME}' already exists — skipping creation")


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest(json_path: Path, host: str, port: int) -> None:
    # 1. Load records
    with open(json_path, "r", encoding="utf-8") as f:
        records: list[dict] = json.load(f)
    print(f"[load]  Loaded {len(records)} records from {json_path}")

    # 2. Filter — skip records where outcome is null
    valid_records = [r for r in records if r.get("outcome") is not None]
    skipped = len(records) - len(valid_records)
    if skipped:
        skipped_ids = [r["setup_id"] for r in records if r.get("outcome") is None]
        print(f"[skip]  Skipping {skipped} record(s) with null outcome: "
              f"{', '.join(skipped_ids)}")

    # 3. Load embedding model
    print(f"[model] Loading sentence-transformer '{MODEL_NAME}' …")
    model = SentenceTransformer(MODEL_NAME)
    print(f"[model] Model ready (output dim={VECTOR_SIZE})")

    # 4. Connect to Qdrant
    print(f"[qdrant] Connecting to Qdrant at {host}:{port} …")
    client = QdrantClient(host=host, port=port)
    ensure_collection(client)

    # 5. Encode all texts in one batch (faster than one-by-one)
    texts = [build_text(r) for r in valid_records]
    print(f"[embed]  Encoding {len(texts)} records …")
    vectors = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)

    # 6. Upsert into Qdrant
    ingested = 0
    points: list[PointStruct] = []

    for idx, (record, vector) in enumerate(zip(valid_records, vectors)):
        setup_id = record.get("setup_id", f"record_{idx}")

        # Use a stable integer ID derived from the setup number
        # "breakout_retest_001" → 1
        try:
            point_id = int(setup_id.split("_")[-1])
        except (ValueError, IndexError):
            point_id = idx + 1

        points.append(
            PointStruct(
                id=point_id,
                vector=vector.tolist(),
                payload=record,          # full JSON stored as payload
            )
        )

    # Batch upsert (single round-trip)
    client.upsert(collection_name=COLLECTION_NAME, points=points)

    # 7. Print per-record confirmations
    for point in points:
        sid = point.payload.get("setup_id")
        ins = point.payload.get("instrument", "?").upper()
        out = point.payload.get("outcome", "?")
        print(f"  ✓  [{point.id:>3}] {sid}  |  {ins}  |  outcome={out}")
        ingested += 1

    # 8. Summary
    collection_info = client.get_collection(COLLECTION_NAME)
    collection_size = collection_info.points_count

    print()
    print(f"[done]  Ingested : {ingested} / {len(records)} records")
    print(f"[done]  Skipped  : {skipped} (null outcome)")
    print(f"[done]  Collection '{COLLECTION_NAME}' size: {collection_size} points")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest trade_memory.json into Qdrant with sentence-transformer embeddings."
    )
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_JSON,
        help=f"Path to trade_memory.json (default: {DEFAULT_JSON})"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Qdrant host (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Qdrant port (default: {DEFAULT_PORT})"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.file.exists():
        sys.exit(f"Error: file not found: {args.file}")
    ingest(args.file, args.host, args.port)
