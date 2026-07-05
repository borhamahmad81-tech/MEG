"""
browser_controller.py
----------------------
Attaches to an ALREADY OPEN Chrome or Edge window (launched with a remote
debugging port) rather than spawning a new controlled browser. This lets you
log in / navigate manually first, then let the assistant take over on the
same tab -- nothing about your session, cookies, or MFA is disturbed.

To make this work, the browser must be started with a debug port BEFORE you
log into the site. Easiest way: use one of the generated shortcuts:
    launch_chrome_debug.bat
    launch_edge_debug.bat

All CSS/XPath selectors used to find page elements live in
config/selectors.json so this file never needs to be edited when the
clinical system's page markup changes -- only the config does.
"""

import json
import os
import time

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DEFAULT_SELECTORS_PATH = os.path.join("config", "selectors.json")

# Fallback defaults -- these are PLACEHOLDERS. Update config/selectors.json
# with the real selectors from the clinical quality-management system
# (right click element -> Inspect -> Copy selector).
DEFAULT_SELECTORS = {
    "record_rows": {"type": "css", "value": "table#records tbody tr"},
    "harm_level_cell": {"type": "css", "value": "td.harm-level"},
    "record_id_cell": {"type": "css", "value": "td.record-id"},
    "open_record_link": {"type": "css", "value": "a.open-record"},
    "safety_event_description_field": {"type": "css", "value": "#safety_event_description"},
    "free_text_description_field": {"type": "css", "value": "#free_text_description"},
    "define_problem_field": {"type": "css", "value": "#define_the_problem"},
    "why1_field": {"type": "css", "value": "#why_1"},
    "save_button": {"type": "css", "value": "button#save-record"},
}


class BrowserController:
    def __init__(self, browser="chrome", debug_port=9222,
                 selectors_path=DEFAULT_SELECTORS_PATH, logger=None,
                 driver_path=None):
        self.browser = browser.lower()
        self.debug_port = debug_port
        self.driver = None
        self.driver_path = (driver_path or "").strip() or None
        self.logger = logger or (lambda msg, level="info": None)
        self.selectors = self._load_selectors(selectors_path)

    # ------------------------------------------------------------------ #
    def _load_selectors(self, path):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_SELECTORS)
                merged.update(data)
                return merged
            except Exception as exc:
                self.logger(f"Failed to parse {path}: {exc}. Using defaults.", "warn")
        return dict(DEFAULT_SELECTORS)

    def save_selectors(self, path=DEFAULT_SELECTORS_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.selectors, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # Attach to an already-running Chrome/Edge instance
    # ------------------------------------------------------------------ #
    def attach(self):
        try:
            if self.browser == "edge":
                options = EdgeOptions()
                options.add_experimental_option(
                    "debuggerAddress", f"127.0.0.1:{self.debug_port}"
                )
                service = EdgeService(executable_path=self.driver_path) if self.driver_path else None
                self.driver = webdriver.Edge(options=options, service=service)
            else:
                options = ChromeOptions()
                options.add_experimental_option(
                    "debuggerAddress", f"127.0.0.1:{self.debug_port}"
                )
                service = ChromeService(executable_path=self.driver_path) if self.driver_path else None
                self.driver = webdriver.Chrome(options=options, service=service)

            self.logger(
                f"Attached to existing {self.browser.title()} session on port {self.debug_port}.",
                "success",
            )
            return self.driver
        except WebDriverException as exc:
            msg = str(exc)
            if "Unable to obtain driver" in msg or "unable to discover" in msg.lower():
                self.logger(
                    f"Could not auto-download the {self.browser.title()} driver "
                    f"(likely no internet access to Microsoft's/Google's driver "
                    f"server, or it's blocked by a firewall on this machine). "
                    f"Fix: download the matching driver manually and set its "
                    f"path in 'Driver Path (optional)' in the GUI. "
                    f"Chrome: https://googlechromelabs.github.io/chrome-for-testing/ "
                    f"| Edge: https://developer.microsoft.com/microsoft-edge/tools/webdriver/",
                    "error",
                )
            else:
                self.logger(
                    f"Could not attach to {self.browser.title()} on port {self.debug_port}: {exc}",
                    "error",
                )
            raise
        except Exception as exc:
            self.logger(
                f"Could not attach to {self.browser.title()} on port {self.debug_port}: {exc}",
                "error",
            )
            raise

    def is_attached(self) -> bool:
        if self.driver is None:
            return False
        try:
            _ = self.driver.title
            return True
        except Exception:
            return False

    def quit(self):
        # We only detach -- we never close the user's real browser window.
        self.driver = None

    # ------------------------------------------------------------------ #
    # Generic element helpers
    # ------------------------------------------------------------------ #
    def _by(self, sel):
        sel_type = sel.get("type")
        if sel_type == "xpath":
            return By.XPATH
        if sel_type == "id":
            return By.ID
        return By.CSS_SELECTOR

    def find(self, key, timeout=10, root=None):
        sel = self.selectors.get(key)
        if not sel:
            raise KeyError(f"Selector '{key}' is not configured in selectors.json")
        context = root or self.driver
        return WebDriverWait(context, timeout).until(
            EC.presence_of_element_located((self._by(sel), sel["value"]))
        )

    def find_all(self, key, timeout=10, root=None):
        sel = self.selectors.get(key)
        if not sel:
            raise KeyError(f"Selector '{key}' is not configured in selectors.json")
        context = root or self.driver
        WebDriverWait(context, timeout).until(
            EC.presence_of_element_located((self._by(sel), sel["value"]))
        )
        return context.find_elements(self._by(sel), sel["value"])

    def find_in(self, element, key, timeout=5):
        sel = self.selectors.get(key)
        if not sel:
            raise KeyError(f"Selector '{key}' is not configured in selectors.json")
        return WebDriverWait(element, timeout).until(
            lambda e: e.find_element(self._by(sel), sel["value"])
        )

    def safe_text(self, element) -> str:
        try:
            return (element.text or "").strip()
        except StaleElementReferenceException:
            return ""

    # ------------------------------------------------------------------ #
    # Direct URL navigation -- MEG (audits.megsupporttools.com) exposes a
    # predictable per-record edit URL, so we can jump straight to a record
    # instead of relying on clicking an icon in a table that might scroll,
    # paginate, or re-render.
    #   https://audits.megsupporttools.com/audit_builder/{country}/edit/{form_id}/observation/{record_id}/
    # ------------------------------------------------------------------ #
    def build_record_url(self, base_url, country, form_id, record_id):
        base_url = base_url.rstrip("/")
        return f"{base_url}/audit_builder/{country}/edit/{form_id}/observation/{record_id}/"

    def navigate_to_record(self, base_url, country, form_id, record_id, timeout=15):
        url = self.build_record_url(base_url, country, form_id, record_id)
        self.driver.get(url)
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.ID, "observation-form"))
        )
        time.sleep(0.3)

    # ------------------------------------------------------------------ #
    # Component A: Level of Harm safety filter -- reads the visible table
    # ------------------------------------------------------------------ #
    def get_record_rows(self):
        """Returns list of dicts: {row, record_id, harm_level, event_description}"""
        rows = self.find_all("record_rows")
        results = []
        for row in rows:
            try:
                harm_el = self.find_in(row, "harm_level_cell")
                id_el = self.find_in(row, "record_id_cell")
                desc_text = ""
                try:
                    desc_el = self.find_in(row, "event_description_cell")
                    desc_text = self.safe_text(desc_el)
                except (KeyError, NoSuchElementException, TimeoutException):
                    pass
                results.append({
                    "row": row,
                    "record_id": self.safe_text(id_el),
                    "harm_level": self.safe_text(harm_el),
                    "event_description": desc_text,
                })
            except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                continue
        return results

    def open_record(self, row_element):
        link = self.find_in(row_element, "open_record_link")
        link.click()
        time.sleep(0.5)  # allow SPA navigation / animations to settle

    # ------------------------------------------------------------------ #
    # Component B/C/D: Record sub-page interactions
    # ------------------------------------------------------------------ #
    def read_event_code_text(self) -> str:
        el = self.find("safety_event_description_field")
        return self.safe_text(el) or el.get_attribute("value") or ""

    def read_free_text_description(self) -> str:
        el = self.find("free_text_description_field")
        return self.safe_text(el) or el.get_attribute("value") or ""

    def fill_define_problem(self, text: str):
        self._fill_field("define_problem_field", text)

    def fill_why1(self, text: str):
        self._fill_field("why1_field", text)

    def fill_why(self, n: int, text: str):
        """Fill Why_Option n (1-5). Silently does nothing if that Why field
        isn't configured in selectors.json (e.g. only why1-5 are mapped)."""
        key = f"why{n}_field"
        if key not in self.selectors:
            self.logger(f"No selector configured for '{key}' -- skipping cause #{n}.", "warn")
            return
        self._fill_field(key, text)

    def _fill_field(self, key, text):
        el = self.find(key)
        tag = el.tag_name.lower()
        if tag == "select":
            self._select_option(el, text)
        else:
            el.clear()
            el.send_keys(text)

    def _select_option(self, select_element, visible_text):
        from selenium.webdriver.support.ui import Select
        Select(select_element).select_by_visible_text(visible_text)

    def click_save(self):
        btn = self.find("save_button")
        btn.click()
        time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # Generic radio-button-group support (e.g. the "Clinic Management
    # Approval" Yes/No questions). Django typically renders a radio group
    # as several <input type="radio" name="field_name" value="Yes/No">
    # sharing the same name. Not yet wired into the automation flow (that
    # screen belongs to a different role/stage), but available for when it
    # is needed.
    # ------------------------------------------------------------------ #
    def set_radio_group(self, field_name: str, value: str, timeout=5):
        """Clicks the radio input in group `field_name` whose value matches
        `value` (case-insensitive)."""
        xpath = (
            f"//input[@type='radio' and @name='{field_name}']"
        )
        try:
            radios = WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(By.XPATH, xpath) or False
            )
        except TimeoutException:
            self.logger(f"No radio group found for field '{field_name}'.", "warn")
            return False

        for radio in radios:
            radio_value = (radio.get_attribute("value") or "").strip()
            if radio_value.lower() == value.strip().lower():
                # Radios are often visually replaced by styled labels, so a
                # plain click can miss -- fall back to JS click if needed.
                try:
                    radio.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", radio)
                return True

        self.logger(f"No option '{value}' found in radio group '{field_name}'.", "warn")
        return False

    # ------------------------------------------------------------------ #
    # Confirmed radio groups on the Analysis/Investigation & Closing
    # sections (real field `name=` attributes, found in a saved live page).
    # ------------------------------------------------------------------ #
    def set_launch_analysis_tool(self, tool_name="5 Whys"):
        """This radio group -- NOT the old id_root_cause_analysis span -- is
        what actually reveals Define the problem / Why 1-5."""
        return self.set_radio_group("launch_analysis_tool", tool_name)

    def set_root_cause_flag(self, n: int, value="Yes"):
        """Sets 'Is this the root cause of the problem (n)?' for Why field n (1-10)."""
        return self.set_radio_group(f"is_this_the_root_cause_of_the_problem_{n}", value)

    def set_analysis_completed(self, value="Yes"):
        return self.set_radio_group("has_the_analysis_for_this_event_been_completed", value)

    def set_further_actions_required(self, value="No"):
        return self.set_radio_group("are_further_actions_required", value)

    # ------------------------------------------------------------------ #
    # Safety Event Owner -- a Select2 "search as you type" widget (not a
    # plain <select>), confirmed against a real Analysis/Investigation page.
    # ------------------------------------------------------------------ #
    def read_owner_display_text(self, field_key="safety_event_owner_field"):
        sel = self.selectors.get(field_key)
        if not sel:
            return ""
        select_id = sel["value"]
        try:
            span = self.driver.find_element(By.ID, f"select2-{select_id}-container")
            return (span.get_attribute("title") or span.text or "").strip()
        except NoSuchElementException:
            return ""

    def ensure_safety_event_owner(self, name, field_key="safety_event_owner_field", timeout=10):
        """Checks the current Safety Event Owner and, if it doesn't already
        match `name`, opens the Select2 widget, types the search text, waits
        for the AJAX-loaded matching result, and clicks it. Returns True if
        a change was made, False if it was already correct."""
        if not name:
            return False

        current = self.read_owner_display_text(field_key)
        if name.lower() in current.lower():
            return False  # already correct -- avoid an unnecessary AJAX round-trip

        sel = self.selectors.get(field_key)
        if not sel:
            self.logger(f"No selector configured for '{field_key}' -- skipping owner check.", "warn")
            return False
        select_id = sel["value"]

        try:
            container = self.driver.find_element(By.ID, f"select2-{select_id}-container")
            container.click()

            search_input = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input.select2-search__field"))
            )
            search_input.send_keys(name)

            option = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//li[contains(@class,'select2-results__option') and "
                    "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'), '{name.lower()}')]"
                ))
            )
            option.click()
            time.sleep(0.3)
            self.logger(f"Safety Event Owner set to '{name}'.", "info")
            return True
        except (TimeoutException, NoSuchElementException) as exc:
            self.logger(f"Could not set Safety Event Owner to '{name}': {exc}", "warn")
            return False

    def set_analysis_tool(self, tool_name="5 Whys"):
        """SUPERSEDED: the actual control that reveals Define/Why fields is
        the 'launch_analysis_tool' radio group -- see set_launch_analysis_tool().
        Kept only as a harmless no-op fallback for the id_root_cause_analysis
        field, which was confirmed to always be a hidden span."""
        try:
            el = self.find("analysis_tool_field", timeout=3)
        except (KeyError, TimeoutException, NoSuchElementException):
            return False

        if el.tag_name.lower() == "select":
            self._select_option(el, tool_name)
            time.sleep(0.3)
            return True
        return False  # already set, or a readonly span (closed record)
