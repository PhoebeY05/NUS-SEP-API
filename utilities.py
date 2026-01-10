from datetime import datetime
import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def resolve_component_root(page, field_selector, frame_candidates=("#main_target_win7", "iframe[name='ptifrmtgtframe']")):
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


def navigate_to_search_course_mappings(page):
    print_new_stage("Starting Navigation")
    # Click Academics (Fluid often updates DOM without full navigation)
    page.wait_for_selector("a#N_STDACAD_SHORTCUT", timeout=30000)
    page.click("a#N_STDACAD_SHORTCUT")
    page.wait_for_timeout(1000)
    print("➡️ Navigated to Academics")

    # Click Global Education tile using robust selector set; fallback to direct component URL
    clicked_global = False
    for sel in [
        "a:has-text('Global Education')",
        "div[role='link']:has-text('Global Education')",
        "div[role='button']:has-text('Global Education')",
        "span:has-text('Global Education')",
        "text=Global Education",
    ]:
        try:
            page.wait_for_selector(sel, state="visible", timeout=5000)
            page.click(sel)
            page.wait_for_timeout(1000)
            clicked_global = True
            break
        except PlaywrightTimeoutError:
            pass

    if not clicked_global:
        # Fallback straight to Search Course Mappings component (avoids tile navigation issues)
        page.goto("https://myedurec.nus.edu.sg/psc/cs90prd_newwin/EMPLOYEE/SA/c/SA_LEARNER_SERVICES.N_EXSP_MOD_SRCH.GBL?&pslnkid=N_MODULE_SRCH_LNK")
    # Try clicking side-nav item by ID (if present), else fall back to text link or direct page
    new_win = None
    target_page = page
    try:
        page.wait_for_selector("#win8divSCC_NAV_TAB_row\\$1", timeout=10000)
        with page.expect_popup() as popup_info:
            page.click("#win8divSCC_NAV_TAB_row\\$1")
        try:
            new_win = popup_info.value
            new_win.wait_for_load_state("load")
            target_page = new_win
        except Exception:
            new_win = None
            target_page = page
    except PlaywrightTimeoutError:
        # Fallback: look for the text link and navigate in-place
        try:
            page.wait_for_selector("span:has-text('Search Course Mappings')", timeout=10000)
            with page.expect_navigation(timeout=60000):
                page.click("span:has-text('Search Course Mappings')")
            target_page = page
        except PlaywrightTimeoutError:
            target_page = page
    print(f"➡️ Navigated to Search Course Mappings")
    return target_page

 # Helper to derive id/name pairs from CSV
def derive_items_from_csv(csv_path, value_cols = ["name"]):
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"⚠️ Could not read {csv_path}: {e}")
        return []
    items = []
    for _, row in df.iterrows():
        base = {
            "id": str(row["id"]).strip() if ("id" in df.columns and pd.notna(row["id"])) else None
        }
        dynamic = {
            col: (str(row[col]).strip() if pd.notna(row[col]) else None)
            for col in value_cols if col in df.columns
        }
        items.append({**base, **dynamic})
    return items

def close_modal(target, page):
    # Close modal/page
    if hasattr(target, "close") and target != page:
        try:
            target.close()
        except Exception:
            pass
    else:
        try:
            closeBtn = page.locator("a[id^='ptModCloseLnk_']").first
            if closeBtn.count() > 0:
                closeBtn.click()
            else:
                imgBtn = page.locator("img[id^='ptModCloseImg_']").first
                if imgBtn.count() > 0:
                    imgBtn.click()
            try:
                page.wait_for_selector("#pt_modalMask", state="hidden", timeout=2000)
            except Exception:
                pass
        except Exception:
            pass

def print_new_stage(msg):
    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")
    print(f"⭐ [{current_time}] {msg}")


def get_partner_universities():
    organisations = derive_items_from_csv(
        "downloads/organisations.csv",
        value_cols=["org", "search", "name"],
    )
    partner_universities = list(filter(lambda u: u.get('org') == "SCHL", organisations))
    return partner_universities

