"""PatentGap — FastAPI server.

Two routes:
  GET  /          → landing page (hero, textarea, chips)
  POST /analyze   → runs analysis, renders results page

Also exposes auto-generated API docs at /docs (courtesy of FastAPI).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analysis import run_analysis

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

if not os.environ.get("SERPAPI_KEY", "").strip():
    # One clear line to the terminal, then refuse to start.
    raise SystemExit(
        "SERPAPI_KEY is missing. Add it to .env before starting the server."
    )

BASE_DIR = Path(__file__).parent

app = FastAPI(title="PatentGap", version="1.0")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------------------
# Example queries — surfaced as chips on the landing page
# ---------------------------------------------------------------------------

EXAMPLES: list[str] = [
    "Smart irrigation using soil sensors and satellite data for smallholder farms in India",
    "Low-cost air quality monitoring with IoT sensors for urban schools",
    "Biodegradable packaging from agricultural waste for cold-chain food shipping",
    "Open-source prosthetic hands with tactile feedback for low-income amputees",
]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "examples": EXAMPLES,
        },
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, query: str = Form(...)) -> HTMLResponse:
    query = query.strip()
    if not query:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "examples": EXAMPLES,
                "error": "Enter a technology idea to analyze.",
            },
        )

    analysis = await run_analysis(query)

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "analysis": analysis,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        },
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}