# ============================================================
# JobsPuzzle — Tier-1 DETERMINISTIC filler. ZERO LLM tokens.
# Ported from the proven _ats_prove.py run (20/20 real job-fills, ats_final.log):
#   Greenhouse, Lever, Ashby, TeamTailor, Workable, CareerFinders.
# Raw Playwright + stable selectors (from extension/src/content/*.ts) + Playwright
# label matching. NEVER submits. Returns the same shape as agent.run_apply so the
# worker can use either engine interchangeably.
#
# Router: ats_platform(url) -> platform name if the URL is a proven zero-LLM host,
# else None (caller falls back to the LLM agent). If Tier-1 fills nothing (selector
# drift), the caller also falls back — graceful degrade, never a hard fail.
# ============================================================
import re, base64, os
from playwright.async_api import async_playwright

# host -> platform, ONLY the 6 with read-back proof. SmartRecruiters excluded (DataDome captcha).
_HOSTS = [
    (re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io", re.I), "greenhouse"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com", re.I), "ashby"),
    (re.compile(r"[a-z0-9-]+\.teamtailor\.com", re.I), "teamtailor"),
    (re.compile(r"apply\.workable\.com", re.I), "workable"),
    (re.compile(r"(?:portal\.)?careerfinders\.com\.cy", re.I), "careerfinders"),
]

def ats_platform(url):
    """Return the proven-zero-LLM platform for this apply URL, else None."""
    if not url:
        return None
    for rx, name in _HOSTS:
        if rx.search(url):
            return name
    return None


async def _try_fill(page, getters, value):
    for g in getters:
        try:
            loc = g()   # page.locator()/get_by_label() are SYNC
            if loc and await loc.count() > 0:
                el = loc.first
                if await el.is_visible() and await el.is_editable():
                    await el.fill(value)
                    return await el.input_value()
        except Exception:
            continue
    return None


async def _fill_form(page, p, cv_path):
    out = {}
    first = await _try_fill(page, [
        lambda: page.locator("#first_name"),
        lambda: page.locator("#first-name-input"),
        lambda: page.locator("input[name*='first' i]"),
        lambda: page.get_by_label(re.compile(r"first name", re.I)),
    ], p["first"])
    if first:
        last = await _try_fill(page, [
            lambda: page.locator("#last_name"),
            lambda: page.locator("#last-name-input"),
            lambda: page.locator("input[name*='last' i]"),
            lambda: page.get_by_label(re.compile(r"last name", re.I)),
        ], p["last"])
        out["name"] = f"{first} {last or ''}".strip()
    else:
        full = await _try_fill(page, [
            lambda: page.locator("input[name='name']"),
            lambda: page.locator("input[name*='candidate.name' i]"),
            lambda: page.get_by_label(re.compile(r"^(full )?name$", re.I)),
            lambda: page.locator("input[id*='name' i]:not([id*='last' i]):not([id*='user' i])"),
        ], p["full"])
        if full:
            out["name"] = full
    email = await _try_fill(page, [
        lambda: page.locator("#email"),
        lambda: page.locator("input[type='email']"),
        lambda: page.get_by_label(re.compile(r"email", re.I)),
        lambda: page.locator("input[name*='email' i]"),
    ], p["email"])
    if email:
        out["email"] = email
    phone = await _try_fill(page, [
        lambda: page.locator("#phone"),
        lambda: page.locator("input[type='tel']"),
        lambda: page.get_by_label(re.compile(r"phone", re.I)),
    ], p["phone"])
    if phone:
        out["phone"] = phone
    # resume — hold element handle across set+read (a re-queried locator loses .files on re-render)
    if cv_path and os.path.exists(cv_path):
        for sel in ["input[type=file]#resume", "input[type=file][id*=resume i]",
                    "input[type=file][name*=resume i]", "input[type=file][accept*=pdf i]", "input[type=file]"]:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    el = await loc.first.element_handle()
                    if not el:
                        continue
                    await el.set_input_files(cv_path)
                    await page.wait_for_timeout(1000)
                    r = await el.evaluate("e => e.files && e.files[0] ? e.files[0].name : null")
                    if r:
                        out["resume"] = r
                        break
            except Exception:
                continue
    return out


# ── SUBMIT phase (post-Approve only) ────────────────────────────────────────
# The FILL path above NEVER submits. This block adds a deterministic SUBMIT used
# ONLY by the worker's submit phase, which runs ONLY after the user clicked
# Approve in the review-gate. submitted=True is returned ONLY when a real
# confirmation (URL or on-page text) is seen — a clicked button alone is not proof.
_SUBMIT = {
    "greenhouse":    ["button:has-text('Submit Application')", "button:has-text('Submit application')", "#submit_app", "input[type=submit]"],
    "lever":         ["button:has-text('Submit application')", "button.template-btn-submit", "button[type=submit]"],
    "ashby":         ["button:has-text('Submit Application')", "button:has-text('Submit')", "button[type=submit]"],
    "teamtailor":    ["button:has-text('Send application')", "button:has-text('Submit application')", "button[type=submit]"],
    "workable":      ["button:has-text('Submit')", "#btn-submit", "button[type=submit]"],
    "careerfinders": ["button:has-text('Submit')", "input[type=submit]", "button[type=submit]"],
}
_CONFIRM_URL = re.compile(r"(confirmation|thank|success|submitted|application-received|/complete)", re.I)
_CONFIRM_TEXT = re.compile(
    r"(thank you|application (has been )?(submitted|received|sent)|successfully (applied|submitted)|"
    r"we('| ha)ve received your application|your application (has been|was) (sent|submitted|received))", re.I)

async def _find_submit(page, platform):
    for sel in _SUBMIT.get(platform, ["button[type=submit]", "input[type=submit]"]):
        try:
            loc = page.locator(sel)
            for i in range(await loc.count()):
                el = loc.nth(i)
                if await el.is_visible() and await el.is_enabled():
                    return el
        except Exception:
            continue
    return None

async def _confirmed(page):
    try:
        if _CONFIRM_URL.search(page.url or ""):
            return True
        body = (await page.inner_text("body"))[:6000]
        if _CONFIRM_TEXT.search(body or ""):
            return True
    except Exception:
        pass
    return False


async def deterministic_submit(url, profile, cv_path, dry_run=False):
    """SUBMIT phase (post-Approve): fill + click the FINAL submit + read back a
    confirmation. ZERO LLM. dry_run=True fills + LOCATES the submit button but does
    NOT click it (proves the submit control is reachable WITHOUT sending anything).
    submitted=True only when a confirmation page/text is actually seen."""
    platform = ats_platform(url) or ""
    name = profile.get("name") or profile.get("full_name") or "Applicant"
    parts = name.split()
    p = {"first": parts[0] if parts else name, "last": " ".join(parts[1:]) or (parts[0] if parts else name),
         "full": name, "email": profile.get("email") or "", "phone": profile.get("phone") or ""}
    result = {"reached": False, "fields": "", "submitted": False, "screenshot_b64": None,
              "error": None, "engine": f"deterministic-submit:{platform}"}
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            pg = await b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(3000)
            title = (await pg.title()) or ""
            if any(x in title.lower() for x in ["just a moment", "attention required", "access denied", "captcha"]):
                result["error"] = f"anti-bot: {title[:40]}"; return result
            for c in ["button:has-text('Accept all')", "button:has-text('Accept All')",
                      "button:has-text('Decline all')", "#onetrust-accept-btn-handler"]:
                try:
                    el = pg.locator(c)
                    if await el.count() > 0 and await el.first.is_visible():
                        await el.first.click(); await pg.wait_for_timeout(600); break
                except Exception:
                    pass
            fields = await _fill_form(pg, p, cv_path)
            if not fields.get("name") and not fields.get("email"):
                for sel in ["button:has-text('Apply for this job')", "button:has-text('Apply now')",
                            "button:has-text(\"I'm interested\")", "a:has-text(\"I'm interested\")",
                            "a:has-text('Apply')", "button:has-text('Apply')"]:
                    try:
                        el = pg.locator(sel)
                        if await el.count() > 0 and await el.first.is_visible():
                            await el.first.click(); await pg.wait_for_timeout(4000); break
                    except Exception:
                        continue
                fields = await _fill_form(pg, p, cv_path)
            result["reached"] = bool(fields.get("name")) and bool(fields.get("email"))
            submit_el = await _find_submit(pg, platform)
            filled_list = ', '.join(k for k in fields if k != 'resume')
            if not result["reached"]:
                result["error"] = "fill incomplete — refusing to submit"
            elif submit_el is None:
                result["error"] = "submit button not found — needs manual submit"
            elif dry_run:
                result["fields"] = (f"DRY-RUN ok: reached + submit button located (NOT clicked). "
                                    f"Fields: {filled_list}. Resume: {'yes' if fields.get('resume') else 'no'}.")
            else:
                # THE REAL SUBMIT — reached only in the non-dry-run submit phase, post-Approve.
                try:
                    await submit_el.scroll_into_view_if_needed()
                except Exception:
                    pass
                await submit_el.click()
                await pg.wait_for_timeout(6000)   # let the POST + any redirect settle
                result["submitted"] = await _confirmed(pg)
                if not result["submitted"]:
                    result["error"] = "clicked submit but no confirmation seen (form may need required fields answered)"
            try:
                shot = await pg.screenshot(full_page=False)
                result["screenshot_b64"] = base64.b64encode(shot).decode()
            except Exception:
                pass
            if not result["fields"]:
                result["fields"] = (f"Reached: {'yes' if result['reached'] else 'no'}. "
                                    f"Submitted: {'yes' if result['submitted'] else 'no'}. Fields: {filled_list}. "
                                    f"Resume: {'yes' if fields.get('resume') else 'no'}.")
        except Exception as e:
            result["error"] = str(e)[:200]
        finally:
            await b.close()
    return result


async def deterministic_fill(url, profile, cv_path):
    """Fill a known-ATS form with NO LLM. Returns run_apply-compatible dict.
    reached=True only if name AND email were filled (the fill actually landed)."""
    name = profile.get("name") or profile.get("full_name") or "Applicant"
    parts = name.split()
    p = {"first": parts[0] if parts else name, "last": " ".join(parts[1:]) or (parts[0] if parts else name),
         "full": name, "email": profile.get("email") or "", "phone": profile.get("phone") or ""}
    result = {"reached": False, "fields": "", "submitted": False, "screenshot_b64": None, "error": None, "engine": "deterministic"}
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            pg = await b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(3000)
            title = (await pg.title()) or ""
            if any(x in title.lower() for x in ["just a moment", "attention required", "access denied", "captcha"]):
                result["error"] = f"anti-bot: {title[:40]}"; return result
            for c in ["button:has-text('Accept all')", "button:has-text('Accept All')",
                      "button:has-text('Decline all')", "#onetrust-accept-btn-handler"]:
                try:
                    el = pg.locator(c)
                    if await el.count() > 0 and await el.first.is_visible():
                        await el.first.click(); await pg.wait_for_timeout(600); break
                except Exception:
                    pass
            fields = await _fill_form(pg, p, cv_path)
            if "captcha" in pg.url.lower():
                result["error"] = "captcha wall"; return result
            if not fields.get("name") and not fields.get("email"):
                for sel in ["button:has-text('Apply for this job')", "button:has-text('Apply now')",
                            "button:has-text(\"I'm interested\")", "a:has-text(\"I'm interested\")",
                            "a:has-text('Apply')", "button:has-text('Apply')"]:
                    try:
                        el = pg.locator(sel)
                        if await el.count() > 0 and await el.first.is_visible():
                            await el.first.click(); await pg.wait_for_timeout(4000); break
                    except Exception:
                        continue
                fields = await _fill_form(pg, p, cv_path)
            try:
                shot = await pg.screenshot(full_page=False)
                result["screenshot_b64"] = base64.b64encode(shot).decode()
            except Exception:
                pass
            result["reached"] = bool(fields.get("name")) and bool(fields.get("email"))
            result["fields"] = (f"Reached form: {'yes' if result['reached'] else 'no'}. "
                                f"Fields filled: {', '.join(k for k in fields if k != 'resume')}. "
                                f"Resume attached: {'yes' if fields.get('resume') else 'no'}.")
            if not result["reached"]:
                result["error"] = "deterministic fill incomplete (name/email not reached)"
        except Exception as e:
            result["error"] = str(e)[:200]
        finally:
            await b.close()
    return result
