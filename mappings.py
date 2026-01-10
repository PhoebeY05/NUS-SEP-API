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
    # Fluid results table
    fluid_table = root.query_selector("#PTSRCHRESULTS") or root.query_selector("table.PSSRCHRESULTSWBO")
    # Legacy grid
    legacy_table = root.query_selector("table.PSLEVEL1GRID")
    no_records = root.locator(f"text={NO_RECORDS_TEXT}").count() > 0
    return (fluid_table is not None or legacy_table is not None) and not no_records

def process_combination(faculty, uni):
    """Download CSV for one faculty/university combo with retries.
    faculty: {id, name}
    uni: {id, name}
    """
    fname = faculty.get("name") or faculty.get("id") or "faculty"
    uname_id = uni.get("id") or uni.get("name") or "univ"
    csv_path = os.path.join(MAPPING_DOWNLOAD_DIR, f"{fname}_{uname_id.replace(' ','_')}.csv")
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
            for _ in range(40):  # ~20s max
                if has_results(root) or root.locator(f"text={NO_RECORDS_TEXT}").count() > 0:
                    break
                root.wait_for_timeout(500)

            if not has_results(root):
                print(f"⚠️ No results: {fname} | {uni.get('name') or uni.get('id')}")
                return None

            with page.expect_download(timeout=15000) as d:
                root.click(DOWNLOAD_ICON)
            download = d.value
            xls_path = os.path.join(MAPPING_DOWNLOAD_DIR, f"{fname}_{uname_id.replace(' ','_')}.xls")
            download.save_as(xls_path)

            df = pd.read_excel(xls_path, engine="xlrd")
            df["Faculty"] = faculty.get("name")
            df["Faculty ID"] = faculty.get("id")
            df["University"] = uni.get("name")
            df["University ID"] = uni.get("id")
            df.to_csv(csv_path, index=False)
            print(f"✅ Saved: {csv_path}")
            page.close()
            return df

        except (PlaywrightTimeoutError, Exception) as e:
            print(f"⚠️ Attempt {attempt} failed for {fname} | {uni.get('name') or uni.get('id')}: {e}")
            page.close()
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
    # for f in faculties:
    #     for u in partner_unis:
    #         disp_f = f.get('id')
    #         disp_u = u.get('id')
    #         print(f"➡️ Processing: {disp_f} | {disp_u}")
    #         df = process_combination(f, u)
    #         if df is not None:
    #             all_csvs.append(df)

    df = process_combination(faculties[2], partner_unis[1172])
    if df is not None:
        all_csvs.append(df)

    context.close()
    browser.close()

# Merge into master CSV
if all_csvs:
    pd.concat(all_csvs, ignore_index=True).to_csv(MASTER_CSV_PATH, index=False)
    print(f"✅ Master CSV created: {MASTER_CSV_PATH}")
else:
    print("⚠️ No CSVs to merge")
