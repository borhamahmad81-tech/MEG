"""
rules_engine.py
----------------
Loads and manages the "Master_Rules_Matrix_Final.xlsx" rules matrix.

IMPORTANT: an Event_Code can have MORE THAN ONE row -- these are
subcategories of the same code (e.g. A.141 "Access covered during
treatment" has one row for "curtains closed" and another for "access
physically covered"). The engine scans the free-text description against
EVERY row for that code and picks the row whose keywords match best. If no
row's keywords match, that's treated the same as "no match" (Component D:
Human-in-the-Loop).

Provides:
  - Event code extraction from free text
  - Row lookup by Event_Code (returns all subcategory rows)
  - Keyword matching against Keywords_List, per row
  - Best-matching-row selection when a code has multiple subcategories
  - Define_The_Problem_Output / Why_Option_1..5 retrieval, per matched row
  - Round-Robin (pointer-based) and Controlled Random cause selection,
    tracked independently per row (each subcategory has its own pointer)
  - Persisting the updated Current_Pointer back to the spreadsheet
"""

import os
import re
import random
import threading
import pandas as pd

BASE_REQUIRED_COLUMNS = [
    "Rule_ID", "Event_Code", "Official_Description", "Keywords_List",
    "Define_The_Problem_Output", "Current_Pointer",
]

WHY_COLUMN_PATTERN = re.compile(r"^Why_Option_(\d+)$")

# Matches short codes like A.24, A.141, B.11, C.16 at the start of a string
EVENT_CODE_PATTERN = re.compile(r"([A-Za-z]{1,3}\.\d{1,4})")


class RuleNotFoundError(Exception):
    pass


class MatchedRule:
    """A single matched row, ready to use for filling the form."""
    def __init__(self, idx, row, matched_keywords, is_default_fallback=False):
        self.idx = idx
        self.row = row
        self.matched_keywords = matched_keywords
        self.is_default_fallback = is_default_fallback
        self.rule_id = row.get("Rule_ID")
        self.event_code = row.get("Event_Code")

    def __repr__(self):
        tag = " DEFAULT" if self.is_default_fallback else ""
        return f"<MatchedRule {self.rule_id} ({self.event_code}) kw={self.matched_keywords}{tag}>"


class RulesEngine:
    def __init__(self, xlsx_path: str):
        self.xlsx_path = xlsx_path
        self.df = None
        self._code_to_indices = {}
        self._lock = threading.Lock()
        self.load_warnings = []
        self.active_codes = None  # None = all codes active
        self.why_columns = []  # detected dynamically in load()
        self.load()

    # ------------------------------------------------------------------ #
    # Loading / Saving
    # ------------------------------------------------------------------ #
    def load(self):
        if not os.path.exists(self.xlsx_path):
            raise FileNotFoundError(f"Rules matrix not found: {self.xlsx_path}")

        df = pd.read_excel(self.xlsx_path, dtype={"Current_Pointer": "Int64"})

        # Detect however many Why_Option_N columns actually exist (1, 2, 6,
        # 10, whatever) instead of requiring exactly 5.
        why_matches = []
        for col in df.columns:
            m = WHY_COLUMN_PATTERN.match(str(col))
            if m:
                why_matches.append((int(m.group(1)), col))
        why_matches.sort(key=lambda t: t[0])
        self.why_columns = [col for _, col in why_matches]

        missing = [c for c in BASE_REQUIRED_COLUMNS if c not in df.columns]
        if not self.why_columns:
            missing.append("Why_Option_1 (at least one Why_Option_N column is required)")
        if missing:
            raise ValueError(
                f"Master rules matrix is missing required columns: {missing}"
            )

        # Clean Excel/Windows carriage-return artifacts ("_x000D_") that show
        # up when text was pasted from Word/Outlook -- otherwise they get
        # typed literally into the web form.
        text_cols = ["Official_Description", "Keywords_List",
                     "Define_The_Problem_Output"] + self.why_columns
        for col in text_cols:
            df[col] = df[col].apply(
                lambda v: str(v).replace("_x000D_", " ").strip() if pd.notna(v) else v
            )

        df["Event_Code"] = df["Event_Code"].astype(str).str.strip()
        df["Current_Pointer"] = df["Current_Pointer"].fillna(0).astype(int)

        self.load_warnings = []

        # Flag very short keywords (<=2 chars) -- these will match almost
        # any free text and cause false positives.
        for idx, row in df.iterrows():
            raw_kw = row.get("Keywords_List")
            if pd.isna(raw_kw):
                continue
            for kw in str(raw_kw).split(","):
                kw = kw.strip()
                if kw and kw.lower() != "nan" and len(kw) <= 2:
                    self.load_warnings.append(
                        f"Rule {row['Rule_ID']} (Event_Code '{row['Event_Code']}') has a "
                        f"very short keyword '{kw}' -- likely to false-match unrelated text."
                    )

        self.df = df

        # An Event_Code can map to MULTIPLE rows (subcategories).
        self._code_to_indices = {}
        for idx, code in zip(df.index, df["Event_Code"]):
            self._code_to_indices.setdefault(code, []).append(idx)

        for code, indices in self._code_to_indices.items():
            if len(indices) > 1:
                rule_ids = df.loc[indices, "Rule_ID"].tolist()
                self.load_warnings.append(
                    f"Event_Code '{code}' has {len(indices)} subcategory rows "
                    f"({rule_ids}) -- the app will pick the one whose keywords "
                    f"match the free-text description."
                )

    def save(self):
        """Persist Current_Pointer (and any other) changes back to the xlsx file."""
        with self._lock:
            self.df.to_excel(self.xlsx_path, index=False)

    def reload(self):
        self.load()

    # ------------------------------------------------------------------ #
    # Active-code filtering, for "process all vs some codes"
    # ------------------------------------------------------------------ #
    def set_active_codes(self, codes):
        """codes: iterable of Event_Code strings to process, or None for all."""
        self.active_codes = set(codes) if codes is not None else None

    def is_code_active(self, event_code: str) -> bool:
        if self.active_codes is None:
            return True
        return event_code in self.active_codes

    # ------------------------------------------------------------------ #
    # Lookup helpers
    # ------------------------------------------------------------------ #
    def extract_event_code(self, text: str):
        """Extract the short code prefix (e.g. A.24) from a longer description string."""
        if not text:
            return None
        match = EVENT_CODE_PATTERN.search(text)
        return match.group(1) if match else None

    def has_rule(self, event_code: str) -> bool:
        return event_code in self._code_to_indices

    def get_rows_for_code(self, event_code: str):
        """Returns all subcategory rows (as a list of (idx, row) tuples) for this code."""
        indices = self._code_to_indices.get(event_code)
        if not indices:
            raise RuleNotFoundError(f"No rule found for Event_Code '{event_code}'")
        return [(idx, self.df.loc[idx]) for idx in indices]

    def get_official_description(self, event_code: str) -> str:
        _, row = self.get_rows_for_code(event_code)[0]
        return str(row.get("Official_Description", ""))

    # ------------------------------------------------------------------ #
    # Row-level accessors (operate on an already-matched row, by idx)
    # ------------------------------------------------------------------ #
    def get_define_problem(self, idx) -> str:
        return str(self.df.at[idx, "Define_The_Problem_Output"])

    def get_why_options(self, idx):
        """Returns the non-empty Why_Option_N values for this specific
        row (subcategory), in column order -- however many Why_Option_N
        columns the matrix actually has."""
        row = self.df.loc[idx]
        options = []
        for col in self.why_columns:
            val = row.get(col)
            if pd.notna(val) and str(val).strip():
                options.append(str(val).strip())
        return options

    # ------------------------------------------------------------------ #
    # Keyword matching (Level 2 of the engine) -- per row
    # ------------------------------------------------------------------ #
    def _matched_keywords_in_row(self, row, free_text: str):
        raw_keywords = row.get("Keywords_List")
        if pd.isna(raw_keywords) or not str(raw_keywords).strip():
            return []
        keywords = [k.strip() for k in str(raw_keywords).split(",") if k.strip()]
        text = free_text or ""
        matched = []
        for kw in keywords:
            # Word-starting match: requires the keyword to begin at a word
            # boundary (so 'K' still won't match inside 'take'/'ask'), but
            # allows anything to follow it in the same word -- so 'request'
            # also matches 'requested'/'requesting'/'requests', not just the
            # exact standalone word.
            pattern = r"(?<!\w)" + re.escape(kw)
            if re.search(pattern, text, re.IGNORECASE):
                matched.append(kw)
        return matched

    def find_matching_rule(self, event_code: str, free_text: str):
        """
        - If this Event_Code has only ONE row, no keyword match is needed --
          there's nothing to disambiguate, so that row is used directly.
        - If it has multiple subcategory rows, scans each one's keywords
          and returns the row with the most hits.
        - If it has multiple rows but NONE match, falls back to the first
          row as a default (marked is_default_fallback=True) instead of
          returning None -- the record still gets processed and saved,
          just flagged so it can be reviewed/corrected later rather than
          left completely untouched.
        """
        rows = self.get_rows_for_code(event_code)

        if len(rows) == 1:
            idx, row = rows[0]
            return MatchedRule(idx, row, [])

        best = None
        for idx, row in rows:
            matched = self._matched_keywords_in_row(row, free_text)
            if matched and (best is None or len(matched) > len(best.matched_keywords)):
                best = MatchedRule(idx, row, matched)

        if best is not None:
            return best

        # No subcategory matched -- fall back to the first row as a default
        # rather than giving up entirely.
        idx, row = rows[0]
        return MatchedRule(idx, row, [], is_default_fallback=True)

    # Back-compat convenience: True/False version of find_matching_rule
    def match_keywords(self, event_code: str, free_text: str) -> bool:
        return self.find_matching_rule(event_code, free_text) is not None

    # ------------------------------------------------------------------ #
    # Component C: Dynamic Response Selection & Variability Logic
    # ------------------------------------------------------------------ #
    def select_causes(self, idx, mode: str = "round_robin", count: int = 1):
        """
        idx: the specific row index (subcategory) to pull Why_Option values from
        mode: "round_robin" or "random"
        count: how many Why_Option values to return
        """
        options = self.get_why_options(idx)
        if not options:
            return []

        count = max(1, min(count, len(options)))

        if mode == "random":
            return random.sample(options, count)

        # --- Round-Robin / Sequential mode (pointer tracked per row) ---
        with self._lock:
            pointer = int(self.df.at[idx, "Current_Pointer"])
            pointer = pointer % len(options)
            selected = []
            p = pointer
            for _ in range(count):
                selected.append(options[p % len(options)])
                p += 1
            self.df.at[idx, "Current_Pointer"] = p % len(options)

        self.save()
        return selected

    # ------------------------------------------------------------------ #
    # Component D helper: unique choices across the whole matrix, for the
    # manual dropdown shown in the Human-in-the-Loop pop-up.
    # ------------------------------------------------------------------ #
    def get_all_unique_why_options(self):
        opts = set()
        for col in self.why_columns:
            opts.update(v for v in self.df[col].dropna().astype(str).tolist() if v.strip())
        return sorted(opts)

    def get_all_event_codes(self):
        return sorted(self._code_to_indices.keys())
