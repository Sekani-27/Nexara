"""
retrieve_similar.py
===================
Queries the Qdrant "trade_memory" collection for the top-N most similar past
setups given a natural-language or structured query string.

Called automatically when a live alert fires (via TraderCopilot.alert_engine or
any external orchestrator).  Can also be run from the command line for manual
lookup.

Dependencies:
    pip install qdrant-client sentence-transformers

Usage (CLI):
    python retrieve_similar.py "EURCAD bearish sell rising wedge OB retest london"
    python retrieve_similar.py "bullish breakout retest demand OB new york" --top 3
    python retrieve_similar.py --query "AUDNZD sniper inverse H&S buy" --host localhost --port 6333

Programmatic usage:
    from trader_copilot.retrieve_similar import retrieve_similar

    results = retrieve_similar("GBPCAD sell london supply OB fvg wedge")
    for r in results:
        print(r["outcome"], r["vision_notes"])
"""

import argparse
import json
import sys
from typing import Any

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from qdrant_client import QdrantClient
except ImportError:
    sys.exit("Missing dependency: pip install qdrant-client")

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    sys.exit("Missing dependency: pip install sentence-transformers")

# ── Constants ─────────────────────────────────────────────────────────────────
COLLECTION_NAME = "trade_memory"
MODEL_NAME      = "all-MiniLM-L6-v2"
DEFAULT_TOP_K   = 5
DEFAULT_HOST    = "localhost"
DEFAULT_PORT    = 6333

# Module-level singletons — loaded once and reused across calls when imported
_model: SentenceTransformer | None = None
_client: QdrantClient | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _get_client(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(host=host, port=port)
    return _client


# ── Core retrieval ────────────────────────────────────────────────────────────

def retrieve_similar(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    score_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Encode *query* and return the top-k most similar trade setups from Qdrant.

    Each returned dict contains the full payload plus a "similarity_score" key.

    Parameters
    ----------
    query           : Free-text description of the current setup.
    top_k           : Number of results to return (default 5).
    host / port     : Qdrant connection details.
    score_threshold : Minimum cosine similarity to include a result (0–1).

    Returns
    -------
    List of payload dicts sorted by descending similarity, each augmented with:
        - "similarity_score" : float  (cosine similarity, 0–1)
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")

    model  = _get_model()
    client = _get_client(host, port)

    # Encode query
    query_vector = model.encode(query, convert_to_numpy=True).tolist()

    # Search Qdrant — qdrant-client ≥1.7 replaced client.search() with
    # client.query_points(), which returns a QueryResponse whose .points
    # attribute holds the list of ScoredPoint results.
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=top_k,
        score_threshold=score_threshold if score_threshold > 0.0 else None,
        with_payload=True,
    )

    results = []
    for hit in response.points:
        record = dict(hit.payload)
        record["similarity_score"] = round(float(hit.score), 4)
        results.append(record)

    return results


# ── Pretty printer ────────────────────────────────────────────────────────────

def _format_confluences(confluences: dict | None) -> str:
    if not confluences:
        return "none"
    active = [k.replace("_", " ") for k, v in confluences.items() if v is True]
    return ", ".join(active) if active else "none"


def _print_results(results: list[dict], query: str) -> None:
    print(f'\nQuery: "{query}"')
    print(f"Top {len(results)} similar setup(s) from trade_memory:\n")
    print("─" * 72)

    for rank, r in enumerate(results, start=1):
        sid       = r.get("setup_id", "?")
        instr     = r.get("instrument", "?").upper()
        direction = r.get("direction", "?")
        timeframe = r.get("timeframe", "?")
        session   = r.get("session", "?")
        outcome   = r.get("outcome", "?")
        grade     = r.get("quality_grade", "?")
        pattern   = r.get("pattern_type", "?").replace("_", " ")
        is_sniper = r.get("is_sniper", False)
        score     = r.get("similarity_score", 0.0)
        notes     = r.get("vision_notes", "")
        confluences = _format_confluences(r.get("confluences"))

        sniper_tag = " [SNIPER]" if is_sniper else ""

        print(f"#{rank}  {sid}{sniper_tag}")
        print(f"    Similarity : {score:.4f}")
        print(f"    Instrument : {instr}  {direction.upper()}  {timeframe}  |  session: {session}")
        print(f"    Pattern    : {pattern}")
        print(f"    Confluences: {confluences}")
        print(f"    Outcome    : {outcome}  (grade {grade})")
        print(f"    Notes      : {notes}")
        print("─" * 72)

    if not results:
        print("No results found. Is the collection populated? Run ingest_trade_memory.py first.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve the most similar past trade setups from Qdrant."
    )
    # Accept query as positional OR --query flag
    parser.add_argument(
        "query_positional", nargs="?", default=None,
        metavar="QUERY",
        help="Query string describing the current setup."
    )
    parser.add_argument(
        "--query", "-q", default=None,
        help="Query string (alternative to positional argument)."
    )
    parser.add_argument(
        "--top", "-n", type=int, default=DEFAULT_TOP_K,
        dest="top_k",
        help=f"Number of results to return (default: {DEFAULT_TOP_K})."
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Qdrant host (default: {DEFAULT_HOST})."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Qdrant port (default: {DEFAULT_PORT})."
    )
    parser.add_argument(
        "--threshold", type=float, default=0.0,
        help="Minimum similarity score to include a result (0.0–1.0, default: 0.0)."
    )
    parser.add_argument(
        "--json", action="store_true", dest="output_json",
        help="Output results as JSON instead of human-readable text."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Resolve query from positional or flag
    query_str = args.query_positional or args.query
    if not query_str:
        print("Error: provide a query string.\n"
              "  python retrieve_similar.py \"EURCAD sell rising wedge OB retest london\"\n"
              "  python retrieve_similar.py --query \"...\"")
        sys.exit(1)

    results = retrieve_similar(
        query=query_str,
        top_k=args.top_k,
        host=args.host,
        port=args.port,
        score_threshold=args.threshold,
    )

    if args.output_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_results(results, query_str)
