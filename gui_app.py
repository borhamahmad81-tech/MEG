"""
gui_app.py
-----------
CustomTkinter desktop UI. Talks to AutomationWorker through queues polled
via `after()` so the Tk main loop never blocks and the worker thread never
touches Tk widgets directly.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from automation_worker import AutomationWorker
from bulk_mode import load_bulk_file, save_bulk_file, run_bulk_mode, create_bulk_template
from browser_controller import BrowserController
from rules_engine import RulesEngine
from settings_manager import SettingsManager

LOG_COLORS = {
    "info": "#D0D0D0",
    "success": "#43C463",
    "warn": "#F2C744",
    "error": "#F25555",
    "report": "#5AB8F2",
}

POLL_MS = 150


class SafetyEventAssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.settings = SettingsManager()
        ctk.set_appearance_mode(self.settings.get("theme", "dark"))
        ctk.set_default_color_theme("blue")

        self.title("Clinical Safety Event Assistant")
        self.geometry(self.settings.get("window_geometry", "1100x750"))
        self.minsize(1050, 700)

        self.worker = None
        self.log_queue = queue.Queue()
        self.review_queue = queue.Queue()
        self.review_response = queue.Queue()
        self.status_queue = queue.Queue()

        self.bulk_df = None
        self.bulk_path = None
        self.bulk_stop_flag = threading.Event()
        self.bulk_thread = None

        self._active_modal = None

        self._build_layout()
        self._poll_queues()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================== #
    # Layout
    # ================================================================== #
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=3)  # tabs get most of the space
        self.grid_rowconfigure(2, weight=2)  # console gets the rest

        self._build_mode_tabs()
        self._build_execution_center()
        self._build_console()
        self._build_status_bar()

    # ------------------------------------------------------------------ #
    def _build_mode_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))
        self.tabs.add("Web-Scrape & Auto-Analyze")
        self.tabs.add("Bulk Excel Upload")

        self._build_webscrape_tab(self.tabs.tab("Web-Scrape & Auto-Analyze"))
        self._build_bulk_tab(self.tabs.tab("Bulk Excel Upload"))

    # ------------------------------------------------------------------ #
    def _build_webscrape_tab(self, parent):
        parent.grid_columnconfigure((0, 1), weight=1)
        parent.grid_rowconfigure(0, weight=1)

        # ---- Logic Control Panel (left) ----
        logic_frame = ctk.CTkScrollableFrame(parent)
        logic_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)

        ctk.CTkLabel(logic_frame, text="Logic Control", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        ctk.CTkLabel(logic_frame, text="Selection Mode").pack(anchor="w", padx=12)
        self.mode_var = tk.StringVar(value=self.settings.get("selection_mode", "round_robin"))
        mode_menu = ctk.CTkOptionMenu(
            logic_frame,
            values=["round_robin", "random"],
            variable=self.mode_var,
            command=lambda v: self.settings.set("selection_mode", v),
        )
        mode_menu.pack(anchor="w", padx=12, pady=(0, 10), fill="x")

        ctk.CTkLabel(logic_frame, text="Number of Causes to Inject").pack(anchor="w", padx=12)
        self.causes_var = tk.IntVar(value=self.settings.get("causes_count", 1))
        causes_spin = ctk.CTkFrame(logic_frame, fg_color="transparent")
        causes_spin.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        ctk.CTkButton(causes_spin, text="-", width=32, command=self._dec_causes).pack(side="left")
        self.causes_entry = ctk.CTkEntry(causes_spin, width=50, textvariable=self.causes_var, justify="center")
        self.causes_entry.pack(side="left", padx=6)
        ctk.CTkButton(causes_spin, text="+", width=32, command=self._inc_causes).pack(side="left")

        ctk.CTkLabel(logic_frame, text="Codes to Process").pack(anchor="w", padx=12, pady=(4, 0))
        self.code_filter_var = tk.StringVar(value=self.settings.get("code_filter_mode", "all"))
        code_radio_frame = ctk.CTkFrame(logic_frame, fg_color="transparent")
        code_radio_frame.pack(anchor="w", padx=12, pady=(2, 4), fill="x")
        ctk.CTkRadioButton(
            code_radio_frame, text="All codes", variable=self.code_filter_var, value="all",
            command=self._on_code_filter_change,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkRadioButton(
            code_radio_frame, text="Some codes", variable=self.code_filter_var, value="selected",
            command=self._on_code_filter_change,
        ).pack(side="left")
        self.choose_codes_btn = ctk.CTkButton(
            logic_frame, text="Choose Codes...", command=self._open_code_picker
        )
        self.choose_codes_btn.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        self._update_choose_codes_btn_state()

        self.test_mode_var = tk.BooleanVar(value=self.settings.get("test_mode", False))
        ctk.CTkCheckBox(
            logic_frame, text="Test Mode (review every record before saving)",
            variable=self.test_mode_var,
            command=self._on_test_mode_change,
        ).pack(anchor="w", padx=12, pady=(4, 4))

        self.test_stage_var = tk.StringVar(value=self.settings.get("test_stage", "single"))
        self.test_stage_frame = ctk.CTkFrame(logic_frame, fg_color="transparent")
        self.test_stage_frame.pack(anchor="w", padx=24, pady=(0, 10), fill="x")
        ctk.CTkRadioButton(
            self.test_stage_frame, text="Stage 1: open + fill ONE record, stop before saving next",
            variable=self.test_stage_var, value="single",
            command=lambda: self.settings.set("test_stage", self.test_stage_var.get()),
        ).pack(anchor="w", pady=2)
        ctk.CTkRadioButton(
            self.test_stage_frame, text="Stage 2: save & continue through the whole queue, then final report",
            variable=self.test_stage_var, value="full",
            command=lambda: self.settings.set("test_stage", self.test_stage_var.get()),
        ).pack(anchor="w", pady=2)
        self._update_test_stage_visibility()

        ctk.CTkLabel(logic_frame, text="Browser").pack(anchor="w", padx=12)
        self.browser_var = tk.StringVar(value=self.settings.get("browser", "chrome"))
        ctk.CTkOptionMenu(
            logic_frame, values=["chrome", "edge"], variable=self.browser_var,
            command=lambda v: self.settings.set("browser", v),
        ).pack(anchor="w", padx=12, pady=(0, 10), fill="x")

        ctk.CTkLabel(logic_frame, text="Debug Port").pack(anchor="w", padx=12)
        self.port_var = tk.StringVar(value=str(self.settings.get("debug_port", 9222)))
        port_entry = ctk.CTkEntry(logic_frame, textvariable=self.port_var)
        port_entry.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        port_entry.bind("<FocusOut>", lambda e: self._save_port())

        ctk.CTkLabel(logic_frame, text="Driver Path (optional -- only needed if "
                                        "auto-download fails)").pack(anchor="w", padx=12)
        driver_path_frame = ctk.CTkFrame(logic_frame, fg_color="transparent")
        driver_path_frame.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        self.driver_path_var = tk.StringVar(value=self.settings.get("driver_path", ""))
        driver_path_entry = ctk.CTkEntry(driver_path_frame, textvariable=self.driver_path_var)
        driver_path_entry.pack(side="left", fill="x", expand=True)
        driver_path_entry.bind(
            "<FocusOut>", lambda e: self.settings.set("driver_path", self.driver_path_var.get().strip())
        )
        ctk.CTkButton(driver_path_frame, text="Browse", width=70,
                      command=self._choose_driver_path).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(logic_frame, text="Review Pop-up Timeout (seconds)").pack(anchor="w", padx=12)
        self.timeout_var = tk.StringVar(value=str(self.settings.get("timeout_seconds", 120)))
        timeout_entry = ctk.CTkEntry(logic_frame, textvariable=self.timeout_var)
        timeout_entry.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        timeout_entry.bind("<FocusOut>", lambda e: self._save_timeout())

        launch_frame = ctk.CTkFrame(logic_frame, fg_color="transparent")
        launch_frame.pack(anchor="w", padx=12, pady=(4, 10), fill="x")
        ctk.CTkButton(launch_frame, text="Launch Chrome (debug)",
                      command=lambda: self._launch_browser_debug("chrome")).pack(fill="x", pady=2)
        ctk.CTkButton(launch_frame, text="Launch Edge (debug)",
                      command=lambda: self._launch_browser_debug("edge")).pack(fill="x", pady=2)

        # ---- File Inputs Section (right) ----
        file_frame = ctk.CTkFrame(parent)
        file_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=6)

        ctk.CTkLabel(file_frame, text="Configuration Files", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        ctk.CTkButton(file_frame, text="Load Master Rules Matrix File",
                      command=self._choose_rules_matrix).pack(padx=12, pady=(6, 2), fill="x")
        self.rules_path_label = ctk.CTkLabel(
            file_frame, text=self.settings.resolve_path("rules_matrix_path"),
            wraplength=380, justify="left", text_color="#9A9A9A",
        )
        self.rules_path_label.pack(padx=12, pady=(0, 10), anchor="w")

        ctk.CTkLabel(file_frame, text="MEG Site Settings", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self.base_url_var = tk.StringVar(value=self.settings.get("site_base_url"))
        self._labeled_entry(file_frame, "Base URL", self.base_url_var, "site_base_url")
        self.country_var = tk.StringVar(value=self.settings.get("site_country"))
        self._labeled_entry(file_frame, "Country segment", self.country_var, "site_country")
        self.form_id_var = tk.StringVar(value=self.settings.get("site_form_id"))
        self._labeled_entry(file_frame, "Form ID", self.form_id_var, "site_form_id")
        self.owner_name_var = tk.StringVar(value=self.settings.get("safety_event_owner_name"))
        self._labeled_entry(file_frame, "Safety Event Owner", self.owner_name_var, "safety_event_owner_name")

        ctk.CTkButton(file_frame, text="Edit Selectors Config (config/selectors.json)",
                      command=self._open_selectors_folder).pack(padx=12, pady=(6, 2), fill="x")
        ctk.CTkLabel(
            file_frame,
            text=("Selectors map this app to the real clinical system's page.\n"
                  "Update config/selectors.json once, using your browser's\n"
                  "'Inspect Element' to copy the correct CSS selectors."),
            wraplength=380, justify="left", text_color="#9A9A9A",
        ).pack(padx=12, pady=(0, 10), anchor="w")

    def _labeled_entry(self, parent, label, var, settings_key):
        ctk.CTkLabel(parent, text=label).pack(anchor="w", padx=12)
        entry = ctk.CTkEntry(parent, textvariable=var)
        entry.pack(anchor="w", padx=12, pady=(0, 10), fill="x")
        entry.bind("<FocusOut>", lambda e: self.settings.set(settings_key, var.get().strip()))
        return entry

    def _on_code_filter_change(self):
        self.settings.set("code_filter_mode", self.code_filter_var.get())
        self._update_choose_codes_btn_state()
        if self.code_filter_var.get() == "selected" and not self.settings.get("selected_codes"):
            self._open_code_picker()

    def _update_choose_codes_btn_state(self):
        state = "normal" if self.code_filter_var.get() == "selected" else "disabled"
        self.choose_codes_btn.configure(state=state)

    def _open_code_picker(self):
        try:
            preview = RulesEngine(self.settings.resolve_path("rules_matrix_path"))
        except Exception as exc:
            messagebox.showerror("Could not load rules matrix", str(exc))
            return

        codes = preview.get_all_event_codes()
        currently_selected = set(self.settings.get("selected_codes", []))

        modal = ctk.CTkToplevel(self)
        modal.title("Choose Codes to Process")
        modal.geometry("360x520")
        modal.grab_set()

        ctk.CTkLabel(modal, text="Select the Event_Code(s) to process:",
                     font=ctk.CTkFont(weight="bold")).pack(padx=12, pady=(12, 4), anchor="w")

        scroll = ctk.CTkScrollableFrame(modal, width=320, height=380)
        scroll.pack(padx=12, pady=6, fill="both", expand=True)

        check_vars = {}
        for code in codes:
            var = tk.BooleanVar(value=(code in currently_selected) or not currently_selected)
            check_vars[code] = var
            ctk.CTkCheckBox(scroll, text=code, variable=var).pack(anchor="w", pady=2)

        def select_all(value):
            for v in check_vars.values():
                v.set(value)

        btn_row = ctk.CTkFrame(modal, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(btn_row, text="Select All", width=100,
                      command=lambda: select_all(True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row, text="Clear All", width=100,
                      command=lambda: select_all(False)).pack(side="left")

        def save_and_close():
            selected = [c for c, v in check_vars.items() if v.get()]
            self.settings.set("selected_codes", selected)
            modal.grab_release()
            modal.destroy()
            self._log(f"Code selection updated: {len(selected)} code(s) selected.", "info")

        ctk.CTkButton(modal, text="Save", fg_color="#2E8B57",
                      command=save_and_close).pack(padx=12, pady=10, fill="x")

    # ------------------------------------------------------------------ #
    def _on_test_mode_change(self):
        self.settings.set("test_mode", self.test_mode_var.get())
        self._update_test_stage_visibility()

    def _update_test_stage_visibility(self):
        for child in self.test_stage_frame.winfo_children():
            child.configure(state="normal" if self.test_mode_var.get() else "disabled")

    def _dec_causes(self):
        v = max(1, self.causes_var.get() - 1)
        self.causes_var.set(v)
        self.settings.set("causes_count", v)

    def _inc_causes(self):
        v = min(10, self.causes_var.get() + 1)
        self.causes_var.set(v)
        self.settings.set("causes_count", v)

    def _choose_driver_path(self):
        path = filedialog.askopenfilename(
            title="Select chromedriver.exe or msedgedriver.exe",
            filetypes=[("Driver executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.driver_path_var.set(path)
            self.settings.set("driver_path", path)

    def _save_port(self):
        try:
            self.settings.set("debug_port", int(self.port_var.get()))
        except ValueError:
            pass

    def _save_timeout(self):
        try:
            self.settings.set("timeout_seconds", int(self.timeout_var.get()))
        except ValueError:
            pass

    def _choose_rules_matrix(self):
        path = filedialog.askopenfilename(
            title="Select Master Rules Matrix",
            filetypes=[("Excel files", "*.xlsx")],
        )
        if path:
            self.settings.set("rules_matrix_path", path)
            self.rules_path_label.configure(text=path)

    def _open_selectors_folder(self):
        cfg_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
        os.makedirs(cfg_dir, exist_ok=True)
        try:
            os.startfile(cfg_dir)  # Windows
        except AttributeError:
            messagebox.showinfo("Selectors", f"Edit the file at:\n{cfg_dir}")

    def _find_browser_exe(self, browser):
        """Looks on PATH first, then the standard Windows install locations,
        then a saved custom path from a previous manual pick."""
        import shutil
        which_name = "chrome" if browser == "chrome" else "msedge"
        found = shutil.which(which_name)
        if found:
            return found

        saved = self.settings.get(f"{browser}_exe_path", "")
        if saved and os.path.exists(saved):
            return saved

        if browser == "chrome":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
        else:
            candidates = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _launch_browser_debug(self, browser):
        import subprocess
        port = self.settings.get("debug_port", 9222)
        user_data_dir = os.path.join(os.path.expanduser("~"), f".safety_assistant_{browser}_profile")

        exe = self._find_browser_exe(browser)
        if not exe:
            # Couldn't find it anywhere -- let the user browse for it once,
            # then remember that path for next time.
            picked = filedialog.askopenfilename(
                title=f"Locate {browser}.exe / {'msedge' if browser=='edge' else 'chrome'}.exe",
                filetypes=[("Browser executable", "*.exe"), ("All files", "*.*")],
            )
            if not picked:
                messagebox.showerror(
                    "Launch failed",
                    f"Could not find {browser.title()} automatically, and no file was selected. "
                    f"It's usually at C:\\Program Files\\{'Google\\Chrome' if browser=='chrome' else 'Microsoft\\Edge'}"
                    f"\\Application\\.",
                )
                return
            exe = picked
            self.settings.set(f"{browser}_exe_path", exe)

        try:
            subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={user_data_dir}"])
            self._log(f"Launching {browser.title()} ({exe}) with debug port {port}...", "info")
        except Exception as exc:
            messagebox.showerror(
                "Launch failed",
                f"Found {browser.title()} at:\n{exe}\nbut couldn't launch it.\n\n{exc}",
            )

    # ------------------------------------------------------------------ #
    def _build_bulk_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(parent, text="Bulk Excel/CSV Upload Mode", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4)
        )
        ctk.CTkLabel(
            parent,
            text="File must contain columns: ID, Define the problem, Why 1 "
                 "(matching is case/spacing-flexible)",
            text_color="#9A9A9A",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))

        ctk.CTkButton(parent, text="Download Template (.xlsx)",
                      command=self._download_bulk_template).grid(
            row=2, column=0, sticky="w", padx=12, pady=(0, 10)
        )

        ctk.CTkButton(parent, text="Load Bulk File (.xlsx / .csv)",
                      command=self._choose_bulk_file).grid(row=3, column=0, sticky="w", padx=12, pady=4)
        self.bulk_path_label = ctk.CTkLabel(parent, text="No file loaded", text_color="#9A9A9A")
        self.bulk_path_label.grid(row=4, column=0, sticky="w", padx=12, pady=(0, 10))

        self.bulk_test_mode_var = tk.BooleanVar(value=self.settings.get("bulk_test_mode", False))
        ctk.CTkCheckBox(
            parent, text="Test Mode (review each row before saving)",
            variable=self.bulk_test_mode_var,
            command=lambda: self.settings.set("bulk_test_mode", self.bulk_test_mode_var.get()),
        ).grid(row=5, column=0, sticky="w", padx=12, pady=(0, 10))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(row=6, column=0, sticky="w", padx=12, pady=10)
        self.bulk_start_btn = ctk.CTkButton(
            btn_frame, text="Start Bulk Upload", fg_color="#2E8B57", hover_color="#256e46",
            command=self._start_bulk_upload,
        )
        self.bulk_start_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame, text="Stop", fg_color="#B23A48", hover_color="#8f2e39",
            command=self._stop_bulk_upload,
        ).pack(side="left")

    def _download_bulk_template(self):
        path = filedialog.asksaveasfilename(
            title="Save Bulk Upload Template",
            defaultextension=".xlsx",
            filetypes=[("Excel file", "*.xlsx")],
            initialfile="bulk_upload_template.xlsx",
        )
        if not path:
            return
        try:
            create_bulk_template(path)
            self._log(f"Template saved to: {path}", "success")
            messagebox.showinfo("Template saved", f"Template saved to:\n{path}")
        except Exception as exc:
            messagebox.showerror("Could not save template", str(exc))

    def _choose_bulk_file(self):
        path = filedialog.askopenfilename(
            title="Select Bulk Upload File",
            filetypes=[("Excel/CSV", "*.xlsx *.xls *.csv")],
        )
        if not path:
            return
        try:
            self.bulk_df = load_bulk_file(path)
            self.bulk_path = path
            self.bulk_path_label.configure(text=f"{path}  ({len(self.bulk_df)} rows)")
            self._log(f"Loaded bulk file: {path}", "success")
        except Exception as exc:
            messagebox.showerror("Failed to load file", str(exc))

    def _start_bulk_upload(self):
        if self.bulk_df is None:
            messagebox.showwarning("No file", "Load a bulk Excel/CSV file first.")
            return

        try:
            browser = BrowserController(
                browser=self.settings.get("browser", "chrome"),
                debug_port=self.settings.get("debug_port", 9222),
                logger=self._log,
                driver_path=self.settings.get("driver_path", ""),
            )
            browser.attach()
        except Exception as exc:
            messagebox.showerror("Could not attach to browser", str(exc))
            return

        def find_by_id(record_id):
            for rec in browser.get_record_rows():
                if rec["record_id"] == record_id:
                    return rec["row"]
            return None

        self.bulk_stop_flag.clear()

        def worker():
            result = run_bulk_mode(
                self.bulk_df, self.bulk_path, browser, find_by_id,
                self._log, self.bulk_stop_flag.is_set,
                test_mode=self.settings.get("bulk_test_mode", False),
                review_queue=self.review_queue,
                review_response=self.review_response,
            )
            self._log(
                f"Bulk mode finished. Processed: {result['processed']} | "
                f"Skipped: {result.get('skipped', 0)} | Failed: {result['failed']}",
                "success",
            )

        self.bulk_thread = threading.Thread(target=worker, daemon=True)
        self.bulk_thread.start()
        self._log("Bulk upload started.", "info")

    def _stop_bulk_upload(self):
        self.bulk_stop_flag.set()

    # ------------------------------------------------------------------ #
    def _build_execution_center(self):
        exec_frame = ctk.CTkFrame(self)
        exec_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        exec_frame.grid_columnconfigure((0, 1), weight=1)

        self.start_btn = ctk.CTkButton(
            exec_frame, text="\u25B6  Start Assistant", height=44,
            fg_color="#2E8B57", hover_color="#256e46",
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_assistant,
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=6)

        self.stop_btn = ctk.CTkButton(
            exec_frame, text="\u25A0  Pause / Stop Safety Switch", height=44,
            fg_color="#B23A48", hover_color="#8f2e39",
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._pause_or_stop,
        )
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=6)

    # ------------------------------------------------------------------ #
    def _build_console(self):
        console_frame = ctk.CTkFrame(self)
        console_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        console_frame.grid_columnconfigure(0, weight=1)
        console_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(console_frame, text="Live Monitoring Console", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2)
        )

        self.console = ctk.CTkTextbox(console_frame, wrap="word")
        self.console.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self.console.configure(state="disabled")

        for level, color in LOG_COLORS.items():
            self.console.tag_config(level, foreground=color)

    # ------------------------------------------------------------------ #
    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, height=32)
        bar.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.status_label = ctk.CTkLabel(
            bar, text="Total Processed: 0 | Deferred: 0 | Blocked: 0", anchor="w"
        )
        self.status_label.pack(side="left", padx=10, pady=6)

    # ================================================================== #
    # Worker lifecycle
    # ================================================================== #
    def _start_assistant(self):
        if self.worker and self.worker.is_alive():
            self.worker.resume()
            self._log("Resumed.", "info")
            return

        try:
            self.settings.set("debug_port", int(self.port_var.get()))
            self.settings.set("timeout_seconds", int(self.timeout_var.get()))
        except ValueError:
            messagebox.showwarning("Invalid input", "Debug port and timeout must be numbers.")
            return

        self.worker = AutomationWorker(
            self.settings, self.log_queue, self.review_queue,
            self.review_response, self.status_queue,
        )
        self.worker.start()
        self._log("Assistant started.", "info")

    def _pause_or_stop(self):
        if not self.worker or not self.worker.is_alive():
            return
        if messagebox.askyesno("Stop", "Pause the automation now? Choose 'No' to fully stop instead.",
                                icon="warning"):
            self.worker.pause()
        else:
            self.worker.stop()
            self._log("Stopping...", "warn")

    # ================================================================== #
    # Queue polling (runs on the Tk main thread)
    # ================================================================== #
    def _poll_queues(self):
        while not self.log_queue.empty():
            message, level = self.log_queue.get()
            self._log(message, level)
            if level == "report":
                messagebox.showinfo("Final Report", message)

        while not self.status_queue.empty():
            counts = self.status_queue.get()
            self.status_label.configure(
                text=f"Total Processed: {counts.get('processed', 0)} | "
                     f"Deferred: {counts.get('deferred', 0)} | "
                     f"Skipped: {counts.get('skipped', 0)} | "
                     f"Blocked: {counts.get('blocked', 0)}"
            )

        if not self.review_queue.empty() and self._active_modal is None:
            request = self.review_queue.get()
            if request.get("kind") == "confirm_save":
                self._show_confirm_save_modal(request)
            else:
                self._show_review_modal(request)

        self.after(POLL_MS, self._poll_queues)

    def _log(self, message, level="info"):
        self.console.configure(state="normal")
        self.console.insert("end", message + "\n", level)
        self.console.see("end")
        self.console.configure(state="disabled")

    # ================================================================== #
    # Component D: Human-in-the-Loop modal with live countdown
    # ================================================================== #
    def _show_review_modal(self, request):
        modal = ctk.CTkToplevel(self)
        self._active_modal = modal
        modal.title("Manual Review Required")
        modal.geometry("480x420")
        modal.grab_set()
        modal.attributes("-topmost", True)
        self.bell()

        ctk.CTkLabel(
            modal, text=f"Record ID: {request['record_id']}",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(16, 4))
        ctk.CTkLabel(modal, text=f"Event Code: {request['event_code']}").pack()
        ctk.CTkLabel(
            modal, text=f"Free text: {request['free_text'][:150] or '(none)'}",
            wraplength=420, justify="left",
        ).pack(pady=(4, 12), padx=16)

        countdown_label = ctk.CTkLabel(modal, text="", font=ctk.CTkFont(size=20, weight="bold"))
        countdown_label.pack(pady=(0, 12))

        ctk.CTkLabel(modal, text="Define the problem").pack(anchor="w", padx=16)
        define_entry = ctk.CTkEntry(modal)
        define_entry.pack(fill="x", padx=16, pady=(0, 10))

        ctk.CTkLabel(modal, text="Why 1 (choose from matrix)").pack(anchor="w", padx=16)
        why_var = tk.StringVar(value=request["options"][0] if request["options"] else "")
        why_menu = ctk.CTkOptionMenu(modal, values=request["options"] or ["(no options available)"],
                                      variable=why_var)
        why_menu.pack(fill="x", padx=16, pady=(0, 16))

        result_holder = {"answered": False}

        def submit():
            result_holder["answered"] = True
            self.review_response.put({
                "define_text": define_entry.get().strip(),
                "why_text": why_var.get().strip(),
            })
            self._close_modal(modal)

        ctk.CTkButton(modal, text="Save & Continue", fg_color="#2E8B57",
                      command=submit).pack(pady=(0, 12))

        deadline = time.time() + request["timeout"]

        def tick():
            if result_holder["answered"] or self._active_modal is not modal:
                return
            remaining = int(deadline - time.time())
            if remaining <= 0:
                self.review_response.put(None)
                self._close_modal(modal)
                return
            mins, secs = divmod(remaining, 60)
            countdown_label.configure(text=f"{mins:02d}:{secs:02d} remaining")
            modal.after(500, tick)

        tick()

    # ================================================================== #
    # Test Mode: confirm-before-save modal (no countdown -- unlimited review time)
    # ================================================================== #
    def _show_confirm_save_modal(self, request):
        modal = ctk.CTkToplevel(self)
        self._active_modal = modal
        modal.title("Test Mode - Review Before Saving")
        modal.geometry("520x560")
        modal.grab_set()
        modal.attributes("-topmost", True)

        ctk.CTkLabel(
            modal, text=f"Record ID: {request['record_id']}  |  Code: {request['event_code']}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(pady=(14, 2), padx=16, anchor="w")
        ctk.CTkLabel(
            modal, text=f"Rule: {request['rule_id']}  |  Matched keywords: {request['matched_keywords']}",
            text_color="#9A9A9A", wraplength=480, justify="left",
        ).pack(padx=16, pady=(0, 10), anchor="w")

        ctk.CTkLabel(
            modal,
            text="These values were already typed into the live page below. "
                 "Review them there, edit here if needed, then choose Save or Skip.\n"
                 "Also auto-set: Analysis Tool = 5 Whys, \"Is this the root cause?\" = Yes "
                 "for each Why shown, \"Analysis completed?\" = Yes, "
                 "\"Further actions required?\" = No.",
            wraplength=480, justify="left",
        ).pack(padx=16, pady=(0, 10), anchor="w")

        ctk.CTkLabel(modal, text="Define the problem").pack(anchor="w", padx=16)
        define_box = ctk.CTkTextbox(modal, height=60)
        define_box.pack(fill="x", padx=16, pady=(0, 10))
        define_box.insert("1.0", request.get("define_text", ""))

        ctk.CTkLabel(modal, text="Why cause(s)").pack(anchor="w", padx=16)
        why_scroll = ctk.CTkScrollableFrame(modal, height=200)
        why_scroll.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        why_boxes = []
        for cause in request.get("causes", []):
            box = ctk.CTkTextbox(why_scroll, height=50)
            box.pack(fill="x", pady=4)
            box.insert("1.0", cause)
            why_boxes.append(box)
        if not why_boxes:
            ctk.CTkLabel(why_scroll, text="(no Why causes for this rule)",
                         text_color="#9A9A9A").pack(anchor="w")

        btn_row = ctk.CTkFrame(modal, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 14))

        def respond(action):
            self.review_response.put({
                "action": action,
                "define_text": define_box.get("1.0", "end").strip(),
                "causes": [b.get("1.0", "end").strip() for b in why_boxes],
            })
            self._close_modal(modal)

        ctk.CTkButton(btn_row, text="Save Record", fg_color="#2E8B57", hover_color="#256e46",
                      command=lambda: respond("save")).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(btn_row, text="Skip (Don't Save)", fg_color="#B23A48", hover_color="#8f2e39",
                      command=lambda: respond("skip")).pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _close_modal(self, modal):
        try:
            modal.grab_release()
            modal.destroy()
        except Exception:
            pass
        self._active_modal = None

    # ================================================================== #
    def _on_close(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
        self.settings.set("window_geometry", self.geometry())
        self.destroy()
