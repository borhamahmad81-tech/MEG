"""
deferred_queue.py
------------------
Holds records that could not be auto-processed (no keyword match, and the
2-minute human-in-the-loop pop-up timed out). These get replayed once the
main table pass is complete (Component D: "The Review Phase").
"""

import json
import os
import time


class DeferredQueue:
    def __init__(self, persist_path="deferred_queue.json"):
        self.persist_path = persist_path
        self.items = []
        self._load()

    def _load(self):
        if os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    self.items = json.load(f)
            except Exception:
                self.items = []

    def _save(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2, ensure_ascii=False)

    def add(self, record_id, event_code, free_text, reason="timeout"):
        self.items.append({
            "record_id": record_id,
            "event_code": event_code,
            "free_text": free_text,
            "reason": reason,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()

    def pop_all(self):
        items, self.items = self.items, []
        self._save()
        return items

    def remove(self, record_id):
        self.items = [i for i in self.items if i["record_id"] != record_id]
        self._save()

    def __len__(self):
        return len(self.items)

    def clear(self):
        self.items = []
        self._save()
