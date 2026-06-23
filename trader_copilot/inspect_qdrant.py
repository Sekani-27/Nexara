"""
inspect_qdrant.py
-----------------
Run this locally to diagnose the regime filter mismatch.

    python inspect_qdrant.py

Prints:
  1. The raw payload of the first 3 Qdrant records — shows the exact
     field name and value stored (including capitalisation).
  2. The filter that retrieve_similar() will pass — if the two don't
     match character-for-character, the filter returns nothing.
  3. A live test: queries with regime="trending" and shows the count.
"""

import os

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from sentence_transformers import SentenceTransformer

HOST = os.getenv("QDRANT_HOST", "localhost")
PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION = os.getenv("QDRANT_COLLECTION", "trade_memory")
MODEL_NAME = os.getenv("QDRANT_MODEL", "all-MiniLM-L6-v2")
REGIME_SENT = "trending"  # ← value retrieve_similar() sends
TEST_QUERY = "EURUSD sell london breakout retest"

client = QdrantClient(host=HOST, port=PORT)

# ── 1. Inspect raw payloads ──────────────────────────────────────────────────
print("=" * 60)
print("RAW QDRANT PAYLOADS (first 3 records)")
print("=" * 60)

records, _ = client.scroll(
    collection_name=COLLECTION,
    limit=3,
    with_payload=True,
    with_vectors=False,
)

if not records:
    print("❌ Collection is empty — re-run ingest_trade_memory.py first.")
    raise SystemExit(1)

for rec in records:
    regime_stored = rec.payload.get("regime", "<NOT PRESENT>")
    print(f"\nPoint ID : {rec.id}")
    print(f"  setup_id     : {rec.payload.get('setup_id')}")
    print(f"  regime       : {regime_stored!r}   (type={type(regime_stored).__name__})")
    print(f"  direction    : {rec.payload.get('direction')!r}")
    print(f"  pattern_type : {rec.payload.get('pattern_type')!r}")

# ── 2. Show the filter retrieve_similar() will build ────────────────────────
print()
print("=" * 60)
print("FILTER RETRIEVE_SIMILAR() SENDS")
print("=" * 60)
f = Filter(must=[FieldCondition(key="regime", match=MatchValue(value=REGIME_SENT))])
print("  key   : 'regime'")
print(f"  value : {REGIME_SENT!r}   (type={type(REGIME_SENT).__name__})")

# ── 3. Live filtered query ───────────────────────────────────────────────────
print()
print("=" * 60)
print(f"LIVE TEST  — query_points with regime={REGIME_SENT!r}")
print("=" * 60)

model = SentenceTransformer(MODEL_NAME)
vector = model.encode(TEST_QUERY, convert_to_numpy=True).tolist()

resp_filtered = client.query_points(
    collection_name=COLLECTION,
    query=vector,
    limit=5,
    with_payload=True,
    query_filter=f,
)
resp_unfiltered = client.query_points(
    collection_name=COLLECTION,
    query=vector,
    limit=5,
    with_payload=True,
)

print(f"  Results WITH    regime filter : {len(resp_filtered.points)}")
print(f"  Results WITHOUT regime filter : {len(resp_unfiltered.points)}")

if len(resp_filtered.points) == 0 and len(resp_unfiltered.points) > 0:
    stored = records[0].payload.get("regime", "<NOT PRESENT>")
    print()
    print("❌ MISMATCH DETECTED")
    print(f"   Filter sends : {REGIME_SENT!r}")
    print(f"   Qdrant stores: {stored!r}")
    print("   Fix: update one side so they match exactly.")
elif len(resp_filtered.points) > 0:
    print()
    print("✅ Filter is working — results returned.")
else:
    print()
    print(
        "❌ Both filtered and unfiltered returned 0 — collection may be empty or wrong."
    )
