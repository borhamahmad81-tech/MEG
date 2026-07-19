"""
browser_controller.py
----------------------
Attaches to an ALREADY OPEN Chrome or Edge window (launched with a remote
debugging port) rather than spawning a new controlled browser.

All CSS/XPath/ID selectors used to find page elements live in
config/selectors.json so this file rarely needs to change when the
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

# Fallback defaults -- these are PLACEHOLDERS, overridden by config/selectors.json.
DEFAULT_SELECTORS = {
    "record_rows": {"type": "css", "value": "div.widget-answertablewidget table.table-striped tbody tr"},
    "record_id_cell": {"type": "css", "value": "td:nth-child(1)"},
    "centre_cell": {"type": "css", "value": "td:nth-child(3)"},
    "event_description_cell": {"type": "css", "value": "td:nth-child(6)"},
    "harm_level_cell": {"type": "css", "value": "td:nth-child(7)"},
    "status_cell": {"type": "css", "value": "td:nth-child(10)"},
    "open_record_link": {"type": "css", "value": "a.answerTableLink"},
    "safety_event_description_field": {"type": "id", "value": "id_aor_code"},
    "free_text_description_field": {"type": "id", "value": "id_detailed_aor_description"},
    "harm_level_field": {"type": "id", "value": "id_min_level_of_harm"},
    "status_field": {"type": "id", "value": "id_status"},
    "analysis_tool_field": {"type": "id", "value": "id_root_cause_analysis"},
    "define_problem_field": {"type": "id", "value": "id_define_the_problem"},
    "why1_field": {"type": "id", "value": "id_why_did_this_problem_occur_why_1"},
    "why2_field": {"type": "id", "value": "id_why_did_this_problem_occur_why_2"},
    "why3_field": {"type": "id", "value": "id_why_did_this_problem_occur_why_3"},
    "why4_field": {"type": "id", "value": "id_why_did_this_problem_occur_why_4"},
    "why5_field": {"type": "id", "value": "id_why_did_this_problem_occur_why_5"},
    "safety_event_owner_field": {"type": "id", "value": "id_aor_owner"},
    "save_button": {"type": "css", "value": "div.audit-report-controls button.btn-submit"},
}


class BrowserController:
    def __init__(self, browser="chrome", debug_port=9222,
                 selectors_path=DEFAULT_SELECTORS_PATH, logger=None,
                 driver_path=None, default_timeout=25):
        self.browser = browser.lower()
        self.debug_port = debug_port
        self.driver = None
        self.driver_path = (driver_path or "").strip() or None
        self.default_timeout = default_timeout
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
                options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.debug_port}")
                service = EdgeService(executable_path=self.driver_path) if self.driver_path else None
                self.driver = webdriver.Edge(options=options, service=service)
            else:
                options = ChromeOptions()
                options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.debug_port}")
                service = ChromeService(executable_path=self.driver_path) if self.driver_path else None
                self.driver = webdriver.Chrome(options=options, service=service)

            self.logger(
                f"Attached to existing {self.browser.title()} session on port {self.debug_port}.",
                "success",
            )
            self.log_attached_context()
            return self.driver
        except WebDriverException as exc:
            msg = str(exc)
            if "Unable to obtain driver" in msg or "unable to discover" in msg.lower():
                self.logger(
                    f"Could not auto-download the {self.browser.title()} driver "
                    f"(likely no internet access to the driver server, or it's blocked "
                    f"by a firewall). Fix: download the matching driver manually and set "
                    f"its path in 'Driver Path' in the GUI. "
                    f"Chrome: https://googlechromelabs.github.io/chrome-for-testing/ "
                    f"| Edge: https://developer.microsoft.com/microsoft-edge/tools/webdriver/",
                    "error",
                )
            else:
                self.logger(f"Could not attach to {self.browser.title()} on port {self.debug_port}: {exc}", "error")
            raise
        except Exception as exc:
            self.logger(f"Could not attach to {self.browser.title()} on port {self.debug_port}: {exc}", "error")
            raise

    # ------------------------------------------------------------------ #
    # Tab handling. Attaching to a debug browser gives us whichever tab the
    # driver picked -- NOT necessarily the one the user is looking at. If an
    # extra tab is open (new-tab page, leftover login redirect), the app can
    # sit on a blank tab waiting for a table that lives in the tab next door.
    # ------------------------------------------------------------------ #
    def log_attached_context(self):
        try:
            handles = self.driver.window_handles
            self.logger(
                f"Attached to tab: '{self.driver.title}' | {self.driver.current_url} "
                f"({len(handles)} tab(s) open in this browser)",
                "info",
            )
        except Exception as exc:
            self.logger(f"Could not read the attached tab's context: {exc}", "warn")

    def _tab_has_rows(self) -> bool:
        sel = self.selectors["record_rows"]
        try:
            return len(self.driver.find_elements(self._by(sel), sel["value"])) > 0
        except Exception:
            return False

    def ensure_tab_with_rows(self) -> bool:
        """If the current tab has no record rows, look through the other open
        tabs and switch to one that does. Returns True if we ended up on a tab
        containing the records table."""
        if self._tab_has_rows():
            return True

        try:
            handles = list(self.driver.window_handles)
            original = self.driver.current_window_handle
        except Exception as exc:
            self.logger(f"Could not enumerate browser tabs: {exc}", "warn")
            return False

        if len(handles) > 1:
            self.logger(
                f"No records table in the current tab -- scanning {len(handles)} open tab(s)...",
                "warn",
            )

        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                if self._tab_has_rows():
                    self.logger(
                        f"Found the records table in tab: {self.driver.current_url}",
                        "success",
                    )
                    return True
                self.logger(f"  - no records table in: {self.driver.current_url}", "info")
            except Exception:
                continue

        try:
            self.driver.switch_to.window(original)
        except Exception:
            pass
        return False

    def _log_table_diagnostics(self):
        """Called when the records table can't be found, to say WHY rather
        than just timing out."""
        try:
            url = self.driver.current_url
            title = self.driver.title
            generic_rows = len(self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
            iframes = len(self.driver.find_elements(By.TAG_NAME, "iframe"))
            self.logger(
                f"Diagnostics -- current tab: '{title}' | {url}\n"
                f"  Generic 'table tbody tr' found on this page: {generic_rows}\n"
                f"  iframes on this page: {iframes}",
                "warn",
            )
            if generic_rows == 0 and iframes == 0:
                self.logger(
                    "  There is no table at all on this page. You are most likely on a "
                    "login/redirect page rather than the dashboard -- log in, open the "
                    "'All Safety Events' dashboard, wait for rows, then Start again.",
                    "warn",
                )
            elif generic_rows > 0:
                self.logger(
                    "  A table IS present but does not match the configured selector. "
                    "The site's markup may have changed -- check record_rows in "
                    "config/selectors.json.",
                    "warn",
                )
            elif iframes > 0:
                self.logger(
                    "  The page has iframes -- the table may be inside one, which "
                    "requires switching into that frame first.",
                    "warn",
                )
        except Exception as exc:
            self.logger(f"Could not gather page diagnostics: {exc}", "warn")

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

    def find(self, key, timeout=None, root=None):
        timeout = timeout if timeout is not None else self.default_timeout
        sel = self.selectors.get(key)
        if not sel:
            raise KeyError(f"Selector '{key}' is not configured in selectors.json")
        context = root or self.driver
        return WebDriverWait(context, timeout).until(
            EC.presence_of_element_located((self._by(sel), sel["value"]))
        )

    def find_all(self, key, timeout=None, root=None):
        timeout = timeout if timeout is not None else self.default_timeout
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
    # Component A: Level of Harm safety filter -- reads the visible table
    # ------------------------------------------------------------------ #
    def get_record_rows(self, timeout=None):
        """Returns list of dicts: {row, record_id, harm_level, event_description, status, centre}.
        Reads cells by direct column index with NO per-cell waits, so a
        single malformed row can never stall or crash the whole scan."""
        timeout = timeout if timeout is not None else self.default_timeout

        # Make sure we're actually on the tab holding the dashboard before
        # spending the full timeout waiting on the wrong page.
        self.ensure_tab_with_rows()

        try:
            rows = self.find_all("record_rows", timeout=timeout)
        except TimeoutException:
            self._log_table_diagnostics()
            raise
        results = []

        # Column indexes (1-based) as confirmed from the dashboard HTML.
        col = {"record_id": 1, "centre": 3, "event_description": 6, "harm_level": 7, "status": 10}

        def cell_text(row_el, n):
            try:
                tds = row_el.find_elements(By.XPATH, "./td")
                if len(tds) >= n:
                    return (tds[n - 1].text or "").strip()
            except Exception:
                pass
            return ""

        for row in rows:
            try:
                rid = cell_text(row, col["record_id"])
                if not rid:
                    continue  # header/spacer/empty row
                results.append({
                    "row": row,
                    "record_id": rid,
                    "harm_level": cell_text(row, col["harm_level"]),
                    "event_description": cell_text(row, col["event_description"]),
                    "status": cell_text(row, col["status"]),
                    "centre": cell_text(row, col["centre"]),
                })
            except StaleElementReferenceException:
                continue
        return results

    def open_record(self, row_element, timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        link = self.find_in(row_element, "open_record_link")

        last_exc = None
        for attempt in range(2):
            try:
                link.click()
                WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.ID, "observation-form"))
                )
                time.sleep(0.3)
                return
            except WebDriverException as exc:
                last_exc = exc
                self.logger(f"Opening record attempt {attempt + 1} failed, retrying...", "warn")
                time.sleep(1)
                try:
                    link = self.find_in(row_element, "open_record_link")
                except Exception:
                    pass
        raise last_exc

    def navigate_to_dashboard(self, base_url, country, form_id, timeout=None):
        """Returns to the dashboard/list page. Required after opening any
        individual record, since row elements captured from a previous page
        load go stale the moment the browser navigates away."""
        timeout = timeout if timeout is not None else self.default_timeout
        base_url = base_url.rstrip("/")
        url = f"{base_url}/{country}/dashboard/report/{form_id}/0"

        last_exc = None
        for attempt in range(2):
            try:
                self.driver.execute_script("window.location.href = arguments[0];", url)
                sel = self.selectors["record_rows"]
                WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((self._by(sel), sel["value"]))
                )
                time.sleep(0.3)
                return
            except WebDriverException as exc:
                last_exc = exc
                self.logger(f"Returning to dashboard attempt {attempt + 1} failed, retrying...", "warn")
                time.sleep(1)
        raise last_exc

    # ------------------------------------------------------------------ #
    # Component B/C/D: Record sub-page interactions
    # ------------------------------------------------------------------ #
    def read_event_code_text(self) -> str:
        el = self.find("safety_event_description_field")
        # Prefer data-value: on readonly <span> display fields (used when the
        # containing accordion section is collapsed), Selenium's .text only
        # returns VISIBLE text and silently comes back empty if the section
        # is hidden -- data-value holds the real content regardless.
        data_value = el.get_attribute("data-value")
        if data_value:
            return data_value.strip()
        return self.safe_text(el) or el.get_attribute("value") or ""

    def read_free_text_description(self) -> str:
        el = self.find("free_text_description_field")
        data_value = el.get_attribute("data-value")
        if data_value:
            return data_value.strip()
        return self.safe_text(el) or el.get_attribute("value") or ""

    def read_patient_id(self) -> str:
        """Read the 'Patient ID / EMR / Teammate or visitor initials' value.

        Same read-only <span data-value=...> widget as the free-text
        description, and it lives in the 'Patient information' section which
        may be collapsed -- so data-value is the only reliable source.

        Purely informational (used for the end-of-run summary), so a failure
        here must never interrupt processing: returns "" instead of raising.
        """
        try:
            el = self.find("patient_id_field", timeout=5)
        except Exception:
            self.logger(
                "Patient ID field not found on this record -- the summary will "
                "show no patient for it.",
                "warn",
            )
            return ""
        try:
            data_value = el.get_attribute("data-value")
            if data_value and data_value.strip():
                return data_value.strip()
            return (self.safe_text(el) or el.get_attribute("value") or "").strip()
        except Exception as exc:
            self.logger(f"Could not read the Patient ID: {exc}", "warn")
            return ""

    def fill_define_problem(self, text: str):
        self._fill_field("define_problem_field", text)

    def fill_why1(self, text: str):
        self._fill_field("why1_field", text)

    def fill_why(self, n: int, text: str):
        key = f"why{n}_field"
        if key not in self.selectors:
            self.logger(f"No selector configured for '{key}' -- skipping cause #{n}.", "warn")
            return
        self._fill_field(key, text)

    def _fill_field(self, key, text, timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        el = self.find(key)
        tag = el.tag_name.lower()
        if tag == "select":
            self._select_option(el, text)
            return

        last_exc = None
        for attempt in range(5):
            try:
                WebDriverWait(self.driver, timeout).until(
                    EC.element_to_be_clickable((self._by(self.selectors[key]), self.selectors[key]["value"]))
                )
                el.clear()
                el.send_keys(text)
                return
            except (WebDriverException, TimeoutException) as exc:
                last_exc = exc
                self.logger(f"Field '{key}' not interactable yet, retrying...", "warn")
                time.sleep(1)
                try:
                    el = self.find(key)
                except Exception:
                    pass
        raise last_exc

    def _select_option(self, select_element, visible_text):
        from selenium.webdriver.support.ui import Select
        Select(select_element).select_by_visible_text(visible_text)

    def click_save(self, timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        sel = self.selectors["save_button"]
        last_exc = None
        for attempt in range(3):
            try:
                btn = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((self._by(sel), sel["value"]))
                )
                try:
                    btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.0)
                return
            except (WebDriverException, TimeoutException) as exc:
                last_exc = exc
                self.logger(f"Save button not found yet (attempt {attempt + 1}), retrying...", "warn")
                time.sleep(1.5)
        raise last_exc

    # ------------------------------------------------------------------ #
    # Generic radio-button-group support
    # ------------------------------------------------------------------ #
    def set_radio_group(self, field_name: str, value: str, timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        xpath = f"//input[@type='radio' and @name='{field_name}']"
        try:
            radios = WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(By.XPATH, xpath) or False
            )
        except TimeoutException:
            self.logger(f"No radio group found for field '{field_name}'.", "warn")
            return False

        for radio in radios:
            radio_value = (radio.get_attribute("value") or "").strip()
            if radio_value.lower() != value.strip().lower():
                continue

            # The radio input itself is often visually hidden (a styled
            # <label> is shown instead), so click the wrapping/associated
            # label -- the same target a human clicks.
            target = radio
            try:
                radio_id = radio.get_attribute("id")
                if radio_id:
                    labels = self.driver.find_elements(By.CSS_SELECTOR, f"label[for='{radio_id}']")
                    if labels:
                        target = labels[0]
                if target is radio:
                    parent_labels = radio.find_elements(By.XPATH, "./ancestor::label[1]")
                    if parent_labels:
                        target = parent_labels[0]
            except Exception:
                pass

            try:
                target.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", target)

            time.sleep(0.3)  # let the site's own logic react to the click
            return True

        self.logger(f"No option '{value}' found in radio group '{field_name}'.", "warn")
        return False

    def wait_for_radio_group_present(self, field_name: str, timeout=10):
        xpath = f"//input[@type='radio' and @name='{field_name}']"
        try:
            return WebDriverWait(self.driver, timeout).until(
                lambda d: any(r.is_displayed() for r in d.find_elements(By.XPATH, xpath)) or False
            )
        except TimeoutException:
            self.logger(f"Radio group '{field_name}' did not appear within {timeout}s.", "warn")
            return False

    def wait_for_field_present(self, key, timeout=10):
        sel = self.selectors.get(key)
        if not sel:
            return False
        try:
            return WebDriverWait(self.driver, timeout).until(
                lambda d: any(e.is_displayed() for e in d.find_elements(self._by(sel), sel["value"])) or False
            )
        except TimeoutException:
            self.logger(f"Field '{key}' did not appear within {timeout}s.", "warn")
            return False

    # ------------------------------------------------------------------ #
    # Confirmed radio groups: Root Cause Analysis / Closing
    # ------------------------------------------------------------------ #
    def set_launch_analysis_tool(self, tool_name="5 Whys"):
        return self.set_radio_group("launch_analysis_tool", tool_name)

    def set_root_cause_flag(self, n: int, value="Yes"):
        return self.set_radio_group(f"is_this_the_root_cause_of_the_problem_{n}", value)

    def set_analysis_completed(self, value="Yes"):
        return self.set_radio_group("has_the_analysis_for_this_event_been_completed", value)

    def set_further_actions_required(self, value="No"):
        return self.set_radio_group("are_further_actions_required", value)

    # ------------------------------------------------------------------ #
    # Confirmed radio groups: Clinic Management Approval
    # ------------------------------------------------------------------ #
    def set_code_correct(self, value="Yes"):
        return self.set_radio_group("is_the_code_correct", value)

    def set_information_complete(self, value="Yes"):
        return self.set_radio_group("is_the_information_complete", value)

    def set_approve_safety_event(self, value="Yes"):
        return self.set_radio_group("do_you_accept_the_aor", value)

    def set_requires_escalation(self, value="No"):
        return self.set_radio_group("does_the_safety_event_requires_additional_escalation_to_managem", value)

    # ------------------------------------------------------------------ #
    # Safety Event Owner -- Select2 "search as you type" widget
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

    def ensure_safety_event_owner(self, name, field_key="safety_event_owner_field", timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        if not name:
            return False

        current = self.read_owner_display_text(field_key)
        if name.lower() in current.lower():
            return False  # already correct

        sel = self.selectors.get(field_key)
        if not sel:
            self.logger(f"No selector configured for '{field_key}' -- skipping owner check.", "warn")
            return False
        select_id = sel["value"]

        try:
            display_span = self.driver.find_element(By.ID, f"select2-{select_id}-container")
            toggle = display_span.find_element(By.XPATH, "./ancestor::*[contains(@class,'select2-selection')][1]")
            try:
                toggle.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", toggle)

            search_inputs = WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "input.select2-search__field") or False
            )
            search_input = search_inputs[-1]
            search_input.send_keys(name)

            typed_value = search_input.get_attribute("value") or ""
            if name.lower() not in typed_value.lower():
                self.logger(
                    f"Owner search box shows '{typed_value}' after typing '{name}' -- "
                    f"typing may have gone to the wrong element.",
                    "warn",
                )

            option = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//li[contains(@class,'select2-results__option') and "
                    "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    f"'abcdefghijklmnopqrstuvwxyz'), '{name.lower()}')]"
                ))
            )
            option.click()
            try:
                self.driver.execute_script("document.activeElement.blur();")
                from selenium.webdriver.common.keys import Keys
                self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            except Exception:
                pass
            time.sleep(1.0)
            self.logger(f"Safety Event Owner set to '{name}'.", "info")
            return True
        except (TimeoutException, NoSuchElementException) as exc:
            self.logger(f"Could not set Safety Event Owner to '{name}': {exc}", "warn")
            return False

    # ------------------------------------------------------------------ #
    # Generic Yes/No radio group helper for other screens (e.g. Clinic
    # Management Approval read via visible text), kept for reuse.
    # ------------------------------------------------------------------ #
    def force_element_visible(self, element_id: str):
        """Walks up from the given element and clears any inline
        'display: none' found on it or its ancestors. NOTE: this can break
        page layout if used on containers with meaningful CSS -- prefer
        wait_for_radio_group_present()/wait_for_field_present() first and
        only use this as an explicit, deliberate last resort."""
        script = """
        var el = document.getElementById(arguments[0]);
        if (!el) return false;
        var node = el;
        var cleared = 0;
        while (node) {
            if (node.style && node.style.display === 'none') {
                node.style.display = '';
                cleared++;
            }
            node = node.parentElement;
        }
        return cleared;
        """
        try:
            cleared = self.driver.execute_script(script, element_id)
            if cleared:
                self.logger(f"Force-revealed hidden section containing '{element_id}' ({cleared} level(s)).", "info")
                time.sleep(0.3)
            return bool(cleared)
        except Exception as exc:
            self.logger(f"Could not force-reveal '{element_id}': {exc}", "warn")
            return False
