import asyncio
from dotenv import load_dotenv
from analysis import run_analysis, extract_pillars

load_dotenv()

async def main():
    for q in [
        "Solid-state battery electrolytes for electric vehicles in cold climates",
        "Open-source prosthetic hands with tactile feedback for low-income amputees",
        "Biodegradable packaging from agricultural waste for cold-chain food shipping",
    ]:
        print("=" * 70)
        print("QUERY:", q)
        print("PILLARS:", extract_pillars(q))
        a = await run_analysis(q)
        print(f"calls: {a.meta.serpapi_calls}  elapsed: {a.meta.elapsed_ms}ms  gaps: {len(a.gaps)}")
        for g in a.gaps[:2]:
            print(f"  [{g.gap_score}] {g.statement}")
        print()

asyncio.run(main())