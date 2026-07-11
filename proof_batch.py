# Datacenter STABILITY test — runs on a GitHub Actions x86 runner (dedicated 7GB,
# unlike Colab's throttled shared box). Fills 3 real ATS forms, reports reached=.
# The per-form asyncio timeout (150s) is the crash-resilience from agent.py — a hung
# browser can't drag the run out to 12 min like ashby did on Colab.
import os, asyncio
from browser_use import Agent, ChatGoogle

KEYS = [k.strip() for k in os.environ.get("GEMINI_KEYS", "").split(",") if k.strip()]
MODEL = "gemini-flash-latest"
open("cv.txt", "w").write("Droid Test\nSoftware Engineer | +31612345678 | test@applyright.dev\nPython, Go, AWS")

FORMS = [
    ("greenhouse", "https://boards.greenhouse.io/careem/jobs/8620725002"),
    ("lever", "https://jobs.lever.co/palantir/8f362a1f-1eff-4327-94c1-ff46e2101c69"),
    ("ashby", "https://jobs.ashbyhq.com/ramp/ffc09fc0-7785-48db-a203-a148f62534be/application"),
]
TASK = ("Go to {url}, wait for it to render, dismiss any cookie banner, click Apply if needed. "
        "Fill ONLY: Name 'Droid Test', Email 'test@applyright.dev', Phone '+31612345678'. "
        "Upload the resume file if a field exists. Do NOT fill optional fields or re-verify. "
        "The moment those are filled + resume attached, call done (aim <=8 steps). "
        "NEVER click Submit/Send. Report: 'Reached form: yes/no.'")

async def run(url, key):
    a = Agent(task=TASK.format(url=url), llm=ChatGoogle(model=MODEL, api_key=key), available_file_paths=["cv.txt"])
    h = await asyncio.wait_for(a.run(max_steps=14), timeout=150)
    return h.final_result()

async def main():
    ok = 0
    for i, (ats, url) in enumerate(FORMS):
        try:
            res = await run(url, KEYS[i % len(KEYS)])
            reached = bool(res) and "reached form: yes" in (res or "").lower()
            ok += int(reached)
            print(f"### {ats}: reached={reached} :: {res}", flush=True)
        except Exception as e:
            print(f"### {ats}: ERROR {str(e)[:150]}", flush=True)
    print(f"\nSUMMARY: {ok}/{len(FORMS)} reached — datacenter x86 runner", flush=True)

if not KEYS:
    print("NO KEYS — set the GEMINI_KEYS secret", flush=True); raise SystemExit(1)
asyncio.run(main())
