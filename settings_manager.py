"""
settings_manager.py
--------------------
Simple persisted JSON settings, saved next to the EXE / script so no admin
rights or installation are required.
"""

import json
import os
import sys


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DEFAULT_SETTINGS = {
    "rules_matrix_path": "Master_Rules_Matrix_Final.xlsx",
    "browser": "chrome",          # "chrome" or "edge"
    "debug_port": 9222,
    "driver_path": "",  # optional explicit path to chromedriver/msedgedriver
    "selection_mode": "round_robin",  # "round_robin" or "random"
    "causes_count": 1,
    "timeout_seconds": 120,
    "page_wait_timeout": 25,
    "deferred_queue_path": "deferred_queue.json",
    "window_geometry": "1250x820",
    "theme": "dark",
    # MEG (audits.megsupporttools.com) site settings, used to build direct
    # per-record URLs instead of clicking icons in the list table.
    "site_base_url": "https://audits.megsupporttools.com",
    "site_country": "saudi-arabia",
    "site_form_id": "7608",
    "analysis_tool_name": "5 Whys",
    "code_filter_mode": "all",   # "all" or "selected"
    "selected_codes": [],
    "required_status": "Management approval",
    "required_centre": "Buraidah 1",
    "test_mode": False,
    "test_stage": "single",  # "single" (Stage 1) or "full" (Stage 2)
    "bulk_test_mode": False,
    "safety_event_owner_name": "Ahmed Mohamed, Specialist",
}


class SettingsManager:
    def __init__(self, filename="settings.json"):
        self.path = os.path.join(_base_dir(), filename)
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.data.update(saved)
            except Exception:
                pass  # fall back to defaults silently

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def resolve_path(self, key):
        """Resolve a stored relative path against the app's base directory."""
        value = self.data.get(key, "")
        if not value:
            return value
        if os.path.isabs(value):
            return value
        return os.path.join(_base_dir(), value)
