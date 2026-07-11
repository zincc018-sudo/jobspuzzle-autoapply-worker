# Datacenter STABILITY test — runs on a GitHub Actions x86 runner (dedicated 7GB,
# unlike Colab's throttled shared box). Fills 3 real ATS forms, reports reached=.
# The per-form asyncio timeout (150s) is the crash-resilience from agent.py — a hung
# browser can't drag the run out to 12 min like ashby did on Colab.
import os, asyncio
from browser_use import Agent, ChatGoogle, BrowserProfile

# chromium_sandbox=False -> browser-use adds --no-sandbox/--disable-dev-shm-usage, required
# to launch Chrome on CI/datacenter runners (GitHub Actions). Without it the browser hangs
# on start and hits browser-use's 30s BrowserStartEvent timeout (the 0/3 we just saw).
PROFILE = BrowserProfile(headless=True, chromium_sandbox=False)

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
    a = Agent(task=TASK.format(url=url), llm=ChatGoogle(model=MODEL, api_key=key), browser_profile=PROFILE, available_file_paths=["cv.txt"])
    h = await asyncio.wait_for(a.run(max_steps=14), timeout=150)
    return h.final_result()

DEAD = set()   # keys that hit their daily cap — skip for the rest of the run

async def run_with_rotation(ats, url, start):
    """Try keys round-robin from `start`; on quota-dead keys move to the next.
    This is what run 29172218851 lacked — it drew the 3 most-burned keys by
    position and never rotated, which misread key quota as a form failure."""
    tried = 0
    i = start
    while tried < len(KEYS):
        key = KEYS[i % len(KEYS)]; i += 1; tried += 1
        if key in DEAD:
            continue
        try:
            res = await run(url, key)
            if res is None:   # agent stalled with no result — often a dry key's retry storm
                DEAD.add(key); print(f"  [key#{(i-1)%len(KEYS)+1} no-result -> next key]", flush=True)
                continue
            return res
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg or "404" in msg:
                DEAD.add(key); print(f"  [key#{(i-1)%len(KEYS)+1} quota/access -> next key]", flush=True)
                continue
            raise
    return None

async def main():
    ok = 0
    for i, (ats, url) in enumerate(FORMS):
        try:
            res = await run_with_rotation(ats, url, i * 4 + 3)  # start at #4/#8/#12 (least burned)
            reached = bool(res) and "reached form: yes" in (res or "").lower()
            ok += int(reached)
            print(f"### {ats}: reached={reached} :: {res}", flush=True)
        except Exception as e:
            print(f"### {ats}: ERROR {str(e)[:150]}", flush=True)
    print(f"\nSUMMARY: {ok}/{len(FORMS)} reached — datacenter x86 runner", flush=True)

if not KEYS:
    print("NO KEYS — set the GEMINI_KEYS secret", flush=True); raise SystemExit(1)
asyncio.run(main())
