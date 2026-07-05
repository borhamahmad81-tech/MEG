# Clinical Safety Event Assistant

A desktop RPA assistant that reads safety-event records from your clinical
quality-management website, applies your "Master_Rules_Matrix_Final.xlsx"
rules, and fills in the "Define the problem" / "Why 1" fields automatically
-- with a hard safety block on anything flagged **Serious**, and a
human-in-the-loop fallback whenever it isn't confident.

## 1a. "Could not attach to browser" / "Unable to obtain driver"
Selenium normally auto-downloads the matching Chrome/Edge driver the first
time it needs it. If the machine has no internet access to Microsoft's/
Google's driver servers (common on locked-down clinic machines) or a
firewall blocks it, this download fails and you'll see an error like
`Unable to obtain driver for MicrosoftEdge`.

**Fix**: download the matching driver manually once, then point the app at
it using the new **"Driver Path"** field (Logic Control panel, just below
Debug Port):
- Edge: https://developer.microsoft.com/microsoft-edge/tools/webdriver/
  (match your Edge version -- check `edge://version`)
- Chrome: https://googlechromelabs.github.io/chrome-for-testing/
  (match your Chrome version -- check `chrome://version`)

Unzip it anywhere (e.g. `C:\WebDrivers\msedgedriver.exe`) and use "Browse"
to select it. Leave it blank to keep using auto-download.

## 2. How it connects to the browser
This app **attaches to a browser window you already have open** (Chrome or
Edge) instead of launching its own -- so you log in and navigate normally,
then hit Start. To allow the attach, the browser must be started with a
debug port open first:

- Double-click `launch_chrome_debug.bat` (or `launch_edge_debug.bat`)
- This opens a *separate* browser window/profile with remote debugging on
  port 9222 -- it does not touch your normal browser profile.
- Log into the clinical system and navigate to the records table in that
  window.
- Then open the Assistant and click **Start Assistant**.

You can also just launch Chrome/Edge yourself from a shortcut with
`--remote-debugging-port=9222` if you'd rather use your normal profile.

## 3. Selector setup (MEG / audits.megsupporttools.com)
All record-page fields are confirmed against real saved pages (Django form,
stable `id_...` attributes), including a live open "Analysis / Investigation"
record: `id_aor_code`, `id_detailed_aor_description`, `id_min_level_of_harm`,
`id_define_the_problem`, `id_why_did_this_problem_occur_why_1..5` (all real
`<textarea>` fields), and the Save button
(`#observation-form button.btn-success[type='submit']`).

**Safety Event Owner**: `id_aor_owner` is a Select2 "search as you type"
widget, not a plain dropdown. The app checks the current value before every
save and, if it isn't already set to the name configured in "Safety Event
Owner" (default `Ahmed Mohamed`), types the name and picks the matching
result automatically. Change the name in the GUI's "MEG Site Settings"
panel if needed.

**Confirmed workflow controls** (real `name=` attributes from a saved live
record page): `launch_analysis_tool` (radio: 5 Whys / Ishikawa / London
Protocol / None) is what actually reveals the Define/Why fields -- not the
hidden field found earlier. For each Why filled in, the app also sets
`is_this_the_root_cause_of_the_problem_{n}` = Yes, then before saving sets
`has_the_analysis_for_this_event_been_completed` = Yes and, in the Closing
section, `are_further_actions_required` = No. Lessons Learned is left
untouched, as instructed.

The app opens records by **direct URL** instead of clicking the list icon:
```
https://audits.megsupporttools.com/audit_builder/{country}/edit/{form_id}/observation/{record_id}/
```
Set your country segment and form ID once in the GUI (defaults are
pre-filled from the sample pages: `saudi-arabia` / `7608`).

## 4. The rules matrix
`Master_Rules_Matrix_Final.xlsx` must keep this schema (one row per Event_Code):

| Rule_ID | Event_Code | Official_Description | Keywords_List | Define_The_Problem_Output | Why_Option_1 | Why_Option_2 | Why_Option_3 | Why_Option_4 | Why_Option_5 | Current_Pointer |

- `Keywords_List` is comma-separated; matching is case-insensitive.
- `Current_Pointer` is updated automatically by Round-Robin mode -- don't
  edit it by hand while the app is running.

## 5. Modes
- **Web-Scrape & Auto-Analyze**: the full automated pass described above.
- **Bulk Excel Upload**: load a file with columns `ID, Define the problem,
  Why 1`; the app injects those values directly (no keyword matching) and
  writes a `Status` column (`Done` / `Failed: ...`) back to the same file.

## 6. Test Mode -- two stages
Turn on **Test Mode** in the Logic Control panel, then pick a stage:

- **Stage 1 (single record)**: opens the first eligible record from the
  queue, fills in Define/Why (and the Safety Event Owner) on the real page,
  then stops and shows a review window -- no timer, take as long as you
  need. Choose **Save Record** or **Skip**, and either way the app stops
  right there instead of moving to the next record. Use this to confirm the
  app can open a record from the queue and fill it correctly, one record at
  a time, with zero risk of it running away.
- **Stage 2 (full queue)**: same per-record review window, but after each
  Save/Skip the app automatically opens the **next** eligible record in the
  queue and repeats, until no safety events remain -- then shows a
  **Final Report** (a popup plus a console summary) with totals for
  Processed / Deferred / Skipped / Blocked. Use this once Stage 1 looks
  correct, to confirm the full save-and-continue loop and the queue-end
  behavior end to end.

A Final Report is also shown at the end of a normal (non-Test-Mode) run.

## 7. Multiple Why causes
"Number of Causes to Inject" (1-5) and "Selection Mode" (Round-Robin /
Random) together control how many `Why_Option` values get pulled from the
matched row and which ones. Round-Robin cycles through a row's non-empty
Why options in order (remembering position via `Current_Pointer`, tracked
per subcategory row); Random picks that many at random each time. Each
selected cause is typed into its own Why field (Why 1, Why 2, ...).

## 8. Human-in-the-Loop pop-up
If a record's Event_Code has no rule, or the free-text description doesn't
match any keyword for that code, a pop-up opens with a 120-second countdown
(configurable). Answer it and the app saves and continues. If it times out,
the record is logged to a Deferred Queue and retried automatically in the
**Review Phase** after the main table finishes.

## 9. Running from source
```bash
pip install -r requirements.txt
python main.py
```

## 10. Building the EXE (no install required for end users)
Push to `main` and GitHub Actions builds `SafetyEventAssistant.exe`
automatically (see `.github/workflows/build.yml`). Download it from the
Actions run's **Artifacts** section. No admin rights are needed to run it --
it's a single portable `.exe`.

> Note: because `.github` is a hidden folder, uploading this project through
> GitHub's web UI can silently drop it. Push with **GitHub Desktop** or
> `git` from the command line instead.

## 11. Files
```
main.py                    entry point
gui_app.py                 CustomTkinter UI
automation_worker.py       background thread: safety filter, keyword engine, timeout logic
rules_engine.py            loads/queries Master_Rules_Matrix_Final.xlsx
browser_controller.py      Selenium attach + page interactions
bulk_mode.py                Bulk Excel/CSV upload mode
deferred_queue.py          persisted deferred-record queue
settings_manager.py        persisted JSON settings (settings.json)
config/selectors.json      CSS/XPath map to your site's real DOM
launch_chrome_debug.bat    opens Chrome with the debug port needed to attach
launch_edge_debug.bat      opens Edge with the debug port needed to attach
.github/workflows/build.yml cloud EXE build pipeline
```
