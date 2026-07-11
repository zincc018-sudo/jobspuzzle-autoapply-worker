# ============================================================
# JobsPuzzle — Auto-apply WORKER (queue poller). Runs on a persistent box
# (Render / Railway / Fly / any VPS) — NOT on Vercel (serverless can't hold a browser).
#
# Loop: claim_autoapply_job() -> download CV -> run_apply() (fills, screenshots, NO submit)
#       -> write status=prepared|failed + screenshot back to Supabase.
# The review-gate UI shows the screenshot; the USER submits. Worker never submits.
#
# Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE, GEMINI_KEYS (comma-sep), WORKER_ID (optional)
# ============================================================
import os, time, tempfile, traceback, urllib.request, asyncio
from supabase import create_client
from agent import run_apply, KeyRing

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_ROLE = os.environ["SUPABASE_SERVICE_ROLE"]
KEYS = [k.strip() for k in os.environ.get("GEMINI_KEYS", "").split(",") if k.strip()]
WORKER_ID = os.environ.get("WORKER_ID", "worker-1")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))  # requeue transient crashes up to this many times

sb = create_client(SUPABASE_URL, SERVICE_ROLE)
ring = KeyRing(KEYS)

def _download_cv(cv_url):
    if not cv_url:
        return None
    try:
        path = os.path.join(tempfile.gettempdir(), f"cv_{int(time.time()*1000)}.pdf")
        req = urllib.request.Request(cv_url, headers={"user-agent": "jobspuzzle-worker"})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception as e:
        print(f"[cv] download failed: {e}", flush=True)
        return None

def _update(job_id, patch):
    sb.table("autoapply_jobs").update(patch).eq("id", job_id).execute()

def process(job):
    jid = job["id"]
    print(f"[claim] {jid} -> {job['apply_url']}", flush=True)
    cv_path = _download_cv(job.get("cv_url"))
    attempts = job.get("attempts") or 1   # claim_autoapply_job already incremented this
    try:
        r = asyncio.run(run_apply(job["apply_url"], job.get("profile") or {}, cv_path, ring))
        if r["reached"]:
            status = "prepared"
        elif attempts < MAX_ATTEMPTS:
            status = "queued"   # transient crash/hang/CDP error — retry on the next drain
        else:
            status = "failed"   # exhausted retries — stop, surface honestly to the user
        _update(jid, {
            "status": status,
            "reached_form": r["reached"],
            "result": {"fields": r.get("fields"), "error": r.get("error"), "model": "gemini-flash-latest", "attempt": attempts},
            "screenshot": r.get("screenshot_b64"),
            "updated_at": "now()",
        })
        print(f"[done] {jid} status={status} reached={r['reached']} attempt={attempts}/{MAX_ATTEMPTS}", flush=True)
    except Exception:
        # even a hard crash requeues (up to the cap) so one bad job never dead-ends silently
        status = "queued" if attempts < MAX_ATTEMPTS else "failed"
        _update(jid, {"status": status, "result": {"error": traceback.format_exc()[:500], "attempt": attempts}, "updated_at": "now()"})
        print(f"[error] {jid} status={status} attempt={attempts}\n{traceback.format_exc()}", flush=True)
    finally:
        if cv_path and os.path.exists(cv_path):
            try: os.remove(cv_path)
            except Exception: pass

def main():
    print(f"[worker] {WORKER_ID} up. keys={len(KEYS)} poll={POLL_SECONDS}s", flush=True)
    if not KEYS:
        print("[worker] FATAL: no GEMINI_KEYS set", flush=True); return
    while True:
        try:
            res = sb.rpc("claim_autoapply_job", {"p_worker": WORKER_ID}).execute()
            jobs = res.data or []
            if jobs:
                process(jobs[0])
                continue                     # immediately try for the next one
        except Exception as e:
            print(f"[poll] error: {e}", flush=True)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
