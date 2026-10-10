# PatentGap

**Find the white space in any technology idea, using live search data.**

Describe a technology idea in plain English. PatentGap searches patents, research papers, products and news through [SerpApi](https://serpapi.com), measures how crowded each part of your idea is, and shows where the combinations are empty, along with the closest prior art so you can check it yourself.

Built for the SerpApi India Hackathon 2026 (Knowledge & Public Interest track).

<!-- Add a screenshot of the results page here: ![PatentGap results](docs/screenshot.png) -->
<!-- Demo video: <paste link here> -->

---

## The problem

Inventors, students and researchers often spend weeks on an idea before finding out that it is already patented or published, or that the real opportunity was somewhere else. The evidence is scattered across patent databases, academic search, product pages and news, and almost nobody checks all four before they start.

PatentGap does that first pass in seconds. Instead of returning a list of links, it returns a landscape and a finding.

## What it does

1. **You enter an idea**, for example: *"Low-cost air quality monitoring with IoT sensors for urban schools."*
2. **It extracts the idea's core technology phrases ("pillars")** with a deterministic, rule-based extractor. No LLM is involved.
3. **It searches live through SerpApi** and counts how much exists for each pillar and for every pair of pillars:

   | Source | SerpApi engine |
   |---|---|
   | Patents | `google_patents` |
   | Research papers | `google_scholar` |
   | Products / web | `google` |
   | News | `google_news` |

4. **It scores every pair of pillars as a possible gap** using three signals (below).
5. **It writes the result as a plain sentence** with the real numbers, and links the closest prior art.

### Example output

From one run of the air quality query above (numbers change as Google's index changes):

> While iot sensors (13,662 patents, 162,000 papers) and low-cost air quality (126 patents, 6,870 papers) are active areas, only 0 patents and 486 papers combine both.

## The three gap signals

| Signal | What it measures | Weight |
|---|---|---|
| **Intersection scarcity** | How few patents combine two pillars compared with the smaller of the two pillars on its own: `1 - (intersection / min(pillar A, pillar B))` | 50% |
| **Missing context** | Whether the closest results mention real-world constraints such as *low cost, small farm, smallholder, rural, off-grid, open source, point of care* | 35% |
| **Recency imbalance** | Whether the relevant results are old and recent activity is thin | 15% |

The weighted result is a gap score from 0 to 100. The six highest-scoring pairs are shown, and the top one appears as the headline finding.

## How it works

```
idea
  |
  v
extract pillars (rule-based, in analysis.py)
  |
  v
parallel SerpApi calls (serpapi_client.py)
  - full query         x 4 engines
  - each pillar        x 4 engines
  - each pillar pair   x patents + scholar
  - top 2 pillars      x 3 context modifiers (patents)
  |
  v
count, score and rank gaps (analysis.py)  ->  results page
```

Design decisions worth knowing:

- **Every query is a quoted phrase query** (for example `"tactile feedback" "low-income amputees"`). Quoting makes Google require the phrase, and it means SerpApi's `total_results` reflects a phrase match count rather than a loose corpus estimate, so pillar counts and intersection counts are on the same scale.
- **A relevance filter** keeps only intersection results whose title or snippet mention tokens from both pillars, because Google Patents sometimes returns off-topic hits for multi-phrase queries.
- **Caching:** SerpApi responses and completed analyses are cached on disk for 24 hours, so a repeated query costs no search credits.
- **Concurrency is bounded** (5 requests at a time), and a single failed call does not fail the whole analysis.

## Project structure

```
app.py               FastAPI app and routes
analysis.py          pillar extraction, gap scoring, statement generation
serpapi_client.py    SerpApi calls and per-engine response normalizers
models.py            Pydantic models (SearchResult, Pillar, Gap, Analysis, ...)
cache.py             file-based cache, 24h TTL
templates/           index.html, results.html, partials/gap-card.html
static/style.css     styling
.env.example         environment variable template
```

## Run it locally

You need Python 3.10+ and a SerpApi API key ([free plan available](https://serpapi.com)).

```bash
git clone https://github.com/prathikshah-07/patentgap-serpapi.git
cd patentgap-serpapi

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # Windows: copy .env.example .env
# edit .env and set SERPAPI_KEY=your_key

uvicorn app:app
```

Open http://localhost:8000. Interactive API docs are at http://localhost:8000/docs.

Your key stays in `.env`, which is gitignored. Never commit it.

**UI development without spending credits:** set `PATENTGAP_MOCK=1` to return a synthetic analysis without calling SerpApi.

## Try these queries

- Open-source prosthetic hands with tactile feedback for low-income amputees
- Low-cost air quality monitoring with IoT sensors for urban schools
- Biodegradable packaging from agricultural waste for cold-chain food shipping
- Smart irrigation using soil sensors and satellite data for smallholder farms in India

## SerpApi usage

Each new query makes roughly 28 to 38 SerpApi calls across four engines (Google Patents, Google Scholar, Google Search, Google News). Every count, every gap score and every prior-art link in the results comes from those live calls. There is no bundled dataset, and with SerpApi removed the app has no function.

## Limitations

- **This is a first-pass signal, not legal advice.** It does not assess patentability, novelty or freedom to operate. Talk to a patent professional before filing or building a product.
- **Counts are Google's reported match counts.** They are estimates, and they shift over time.
- **A gap can mean white space, or just different wording.** Try rephrasing the idea before drawing conclusions. Empty intersections in patents are common for very specific phrases.
- **Pillar extraction is rule-based.** It works best for domains its vocabulary covers (agriculture, energy, IoT, materials, health, robotics, and similar areas) and falls back to simpler matching elsewhere. Context modifiers are a fixed list.
- **Search credits are limited** on the free SerpApi plan, which is why results are cached.

## Tech stack

FastAPI, httpx, Pydantic, Jinja2, hand-written CSS, SerpApi.

## AI tools used

- **<ADD YOUR CODING ASSISTANT(S) HERE, e.g. Cursor, Kiro, Copilot>:** code generation and iteration.
- **Claude (Anthropic):** drafting this README and the project description.

The app itself uses no AI models at runtime; analysis is deterministic. All code was reviewed and tested by the author.

## Author

Prathiksha, B.Tech IT, SRM Madurai.

## License

<Add a license (MIT is a common choice) or remove this section.>
