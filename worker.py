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
from datetime import datetime, timedelta, timezone
from supabase import create_client
from agent import smart_fill, KeyRing   # smart_fill routes deterministic (zero-LLM) vs LLM

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_ROLE = os.environ["SUPABASE_SERVICE_ROLE"]
KEYS = [k.strip() for k in os.environ.get("GEMINI_KEYS", "").split(",") if k.strip()]
# Track-2 pool (2026-07-12): Groq keys ride the same ring with a "groq:" prefix —
# agent._make_llm() routes them to ChatGroq/llama-4-scout (vision, ~500K tok/day/key
# free). INACTIVE until the GROQ_KEYS secret is set (zinc delivering 10 keys).
KEYS += ["groq:" + k.strip() for k in os.environ.get("GROQ_KEYS", "").split(",") if k.strip()]
# Mistral pool (2026-07-13): free Experiment tier ≈1B tokens/month. Listed FIRST so the
# ring prefers Mistral (deep quota) before burning the 20/day Gemini keys.
KEYS = ["mistral:" + k.strip() for k in os.environ.get("MISTRAL_KEYS", "").split(",") if k.strip()] + KEYS
WORKER_ID = os.environ.get("WORKER_ID", "worker-1")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))  # requeue transient crashes up to this many times
DRAIN_ONCE = os.environ.get("DRAIN_ONCE") == "1"          # drain the queue then exit (for cron/CI)

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

def process(job, submit=False):
    """submit=False = FILL phase (fill + screenshot + STOP, -> prepared for user review).
       submit=True  = SUBMIT phase (user approved; re-fill + click submit -> submitted)."""
    jid = job["id"]
    phase = "submit" if submit else "fill"
    retry_status = "approved" if submit else "queued"   # requeue back into the same phase
    print(f"[{phase}] {jid} -> {job['apply_url']}", flush=True)
    cv_path = _download_cv(job.get("cv_url"))
    attempts = job.get("attempts") or 1   # claim RPC already incremented this
    try:
        r = asyncio.run(smart_fill(job["apply_url"], job.get("profile") or {}, cv_path, ring, submit=submit))
        done_ok = r["submitted"] if submit else r["reached"]
        if done_ok:
            status = "submitted" if submit else "prepared"
        elif attempts < MAX_ATTEMPTS:
            status = retry_status   # transient crash/hang/CDP/rate error — retry this phase
        else:
            status = "failed"       # exhausted retries — surface honestly to the user
        patch = {
            "status": status,
            "result": {"fields": r.get("fields"), "error": r.get("error"), "engine": r.get("engine"), "attempt": attempts, "phase": phase},
            "updated_at": "now()",
        }
        if not submit:
            patch["reached_form"] = r["reached"]
            patch["screenshot"] = r.get("screenshot_b64")   # the review-gate image
        _update(jid, patch)
        print(f"[{phase} done] {jid} status={status} ok={done_ok} attempt={attempts}/{MAX_ATTEMPTS}", flush=True)
    except Exception:
        status = retry_status if attempts < MAX_ATTEMPTS else "failed"
        _update(jid, {"status": status, "result": {"error": traceback.format_exc()[:500], "attempt": attempts, "phase": phase}, "updated_at": "now()"})
        print(f"[{phase} error] {jid} status={status} attempt={attempts}\n{traceback.format_exc()}", flush=True)
    finally:
        if cv_path and os.path.exists(cv_path):
            try: os.remove(cv_path)
            except Exception: pass

def reclaim_stale():
    """A worker that died AFTER claiming but BEFORE writing back leaves a job stuck in
    'preparing'/'submitting'. Reset anything stuck >15 min back to its queue so it retries."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        for stuck, back in (("preparing", "queued"), ("submitting", "approved")):
            r = sb.table("autoapply_jobs").update({"status": back}).eq("status", stuck).lt("updated_at", cutoff).execute()
            if r.data:
                print(f"[reclaim] {len(r.data)} stuck '{stuck}' -> '{back}'", flush=True)
    except Exception as e:
        print(f"[reclaim] error: {e}", flush=True)

def main():
    print(f"[worker] {WORKER_ID} up. keys={len(KEYS)} poll={POLL_SECONDS}s", flush=True)
    if not KEYS:
        print("[worker] FATAL: no GEMINI_KEYS set", flush=True); return
    reclaim_stale()
    while True:
        did = False
        try:
            # SUBMIT phase first — a user is actively waiting on an approved job.
            res = sb.rpc("claim_autoapply_job", {"p_worker": WORKER_ID, "p_status": "approved", "p_next": "submitting"}).execute()
            if res.data:
                process(res.data[0], submit=True); did = True
            else:
                # else FILL phase — prepare a queued job for review.
                res = sb.rpc("claim_autoapply_job", {"p_worker": WORKER_ID, "p_status": "queued", "p_next": "preparing"}).execute()
                if res.data:
                    process(res.data[0], submit=False); did = True
        except Exception as e:
            print(f"[poll] error: {e}", flush=True)
        if not did:
            if DRAIN_ONCE:
                print("[worker] queue empty — exiting (drain-once)", flush=True); return
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
