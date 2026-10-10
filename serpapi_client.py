"""SerpApi client. Four engines: patents, scholar, search, news.

Uses httpx async. Each engine has a normalizer that maps SerpApi's
response shape onto our SearchResult model.

IMPORTANT — count semantics:
We do not blindly trust SerpApi's `total_results` field. For BROAD queries
(pillar names, unquoted), it reports corpus-level estimates that ignore the
actual phrase match and produce absurd numbers like "1,730,000 papers on
battery electrolytes". For QUOTED phrase queries (intersections, contexts),
it reports a real phrase-match count that is exactly what we need to
discriminate between a busy pillar and a sparse intersection.

So: quoted query -> use total_results. Unquoted query -> use len(results).
Every query fired by analysis.py is quoted, so in practice you get honest
totals everywhere.
"""

import os
from typing import Any

import httpx

from cache import get as cache_get, set as cache_set
from models import Engine, SearchResult

SERPAPI_URL = "https://serpapi.com/search"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Map our engine name to SerpApi's `engine` param
ENGINE_PARAM: dict[Engine, str] = {
    "patents": "google_patents",
    "scholar": "google_scholar",
    "search": "google",
    "news": "google_news",
}

# How many results to request per call. 10 is SerpApi's max for most engines.
NUM_RESULTS = 10


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def _api_key() -> str:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SERPAPI_KEY is missing. Add it to .env before starting the server."
        )
    return key


# ---------------------------------------------------------------------------
# Year extraction — SerpApi returns dates in many shapes
# ---------------------------------------------------------------------------


def _year_from(value: Any) -> int | None:
    """Best-effort year extraction from any SerpApi date shape.

    Handles: 2021, "2021", "2021-03-15", "Mar 15, 2021", "Published 2019".
    """
    if value is None:
        return None
    if isinstance(value, int) and 1900 < value < 2100:
        return value
    s = str(value)
    for i in range(len(s) - 3):
        chunk = s[i : i + 4]
        if chunk.isdigit():
            y = int(chunk)
            if 1900 < y < 2100:
                return y
    return None


# ---------------------------------------------------------------------------
# Per-engine normalizers
# ---------------------------------------------------------------------------


def _normalize_patents(data: dict, query: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in data.get("organic_results", [])[:NUM_RESULTS]:
        cited_by = item.get("cited_by")
        citations = cited_by.get("total") if isinstance(cited_by, dict) else None
        results.append(
            SearchResult(
                engine="patents",
                title=(item.get("title") or "")[:300],
                snippet=(item.get("snippet") or "")[:400],
                year=_year_from(
                    item.get("publication_date")
                    or item.get("priority_date")
                    or item.get("filing_date")
                ),
                assignee=item.get("assignee"),
                citations=citations,
                url=item.get("link") or item.get("patent_link") or "",
                query_used=query,
            )
        )
    return results


def _normalize_scholar(data: dict, query: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in data.get("organic_results", [])[:NUM_RESULTS]:
        pub_info = item.get("publication_info") or {}
        authors_raw = pub_info.get("authors") or []
        authors = [a.get("name", "") for a in authors_raw if a.get("name")] or None
        year = _year_from(pub_info.get("summary") or item.get("year"))
        cited_by = (item.get("inline_links") or {}).get("cited_by") or {}
        citations = cited_by.get("total") if isinstance(cited_by, dict) else None
        results.append(
            SearchResult(
                engine="scholar",
                title=(item.get("title") or "")[:300],
                snippet=(item.get("snippet") or "")[:400],
                year=year,
                authors=authors,
                citations=citations,
                url=item.get("link", ""),
                query_used=query,
            )
        )
    return results


def _normalize_search(data: dict, query: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in data.get("organic_results", [])[:NUM_RESULTS]:
        results.append(
            SearchResult(
                engine="search",
                title=(item.get("title") or "")[:300],
                snippet=(item.get("snippet") or "")[:400],
                url=item.get("link", ""),
                query_used=query,
            )
        )
    return results


def _normalize_news(data: dict, query: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in data.get("news_results", [])[:NUM_RESULTS]:
        source = item.get("source")
        source_name = source.get("name") if isinstance(source, dict) else source
        results.append(
            SearchResult(
                engine="news",
                title=(item.get("title") or "")[:300],
                snippet=(item.get("snippet") or "")[:400],
                year=_year_from(item.get("date")),
                assignee=source_name,
                url=item.get("link", ""),
                query_used=query,
            )
        )
    return results


NORMALIZERS = {
    "patents": _normalize_patents,
    "scholar": _normalize_scholar,
    "search": _normalize_search,
    "news": _normalize_news,
}


# ---------------------------------------------------------------------------
# Count extraction
# ---------------------------------------------------------------------------


def _extract_total(data: dict, query: str, returned: int) -> int:
    """Decide what "total" means for this query.

    Quoted query -> trust SerpApi's `total_results` (it's a real phrase count).
    Unquoted query -> use the returned count (SerpApi's number is a corpus
    estimate and unreliable for broad phrases).
    """
    if '"' not in query:
        return returned

    raw = (
        data.get("search_information", {}).get("total_results")
        or data.get("search_metadata", {}).get("total_results")
    )
    if raw is None:
        return returned
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return returned
    if value <= 0:
        return returned
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search(
    client: httpx.AsyncClient,
    engine: Engine,
    query: str,
) -> tuple[list[SearchResult], int]:
    """Run one SerpApi call.

    Returns (results, total):
      - results is the normalized list (0-10 items)
      - total   is the honest count used by gap scoring. For quoted queries
                it's SerpApi's real phrase count. For unquoted queries it's
                len(results).

    Every call is cached on disk keyed by (engine, query). A repeat query
    within 24h serves from cache without touching the network.
    """
    cached = cache_get(f"serpapi:{engine}", query)
    if cached is not None:
        results = [SearchResult(**r) for r in cached["results"]]
        return results, cached["total"]

    params = {
        "api_key": _api_key(),
        "engine": ENGINE_PARAM[engine],
        "q": query,
        "num": NUM_RESULTS,
    }

    resp = await client.get(SERPAPI_URL, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(
            f"SerpApi error for {engine} '{query}': {data['error']}"
        )

    results = NORMALIZERS[engine](data, query)
    total = _extract_total(data, query, len(results))

    cache_set(
        f"serpapi:{engine}",
        query,
        {"results": [r.model_dump() for r in results], "total": total},
    )
    return results, total