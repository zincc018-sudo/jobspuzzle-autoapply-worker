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
try:
    from deterministic import ats_platform, deterministic_fill, deterministic_submit   # Tier-1 zero-LLM
except Exception:
    ats_platform = lambda u: None; deterministic_fill = None; deterministic_submit = None  # LLM-only if unavailable

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

def _task(url, p, submit=False):
    name = p.get("name") or p.get("full_name") or "Applicant"
    email = p.get("email") or ""
    phone = p.get("phone") or ""
    common = (
        f"Go to {url} and WAIT for it to fully render (some are JavaScript single-page apps). "
        "Dismiss any cookie/consent banner (Accept or Decline). "
        "If there is an 'Apply' / 'Apply for this job' / 'Apply now' button, click it to open the form. "
        f"Fill ONLY these three fields: Name '{name}', Email '{email}', Phone '{phone}'. "
        "Upload the available resume file into the resume/CV field if one exists. "
        "Do NOT search for, scroll to, or fill any OPTIONAL field (location, cover letter, links, "
        "demographics, work authorization). Do NOT re-verify fields you already filled. "
    )
    if submit:
        # SUBMIT PHASE — only reached AFTER the user reviewed the filled form + clicked Approve.
        return common + (
            "Once name+email+phone are filled and the resume is attached, click the FINAL Submit / "
            "Apply / Send button to submit the application. If a confirmation dialog appears, confirm it. "
            "Then call done and report EXACTLY: 'Submitted: yes/no. Confirmation seen: <text or no>.'"
        )
    # FILL PHASE — fills + STOPS, so the user can review before anything is sent.
    return common + (
        "The MOMENT name+email+phone are filled and the resume is attached, call done IMMEDIATELY "
        "(aim <=8 steps). ABSOLUTE RULE: NEVER click Submit / Apply-final / Send / Continue-to-submit. "
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

async def run_apply(url, profile, cv_path, keys, max_steps=18, submit=False):
    ring = keys if isinstance(keys, KeyRing) else KeyRing(keys)
    last_err = None
    for _attempt in range(2):            # rotate to a fresh key once on failure
        key = ring.next()
        if not key:
            return {"reached": False, "fields": "", "submitted": False, "screenshot_b64": None, "error": "no usable key"}
        try:
            files = [cv_path] if cv_path and os.path.exists(cv_path) else []
            agent = Agent(task=_task(url, profile, submit=submit),
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
            submitted = submit and "submitted: yes" in low
            return {"reached": reached or submitted, "fields": final, "submitted": submitted,
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


async def smart_fill(url, profile, cv_path, keys, max_steps=18, submit=False):
    """Router: Tier-1 deterministic (zero LLM) for the 6 proven ATS hosts; LLM agent
    otherwise, OR if the deterministic fill doesn't land (selector drift -> graceful
    fallback). The SUBMIT phase always uses the LLM path (deterministic submit unproven)."""
    plat = ats_platform(url)
    if plat and not submit and deterministic_fill is not None:
        try:
            r = await deterministic_fill(url, profile, cv_path)
            if r.get("reached"):
                r["engine"] = f"deterministic:{plat}"
                return r
            # fill didn't land -> fall through to the LLM agent
        except Exception:
            pass
    if plat and submit and deterministic_submit is not None:
        # SUBMIT phase, proven host: deterministic fill + click + confirmation read-back
        # (zero LLM). Runs ONLY here (post-Approve). If it can't confirm, fall through
        # to the LLM submit as a backstop.
        try:
            r = await deterministic_submit(url, profile, cv_path)
            if r.get("submitted"):
                return r   # engine already 'deterministic-submit:<plat>'
        except Exception:
            pass
    r = await run_apply(url, profile, cv_path, keys, max_steps=max_steps, submit=submit)
    if isinstance(r, dict):
        r.setdefault("engine", "llm")
    return r


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
