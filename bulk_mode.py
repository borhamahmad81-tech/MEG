"""
bulk_mode.py
------------
Component E: Bulk File Upload Mode.

Reads a user-prepared Excel/CSV file with columns: ID, Define the problem, Why 1
Bypasses the keyword engine entirely and injects the explicit values straight
into the record's web fields, saves, then writes a "Status" column of
"Done" / "Failed: <reason>" back into the same file.
"""

import os
import queue
import pandas as pd

REQUIRED_BULK_COLUMNS = ["ID", "Define the problem", "Why 1"]


class BulkModeError(Exception):
    pass


def _normalize(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def load_bulk_file(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(path)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        raise BulkModeError(f"Unsupported file type: {ext}")

    # Match required columns leniently -- ignoring case and extra/leading/
    # trailing whitespace -- since a hand-typed header like "id" or
    # "Define The Problem " should still work.
    normalized_to_actual = {_normalize(c): c for c in df.columns}
    rename_map = {}
    missing = []
    for required in REQUIRED_BULK_COLUMNS:
        actual = normalized_to_actual.get(_normalize(required))
        if actual is None:
            missing.append(required)
        elif actual != required:
            rename_map[actual] = required

    if missing:
        raise BulkModeError(
            f"Bulk file is missing required columns: {missing}. "
            f"Columns found in your file: {list(df.columns)}. "
            f"Column names must match (case/spacing is flexible) -- "
            f"use the provided template to be sure."
        )

    if rename_map:
        df = df.rename(columns=rename_map)

    if "Status" not in df.columns:
        df["Status"] = ""

    return df


def create_bulk_template(path: str):
    """Writes a ready-to-fill template with the exact expected headers plus
    one example row, so the column names are guaranteed to match."""
    df = pd.DataFrame([
        {
            "ID": "8431321",
            "Define the problem": "Patient refused to complete HD treatment time.",
            "Why 1": "Because patient wanted to take less than 4 hours.",
            "Status": "",
        }
    ])
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df.to_csv(path, index=False)
    else:
        df.to_excel(path, index=False)


def save_bulk_file(df: pd.DataFrame, path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df.to_csv(path, index=False)
    else:
        df.to_excel(path, index=False)


def run_bulk_mode(df: pd.DataFrame, path: str, browser, find_record_by_id,
                   logger, stop_flag, test_mode=False, review_queue=None,
                   review_response=None):
    """
    find_record_by_id: callable(record_id) -> row element or None
    browser: BrowserController instance (already attached)
    stop_flag: callable() -> bool, checked between records so Pause/Stop works
    test_mode: if True, fills fields then waits for a Save/Skip decision via
               review_queue/review_response (same "confirm_save" message
               shape used by the web-scrape Test Mode) before saving.
    """
    processed = 0
    failed = 0
    skipped = 0

    for idx, row in df.iterrows():
        if stop_flag():
            logger("Bulk mode stopped by user.", "warn")
            break

        if str(row.get("Status", "")).strip().lower() == "done":
            continue  # already processed in a previous run

        record_id = str(row["ID"]).strip()
        define_text = str(row["Define the problem"]).strip()
        why1_text = str(row["Why 1"]).strip()

        try:
            row_element = find_record_by_id(record_id)
            if row_element is None:
                raise BulkModeError(f"Record ID {record_id} not found on page")

            browser.open_record(row_element)
            browser.fill_define_problem(define_text)
            browser.fill_why1(why1_text)

            if test_mode and review_queue is not None and review_response is not None:
                review_queue.put({
                    "kind": "confirm_save",
                    "record_id": record_id,
                    "event_code": "(bulk upload)",
                    "rule_id": "",
                    "matched_keywords": [],
                    "define_text": define_text,
                    "causes": [why1_text] if why1_text else [],
                })
                answer = None
                while not stop_flag():
                    try:
                        answer = review_response.get(timeout=1)
                        break
                    except queue.Empty:
                        continue

                if answer is None or answer.get("action") == "skip":
                    df.at[idx, "Status"] = "Skipped (test mode)"
                    skipped += 1
                    logger(f"[Bulk Test Mode] ID {record_id}: skipped, not saved.", "warn")
                    save_bulk_file(df, path)
                    continue

                final_define = answer.get("define_text", define_text)
                final_causes = answer.get("causes", [why1_text])
                browser.fill_define_problem(final_define)
                if final_causes:
                    browser.fill_why1(final_causes[0])
                browser.click_save()
                df.at[idx, "Status"] = "Done"
                processed += 1
                logger(f"[Bulk Test Mode] ID {record_id}: saved successfully.", "success")
            else:
                browser.click_save()
                df.at[idx, "Status"] = "Done"
                processed += 1
                logger(f"[Bulk] ID {record_id}: saved successfully.", "success")

        except Exception as exc:
            df.at[idx, "Status"] = f"Failed: {exc}"
            failed += 1
            logger(f"[Bulk] ID {record_id}: FAILED - {exc}", "error")

        # Persist progress after every row so a crash never loses work
        save_bulk_file(df, path)

    return {"processed": processed, "failed": failed, "skipped": skipped}
