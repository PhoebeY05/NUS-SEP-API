# Program to retrieve all past course mappings => ~2 hours

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from utilities import (close_modal, derive_items_from_csv,
                       get_partner_universities,
                       navigate_to_search_course_mappings)

EDUREC_HOME = "https://myedurec.nus.edu.sg"
DOWNLOAD_DIR = "data/"
MAPPING_DOWNLOAD_DIR = DOWNLOAD_DIR + "mappings/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(MAPPING_DOWNLOAD_DIR, exist_ok=True)
MASTER_CSV_PATH = os.path.join(MAPPING_DOWNLOAD_DIR, "master_partner_mappings.csv")
SEARCH_PAGE = None
FACULTIES_CSV = os.path.join(DOWNLOAD_DIR, "faculties.csv")

FACULTY_FIELD = "input[id^='N_EXSP_DRVD_ACAD_GROUP']"
FACULTY_PROMPT = "a[id^='N_EXSP_DRVD_ACAD_GROUP$prompt']"
UNIV_FIELD = "input[id^='N_EXSP_DRVD_EXT_ORG_ID']"
UNIV_PROMPT = "a[id^='N_EXSP_DRVD_EXT_ORG_ID$prompt']"
DOWNLOAD_ICON = "[id^='N_EXSP_DRVD$hexcel$img']"
NO_RECORDS_TEXT = "No matching records found"

MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds between retries

# ---------------- Component resolution ----------------

def resolve_component_root(page, field_selector=FACULTY_FIELD, frame_candidates=("#main_target_win7", "iframe[name='ptifrmtgtframe']")):
    """Return the root (Page or Frame) containing the target field.
    Tries the current page first, then looks into common PeopleSoft frames.
    """
    try:
        if page.locator(field_selector).count() > 0:
            return page
    except Exception:
        pass

    # Wait briefly for known frames and search within all frames
    for sel in frame_candidates:
        try:
            page.wait_for_selector(sel, timeout=3000)
        except Exception:
            pass

    for f in page.frames:
        try:
            if f.locator(field_selector).count() > 0:
                return f
        except Exception:
            continue

    # Fallback to page if nothing found; callers will handle waits
    return page

# ---------------- Helper functions ----------------

def fill_search_field(root, field_selector, value):
    root.fill(field_selector, value)
    # Avoid pressing Enter as PeopleSoft may treat it as a default action (e.g., Back)
    try:
        root.press(field_selector, "Tab")
    except Exception:
        # Fallback: blur the input
        root.dispatch_event(field_selector, "blur")
    root.wait_for_timeout(300)


def has_results(root):
    # Consider multiple table variants used by PeopleSoft
    table_variants = [
        "#PTSRCHRESULTS",
        "table.PSSRCHRESULTSWBO",
        "table#N_EXSP_DRVD$scroll$0",
        "table.PSLEVEL1GRIDWBO",
        "table.PSLEVEL1GRID",
    ]
    exists = False
    for sel in table_variants:
        try:
            if root.locator(sel).count() > 0:
                exists = True
                break
        except Exception:
            continue
    no_records = False
    try:
        no_records = root.locator(f"text={NO_RECORDS_TEXT}").count() > 0
    except Exception:
        no_records = False
    return exists and not no_records


def wait_for_results_or_empty(root, timeout_ms: int = 20000):
    """Wait until any known results table appears or the 'No matching records' text shows."""
    table_variants = [
        "#PTSRCHRESULTS",
        "table.PSSRCHRESULTSWBO",
        "table#N_EXSP_DRVD$scroll$0",
        "table.PSLEVEL1GRIDWBO",
        "table.PSLEVEL1GRID",
    ]
    deadline = timeout_ms
    step = 500
    waited = 0
    while waited < deadline:
        if has_results(root) or root.locator(f"text={NO_RECORDS_TEXT}").count() > 0:
            return True
        try:
            # Try to nudge layout to render
            for sel in table_variants:
                try:
                    root.eval_on_selector(sel, "el => { el.scrollTop = el.scrollHeight; }")
                except Exception:
                    continue
            root.evaluate("() => { const s = document.scrollingElement || document.body; s.scrollTop = s.scrollHeight; }")
        except Exception:
            pass
        root.wait_for_timeout(step)
        waited += step
    return False


def read_download_to_df(path):
    """Read a downloaded export file into a DataFrame.
    Detects legacy XLS (OLE, xlrd), modern XLSX (ZIP, openpyxl), or HTML exports
    (PeopleSoft often sends HTML tables with an .xls filename).
    Returns a tuple of (df, fmt) where fmt is one of 'xls' | 'xlsx' | 'html'.
    """
    fmt = None
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except Exception as e:
        raise ValueError(f"Unable to read downloaded file: {e}")

    # OLE Compound File (legacy .xls)
    if head.startswith(b"\xD0\xCF\x11\xE0"):
        fmt = "xls"
        return pd.read_excel(path, engine="xlrd"), fmt

    # ZIP header (modern .xlsx)
    if head.startswith(b"PK"):
        fmt = "xlsx"
        # openpyxl handles .xlsx content; extension mismatch is fine
        return pd.read_excel(path, engine="openpyxl"), fmt

    # HTML-based export (often served with .xls extension)
    if (b"<!DOCTYPE" in head) or (b"<html" in head.lower()):
        fmt = "html"
        tables = pd.read_html(path)
        if not tables:
            raise ValueError("HTML export contained no tables")
        # Choose the largest table heuristically
        df = max(tables, key=lambda d: (d.shape[0] * d.shape[1]))
        return df, fmt

    raise ValueError("Unknown or unsupported export format (not XLS/XLSX/HTML)")


def get_csv_path_for(faculty, uni) -> str:
    """Derive the output CSV path for a faculty/university combination using
    the same filename convention throughout the script. This enables resuming
    by checking for existing CSV files.
    """
    fname = faculty.get("name") or faculty.get("id") or "faculty"
    uname_id = uni.get("id") or uni.get("name") or "univ"
    # Normalize minimal characters to keep paths safe and consistent
    safe_uname = str(uname_id).replace(" ", "_")
    return os.path.join(MAPPING_DOWNLOAD_DIR, f"{fname}_{safe_uname}.csv")


def click_download_excel(root, page):
    """Robustly trigger the Excel download for the mappings grid.
    Tries multiple selectors and a direct JS submitAction fallback.
    Returns the Playwright Download object.
    """
    # Candidate selectors: prefer the anchor, then the image, then alt/title fallbacks
    candidates = [
        "a[id^='N_EXSP_DRVD$hexcel$']",
        "a[name^='N_EXSP_DRVD$hexcel$']",
        "img[id^='N_EXSP_DRVD$hexcel$img']",
        "img.PTDOWNLOAD",
        "[role='button'][title*='Download']",
        "img[alt*='Download']",
    ]
    # Ensure element is in view and clickable
    for sel in candidates:
        try:
            loc = root.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                loc.scroll_into_view_if_needed()
            except Exception:
                pass
            # Attempt anchor click with expect_download
            try:
                with page.expect_download(timeout=15000) as d:
                    loc.click(force=True)
                return d.value
            except Exception:
                # Try a dispatch event as fallback
                try:
                    with page.expect_download(timeout=15000) as d:
                        loc.dispatch_event("click")
                    return d.value
                except Exception:
                    pass
        except Exception:
            continue

    # Direct JS fallback: call PeopleSoft submitAction with the anchor id
    try:
        # Find anchor id if present
        a = root.locator("a[id^='N_EXSP_DRVD$hexcel$']").first
        if a.count() > 0:
            aid = a.get_attribute("id")
            if aid:
                # Ensure function exists on window; invoke with expect_download
                try:
                    with page.expect_download(timeout=15000) as d:
                        root.evaluate("(id) => { if (window.submitAction_win2) submitAction_win2(document.win2, id); }", aid)
                    return d.value
                except Exception:
                    pass
        # If only the image is found, derive the anchor id by stripping '$img'
        img = root.locator("img[id^='N_EXSP_DRVD$hexcel$img']").first
        if img.count() > 0:
            iid = img.get_attribute("id") or ""
            if iid and iid.endswith("img$0"):
                aid = iid.replace("img$0", "$0")
            else:
                aid = iid.replace("img", "")
            try:
                with page.expect_download(timeout=15000) as d:
                    root.evaluate("(id) => { if (window.submitAction_win2) submitAction_win2(document.win2, id); }", aid)
                return d.value
            except Exception:
                pass
    except Exception:
        pass

    # Last resort: use the global DOWNLOAD_ICON constant directly
    try:
        with page.expect_download(timeout=15000) as d:
            root.click(DOWNLOAD_ICON)
        return d.value
    except Exception:
        pass

    raise PlaywrightTimeoutError("Failed to trigger Excel download via all methods")

def process_combination(faculty, uni):
    """Download CSV for one faculty/university combo with retries.
    faculty: {id, name}
    uni: {id, name}
    """
    fname = faculty.get("name") or faculty.get("id") or "faculty"
    uname_id = uni.get("id") or uni.get("name") or "univ"
    csv_path = get_csv_path_for(faculty, uni)
    if os.path.exists(csv_path):
        print(f"⏩ Already downloaded: {fname} | {uni.get('name') or uni.get('id')}")
        return pd.read_csv(csv_path)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            root = resolve_component_root(page)
            # Wait until field is present (attached) in the resolved root
            try:
                root.wait_for_selector(FACULTY_FIELD, timeout=120000)
            except Exception:
                # As a fallback, try attached state
                root.wait_for_selector(FACULTY_FIELD, state="attached", timeout=120000)

            # Use faculty ID when available (fallback to name), university by ID
            print("➡️ Filling faculty field...")
            fill_search_field(root, FACULTY_FIELD, faculty.get("id") or faculty.get("name") or "")
            print("➡️ Filling university field...")
            fill_search_field(root, UNIV_FIELD, uni.get("id") or "")

            # Click the correct action button to fetch mappings
            clicked = False
            for sel in [
                "#N_EXSP_DRVD_SEARCH",
                "input[name='N_EXSP_DRVD_SEARCH']",
                "text=Fetch Mappings",
                "text=Search",
            ]:
                try:
                    if root.locator(sel).count() > 0:
                        print(f"➡️ Clicking: {sel}")
                        root.click(sel)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # Fallback to a generic click if nothing matched
                print("⚠️ Could not find explicit fetch button; attempting generic 'Search' click")
                root.click("text=Search")

            # Wait for either results or an empty-state message
            wait_for_results_or_empty(root, timeout_ms=20000)

            if not has_results(root):
                print(f"⚠️ No results: {fname} | {uni.get('name') or uni.get('id')}")
                return None

            # Robustly trigger the Excel download
            download = click_download_excel(root, page)
            xls_path = os.path.join(MAPPING_DOWNLOAD_DIR, f"{fname}_{str(uname_id).replace(' ','_')}.xls")
            download.save_as(xls_path)

            df, fmt = read_download_to_df(xls_path)
            print(f"📄 Parsed export format: {fmt}")
            df["Faculty"] = faculty.get("name")
            df["Faculty ID"] = faculty.get("id")
            df["University"] = uni.get("name")
            df["University ID"] = uni.get("id")
            df.to_csv(csv_path, index=False)
            print(f"✅ Saved: {csv_path}")
            return df

        except (PlaywrightTimeoutError, Exception) as e:
            print(f"⚠️ Attempt {attempt} failed for {fname} | {uni.get('name') or uni.get('id')}: {e}")
            if attempt < MAX_RETRIES:
                print(f"⏳ Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"❌ Skipped: {fname} | {uni.get('name') or uni.get('id')} after {MAX_RETRIES} attempts")
                return None

# ---------------- Main script ----------------

all_csvs = []

# Derive faculties
faculties = derive_items_from_csv(
    FACULTIES_CSV,
)
print("➡️ Obtained list of faculties: " + ", ".join([(f.get('name') or f.get('id') or '') for f in faculties]))
# Derive universities
partner_unis = get_partner_universities()
print("➡️ Obtained list of universities: " + ", ".join([((u.get('name') or '') + (f" ({u.get('id')})" if u.get('id') else '')) for u in partner_unis]))
print(f"Discovered {len(faculties)} faculties × {len(partner_unis)} universities")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.goto(EDUREC_HOME)
    input("➡️ Log in to EduRec manually and complete 2FA, then press ENTER...")

    # Navigate: Academics → Global Education → Search Course Mappings
    target_page = navigate_to_search_course_mappings(page)

    target_page.wait_for_load_state("domcontentloaded")
    root = resolve_component_root(target_page)
    try:
        root.wait_for_selector(FACULTY_FIELD, timeout=120000)
    except Exception:
        root.wait_for_selector(FACULTY_FIELD, state="attached", timeout=120000)
    # Record the component URL for subsequent combos
    SEARCH_PAGE = target_page.url

    # Process all combinations sequentially to avoid cross-thread page/context issues
    for f in faculties:
        for u in partner_unis:
            disp_f = f.get('name')
            disp_u = u.get('name')
            expected_csv = get_csv_path_for(f, u)
            if os.path.exists(expected_csv):
                print(f"⏩ Resume skip (exists): {os.path.basename(expected_csv)}")
                continue
            print(f"➡️ Processing: {disp_f} | {disp_u}")
            df = process_combination(f, u)
            if df is not None:
                all_csvs.append(df)


    context.close()
    browser.close()

# Merge into master CSV
if all_csvs:
    pd.concat(all_csvs, ignore_index=True).to_csv(MASTER_CSV_PATH, index=False)
    print(f"✅ Master CSV created: {MASTER_CSV_PATH}")
    # Cleanup: remove per-combination XLS/CSV files, keep only the master
    try:
        master_abs = os.path.abspath(MASTER_CSV_PATH)
        for fname in os.listdir(MAPPING_DOWNLOAD_DIR):
            fp = os.path.join(MAPPING_DOWNLOAD_DIR, fname)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".xls", ".csv") and os.path.abspath(fp) != master_abs:
                try:
                    os.remove(fp)
                except Exception:
                    pass
        print("🧹 Cleaned up per-combination XLS/CSV files")
    except Exception as e:
        print(f"⚠️ Cleanup failed: {e}")
else:
    print("⚠️ No CSVs to merge")
