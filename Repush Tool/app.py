"""
app.py
======
The Tkinter window. This file is only about the SCREEN: laying out widgets,
reacting to button clicks, and showing results. All the heavy lifting is
delegated:

  * parsing.py  - read dates and meter-number files
  * queries.py  - build the SQL
  * db_ops.py   - actually talk to the database (on a background thread)
  * config.py   - defaults + load/save settings

Workflow (one button per step):
  1. Check   - count rows that breached the SLA
  2. Backup  - copy those rows (sequenceid, meternumber, rtcdateat, createdat)
  3. Update  - fix createdat so the rows fall back inside the SLA
  4. Repush  - insert the fixed rows into the repush table
"""

import os
import threading
import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import config
from parsing import parse_dt, load_meter_numbers
from queries import build_breach_where, build_temp1_where
import db_ops


class App:
    def __init__(self, root):
        self.root = root
        root.title("Repush Tool - Meter Blockload SLA Fix")
        root.geometry("780x900")
        root.minsize(700, 780)

        self.vars = {}
        self.breach = None            # (table, where_sql, params) from the last Check
        self.last_count = 0           # breach count from the last Check
        self.meter_values = None      # meter numbers loaded from a file
        self.backup_done = False
        self.update_done = False

        self._timer_on = False        # elapsed-time ticker state
        self._elapsed = 0
        self._action = ""             # label used in progress log lines

        self._build_ui()
        self._load_config()

    # ===================== UI construction =====================
    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.tab_run = ttk.Frame(nb)
        self.tab_setup = ttk.Frame(nb)
        nb.add(self.tab_run, text="  Run  ")
        nb.add(self.tab_setup, text="  Setup  ")

        self._build_setup(self.tab_setup)
        self._build_run(self.tab_run)

    def _build_setup(self, parent):
        pad = {"padx": 8, "pady": 4}

        conn = ttk.LabelFrame(parent, text="Database connection")
        conn.pack(fill="x", **pad)
        self._row(conn, "Host", "host", 0, default="localhost")
        self._row(conn, "Port", "port", 1, default="5432", width=10)
        self._row(conn, "Database", "dbname", 2)
        self._row(conn, "User", "user", 3)
        self._row(conn, "Password", "password", 4, show="*")
        self.vars["remember_pw"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn, text="Remember password",
                        variable=self.vars["remember_pw"]).grid(
            row=5, column=1, sticky="w", padx=8, pady=2)
        conn.columnconfigure(1, weight=1)

        tgt = ttk.LabelFrame(parent, text="Target tables")
        tgt.pack(fill="x", **pad)
        self._build_main_table_row(tgt, 0)
        self._row(tgt, "Backup table (temp2)", "backup_table", 1,
                  default=config.DEFAULT_BACKUP)
        self.vars["create_backup"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(tgt, text="Create backup table if it doesn't exist",
                        variable=self.vars["create_backup"]).grid(
            row=2, column=1, sticky="w", padx=8, pady=2)
        self._row(tgt, "temp1 table (repush source)", "temp1_table", 3,
                  default=config.DEFAULT_TEMP1)
        self.vars["create_temp1"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(tgt, text="Create temp1 table if it doesn't exist",
                        variable=self.vars["create_temp1"]).grid(
            row=4, column=1, sticky="w", padx=8, pady=2)
        # Repush + settings tables are fixed - shown for reference, not editable.
        self._row(tgt, "Repush table (by sequence)", "repush_table", 5,
                  default=config.DEFAULT_REPUSH, state="readonly")
        self._row(tgt, "Settings table", "settings_table", 6,
                  default=config.DEFAULT_SETTINGS, state="readonly")
        tgt.columnconfigure(1, weight=1)

        ttk.Button(parent, text="Save settings",
                   command=self._save_config).pack(anchor="w", padx=12, pady=8)
        ttk.Label(parent, foreground="#888", wraplength=680, justify="left",
                  text=("Connection + targets are saved to repush_config.json "
                        "next to this app. Password is only saved if 'Remember "
                        "password' is ticked.")).pack(anchor="w", padx=12)

    def _build_main_table_row(self, parent, row):
        """Main table as a profile dropdown (LS/DP/...) mapped to the DB table."""
        ttk.Label(parent, text="Main table").grid(row=row, column=0, sticky="e",
                                                   padx=8, pady=4)
        self.vars["profile"] = tk.StringVar(value=config.DEFAULT_PROFILE)
        self.vars["table"] = tk.StringVar(value=config.DEFAULT_TABLE)
        mainf = ttk.Frame(parent)
        mainf.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        self.profile_combo = ttk.Combobox(
            mainf, textvariable=self.vars["profile"], state="readonly", width=10,
            values=list(config.PROFILE_TABLES.keys()))
        self.profile_combo.pack(side="left")
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_change)
        # read-only label so the operator can see the real table the profile maps to
        ttk.Label(mainf, textvariable=self.vars["table"],
                  foreground="#555").pack(side="left", padx=10)

    def _on_profile_change(self, _event=None):
        table = config.PROFILE_TABLES.get(self.vars["profile"].get())
        if table:
            self.vars["table"].set(table)

    def _build_run(self, parent):
        pad = {"padx": 8, "pady": 4}

        params = ttk.LabelFrame(parent, text="Run parameters (this RTC day)")
        params.pack(fill="x", **pad)
        self._row(params, "Project ID", "projectid", 0, default="1", width=10)
        self._row(params, "RTC from  >=", "rtc_from", 1)
        self._row(params, "RTC to    <=", "rtc_to", 2)
        self._row(params, "SLA hours", "sla_hours", 3,
                  default=config.DEFAULT_SLA_HOURS, width=10)

        self._build_cutoff_row(params)
        self._build_created_range_row(params, 5)

        self._row(params, "Createdat from >= (temp1)", "created_from", 6)
        self._row(params, "Createdat to   <= (temp1)", "created_to", 7)
        ttk.Label(params, foreground="#888", wraplength=680, justify="left",
                  text=("^ Createdat from/to select the rows from "
                        "meter_blockloadprofile that are inserted into temp1 "
                        "(step 4 repush source: in-SLA + just-fixed rows).")).grid(
            row=8, column=1, sticky="w", padx=8)

        self._build_meter_row(params)

        ttk.Label(params, foreground="#888",
                  text="Dates: YYYY-MM-DD HH:MM").grid(
            row=10, column=1, sticky="w", padx=8)
        params.columnconfigure(1, weight=1)

        rp = ttk.LabelFrame(parent, text="Repush settings (step 4)")
        rp.pack(fill="x", **pad)
        self._row(rp, "Profile name", "profilename", 0,
                  default=config.DEFAULT_PROFILENAME, width=24)
        ttk.Label(rp, foreground="#888", wraplength=680, justify="left",
                  text=("Written to data_repush_settings.profilename. The "
                        "datarepushid is taken automatically from "
                        "data_repush_settings (a row is created if none "
                        "exists), so you don't enter it.")).grid(
            row=1, column=1, sticky="w", padx=8)
        rp.columnconfigure(1, weight=1)

        self._build_workflow_buttons(parent)
        self._build_result(parent)
        self._build_progress(parent)
        self._build_log(parent)

    def _build_cutoff_row(self, parent):
        """The auto-computed 'createdat > cutoff' deadline (rtc_to + SLA hours)."""
        cut = ttk.Frame(parent)
        cut.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4)
        ttk.Label(cut, text="createdat > cutoff").grid(row=0, column=0,
                                                        sticky="e", padx=4)
        self.vars["created_cutoff"] = tk.StringVar()
        self.cutoff_entry = ttk.Entry(cut, textvariable=self.vars["created_cutoff"],
                                      state="readonly")
        self.cutoff_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.vars["manual_cutoff"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(cut, text="set manually",
                        variable=self.vars["manual_cutoff"],
                        command=self._toggle_cutoff).grid(row=0, column=2, padx=4)
        ttk.Button(cut, text="Recompute", command=self._recompute_cutoff).grid(
            row=0, column=3, padx=4)
        cut.columnconfigure(1, weight=1)

    def _build_created_range_row(self, parent, row):
        """Optional, tickable createdat lower/upper bounds for step 1 (Check).

        When ticked, these REPLACE the 'createdat > cutoff' test (each bound is
        used only if filled). Untick to check on the cutoff alone - never both.
        """
        cr = ttk.Frame(parent)
        cr.grid(row=row, column=0, columnspan=2, sticky="ew", padx=4)
        self.vars["use_created_range"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(cr, text="Also limit createdat (step 1)",
                        variable=self.vars["use_created_range"],
                        command=self._toggle_created_range).grid(
            row=0, column=0, padx=4)
        ttk.Label(cr, text="createdat >").grid(row=0, column=1, sticky="e", padx=2)
        self.vars["breach_created_gt"] = tk.StringVar()
        self.breach_gt_entry = ttk.Entry(
            cr, textvariable=self.vars["breach_created_gt"], width=18)
        self.breach_gt_entry.grid(row=0, column=2, padx=2)
        ttk.Label(cr, text="createdat <").grid(row=0, column=3, sticky="e", padx=2)
        self.vars["breach_created_lt"] = tk.StringVar()
        self.breach_lt_entry = ttk.Entry(
            cr, textvariable=self.vars["breach_created_lt"], width=18)
        self.breach_lt_entry.grid(row=0, column=4, padx=2)

    def _build_meter_row(self, parent):
        """Restrict to meternumbers - either from a DB table or an uploaded file."""
        mf = ttk.Frame(parent)
        mf.grid(row=9, column=0, columnspan=2, sticky="ew", padx=4)
        self.vars["use_meter"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(mf, text="Restrict to meternumbers",
                        variable=self.vars["use_meter"],
                        command=self._toggle_meter).grid(row=0, column=0, padx=4)

        self.vars["meter_source"] = tk.StringVar(value=config.DEFAULT_METER_SOURCE)
        ttk.Radiobutton(mf, text="from table", variable=self.vars["meter_source"],
                        value="table", command=self._toggle_meter).grid(
            row=0, column=1, padx=4)
        ttk.Radiobutton(mf, text="from file", variable=self.vars["meter_source"],
                        value="file", command=self._toggle_meter).grid(
            row=0, column=2, padx=4)

        ttk.Label(mf, text="Table:").grid(row=1, column=0, sticky="e", padx=4)
        self.vars["meter_table"] = tk.StringVar(value=config.DEFAULT_METER_TABLE)
        self.meter_entry = ttk.Entry(mf, textvariable=self.vars["meter_table"],
                                     width=18)
        self.meter_entry.grid(row=1, column=1, padx=2, sticky="w")
        self.load_file_btn = ttk.Button(mf, text="Load file...",
                                        command=self._load_meter_file)
        self.load_file_btn.grid(row=1, column=2, padx=2, sticky="w")
        self.meter_file_label = ttk.Label(mf, text="No file selected",
                                          foreground="#555")
        self.meter_file_label.grid(row=1, column=3, sticky="w", padx=4)

    def _build_workflow_buttons(self, parent):
        wf = ttk.LabelFrame(parent, text="Workflow")
        wf.pack(fill="x", padx=8, pady=4)
        self.check_btn = ttk.Button(wf, text="1.  Check SLA breaches",
                                    command=self.do_check)
        self.check_btn.pack(side="left", padx=8, pady=8)
        self.backup_btn = ttk.Button(wf, text="2.  Backup breaching rows",
                                     command=self.do_backup, state="disabled")
        self.backup_btn.pack(side="left", padx=8, pady=8)
        self.update_btn = ttk.Button(wf, text="3.  Update createdat for breaches",
                                     command=self.do_update, state="disabled")
        self.update_btn.pack(side="left", padx=8, pady=8)
        self.repush_btn = ttk.Button(wf, text="4.  Repush breaching rows",
                                     command=self.do_repush)
        self.repush_btn.pack(side="left", padx=8, pady=8)

    def _build_result(self, parent):
        res = ttk.Frame(parent)
        res.pack(fill="x", padx=8, pady=4)
        ttk.Label(res, text="Breaches:").pack(side="left", padx=8)
        self.result_var = tk.StringVar(value="-")
        ttk.Label(res, textvariable=self.result_var,
                  font=("", 22, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(res, textvariable=self.status_var,
                  foreground="#0a7").pack(side="left", padx=16)

    def _build_progress(self, parent):
        """Progress bar + a live elapsed-time / 'done out of total' readout."""
        pf = ttk.Frame(parent)
        pf.pack(fill="x", padx=8, pady=2)
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.elapsed_var = tk.StringVar(value="")
        ttk.Label(pf, textvariable=self.elapsed_var, width=14).pack(
            side="left", padx=6)
        self.progress_var = tk.StringVar(value="")
        ttk.Label(pf, textvariable=self.progress_var, width=22).pack(
            side="left", padx=6)

    def _build_log(self, parent):
        logf = ttk.LabelFrame(parent, text="SQL / log")
        logf.pack(fill="both", expand=True, padx=8, pady=4)
        self.log_text = tk.Text(logf, height=14, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _row(self, parent, label, key, row, default="", show=None, width=None,
             state=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e",
                                           padx=8, pady=4)
        self.vars[key] = tk.StringVar(value=default)
        kw = {"textvariable": self.vars[key]}
        if show:
            kw["show"] = show
        if width:
            kw["width"] = width
        e = ttk.Entry(parent, **kw)
        if state:
            e.configure(state=state)
        e.grid(row=row, column=1, sticky="ew" if not width else "w",
               padx=8, pady=4)

    # ===================== small helpers =====================
    def _toggle_meter(self):
        enabled = self.vars["use_meter"].get()
        source = self.vars["meter_source"].get()
        self.meter_entry.configure(
            state="normal" if enabled and source == "table" else "disabled")
        self.load_file_btn.configure(
            state="normal" if enabled and source == "file" else "disabled")

    def _load_meter_file(self):
        path = filedialog.askopenfilename(
            title="Select meter number file",
            filetypes=[
                ("CSV files", "*.csv"),
                ("Text files", "*.txt"),
                ("Excel files", "*.xlsx *.xls"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            values = load_meter_numbers(path)
        except Exception as e:
            messagebox.showerror("Failed to load meter file", str(e))
            return
        self.meter_values = values
        self.meter_file_label.configure(text=f"Loaded {len(values):,} meters")
        self.log(f"Loaded {len(values):,} meter numbers from "
                 f"{os.path.basename(path)}")

    def _toggle_cutoff(self):
        if self.vars["manual_cutoff"].get():
            self.cutoff_entry.configure(state="normal")
        else:
            self.cutoff_entry.configure(state="readonly")
            self._recompute_cutoff()

    def _toggle_created_range(self):
        state = "normal" if self.vars["use_created_range"].get() else "disabled"
        self.breach_gt_entry.configure(state=state)
        self.breach_lt_entry.configure(state=state)

    def _recompute_cutoff(self):
        if self.vars["manual_cutoff"].get():
            return
        try:
            rtc_to = parse_dt(self.vars["rtc_to"].get())
            hours = float(self.vars["sla_hours"].get() or config.DEFAULT_SLA_HOURS)
        except ValueError:
            return
        cutoff = rtc_to + dt.timedelta(hours=hours)
        self.cutoff_entry.configure(state="normal")
        self.vars["created_cutoff"].set(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
        self.cutoff_entry.configure(state="readonly")

    def log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    # ----- progress bar + elapsed timer -----
    def _start_timer(self):
        self._elapsed = 0
        self._timer_on = True
        self._tick()

    def _tick(self):
        if not self._timer_on:
            return
        m, s = divmod(self._elapsed, 60)
        self.elapsed_var.set(f"elapsed {m}:{s:02d}")
        self._elapsed += 1
        self.root.after(1000, self._tick)

    def _begin_indeterminate(self, action):
        """For Check: a single long query with no measurable progress."""
        self._action = action
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_var.set("running in background...")
        self._start_timer()

    def _end_progress(self):
        self._timer_on = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if self.progress["maximum"]:
            self.progress.configure(value=self.progress["maximum"])
        self.progress_var.set("")

    def _sql_cb(self):
        """Thread-safe callback: db_ops sends the exact SQL here to be logged."""
        return lambda text: self.root.after(0, self._log_sql_text, text)

    def _log_sql_text(self, text):
        self.log("  SQL executed:")
        for line in text.splitlines():
            self.log("    " + line)

    # ===================== input validation =====================
    def _validate(self):
        projectid = self.vars["projectid"].get().strip()
        if not projectid:
            raise ValueError("Project ID is required.")
        rtc_from = parse_dt(self.vars["rtc_from"].get())
        rtc_to = parse_dt(self.vars["rtc_to"].get())
        if rtc_from > rtc_to:
            raise ValueError("RTC 'from' is after RTC 'to'.")
        self._recompute_cutoff()
        cutoff = self.vars["created_cutoff"].get().strip()
        # cutoff is only needed when NOT using the tickable createdat range.
        if not self.vars["use_created_range"].get():
            if not cutoff:
                raise ValueError("createdat cutoff is empty - check RTC-to / SLA.")
            parse_dt(cutoff)
        return projectid, rtc_from, rtc_to, cutoff

    def _breach_where(self, projectid, rtc_from, rtc_to, cutoff):
        use_range = self.vars["use_created_range"].get()
        created_gt = created_lt = None
        if use_range:
            gt = self.vars["breach_created_gt"].get().strip()
            lt = self.vars["breach_created_lt"].get().strip()
            created_gt = parse_dt(gt) if gt else None
            created_lt = parse_dt(lt) if lt else None
            if created_gt is None and created_lt is None:
                raise ValueError(
                    "'Also limit createdat (step 1)' is ticked but no createdat "
                    "bound is set. Fill '>' and/or '<', or untick to use cutoff.")
            if (created_gt is not None and created_lt is not None
                    and created_gt >= created_lt):
                raise ValueError("createdat '>' bound is not before the '<' bound.")
        return build_breach_where(
            projectid, rtc_from, rtc_to,
            cutoff=None if use_range else cutoff,   # range mode replaces cutoff
            use_meter=self.vars["use_meter"].get(),
            meter_source=self.vars["meter_source"].get(),
            meter_table=self.vars["meter_table"].get().strip(),
            meter_values=self.meter_values,
            created_gt=created_gt, created_lt=created_lt,
        )

    def _temp1_where(self):
        """Build the temp1 WHERE (step 4) from the current Run parameters."""
        projectid = self.vars["projectid"].get().strip()
        if not projectid:
            raise ValueError("Project ID is required.")
        rtc_from = parse_dt(self.vars["rtc_from"].get())
        rtc_to = parse_dt(self.vars["rtc_to"].get())
        if rtc_from > rtc_to:
            raise ValueError("RTC 'from' is after RTC 'to'.")
        created_from = parse_dt(self.vars["created_from"].get())
        created_to = parse_dt(self.vars["created_to"].get())
        if created_from > created_to:
            raise ValueError("Createdat 'from' is after Createdat 'to'.")
        return build_temp1_where(
            projectid, rtc_from, rtc_to, created_from, created_to,
            use_meter=self.vars["use_meter"].get(),
            meter_source=self.vars["meter_source"].get(),
            meter_table=self.vars["meter_table"].get().strip(),
            meter_values=self.meter_values,
        )

    def _conn_kw(self):
        return {
            "host": self.vars["host"].get().strip() or "localhost",
            "port": self.vars["port"].get().strip() or "5432",
            "dbname": self.vars["dbname"].get().strip(),
            "user": self.vars["user"].get().strip(),
            "password": self.vars["password"].get(),
        }

    def _disable_all(self):
        for b in (self.check_btn, self.backup_btn, self.update_btn, self.repush_btn):
            b.configure(state="disabled")

    def _restore_buttons(self):
        """
        Re-enable buttons based on what is possible right now (called after every
        step finishes or errors, so a failed step can be retried):
          * Check  - always.
          * Backup - once Check (this session) found breaches.
          * Update - once Backup ran this session.
          * Repush - always: it reads temp2 + the settings table directly, so it
                     can run on its own (e.g. to retry after an error or after a
                     restart) without redoing Check/Backup/Update.
        """
        self.check_btn.configure(state="normal")
        self.backup_btn.configure(
            state="normal" if (self.breach and self.last_count) else "disabled")
        self.update_btn.configure(state="normal" if self.backup_done else "disabled")
        self.repush_btn.configure(state="normal")

    # ===================== step 1: CHECK =====================
    def do_check(self):
        try:
            pid, rf, rt, cutoff = self._validate()
            where, params, _ = self._breach_where(pid, rf, rt, cutoff)
        except ValueError as e:
            messagebox.showwarning("Check inputs", str(e))
            return

        tbl = self.vars["table"].get().strip() or config.DEFAULT_TABLE
        self.log("-" * 60)
        self.log("CHECK: counting SLA breaches...")
        self._disable_all()
        self.status_var.set("Checking... (this can take several minutes)")
        self.result_var.set("...")
        self.breach = (tbl, where, params)
        self.backup_done = False
        self.update_done = False
        self._begin_indeterminate("CHECK")

        threading.Thread(target=self._do_check,
                         args=(self._conn_kw(), tbl, where, params),
                         daemon=True).start()

    def _do_check(self, conn_kw, table, where, params):
        try:
            count = db_ops.run_check(conn_kw, table, where, params,
                                     sql_cb=self._sql_cb())
            self.root.after(0, self._check_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, str(e))

    def _check_done(self, count):
        self._end_progress()
        self.last_count = count
        self.result_var.set(f"{count:,}")
        self.log(f"  -> {count:,} breaching record(s) (counted in {self._elapsed}s).")
        if count:
            self.status_var.set("Breaches found - run backup first.")
        else:
            self.status_var.set("No breaches. Nothing to fix.")
        self._restore_buttons()

    # ===================== step 2: BACKUP =====================
    def do_backup(self):
        if not self.breach:
            messagebox.showinfo("Nothing to do", "Run Check first.")
            return
        backup = self.vars["backup_table"].get().strip() or config.DEFAULT_BACKUP
        if not messagebox.askyesno(
                "Confirm backup",
                f"This will back up {self.last_count:,} breaching row(s) "
                f"into '{backup}'.\n\nProceed?"):
            return

        table, where, params = self.breach
        self._disable_all()
        self.status_var.set("Backing up...")
        self.log("-" * 60)
        self.log("BACKUP: copying breaching rows into the backup table...")
        self._begin_indeterminate("BACKUP")
        threading.Thread(
            target=self._do_backup,
            args=(self._conn_kw(), table, backup, where, params,
                  self.vars["create_backup"].get()),
            daemon=True).start()

    def _do_backup(self, conn_kw, table, backup, where, params, create_backup):
        try:
            count = db_ops.run_backup(conn_kw, table, backup, where, params,
                                      create_backup=create_backup,
                                      sql_cb=self._sql_cb())
            self.root.after(0, self._backup_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nBackup rolled back.")

    def _backup_done(self, count):
        self._end_progress()
        self.backup_done = True
        self.log(f"BACKUP: backed up {count:,} row(s) into the backup table "
                 "(truncated first).")
        self.status_var.set("Backup complete. Ready for update.")
        self._restore_buttons()

    # ===================== step 3: UPDATE =====================
    def do_update(self):
        if not self.backup_done:
            messagebox.showwarning("Backup required",
                                   "Run Check and Backup before updating createdat.")
            return
        if not messagebox.askyesno(
                "Confirm update",
                f"This will update createdat for the {self.last_count:,} backed-up "
                f"row(s).\n\nProceed?"):
            return

        table, where, params = self.breach
        backup = self.vars["backup_table"].get().strip() or config.DEFAULT_BACKUP
        self._disable_all()
        self.status_var.set("Updating createdat...")
        self.log("-" * 60)
        self.log("UPDATE: rewriting createdat on backed-up breaching rows...")
        self._begin_indeterminate("UPDATE")
        threading.Thread(
            target=self._do_update,
            args=(self._conn_kw(), table, backup, where, params),
            daemon=True).start()

    def _do_update(self, conn_kw, table, backup, where, params):
        try:
            count = db_ops.run_update(conn_kw, table, backup, where, params,
                                      sql_cb=self._sql_cb())
            self.root.after(0, self._update_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nUpdate rolled back.")

    def _update_done(self, count):
        self._end_progress()
        self.update_done = True
        self.log(f"UPDATE: updated createdat on {count:,} row(s).")
        self.status_var.set("Update complete. Ready for repush.")
        self._restore_buttons()

    # ===================== step 4: REPUSH =====================
    def do_repush(self):
        profilename = self.vars["profilename"].get().strip()
        if not profilename:
            messagebox.showwarning("Profile name required",
                                   "Enter a profile name (Repush settings) first.")
            return
        try:
            temp1_where, temp1_params, temp1_preview = self._temp1_where()
        except ValueError as e:
            messagebox.showwarning("Repush inputs", str(e))
            return

        projectid = self.vars["projectid"].get().strip()
        source = self.vars["table"].get().strip() or config.DEFAULT_TABLE
        temp1 = self.vars["temp1_table"].get().strip() or config.DEFAULT_TEMP1
        by_seq = self.vars["repush_table"].get().strip() or config.DEFAULT_REPUSH
        settings = self.vars["settings_table"].get().strip() or config.DEFAULT_SETTINGS
        create_temp1 = self.vars["create_temp1"].get()
        if not messagebox.askyesno(
                "Confirm repush",
                f"This will (re)build '{temp1}' from '{source}' where:\n"
                f"  {chr(10).join('    ' + p for p in temp1_preview)}\n\n"
                f"then TRUNCATE '{by_seq}', copy '{temp1}' into it, and update one "
                f"row in '{settings}'\n"
                f"(projectid={projectid}, profilename='{profilename}', "
                f"startdate/enddate = min/max(createdat) of '{temp1}', "
                f"datarepushid taken from '{settings}').\n\nProceed?"):
            return

        self._disable_all()
        self.status_var.set("Repushing...")
        self.log("-" * 60)
        self.log(f"REPUSH: building '{temp1}' from '{source}', truncating '{by_seq}', "
                 f"copying from '{temp1}', updating '{settings}'...")
        self._begin_indeterminate("REPUSH")
        threading.Thread(
            target=self._do_repush,
            args=(self._conn_kw(), source, temp1, by_seq, settings,
                  projectid, profilename, temp1_where, temp1_params, create_temp1),
            daemon=True).start()

    def _do_repush(self, conn_kw, source, temp1, by_seq, settings,
                   projectid, profilename, temp1_where, temp1_params, create_temp1):
        try:
            result = db_ops.run_repush(conn_kw, source, temp1, by_seq, settings,
                                       projectid, profilename,
                                       temp1_where, temp1_params,
                                       create_temp1=create_temp1,
                                       sql_cb=self._sql_cb())
            self.root.after(0, self._repush_done, result)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nRepush rolled back.")

    def _repush_done(self, result):
        self._end_progress()
        self.log(f"REPUSH: filled temp1 with {result['temp1_count']:,} row(s); "
                 f"inserted {result['inserted']:,} row(s) into the repush "
                 f"table with datarepushid = {result['datarepushid']}.")
        self.log(f"  settings window: startdate = {result['startdate']}, "
                 f"enddate = {result['enddate']}")
        self.log(f"  snapshot: appended {result['snapshot_count']:,} row(s) into "
                 f"'{result['snapshot_table']}'.")
        self.status_var.set("Repush complete.")
        self._restore_buttons()
        messagebox.showinfo(
            "Done",
            f"Repush complete: temp1 = {result['temp1_count']:,} rows, "
            f"repushed {result['inserted']:,} rows.\n"
            f"datarepushid = {result['datarepushid']}\n"
            f"startdate = {result['startdate']}\nenddate = {result['enddate']}\n"
            f"snapshot: {result['snapshot_count']:,} rows -> "
            f"{result['snapshot_table']}")

    # ===================== errors / config =====================
    def _on_error(self, msg):
        self._end_progress()
        self.status_var.set("Error.")
        self.log("ERROR: " + msg)
        self._restore_buttons()
        messagebox.showerror("Failed", msg)

    def _save_config(self):
        data = {k: v.get() for k, v in self.vars.items()
                if isinstance(v, (tk.StringVar, tk.BooleanVar))}
        if not self.vars["remember_pw"].get():
            data["password"] = ""
        try:
            config.save_config(data)
            messagebox.showinfo("Saved", f"Settings saved to:\n{config.CONFIG_PATH}")
        except OSError as e:
            messagebox.showerror("Save failed", str(e))

    def _load_config(self):
        data = config.load_config()
        for k, val in data.items():
            if k in self.vars:
                self.vars[k].set(val)
        self._on_profile_change()      # keep Main table in sync with the profile
        self._toggle_meter()
        self._toggle_cutoff()
        self._toggle_created_range()
