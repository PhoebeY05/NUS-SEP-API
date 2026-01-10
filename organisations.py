# Program to retrieve all partner universities (SCHL) => ~2 hours
import os
import sys
import json
from datetime import datetime, timezone

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utilities import (close_modal, derive_items_from_csv,
                       navigate_to_search_course_mappings, print_new_stage,
                       resolve_component_root)

EDUREC_HOME = "https://myedurec.nus.edu.sg"
DOWNLOAD_DIR = "data"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
SEARCH_PAGE = None
ORG_CSV = os.path.join(DOWNLOAD_DIR, "organisations.csv")

UNIV_FIELD = "input[id^='N_EXSP_DRVD_EXT_ORG_ID']"
UNIV_PROMPT = "a[id^='N_EXSP_DRVD_EXT_ORG_ID$prompt']"
DOWNLOAD_ICON = "[id^='N_EXSP_DRVD$hexcel$img']"
NO_RECORDS_TEXT = "No matching records found"

TABLE_ID = "#PTSRCHRESULTS"
NUM_WIDTH = 10  # digits after 'E'
CHUNK_DIGITS = 2  # default chunk size used by bump_prefix_chunk
BASE_DIGITS = 2  # number of trailing digits to iterate within each base (e.g., 1 for tens, 2 for hundreds)
CHECKPOINT_PATH = os.path.join(DOWNLOAD_DIR, "organisations.resume.json")

def apply_filter(target, option, search):
    print_new_stage(f"Applying filter {search}")
    field_loc = None
    # Set dropdown
    try:
        target.wait_for_selector("[name='#ICKeySelect']", timeout=5000)
        dd = target.locator("[name='#ICKeySelect']").first
        if dd.count() > 0:
            try:
                dd.select_option(value=option["number"])
            except Exception:
                try:
                    dd.select_option(label=option["label"])
                except Exception:
                    pass
    except Exception:
        pass
    print("➡️ Set dropdown")
    # Determine which field to fill based on option
    try:
        opt_num = option.get("number")
        if not opt_num:
            label = (option.get("label") or "").strip()
            opt_num = {"External Org ID": "0", "Organization Type": "2", "Search Name": "1"}.get(label)
        # Candidate selectors per option
        candidates = []
        if opt_num == "2":
            candidates = ["#EXT_ORG_TBL_EXT_ORG_TYPE"]
        elif opt_num == "0":
            candidates = [
                "#EXT_ORG_TBL_EXT_ORG_ID",
                "input[name='EXT_ORG_TBL_EXT_ORG_ID']",
                "[id^='EXT_ORG_TBL_EXT_ORG_ID']",
            ]
        elif opt_num == "1":
            candidates = [
                "#EXT_ORG_TBL_OTH_NAME_SORT_SRCH",
                "input[name='EXT_ORG_TBL_OTH_NAME_SORT_SRCH']",
                "[id^='EXT_ORG_TBL_OTH_NAME_SORT_SRCH']",
            ]
        else:
            # Fallback: try known fields in order
            candidates = [
                "#EXT_ORG_TBL_EXT_ORG_TYPE",
                "#EXT_ORG_TBL_EXT_ORG_ID",
                "#EXT_ORG_TBL_SEARCH_NAME",
            ]

        # Wait for any candidate to appear after the dropdown updates (visible + enabled)
        for sel in candidates:
            try:
                vis_sel = f"{sel}:not([disabled])"
                target.wait_for_selector(vis_sel, timeout=5000)
                loc = target.locator(vis_sel).first
                if loc.count() > 0:
                    try:
                        # Prefer visible element
                        if loc.is_visible():
                            field_loc = loc
                            break
                    except Exception:
                        field_loc = loc
                        break
            except Exception:
                continue
        if not field_loc:
            # Small grace period then last-chance probe
            target.wait_for_timeout(250)
            for sel in candidates:
                loc = target.locator(f"{sel}:not([disabled])").first
                if loc.count() > 0:
                    field_loc = loc
                    break
        if field_loc:
            try:
                field_loc.focus()
            except Exception:
                pass
            # Clear then set value atomically to avoid mid-typing handlers truncating input
            try:
                # Strong clear: select-all then delete, then fallback to fill("")
                try:
                    field_loc.press("Meta+a")
                    field_loc.press("Delete")
                except Exception:
                    pass
                field_loc.fill("")
            except Exception:
                pass
            try:
                field_loc.fill(search)
            except Exception:
                # Last-resort direct set + change/input events
                try:
                    field_loc.evaluate(
                        "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                        search,
                    )
                except Exception:
                    pass
            # Commit value so PeopleSoft onblur handlers register it
            try:
                field_loc.press("Tab")
            except Exception:
                pass
            try:
                # Explicit blur to ensure onchange/onblur runs
                field_loc.evaluate("el => el.blur()")
            except Exception:
                pass
    except Exception:
        pass
    print("➡️ Filled search field")

    # Extra hardening: re-acquire after re-render, fill ALL candidates, verify value sticks (retry loop)
    try:
        opt_num = option.get("number") or {"External Org ID": "0", "Organization Type": "2", "Search Name": "1"}.get((option.get("label") or "").strip())
        extra_candidates = []
        if opt_num == "2":
            extra_candidates = ["#EXT_ORG_TBL_EXT_ORG_TYPE"]
        elif opt_num == "0":
            extra_candidates = [
                "#EXT_ORG_TBL_EXT_ORG_ID",
                "input[name='EXT_ORG_TBL_EXT_ORG_ID']",
                "[id^='EXT_ORG_TBL_EXT_ORG_ID']",
            ]
        elif opt_num == "1":
            extra_candidates = [
                "#EXT_ORG_TBL_OTH_NAME_SORT_SRCH",
                "input[name='EXT_ORG_TBL_OTH_NAME_SORT_SRCH']",
                "[id^='EXT_ORG_TBL_OTH_NAME_SORT_SRCH']",
            ]
        if extra_candidates:
            combo_sel = ",".join([f"{s}:not([disabled])" for s in extra_candidates])
            for _ in range(4):
                try:
                    target.wait_for_selector(combo_sel, timeout=2000)
                except Exception:
                    pass
                try:
                    all_inputs = target.locator(combo_sel)
                    cnt = all_inputs.count()
                    # Fill all visible inputs
                    for i in range(cnt):
                        try:
                            el = all_inputs.nth(i)
                            if not el.is_visible():
                                continue
                            # Debug: log id/name/value
                            try:
                                id_attr = el.get_attribute("id") or ""
                                name_attr = el.get_attribute("name") or ""
                                cur_val = el.input_value(timeout=500) if hasattr(el, "input_value") else ""
                                print(f"   ↪︎ targeting {id_attr or name_attr}: '{cur_val}' -> '{search}'")
                            except Exception:
                                pass
                            try:
                                el.focus()
                            except Exception:
                                pass
                            try:
                                el.press("Meta+a"); el.press("Delete")
                            except Exception:
                                pass
                            try:
                                el.fill("")
                            except Exception:
                                pass
                            try:
                                el.fill(search)
                            except Exception:
                                try:
                                    el.evaluate(
                                        "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                                        search,
                                    )
                                except Exception:
                                    pass
                            try:
                                el.press("Tab")
                            except Exception:
                                pass
                            try:
                                el.evaluate("el => el.blur()")
                            except Exception:
                                pass
                        except Exception:
                            continue
                    # Verify value stuck on any visible, enabled input
                    ok = False
                    try:
                        ok = target.evaluate(
                            "(arg) => { const sels=arg.sels; const val=arg.val; for (const sel of sels) { const els = Array.from(document.querySelectorAll(sel)); for (const el of els) { if (!el.disabled && el.offsetParent!==null && el.value===val) return true; } } return false; }",
                            {"sels": [f"{s}:not([disabled])" for s in extra_candidates], "val": search},
                        )
                    except Exception:
                        ok = False
                    if ok:
                        break
                    # Small wait before retry to allow re-render that may wipe value
                    try:
                        target.wait_for_timeout(250)
                    except Exception:
                        pass
                except Exception:
                    continue
    except Exception:
        pass

    # Trigger search
    for ssel in [
        "[id='#ICSearch']",
        "[name='#ICSearch']",
        "input[name='#ICSearch']",
        "input[id='#ICSearch']",
        "button:has-text('Search')",
        "a:has-text('Search')",
        "span:has-text('Search')",
    ]:
        try:
            loc = target.locator(ssel).first
            if loc.count() == 0:
                continue
            loc.click(force=True)
            break
        except Exception:
            continue
    # Always try an Enter on the active field to ensure submission
    try:
        if field_loc:
            field_loc.press("Enter")
        else:
            target.locator("#EXT_ORG_TBL_EXT_ORG_TYPE").first.press("Enter")
    except Exception:
        pass
    print("➡️ Triggered search")

    # Wait briefly for results to load
    try:
        target.wait_for_selector(f"{TABLE_ID}, table.PSSRCHRESULTSWBO", timeout=8000)
    except Exception:
        pass

    # Confirm the results reflect the new prefix or no-records sentinel appears; else, retry Enter
    try:
        ok = False
        try:
            target.wait_for_function(
                "(arg) => { const val=arg.val; const none=arg.no; if (document.body && document.body.innerText.includes(none)) return true; const anchors = Array.from(document.querySelectorAll(\"a[name^='RESULT0$'], a[id^='RESULT0$']\")); for (const a of anchors) { const t=(a.textContent||'').trim(); if (t && t.startsWith(val)) return true; } return false; }",
                {"val": search, "no": NO_RECORDS_TEXT},
                timeout=2000,
            )
            ok = True
        except Exception:
            ok = False
        if not ok:
            try:
                if field_loc:
                    field_loc.focus()
                    field_loc.press("Enter")
                else:
                    target.locator("#EXT_ORG_TBL_EXT_ORG_TYPE").first.press("Enter")
                target.wait_for_function(
                    "(arg) => { const val=arg.val; const none=arg.no; if (document.body && document.body.innerText.includes(none)) return true; const anchors = Array.from(document.querySelectorAll(\"a[name^='RESULT0$'], a[id^='RESULT0$']\")); for (const a of anchors) { const t=(a.textContent||'').trim(); if (t && t.startsWith(val)) return true; } return false; }",
                    {"val": search, "no": NO_RECORDS_TEXT},
                    timeout=2000,
                )
            except Exception:
                pass
    except Exception:
        pass


# Scrape function that returns an array of row objects with key->value pairs
def scrape_rows(on):
    try:
        on.wait_for_selector(f"{TABLE_ID}, table.PSSRCHRESULTSWBO", timeout=10000)
    except Exception:
        pass
    rows = []
    try:
        if on.locator(TABLE_ID).count() > 0:
            data = on.eval_on_selector(
                TABLE_ID,
                (
                    "el => {"+
                    " const out = [];"+
                    " const trs = Array.from(el.querySelectorAll('tr'));"+
                    " trs.forEach(r => {"+
                    "   const idEl = r.querySelector(\"a[name^='RESULT0$'], a[id^='RESULT0$'], a[id^='SEARCH_RESULTLAST'], a[name^='SEARCH_RESULTLAST']\");"+
                    "   const searchEl = r.querySelector(\"span[name^='RESULT3$'], span[id^='RESULT3$']\");"+
                    "   const orgEl = r.querySelector(\"span[name^='RESULT4$'], span[id^='RESULT4$']\");"+
                    "   const nameEl = r.querySelector(\"span[id^='RESULT5$'], span[name^='RESULT5$']\");"+
                    "   const idText = idEl ? (idEl.textContent || '').trim() : '';"+
                    "   const orgText = orgEl ? (orgEl.textContent || '').trim() : '';"+
                    "   const searchText = searchEl ? (searchEl.textContent || '').trim() : '';"+
                    "   const nameText = nameEl ? (nameEl.textContent || '').trim() : '';"+
                    "   if (idText || nameText || orgText || searchText) out.push({ id: idText, search: searchText, org: orgText, name: nameText });"+
                    " });"+
                    " return out;"+
                    "}"
                )
            )
            if data:
                rows.extend(data)
        else:
            # Fallback: document-level scan
            data2 = on.evaluate(
                (
                    "() => {"+
                    " const anchors = Array.from(document.querySelectorAll(\"a[name^='RESULT0$'], a[id^='RESULT0$'], a[id^='SEARCH_RESULTLAST'], a[name^='SEARCH_RESULTLAST']\"));"+
                    " const rows = anchors.map(a => {"+
                    "   const tr = a.closest('tr');"+
                    "   const searchEl = tr ? tr.querySelector(\"span[id^='RESULT3$'], span[name^='RESULT3$']\") : null;"+
                    "   const orgEl = tr ? tr.querySelector(\"span[id^='RESULT4$'], span[name^='RESULT4$']\") : null;"+
                    "   const nameEl = tr ? tr.querySelector(\"span[id^='RESULT5$'], span[name^='RESULT5$']\") : null;"+
                    "   const idText = (a.textContent || '').trim();"+
                    "   const searchText = searchEl ? (searchEl.textContent || '').trim() : '';"+
                    "   const orgText = orgEl ? (orgEl.textContent || '').trim() : '';"+
                    "   const nameText = nameEl ? (nameEl.textContent || '').trim() : '';"+
                    "   return (idText || nameText || searchText || orgText) ? { id: idText, search: searchText, org: orgText, name: nameText } : null;"+
                    " }).filter(Boolean);"+
                    " return rows;"+
                    "}"
                )
            )
            if data2:
                rows.extend(data2)
    except Exception:
        pass
    return rows


def get_all_rows(target):
    print_new_stage("Starting Scrape of Current Rows")
    all_rows = []
    pages = 0
    while True:
        pages += 1
        if pages > 100:
            break
        batch = scrape_rows(target)
        if batch:
            all_rows.extend(batch)
        # Next page
        advanced = False
        for sel in [
            "span:has-text('Next')",
            "a:has-text('Next')",
            "span.PSSRCHRESULTSHYPERLINKD.PTNEXTROW_D1",
            "#NextPageimg",
            "a[id*='NEXT']",
            "a[name*='NEXT']",
        ]:
            try:
                loc = target.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click()
                target.wait_for_timeout(600)
                advanced = True
                break
            except Exception:
                continue
        if not advanced:
            break
    return all_rows

def bump_prefix(s: str) -> str | None:
    """Advance a numeric suffix to sweep decades the way you described.

    Behavior:
    - If the last digit isn't 9, jump to the end of the current decade (set last digit to 9).
    - If the last digit is 9, move to the start of the next decade (add 1).
    - Preserves any non-digit head (e.g. leading 'E') and the width of the digit run.
    - Returns None if the digit run is all 9s (overflow for that width).

    Examples:
    - E00000001 -> E00000009
    - E00000010 -> E00000019 -> E00000020 -> E00000029 -> ... -> E00000090 -> E00000099
    - E00000100 -> E00000109 -> E00000110 -> ... -> E00000199 -> E00000200
    """
    if not s:
        return None
    # Split into head (non-digits ending position) and trailing digit run
    i = len(s) - 1
    while i >= 0 and s[i].isdigit():
        i -= 1
    start = i + 1
    digits = s[start:]
    if not digits:
        return None
    head = s[:start]

    # Work within the width of the trailing digit run
    width = len(digits)
    try:
        n = int(digits)
    except Exception:
        return None
    max_n = 10 ** width - 1
    if n >= max_n:
        return None

    if n % 10 != 9:
        # Jump to end of current decade: ...x -> ...9
        n = n - (n % 10) + 9
    else:
        # At ...9 -> move to start of next decade/higher boundary as carries apply
        n = n + 1

    return head + str(n).zfill(width)

def bump_prefix_fixed(s: str) -> str | None:
    """Increment the trailing digit run and PRESERVE its length (no dropping).
    Returns next token or None if overflow (all 9s).
    Example: E0000002300 -> E0000002301 -> ... -> E0000002999 -> E0000003000.
    """
    if not s:
        return None
    i = len(s) - 1
    while i >= 0 and s[i].isdigit():
        i -= 1
    start = i + 1
    digits = s[start:]
    if not digits:
        return None
    n = int(digits)
    width = len(digits)
    if n == 10**width - 1:
        return None  # overflow
    n += 1
    return s[:start] + str(n).zfill(width)

def bump_prefix_chunk(prefix: str, *, width: int = NUM_WIDTH, chunk_digits: int = CHUNK_DIGITS) -> str | None:
    """Bump by chunk (e.g., 2 digits) preserving chunk size. Prefix is the string used for 'begins with',
    typically 'E' + first width - chunk_digits digits. Returns next prefix or None when overflow.
    Example: 'E00000023' -> 'E00000024'.
    """
    if not prefix:
        return None
    if prefix[0].upper() == 'E':
        d = prefix[1:]
    else:
        d = prefix
    base_val = int((d + '0' * chunk_digits).zfill(width))
    step = 10 ** chunk_digits
    nxt = base_val + step
    if nxt >= 10 ** width:
        return None
    z = str(nxt).zfill(width)
    return 'E' + z[: width - chunk_digits]

def ceil_to_tens_prefix(prefix: str, *, width: int = NUM_WIDTH) -> str:
    """Round up numeric part to next multiple of 10, preserving width. 'E' is preserved."""
    d = prefix[1:] if prefix and prefix[0].upper() == 'E' else prefix
    n = int(str(d).zfill(width))
    tens = ((n + 9) // 10) * 10
    return ('E' if prefix and prefix[0].upper() == 'E' else '') + str(tens).zfill(width)

def bump_tens_base(prefix: str, *, width: int = NUM_WIDTH) -> str | None:
    """Advance numeric part by +10, preserving width. Returns None on overflow."""
    d = prefix[1:] if prefix and prefix[0].upper() == 'E' else prefix
    n = int(str(d).zfill(width))
    n += 10
    if n >= 10 ** width:
        return None
    return ('E' if prefix and prefix[0].upper() == 'E' else '') + str(n).zfill(width)

def ceil_to_base_prefix(prefix: str, *, width: int = NUM_WIDTH, base_digits: int = BASE_DIGITS) -> str:
    """Round up numeric part to the next base boundary (10^base_digits), preserving width. 'E' is preserved.
    Returns an 'E' + digits string truncated to width - base_digits (the base prefix).
    """
    d = prefix[1:] if prefix and prefix[0].upper() == 'E' else prefix
    n = int(str(d).zfill(width))
    step = 10 ** base_digits
    base_val = ((n + (step - 1)) // step) * step
    z = str(base_val).zfill(width)
    return ('E' if prefix and prefix[0].upper() == 'E' else '') + z[: width - base_digits]

def bump_base_prefix(prefix: str, *, width: int = NUM_WIDTH, base_digits: int = BASE_DIGITS) -> str | None:
    """Advance the base prefix by one base step (10^base_digits). Returns next base prefix or None on overflow.
    Base prefix is 'E' + digits truncated to width - base_digits.
    """
    if not prefix:
        return None
    d = prefix[1:] if prefix and prefix[0].upper() == 'E' else prefix
    # Reconstruct full-width numeric by appending base_digits zeros
    full = (d + ('0' * base_digits)).zfill(width)
    n = int(full)
    step = 10 ** base_digits
    n += step
    if n >= 10 ** width:
        return None
    z = str(n).zfill(width)
    return ('E' if prefix and prefix[0].upper() == 'E' else '') + z[: width - base_digits]

def normalize_base_input(value: str, *, width: int = NUM_WIDTH, base_digits: int = BASE_DIGITS) -> str:
    """Normalize a user-provided resume base to canonical 'E' + digits form (length width-base_digits).
    Accepts inputs like 'E00000023', '00000023', 'E23', '23'.
    """
    if not value:
        return ''
    # Keep only digits from the input
    digits = ''.join(ch for ch in value if ch.isdigit())
    if not digits:
        return ''
    z = digits.zfill(width)
    return 'E' + z[: width - base_digits]

def get_resume_base_arg() -> str | None:
    """Parse --resume-base from CLI args. Supports '--resume-base=VAL' or '--resume-base VAL'."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg.startswith('--resume-base='):
            return arg.split('=', 1)[1].strip()
        if arg == '--resume-base' and i + 1 < len(argv):
            return argv[i + 1].strip()
    return None

def load_checkpoint(path: str = CHECKPOINT_PATH) -> dict | None:
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # minimal validation
        if isinstance(data, dict) and data.get('next_base'):
            return data
    except Exception:
        return None
    return None

def save_checkpoint(next_base: str | None, *, path: str = CHECKPOINT_PATH) -> None:
    try:
        if not next_base:
            return
        payload = {
            'next_base': next_base,
            'base_digits': BASE_DIGITS,
            'num_width': NUM_WIDTH,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def clear_checkpoint(path: str = CHECKPOINT_PATH) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def count_rows(target) -> int:
    """Count current visible result rows in the prompt table."""
    try:
        if target.locator(TABLE_ID).count() > 0:
            return target.eval_on_selector(TABLE_ID, "el => el.querySelectorAll('tr').length") or 0
        if target.locator("table.PSSRCHRESULTSWBO").count() > 0:
            return target.eval_on_selector("table.PSSRCHRESULTSWBO", "el => el.querySelectorAll('tr').length") or 0
    except Exception:
        pass
    return 0

def has_no_records(target) -> bool:
    try:
        return target.locator(f"text={NO_RECORDS_TEXT}").first.count() > 0
    except Exception:
        return False

def click_view_all(target):
    try:
        target.locator("span:has-text('View All')").first.click()
        target.wait_for_timeout(300)
    except Exception:
        pass

def scrape_prefix(target, prefix: str, seen: set[str]) -> list[dict]:
    """Apply External Org ID prefix and scrape results. If the batch looks large (>=300),
    refine by exploring sub-prefixes prefix+digit for 0-9 recursively. Returns row dicts."""
    # Avoid loops
    if prefix in seen:
        return []
    seen.add(prefix)
    # Apply filter
    apply_filter(target, {"number": "0", "label": "External Org ID"}, prefix)
    # If no records, stop
    if has_no_records(target):
        return []
    # Prefer View All if present (caps at 300)
    click_view_all(target)
    rows_count = count_rows(target)
    # If result set is big, refine by appending 0..9 and union results
    if rows_count >= 300 and len(prefix) < 11:
        out = []
        for d in "0123456789":
            out.extend(scrape_prefix(target, prefix + d, seen))
        return out
    # Otherwise, scrape current rows directly
    return get_all_rows(target)

def export_universities_table(page, root, resume_base: str | None = None):
    """Open a prompt and export all table columns for all pages to a CSV.
    Columns are normalized to RESULT{index} when available, otherwise COL{pos}.
    """
    root.wait_for_selector(UNIV_FIELD, timeout=60000)
    root.wait_for_selector(UNIV_PROMPT, timeout=60000)
    prompt = root.locator(UNIV_PROMPT).first
    prompt.scroll_into_view_if_needed()

    target = None
    try:
        prompt.click(force=True)
        page.wait_for_selector("#pt_modalMask", timeout=3000)
        page.wait_for_selector("iframe[name^='ptModFrame_']", timeout=3000)
        # Choose frame with results
        chosen = None
        for f in page.frames:
            try:
                if f.name and f.name.startswith("ptModFrame_"):
                    if f.locator(f"{TABLE_ID}, table.PSSRCHRESULTSWBO").count() > 0:
                        chosen = f
                        break
                    if f.locator("[id^='RESULT']").count() > 0:
                        chosen = f
            except Exception:
                continue
        target = chosen or next((f for f in page.frames if f.name and f.name.startswith("ptModFrame_")), None)
    except Exception:
        target = None

    if target is None:
        try:
            with page.expect_popup(timeout=5000) as pi:
                prompt.click()
            target = pi.value
        except PlaywrightTimeoutError:
            try:
                with page.context.expect_page(timeout=5000) as pi2:
                    prompt.click()
                target = pi2.value
            except PlaywrightTimeoutError:
                target = page

    try:
        target.wait_for_load_state("domcontentloaded")
    except Exception:
        pass

    # Try to show all rows
    try:
        target.locator("span:has-text('View All')").first.click()
        target.wait_for_timeout(800)
    except Exception:
        pass

    # Attempt to load all rows by scrolling container until row count stabilizes
    try:
        for container_sel in [TABLE_ID, "table.PSSRCHRESULTSWBO"]:
            if target.locator(container_sel).count() == 0:
                continue
            prev = -1
            stable_iters = 0
            for _ in range(30):
                try:
                    count = target.eval_on_selector(container_sel, "el => el.querySelectorAll('tr').length")
                except Exception:
                    count = prev
                # Scroll container and page
                try:
                    target.eval_on_selector(container_sel, "el => { el.scrollTop = el.scrollHeight; }")
                except Exception:
                    pass
                try:
                    target.evaluate("() => { const s = document.scrollingElement || document.body; s.scrollTop = s.scrollHeight; }")
                except Exception:
                    pass
                target.wait_for_timeout(300)
                if count == prev:
                    stable_iters += 1
                    if stable_iters >= 3:
                        break
                else:
                    stable_iters = 0
                    prev = count
    except Exception:
        pass

    all_rows = []

    # Enumerate by base (10^BASE_DIGITS) and iterate suffix 0..(10^BASE_DIGITS - 1)
    # Determine starting base: CLI/env overrides checkpoint; else use start of space
    start_base = None
    if resume_base:
        start_base = normalize_base_input(resume_base)
    else:
        cp = load_checkpoint()
        if cp and cp.get('next_base'):
            start_base = normalize_base_input(str(cp.get('next_base')))
    if not start_base:
        start_base = ceil_to_base_prefix('E' + '0'.zfill(NUM_WIDTH))
        print_new_stage(f"Enumerating universities by base 10^{BASE_DIGITS}, iterating suffixes")
    else:
        print_new_stage(f"Resuming from base {start_base} with 10^{BASE_DIGITS} suffix sweep")
    base = start_base
    seen_bases = set()
    while True:
        if not base or base in seen_bases:
            break
        seen_bases.add(base)
        any_rows_in_base = False
        # Apply the base prefix directly (e.g., 'E00000000') and refine only if large
        apply_filter(target, {"number": "0", "label": "External Org ID"}, base)
        click_view_all(target)
        rc = count_rows(target)
        if not has_no_records(target) and rc > 0:
            any_rows_in_base = True
            if rc >= 300:
                sub = scrape_prefix(target, base, set())
                if sub:
                    all_rows.extend(sub)
            else:
                batch = get_all_rows(target)
                if batch:
                    all_rows.extend(batch)
        # Compute and persist checkpoint for next base
        next_base = bump_base_prefix(base)
        if not any_rows_in_base:
            # Early stop after first empty base; save checkpoint to resume from next base if any
            if next_base:
                save_checkpoint(next_base)
            else:
                clear_checkpoint()
            break
        # Advance to next base and save checkpoint
        save_checkpoint(next_base)
        base = next_base

    # If we've completed iteration (no further base), clear checkpoint
    if not base:
        clear_checkpoint()
    
    # Deduplicate
    dedup = []
    seen = set()
    for r in all_rows:
        key = f"{r.get('id') or ''}|{r.get('name') or ''}"
        if key not in seen:
            seen.add(key)
            dedup.append(r)

    if dedup:
        df = pd.DataFrame(dedup)
        df = df.reindex(columns=["id", "search", "org", "name"])  # consistent column order
        df.to_csv(ORG_CSV, index=False)
        print(f"✅ Exported universities: {ORG_CSV} ({len(df)} rows)")
    else:
        print("⚠️ No university rows found to export")

    close_modal(target, page)

# ---------------- Main script ----------------

all_csvs = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.goto(EDUREC_HOME)
    input("➡️ Log in to EduRec manually and complete 2FA, then press ENTER...")

    # Navigate: Academics → Global Education → Search Course Mappings
    target_page = navigate_to_search_course_mappings(page)
    

    # Scrape all faculties and universities from direct component page using the same page/frame
    target_page.wait_for_load_state("domcontentloaded")
    root = resolve_component_root(target_page, UNIV_FIELD)
    try:
        root.wait_for_selector(UNIV_FIELD, timeout=120000)
    except Exception:
        root.wait_for_selector(UNIV_FIELD, state="attached", timeout=120000)
    # Record the component URL for subsequent combos
    SEARCH_PAGE = target_page.url
    # Export full prompt tables (support resume from base via CLI or ENV)
    resume_env = os.getenv('EDUREC_RESUME_BASE')
    resume_arg = get_resume_base_arg()
    start_base = resume_arg or resume_env
    export_universities_table(target_page, root, start_base)

    # Derive universities: ID from RESULT0, name prefers RESULT5 then RESULT3
    partner_unis = derive_items_from_csv(
        ORG_CSV,
    )
    print("➡️ Obtained list of universities: " + ", ".join([((u.get('name') or '') + (f" ({u.get('id')})" if u.get('id') else '')) for u in partner_unis]))

    context.close()
    browser.close()

