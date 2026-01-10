# Program to retrieve all faculties => ~5 mins
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utilities import close_modal, derive_items_from_csv, navigate_to_search_course_mappings, resolve_component_root

EDUREC_HOME = "https://myedurec.nus.edu.sg"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
SEARCH_PAGE = None
FACULTIES_CSV = os.path.join(DOWNLOAD_DIR, "faculties.csv")

FACULTY_FIELD = "input[id^='N_EXSP_DRVD_ACAD_GROUP']"
FACULTY_PROMPT = "a[id^='N_EXSP_DRVD_ACAD_GROUP$prompt']"
DOWNLOAD_ICON = "[id^='N_EXSP_DRVD$hexcel$img']"
NO_RECORDS_TEXT = "No matching records found"

# ---------------- Helper functions ----------------

def export_faculties_table(page, root):
    """Open a prompt and export all table columns for all pages to a CSV.
    Columns are normalized to RESULT{index} when available, otherwise COL{pos}.
    """
    # Ensure controls are ready
    root.wait_for_selector(FACULTY_FIELD, timeout=60000)
    root.wait_for_selector(FACULTY_PROMPT, timeout=60000)
    prompt = root.locator(FACULTY_PROMPT).first
    prompt.scroll_into_view_if_needed()

    # Open modal/popup
    target = None
    try:
        prompt.click(force=True)
        page.wait_for_selector("#pt_modalMask", timeout=3000)
        page.wait_for_selector("iframe[name^='ptModFrame_']", timeout=3000)
        chosen = None
        for f in page.frames:
            try:
                if f.name and f.name.startswith("ptModFrame_"):
                    if f.locator("#PTSRCHRESULTS, table.PSSRCHRESULTSWBO").count() > 0:
                        chosen = f
                        break
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
        except Exception:
            try:
                with page.context.expect_page(timeout=5000) as pi2:
                    prompt.click()
                target = pi2.value
            except Exception:
                target = page

    try:
        target.wait_for_load_state("domcontentloaded")
    except Exception:
        pass

    # Show more rows if available
    for sel in ["span:has-text('View 100')", "span:has-text('View All')"]:
        try:
            target.locator(sel).first.click()
            target.wait_for_timeout(800)
            break
        except Exception:
            continue

    # Scrape ID (RESULT0/SEARCH_RESULTLAST*) and Name (RESULT4)
    rows = []
    try:
        if target.locator("#PTSRCHRESULTS").count() > 0:
            data = target.eval_on_selector(
                "#PTSRCHRESULTS",
                (
                    "el => {"+
                    " const out = [];"+
                    " const trs = Array.from(el.querySelectorAll('tr'));"+
                    " trs.forEach(r => {"+
                    "   const idEl = r.querySelector(\"a[name^='RESULT0$'], a[id^='RESULT0$'], a[id^='SEARCH_RESULTLAST'], a[name^='SEARCH_RESULTLAST']\");"+
                    "   const nameEl = r.querySelector(\"span[id^='RESULT4$'], span[name^='RESULT4$']\");"+
                    "   const idText = idEl ? (idEl.textContent || '').trim() : '';"+
                    "   const nameText = nameEl ? (nameEl.textContent || '').trim() : '';"+
                    "   if (idText || nameText) out.push({ id: idText, name: nameText });"+
                    " });"+
                    " return out;"+
                    "}"
                )
            )
            if data:
                rows.extend(data)
        else:
            # Fallback: document-level scan
            data2 = target.evaluate(
                (
                    "() => {"+
                    " const anchors = Array.from(document.querySelectorAll(\"a[name^='RESULT0$'], a[id^='RESULT0$'], a[id^='SEARCH_RESULTLAST'], a[name^='SEARCH_RESULTLAST']\"));"+
                    " const rows = anchors.map(a => {"+
                    "   const tr = a.closest('tr');"+
                    "   const nameEl = tr ? tr.querySelector(\"span[id^='RESULT4$'], span[name^='RESULT4$']\") : null;"+
                    "   const idText = (a.textContent || '').trim();"+
                    "   const nameText = nameEl ? (nameEl.textContent || '').trim() : '';"+
                    "   return (idText || nameText) ? { id: idText, name: nameText } : null;"+
                    " }).filter(Boolean);"+
                    " return rows;"+
                    "}"
                )
            )
            if data2:
                rows.extend(data2)
    except Exception:
        pass

    # Deduplicate
    rows = list(filter(lambda r: r.get('org') == 'SCHL', rows))
    dedup = []
    seen = set()
    for r in rows:
        key = f"{r.get('id') or ''}|{r.get('name') or ''}"
        if key not in seen:
            seen.add(key)
            dedup.append(r)

    if dedup:
        df = pd.DataFrame(dedup)
        df = df.reindex(columns=["id", "name"])  # consistent column order
        df.to_csv(FACULTIES_CSV, index=False)
        print(f"✅ Exported faculties: {FACULTIES_CSV} ({len(df)} rows)")
    else:
        print("⚠️ No faculty rows found to export")

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
    root = resolve_component_root(target_page, FACULTY_FIELD)
    try:
        root.wait_for_selector(FACULTY_FIELD, timeout=120000)
    except Exception:
        root.wait_for_selector(FACULTY_FIELD, state="attached", timeout=120000)
    # Record the component URL for subsequent combos
    SEARCH_PAGE = target_page.url
    # Export full prompt tables
    export_faculties_table(target_page, root)


    # Derive faculties: ID from RESULT0, name prefers RESULT3 then RESULT4 then RESULT5
    faculties = derive_items_from_csv(
        FACULTIES_CSV,
        value_cols = ["org", "search" "name"]
    )
    print("➡️ Obtained list of faculties: " + ", ".join([(f.get('name') or f.get('id') or '') for f in faculties]))    

    context.close()
    browser.close()

