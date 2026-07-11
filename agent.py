# ============================================================
# JobsPuzzle — Auto-apply worker CORE (vision agent).
# Proven engine: browser-use + gemini-flash-latest, 9/10 real forms filled across
# Greenhouse/Lever/Ashby/SmartRecruiters (batch_results3.log, 2026-07-12).
#
# run_apply(url, profile, cv_path, keys) ->
#     { reached: bool, fields: str, submitted: False, screenshot_b64: str|None, error: str|None }
#
# HARD RULE: this NEVER submits. The task forbids it and there is no submit path here.
# The review-gate (user clicks send) is the whole safety moat. Do not add a submit here.
# ============================================================
import asyncio, base64, os
from browser_use import Agent, ChatGoogle, BrowserProfile

# chromium_sandbox=False -> browser-use adds --no-sandbox/--disable-dev-shm-usage, required
# to launch headless Chrome on datacenter/CI boxes (GitHub Actions, most VPS/containers).
# Without it Chrome hangs on start and hits browser-use's 30s BrowserStartEvent timeout.
BROWSER_PROFILE = BrowserProfile(headless=True, chromium_sandbox=False)

MODEL = "gemini-flash-latest"   # the only model available on all 12 keys (flash-lite 404s on most)
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "150"))  # hard cap per form — a hung browser
# (the ashby CDP hang on Colab ran 11 min) must NOT block the queue. Timeout -> caught -> requeued.

# --- key rotation: round-robin, skip keys that 404/quota-fail this process ---
class KeyRing:
    def __init__(self, keys):
        self.keys = list(keys)
        self.i = 0
        self.bad = set()
    def next(self):
        for _ in range(len(self.keys)):
            k = self.keys[self.i % len(self.keys)]
            self.i += 1
            if k not in self.bad:
                return k
        return self.keys[0] if self.keys else None   # all bad → last resort
    def mark_bad(self, k):
        self.bad.add(k)

def _task(url, p):
    name = p.get("name") or p.get("full_name") or "Applicant"
    email = p.get("email") or ""
    phone = p.get("phone") or ""
    return (
        f"Go to {url} and WAIT for it to fully render (some are JavaScript single-page apps). "
        "Dismiss any cookie/consent banner (Accept or Decline). "
        "If there is an 'Apply' / 'Apply for this job' / 'Apply now' button, click it to open the form. "
        f"Fill ONLY these three fields: Name '{name}', Email '{email}', Phone '{phone}'. "
        "Upload the available resume file into the resume/CV field if one exists. "
        "EFFICIENCY RULE: do NOT search for, scroll to, or fill any OPTIONAL field (location, "
        "cover letter, links, demographics, work authorization). Do NOT re-verify fields you already "
        "filled. The MOMENT name+email+phone are filled and the resume is attached, call done "
        "IMMEDIATELY — aim to finish within 8 steps total. "
        "ABSOLUTE RULE: NEVER click Submit / Apply-final / Send / Continue-to-submit. "
        "Report EXACTLY: 'Reached form: yes/no. Fields filled: <list>. Resume attached: yes/no. "
        "Captcha/login: yes/no.'"
    )

async def _screenshot(agent, history):
    # try history first (base64 png strings), then a live grab
    try:
        shots = history.screenshots() if hasattr(history, "screenshots") else None
        if shots:
            s = shots[-1]
            if s: return s if isinstance(s, str) else base64.b64encode(s).decode()
    except Exception:
        pass
    try:
        bs = getattr(agent, "browser_session", None)
        if bs:
            raw = await bs.take_screenshot()
            if raw: return raw if isinstance(raw, str) else base64.b64encode(raw).decode()
    except Exception:
        pass
    return None

async def run_apply(url, profile, cv_path, keys, max_steps=18):
    ring = keys if isinstance(keys, KeyRing) else KeyRing(keys)
    last_err = None
    for _attempt in range(2):            # rotate to a fresh key once on failure
        key = ring.next()
        if not key:
            return {"reached": False, "fields": "", "submitted": False, "screenshot_b64": None, "error": "no usable key"}
        try:
            files = [cv_path] if cv_path and os.path.exists(cv_path) else []
            agent = Agent(task=_task(url, profile),
                          llm=ChatGoogle(model=MODEL, api_key=key),
                          browser_profile=BROWSER_PROFILE,
                          available_file_paths=files)
            # hard timeout so a hung/CDP-stuck browser can't block the worker forever
            history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=RUN_TIMEOUT)
            final = history.final_result()
            shot = await _screenshot(agent, history)
            if final is None:
                last_err = "no result (agent stalled)"
                continue
            low = final.lower()
            reached = "reached form: yes" in low
            return {"reached": reached, "fields": final, "submitted": False,
                    "screenshot_b64": shot, "error": None}
        except asyncio.TimeoutError:
            last_err = f"run timed out after {RUN_TIMEOUT}s (browser hang) — requeue"
            continue
        except Exception as e:
            msg = str(e)
            last_err = msg[:200]
            if "404" in msg or "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                ring.mark_bad(key)       # this key can't serve the model → try another
                continue
            break
    return {"reached": False, "fields": "", "submitted": False, "screenshot_b64": None, "error": last_err}


if __name__ == "__main__":
    # local smoke test: fill ONE real form with a test profile, prove no-submit + screenshot
    import sys, json
    sp = sys.argv[1]
    keys = [k.strip() for k in open(f"{sp}/keys.txt") if k.strip()][-4:]  # freshest keys for the smoke test
    profile = {"name": "Droid Test", "email": "droid-autofill-test@applyright.dev",
               "phone": "+31612345678", "location": "Amsterdam"}
    url = sys.argv[2] if len(sys.argv) > 2 else "https://jobs.lever.co/palantir/8f362a1f-1eff-4327-94c1-ff46e2101c69"
    r = asyncio.run(run_apply(url, profile, f"{sp}/test_cv.pdf", keys))
    if r.get("screenshot_b64"):
        open(f"{sp}/apply_shot.png", "wb").write(base64.b64decode(r["screenshot_b64"]))
        r["screenshot_b64"] = f"[saved {len(r['screenshot_b64'])} b64 chars -> apply_shot.png]"
    print(json.dumps(r, indent=2))
