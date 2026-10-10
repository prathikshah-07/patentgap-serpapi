"""Pydantic models. Automatically appear in FastAPI's /docs."""

from typing import Any, Literal
from pydantic import BaseModel

Engine = Literal["patents", "scholar", "search", "news"]


class SearchResult(BaseModel):
    engine: Engine
    title: str
    snippet: str = ""
    year: int | None = None
    assignee: str | None = None
    authors: list[str] | None = None
    citations: int | None = None
    url: str = ""
    query_used: str = ""


class Pillar(BaseModel):
    id: str
    name: str
    counts: dict[Engine, int]
    total: int
    top_results: list[SearchResult]
    median_year: int | None = None
    recent_fraction: float = 0.0


class Intersection(BaseModel):
    pillar_a: str
    pillar_b: str
    counts: dict[Engine, int]
    total: int
    top_results: list[SearchResult]
    context_coverage: float = 0.0


class Gap(BaseModel):
    id: str
    pillar_a: str
    pillar_b: str
    gap_score: float          # 0-100
    scarcity_score: float     # 0-1
    context_score: float      # 0-1
    recency_score: float      # 0-1
    statement: str
    closest_prior_art: list[SearchResult]


class AnalysisMeta(BaseModel):
    query: str
    serpapi_calls: int
    elapsed_ms: int
    cached: bool
    generated_at: str


class Analysis(BaseModel):
    query: str
    pillars: list[Pillar]
    intersections: list[Intersection]
    gaps: list[Gap]
    meta: AnalysisMeta


class AnalyzeRequest(BaseModel):
    query: str