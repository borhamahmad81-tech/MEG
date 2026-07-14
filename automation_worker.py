"""
automation_worker.py
---------------------
Runs the full web-scrape & auto-analyze pass in a background thread so the
GUI stays responsive. Talks to the GUI only through thread-safe queues:

  log_queue      -> (message, level) tuples for the console
  review_queue    -> requests for the GUI to show the Human-in-the-Loop modal
  review_response -> the GUI's answer to a pending review request (or None)
  status_queue    -> dict updates for the status bar counters

This file contains NO GUI code, so it can also be unit-tested headlessly.
"""

import queue
import threading
import time

from rules_engine import RulesEngine, RuleNotFoundError
from browser_controller import BrowserController
from deferred_queue import DeferredQueue

SERIOUS_LABEL = "serious"


class AutomationWorker(threading.Thread):
    def __init__(self, settings, log_queue: queue.Queue,
                 review_queue: queue.Queue, review_response: queue.Queue,
                 status_queue: queue.Queue):
        super().__init__(daemon=True)
        self.settings = settings
        self.log_queue = log_queue
        self.review_queue = review_queue
        self.review_response = review_response
        self.status_queue = status_queue

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set == paused
        self._test_single_record_mode = False  # Test Mode Stage 1 setting
        self._single_test_stop = False  # set True once that one record is handled

        self.rules = None
        self.browser = None
        self.deferred = DeferredQueue(settings.resolve_path("deferred_queue_path"))

        self.counts = {"processed": 0, "deferred": 0, "blocked": 0, "skipped": 0}

    # ------------------------------------------------------------------ #
    def log(self, message, level="info"):
        self.log_queue.put((message, level))

    def push_status(self):
        self.status_queue.put(dict(self.counts))

    def stop(self):
        self._stop_event.set()

    def pause(self):
        self._pause_event.set()
        self.log("Paused. Click Start to resume.", "warn")

    def resume(self):
        self._pause_event.clear()

    def _check_pause_stop(self):
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.2)
        return self._stop_event.is_set()

    # ------------------------------------------------------------------ #
    def run(self):
        try:
            self.rules = RulesEngine(self.settings.resolve_path("rules_matrix_path"))
            for warning in self.rules.load_warnings:
                self.log(f"[Matrix Warning] {warning}", "warn")
        except Exception as exc:
            self.log(f"Failed to load rules matrix: {exc}", "error")
            return

        if self.settings.get("code_filter_mode", "all") == "selected":
            selected = self.settings.get("selected_codes", [])
            self.rules.set_active_codes(selected)
            self.log(f"Processing only {len(selected)} selected code(s): {selected}", "info")
        else:
            self.rules.set_active_codes(None)

        self._test_single_record_mode = (
            self.settings.get("test_mode", False)
            and self.settings.get("test_stage", "single") == "single"
        )
        if self._test_single_record_mode:
            self.log(
                "[Test Mode Stage 1] Will open ONE eligible record, fill it, "
                "and stop before saving the next.",
                "info",
            )

        self.browser = BrowserController(
            browser=self.settings.get("browser", "chrome"),
            debug_port=self.settings.get("debug_port", 9222),
            logger=self.log,
            driver_path=self.settings.get("driver_path", ""),
            default_timeout=int(self.settings.get("page_wait_timeout", 25)),
        )
        try:
            self.browser.attach()
        except Exception:
            self.log("Attach failed. Is the browser running with the debug flag?", "error")
            return

        self._process_main_queue()

        if self._stop_event.is_set():
            self.log("Stopped by user before completion.", "warn")
            return

        if self._single_test_stop:
            # Test Mode Stage 1: "open from queue + fill, stop before next".
            # We already stopped right after handling the one test record --
            # skip the deferred/review phase entirely, this was just a
            # single-record dry run.
            self._final_report("TEST MODE - STAGE 1 (single record) complete")
            return

        self._process_deferred_review_phase()

        if self.settings.get("test_mode", False):
            self._final_report("TEST MODE - STAGE 2 (full queue) complete")
        else:
            self._final_report("Run complete")

    def _final_report(self, heading: str):
        c = self.counts
        total = c["processed"] + c["deferred"] + c["blocked"] + c["skipped"]
        lines = [
            f"=== FINAL REPORT: {heading} ===",
            f"Total records scanned : {total}",
            f"Processed (saved)     : {c['processed']}",
            f"Deferred (needs review) : {c['deferred']}",
            f"Skipped (filtered out)  : {c['skipped']}",
            f"Blocked (Serious)     : {c['blocked']}",
            "No more safety events remain in the queue." if not self._single_test_stop
            else "Stopped after the first record, as requested for Stage 1 testing.",
        ]
        self.log("\n".join(lines), "report")

    # ------------------------------------------------------------------ #
    def _process_main_queue(self):
        handled_ids = set()
        first_scan_logged = False

        while True:
            if self._check_pause_stop():
                return

            try:
                rows = self.browser.get_record_rows()
            except Exception as exc:
                self.log(f"Could not read the records table: {exc}", "error")
                return

            if not first_scan_logged:
                self.log(f"Found {len(rows)} record(s) on the page.", "info")
                first_scan_logged = True

            record = next((r for r in rows if (r["record_id"] or "UNKNOWN") not in handled_ids), None)
            if record is None:
                return  # every record on the page has been handled

            record_id = record["record_id"] or "UNKNOWN"
            handled_ids.add(record_id)
            harm_level = (record["harm_level"] or "").strip().lower()

            # --- Component A: Level of Harm safety filter ---
            if harm_level == SERIOUS_LABEL:
                self.log(f"[Warning] ID: {record_id} flagged as SERIOUS", "warn")
                self.counts["blocked"] += 1
                self.push_status()
                continue  # still on the dashboard -- no re-navigation needed

            # Process only records currently awaiting Management Approval --
            # not Closed, not Analysis/Investigation.
            status_text = (record.get("status") or "").strip().lower()
            required_status = self.settings.get("required_status", "management approval").strip().lower()
            if status_text and status_text != required_status:
                self.log(
                    f"[Skipped] ID: {record_id}: status is '{record.get('status')}', "
                    f"required status is '{self.settings.get('required_status')}'.",
                    "info",
                )
                self.counts["skipped"] += 1
                self.push_status()
                continue

            # Only process the configured Centre (e.g. "Buraidah 1").
            centre_text = (record.get("centre") or "").strip().lower()
            required_centre = self.settings.get("required_centre", "").strip().lower()
            if required_centre and centre_text != required_centre:
                self.log(
                    f"[Skipped] ID: {record_id}: centre is '{record.get('centre')}', "
                    f"required centre is '{self.settings.get('required_centre')}'.",
                    "info",
                )
                self.counts["skipped"] += 1
                self.push_status()
                continue

            # Early code filter using the list page's description column,
            # so records outside the selected code list never get opened.
            if self.rules is not None:
                list_code = self.rules.extract_event_code(record.get("event_description", ""))
                if list_code and not self.rules.is_code_active(list_code):
                    self.log(f"[Skipped] ID: {record_id} ({list_code}): not in selected codes.", "info")
                    self.counts["skipped"] += 1
                    self.push_status()
                    continue

            try:
                self._process_single_record(record)
            except Exception as exc:
                self.log(f"[Error] ID: {record_id}: {exc}", "error")

            if self._test_single_record_mode:
                self._single_test_stop = True
                self.log(
                    "[Test Mode Stage 1] Stopping after this one record, as requested.",
                    "info",
                )
                return

            # This record was opened (navigated away from the dashboard),
            # so the table must be re-fetched fresh next loop -- return to
            # the dashboard now rather than reusing now-stale row elements.
            try:
                self.browser.navigate_to_dashboard(
                    self.settings.get("site_base_url"),
                    self.settings.get("site_country"),
                    self.settings.get("site_form_id"),
                )
            except Exception as exc:
                self.log(f"Could not return to the dashboard: {exc}", "error")
                return

    def _process_single_record(self, record):
        record_id = record["record_id"] or "UNKNOWN"
        self.browser.open_record(record["row"])

        raw_code_text = self.browser.read_event_code_text()
        event_code = self.rules.extract_event_code(raw_code_text)

        if not event_code or not self.rules.has_rule(event_code):
            self.log(
                f"[Warning] ID: {record_id}: no matching Event_Code found for '{raw_code_text}'.",
                "warn",
            )
            self._defer_or_review(record_id, event_code or "UNKNOWN", "")
            return

        if not self.rules.is_code_active(event_code):
            self.log(f"[Skipped] ID: {record_id} ({event_code}): not in selected codes.", "info")
            self.counts["skipped"] += 1
            self.push_status()
            return

        free_text = self.browser.read_free_text_description()

        matched_rule = self.rules.find_matching_rule(event_code, free_text)
        if matched_rule is None:
            self.log(
                f"[Info] ID: {record_id} ({event_code}): no keyword match in free text.",
                "warn",
            )
            self._defer_or_review(record_id, event_code, free_text)
            return

        # --- Component B/C: Form injection ---
        self._inject_and_save(record_id, matched_rule)

    def _inject_and_save(self, record_id, matched_rule):
        define_text = self.rules.get_define_problem(matched_rule.idx)
        causes = self.rules.select_causes(
            matched_rule.idx,
            mode=self.settings.get("selection_mode", "round_robin"),
            count=int(self.settings.get("causes_count", 1)),
        )

        self._apply_fields(define_text, causes)

        if self.settings.get("test_mode", False):
            self._confirm_before_save(record_id, matched_rule, define_text, causes)
            return

        self.browser.click_save()
        self.counts["processed"] += 1
        self.push_status()
        self.log(
            f"[Saved] ID: {record_id} ({matched_rule.event_code} / {matched_rule.rule_id}, "
            f"matched: {matched_rule.matched_keywords}): '{define_text}' | causes: {causes}",
            "success",
        )

    def _apply_fields(self, define_text, causes):
        """Fills the record's fields on the live page WITHOUT saving. Used by
        both normal mode (immediately followed by Save) and Test Mode (where
        the user reviews the filled page before approving the Save)."""
        # Clinic Management Approval section (same single-page form).
        self.browser.set_code_correct("Yes")
        self.browser.set_information_complete("Yes")
        self.browser.set_approve_safety_event("Yes")
        self.browser.set_requires_escalation("No")
        self.browser.ensure_safety_event_owner(self.settings.get("safety_event_owner_name", "Ahmed Mohamed, Specialist"))

        # This radio group is what actually reveals Define/Why 1.
        # The Root Cause Analysis section only appears after "Do you approve
        # the safety event?" is answered Yes (already done above) -- the
        # site's own JS reveals it, same cascading pattern as the earlier
        # Yes/No questions. set_launch_analysis_tool already waits for its
        # radio group to appear, so no forced reveal should be needed here.
        self.browser.set_launch_analysis_tool(self.settings.get("analysis_tool_name", "5 Whys"))
        self.browser.fill_define_problem(define_text)

        if not causes:
            causes = [""]  # still need to answer the completed-flag even with no causes

        for i, cause in enumerate(causes, start=1):
            if cause:
                self.browser.fill_why(i, cause)
                self.browser.set_root_cause_flag(i, "Yes")

            is_last = (i == len(causes))
            # "Has the analysis for this event been completed?" is NOT a
            # final flag -- it's the control that reveals the NEXT Why box.
            # Answer "No" to reveal Why (i+1); only answer "Yes" on the last
            # requested cause, which finalizes analysis and reveals Closing.
            self.browser.set_analysis_completed("Yes" if is_last else "No")

        # Closing section (revealed by the final "Yes" above).
        # Lessons Learned is intentionally left untouched.
        self.browser.set_further_actions_required("No")

    # ------------------------------------------------------------------ #
    # Test Mode: review every auto-filled record before it's actually saved
    # ------------------------------------------------------------------ #
    def _confirm_before_save(self, record_id, matched_rule, define_text, causes):
        self.review_queue.put({
            "kind": "confirm_save",
            "record_id": record_id,
            "event_code": matched_rule.event_code,
            "rule_id": matched_rule.rule_id,
            "matched_keywords": matched_rule.matched_keywords,
            "define_text": define_text,
            "causes": causes,
        })

        # No countdown here -- Test Mode is meant to give unlimited review
        # time. We just poll so Stop/Pause still work while waiting.
        answer = None
        while not self._stop_event.is_set():
            try:
                answer = self.review_response.get(timeout=1)
                break
            except queue.Empty:
                continue

        if answer is None or answer.get("action") == "skip":
            self.log(f"[Test Mode] ID: {record_id}: skipped, not saved.", "warn")
            return

        # Re-apply in case the user edited the text in the review dialog
        final_define = answer.get("define_text", define_text)
        final_causes = answer.get("causes", causes)
        self._apply_fields(final_define, final_causes)

        self.browser.click_save()
        self.counts["processed"] += 1
        self.push_status()
        self.log(
            f"[Test Mode - Saved] ID: {record_id} ({matched_rule.event_code}): "
            f"'{final_define}' | causes: {final_causes}",
            "success",
        )

    # ------------------------------------------------------------------ #
    # Component D: Human-in-the-Loop Timeout & Deferred Queue
    # ------------------------------------------------------------------ #
    def _defer_or_review(self, record_id, event_code, free_text):
        """No popup, no waiting -- log it, add to the Deferred Queue, and
        return immediately so the main loop moves straight to the next
        record. You review deferred records yourself afterward, at your
        own pace (they're saved in deferred_queue.json)."""
        self.log(f"[Deferred] ID: {record_id} ({event_code}): could not auto-determine the problem. Moved to Deferred Queue for manual review later.", "warn")
        self.deferred.add(record_id, event_code, free_text, reason="no_match")
        self.counts["deferred"] += 1
        self.push_status()

    # ------------------------------------------------------------------ #
    # Review phase: just reports what's waiting -- doesn't pop up or wait
    # ------------------------------------------------------------------ #
    def _process_deferred_review_phase(self):
        pending_count = len(self.deferred)
        if pending_count:
            self.log(
                f"{pending_count} record(s) are in the Deferred Queue "
                f"(deferred_queue.json) waiting for your manual review.",
                "info",
            )
