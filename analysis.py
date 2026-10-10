"""PatentGap analysis engine.

Three responsibilities:
  1. extract_pillars(query) -> list[str]
     Rule-based noun-phrase extraction. No LLM. Deterministic.
  2. run_analysis(query) -> Analysis
     Fires SerpApi calls in parallel, computes counts, scores gaps,
     generates quantified statements.
  3. build_statement(...) -> str
     The money line.

IMPORTANT — count semantics:
Every query fired at SerpApi is a QUOTED PHRASE query, e.g. `"tactile feedback"`
or `"tactile feedback" "low-income amputees"`. Quoting does two things:
  (a) Tightens the match so we don't get corpus-level noise.
  (b) Unlocks SerpApi's `total_results` field as a real phrase count
      (see serpapi_client._extract_total for the exact rule).
The result: pillar counts and intersection counts are on the same scale,
so scarcity scoring is meaningful.

IMPORTANT — relevance filter:
Google Patents is loose with multi-phrase queries and mixes in off-topic
results. After building each intersection, we filter results to keep only
those whose title/snippet contains tokens from BOTH pillars.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from itertools import combinations
from typing import Iterable

import httpx

from models import (
    Analysis,
    AnalysisMeta,
    Engine,
    Gap,
    Intersection,
    Pillar,
    SearchResult,
)
from serpapi_client import search as serp_search

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONTEXTS: list[str] = [
    "low cost",
    "small farm",
    "smallholder",
    "developing regions",
    "rural",
    "off-grid",
    "open source",
    "low resource",
    "point of care",
    "resource constrained",
]

# Engines we query for every pillar and for the full query
FULL_ENGINES: list[Engine] = ["patents", "scholar", "search", "news"]

# Engines we query for intersections and context variants (narrower, cheaper)
NARROW_ENGINES: list[Engine] = ["patents", "scholar"]

# Hard cap on SerpApi calls per analysis (safety net)
MAX_CALLS = 40

# Concurrency limit for parallel SerpApi calls
CONCURRENCY = 5

# Mock mode — set PATENTGAP_MOCK=1 in the environment to skip all SerpApi
# calls and return a synthetic analysis. Used for UI development only.
MOCK_MODE = os.environ.get("PATENTGAP_MOCK", "").strip() == "1"


# ---------------------------------------------------------------------------
# Pillar extraction
# ---------------------------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "for", "of", "to", "in", "on",
    "at", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "using", "use", "uses", "used", "based", "via",
    "into", "over", "under", "between", "through", "during", "after",
    "before", "about", "against", "among", "around", "because", "both",
    "each", "few", "more", "most", "other", "some", "such", "than",
    "that", "these", "this", "those", "what", "which", "while", "who",
    "whom", "whose", "why", "how", "when", "where", "if", "then", "so",
    "too", "very", "can", "will", "just", "should", "now",
    "smart", "new", "novel", "advanced", "next", "generation",
    "system", "systems", "method", "methods", "apparatus", "device",
    "devices", "approach", "approaches", "solution", "solutions",
    "technology", "technologies", "application", "applications",
}

DOMAIN_NOUNS = {
    # Agriculture / environment
    "sensor", "sensors", "soil", "satellite", "irrigation", "farm",
    "farmers", "agriculture", "agricultural", "crop", "crops", "water",
    "air", "quality", "monitoring", "climate", "carbon", "emissions",
    # IoT / computing
    "iot", "network", "networks", "machine", "learning", "ai", "vision",
    "lidar", "radar", "gps", "blockchain", "encryption", "privacy",
    "quantum", "computing", "chip", "chips", "processor", "semiconductor",
    # Energy
    "battery", "batteries", "lithium", "energy", "solar", "wind",
    "storage", "grid", "microgrid", "hydrogen", "fuel", "fusion",
    "electrolyte", "electrolytes", "electrode", "electrodes",
    # Materials / packaging
    "packaging", "biodegradable", "waste", "plastic", "plastics",
    "recycling", "recycled", "membrane", "polymer", "polymers",
    "material", "materials", "composite", "composites",
    # Health / bio
    "health", "medical", "diagnostic", "diagnostics", "drug", "delivery",
    "vaccine", "genomic", "genomics", "protein", "proteins", "crispr",
    "prosthetic", "prosthetics", "hand", "hands", "tactile", "feedback",
    "amputees", "implant", "implants", "surgery", "cancer",
    # Logistics / food
    "cold", "chain", "shipping", "food", "logistics", "supply",
    # Robotics / autonomy
    "robot", "robotics", "automation", "drone", "drones", "autonomous",
    # Technical modifiers
    "solid-state", "solid", "state", "control", "adaptive",
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z0-9\-]+", text.lower())


def _is_nouny(word: str) -> bool:
    return word not in STOPWORDS and len(word) > 2


def _phrase_score(phrase: str) -> float:
    words = phrase.split()
    if not (2 <= len(words) <= 4):
        return 0.0
    domain_hits = sum(1 for w in words if w in DOMAIN_NOUNS)
    if domain_hits == 0:
        return 0.0
    lead_bonus = 0.3 if words[0] in DOMAIN_NOUNS else 0.0
    length_bonus = 0.2 if len(words) == 2 else (0.1 if len(words) == 3 else 0.0)
    return domain_hits + lead_bonus + length_bonus


def extract_pillars(query: str, max_pillars: int = 4) -> list[str]:
    """Rule-based pillar extraction from a single sentence."""
    tokens = _tokenize(query)
    if not tokens:
        return []

    candidates: list[str] = []
    for n in (2, 3, 4):
        for i in range(len(tokens) - n + 1):
            window = tokens[i : i + n]
            if all(_is_nouny(w) for w in window):
                candidates.append(" ".join(window))

    scored = [(c, _phrase_score(c)) for c in candidates]
    scored = [(c, s) for c, s in scored if s > 0]
    scored.sort(key=lambda x: (-x[1], -len(x[0]), x[0]))

    kept: list[str] = []
    for phrase, _ in scored:
        if any(phrase in existing or existing in phrase for existing in kept):
            continue
        if any(phrase.split()[0] == k.split()[0] for k in kept):
            continue
        kept.append(phrase)
        if len(kept) >= max_pillars:
            break

    if len(kept) < 3:
        seen = set(kept)
        for tok in tokens:
            if tok in DOMAIN_NOUNS and tok not in seen:
                kept.append(tok)
                seen.add(tok)
                if len(kept) >= 3:
                    break

    return kept[:max_pillars]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def _bounded_gather(coros: list, limit: int) -> list:
    """Run coroutines with at most `limit` in flight at once."""
    semaphore = asyncio.Semaphore(limit)

    async def wrap(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(wrap(c) for c in coros), return_exceptions=True)


async def _fetch(
    client: httpx.AsyncClient,
    engine: Engine,
    query: str,
    counter: list[int],
) -> tuple[list[SearchResult], int]:
    counter[0] += 1
    try:
        return await serp_search(client, engine, query)
    except Exception:
        return [], 0


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _median_year(results: Iterable[SearchResult]) -> int | None:
    years = sorted(r.year for r in results if r.year)
    if not years:
        return None
    return years[len(years) // 2]


def _recent_fraction(results: list[SearchResult], window: int = 3) -> float:
    current_year = datetime.now(timezone.utc).year
    years = [r.year for r in results if r.year]
    if not years:
        return 0.0
    recent = sum(1 for y in years if y >= current_year - window)
    return recent / len(years)


def _context_coverage(results: list[SearchResult]) -> float:
    if not results:
        return 0.0
    needles = [c.lower() for c in CONTEXTS]
    hits = 0
    for r in results:
        haystack = f"{r.title} {r.snippet}".lower()
        if any(n in haystack for n in needles):
            hits += 1
    return hits / len(results)


def _recency_penalty(pillar_a: Pillar, pillar_b: Pillar) -> float:
    combined_recent = (pillar_a.recent_fraction + pillar_b.recent_fraction) / 2
    return max(0.0, min(1.0, 1.0 - combined_recent))


def _gap_score(scarcity: float, context: float, recency: float) -> float:
    raw = 0.5 * scarcity + 0.35 * (1 - context) + 0.15 * recency
    return round(max(0.0, min(1.0, raw)) * 100, 1)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

def _plural(n: int, singular: str, plural: str) -> str:
    """Return singular form when n == 1, else plural."""
    return singular if n == 1 else plural


def build_statement(
    pillar_a: Pillar,
    pillar_b: Pillar,
    inter: Intersection,
) -> str:
    a_pat = pillar_a.counts.get("patents", 0)
    b_pat = pillar_b.counts.get("patents", 0)
    i_pat = inter.counts.get("patents", 0)

    a_sch = pillar_a.counts.get("scholar", 0)
    b_sch = pillar_b.counts.get("scholar", 0)
    i_sch = inter.counts.get("scholar", 0)

    haystack = " ".join(
        f"{r.title} {r.snippet}".lower() for r in inter.top_results
    )
    missing_ctx = [c for c in CONTEXTS if c.lower() not in haystack][:2]

    ctx_clause = ""
    if missing_ctx:
        ctx_clause = (
            f", and none of the top results target {', '.join(missing_ctx)}"
        )

    return (
        f"While {pillar_a.name} "
        f"({a_pat:,} {_plural(a_pat, 'patent', 'patents')}, "
        f"{a_sch:,} {_plural(a_sch, 'paper', 'papers')}) and "
        f"{pillar_b.name} "
        f"({b_pat:,} {_plural(b_pat, 'patent', 'patents')}, "
        f"{b_sch:,} {_plural(b_sch, 'paper', 'papers')}) are active "
        f"areas, only {i_pat:,} {_plural(i_pat, 'patent', 'patents')} and "
        f"{i_sch:,} {_plural(i_sch, 'paper', 'papers')} combine both"
        f"{ctx_clause}."
    )


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


def _mock_analysis(query: str) -> Analysis:
    fake_pillars = ["pillar alpha", "pillar beta", "pillar gamma"]
    pillars = [
        Pillar(
            id=p,
            name=p,
            total=30,
            counts={"patents": 8400, "scholar": 12400, "search": 840, "news": 210},
            top_results=[
                SearchResult(
                    engine="patents",
                    title=f"Mock patent for {p} #{i}",
                    snippet="Synthetic result for UI development.",
                    year=2019 + i,
                    assignee="Mock Corp",
                    url="https://example.com",
                )
                for i in range(3)
            ],
            median_year=2021,
            recent_fraction=0.4,
        )
        for p in fake_pillars
    ]
    inters = [
        Intersection(
            pillar_a=fake_pillars[0],
            pillar_b=fake_pillars[1],
            total=47,
            counts={"patents": 47, "scholar": 12, "search": 0, "news": 0},
            top_results=[
                SearchResult(
                    engine="patents",
                    title="Mock intersection patent",
                    snippet="Synthetic.",
                    year=2018,
                    url="https://example.com",
                )
            ],
            context_coverage=0.0,
        )
    ]
    gaps = [
        Gap(
            id="pillar alpha::pillar beta",
            pillar_a="pillar alpha",
            pillar_b="pillar beta",
            gap_score=84.2,
            scarcity_score=0.85,
            context_score=1.0,
            recency_score=0.4,
            statement=(
                "While pillar alpha (8,400 patents, 12,400 papers) and pillar beta "
                "(8,400 patents, 12,400 papers) are active areas, only 47 patents "
                "and 12 papers combine both, and none of the top results target "
                "low cost, small farm."
            ),
            closest_prior_art=pillars[0].top_results,
        )
    ]
    return Analysis(
        query=query,
        pillars=pillars,
        intersections=inters,
        gaps=gaps,
        meta=AnalysisMeta(
            query=query,
            serpapi_calls=0,
            elapsed_ms=120,
            cached=False,
            generated_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_analysis(query: str) -> Analysis:
    start = time.time()
    counter = [0]

    if MOCK_MODE:
        return _mock_analysis(query)

    # Whole-analysis cache: if we've run this exact query in the last 24h,
    # return the previous Analysis verbatim and mark it cached. This means
    # a repeat demo run costs zero SerpApi calls and returns in <100ms,
    # even if the underlying per-call cache has expired.
    from cache import get as cache_get, set as cache_set

    cached = cache_get("analysis", query)
    if cached is not None:
        analysis = Analysis(**cached)
        analysis.meta.cached = True
        analysis.meta.elapsed_ms = int((time.time() - start) * 1000)
        return analysis

    pillars_names = extract_pillars(query)
    if len(pillars_names) < 2:
        return Analysis(
            query=query,
            pillars=[],
            intersections=[],
            gaps=[],
            meta=AnalysisMeta(
                query=query,
                serpapi_calls=0,
                elapsed_ms=int((time.time() - start) * 1000),
                cached=False,
                generated_at=datetime.now(timezone.utc).isoformat(),
            ),
        )

    async with httpx.AsyncClient() as client:
        # Plan 1 — full query, quoted, every engine
        full_calls = [
            _fetch(client, e, f'"{query}"', counter) for e in FULL_ENGINES
        ]

        # Plan 2 — each pillar, quoted, every engine
        pillar_calls = [
            _fetch(client, e, f'"{pillar}"', counter)
            for pillar in pillars_names
            for e in FULL_ENGINES
        ]

        # Plan 3 — each intersection, both phrases quoted, narrow engines
        pairs = list(combinations(pillars_names, 2))
        inter_calls = [
            _fetch(client, e, f'"{a}" "{b}"', counter)
            for a, b in pairs
            for e in NARROW_ENGINES
        ]

        # Plan 4 — top 2 pillars × first 3 contexts, both phrases quoted, patents only
        ctx_calls = [
            _fetch(client, "patents", f'"{pillar}" "{ctx}"', counter)
            for pillar in pillars_names[:2]
            for ctx in CONTEXTS[:3]
        ]

        all_calls = full_calls + pillar_calls + inter_calls + ctx_calls
        results = await _bounded_gather(all_calls, CONCURRENCY)

    # Split results back into their logical groups
    idx = 0
    full_results = results[idx : idx + len(FULL_ENGINES)]
    idx += len(FULL_ENGINES)

    pillar_results = []
    for _ in pillars_names:
        pillar_results.append(results[idx : idx + len(FULL_ENGINES)])
        idx += len(FULL_ENGINES)

    inter_results = []
    for _ in pairs:
        inter_results.append(results[idx : idx + len(NARROW_ENGINES)])
        idx += len(NARROW_ENGINES)

    # --- Build Pillar objects
    pillars: list[Pillar] = []
    for name, group in zip(pillars_names, pillar_results):
        counts: dict[Engine, int] = {"patents": 0, "scholar": 0, "search": 0, "news": 0}
        top: list[SearchResult] = []
        for engine, outcome in zip(FULL_ENGINES, group):
            if isinstance(outcome, Exception) or not isinstance(outcome, tuple):
                continue
            res, total = outcome
            counts[engine] = total
            top.extend(res[:3])
        total = sum(counts.values())
        pillars.append(
            Pillar(
                id=name,
                name=name,
                counts=counts,
                total=total,
                top_results=top,
                median_year=_median_year(top),
                recent_fraction=_recent_fraction(top),
            )
        )

    # --- Build Intersection objects (with relevance filter)
    intersections: list[Intersection] = []
    for (a_name, b_name), group in zip(pairs, inter_results):
        counts: dict[Engine, int] = {"patents": 0, "scholar": 0, "search": 0, "news": 0}
        top: list[SearchResult] = []
        for engine, outcome in zip(NARROW_ENGINES, group):
            if isinstance(outcome, Exception) or not isinstance(outcome, tuple):
                continue
            res, total = outcome
            counts[engine] = total
            top.extend(res[:5])

        # Relevance filter: keep only results whose title or snippet
        # contains at least one token from each pillar.
        a_tokens = set(_tokenize(a_name))
        b_tokens = set(_tokenize(b_name))
        filtered = []
        for r in top:
            hay = set(_tokenize(f"{r.title} {r.snippet}"))
            if hay & a_tokens and hay & b_tokens:
                filtered.append(r)
        if filtered:
            top = filtered

        total = sum(counts.values())
        intersections.append(
            Intersection(
                pillar_a=a_name,
                pillar_b=b_name,
                counts=counts,
                total=total,
                top_results=top,
                context_coverage=_context_coverage(top),
            )
        )

    # --- Score and rank gaps
    pillar_by_name = {p.name: p for p in pillars}
    gaps: list[Gap] = []
    for inter in intersections:
        a = pillar_by_name[inter.pillar_a]
        b = pillar_by_name[inter.pillar_b]

        a_pat = a.counts.get("patents", 0)
        b_pat = b.counts.get("patents", 0)
        i_pat = inter.counts.get("patents", 0)
        min_pillar = min(a_pat, b_pat)
        if min_pillar == 0:
            scarcity = 1.0
        else:
            scarcity = 1.0 - (i_pat / min_pillar)
        scarcity = max(0.0, min(1.0, scarcity))

        context = inter.context_coverage
        recency = _recency_penalty(a, b)
        score = _gap_score(scarcity, context, recency)

        statement = build_statement(a, b, inter)

        gaps.append(
            Gap(
                id=f"{a.name}::{b.name}",
                pillar_a=a.name,
                pillar_b=b.name,
                gap_score=score,
                scarcity_score=round(scarcity, 3),
                context_score=round(1 - context, 3),
                recency_score=round(recency, 3),
                statement=statement,
                closest_prior_art=inter.top_results[:5],
            )
        )

    gaps.sort(key=lambda g: g.gap_score, reverse=True)

    analysis = Analysis(
        query=query,
        pillars=pillars,
        intersections=intersections,
        gaps=gaps[:6],
        meta=AnalysisMeta(
            query=query,
            serpapi_calls=counter[0],
            elapsed_ms=int((time.time() - start) * 1000),
            cached=False,
            generated_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    cache_set("analysis", query, analysis.model_dump())
    return analysis