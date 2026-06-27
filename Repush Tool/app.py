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
from queries import build_breach_where
import db_ops


class App:
    def __init__(self, root):
        self.root = root
        root.title("Repush Tool - Meter Blockload SLA Fix")
        root.geometry("780x900")
        root.minsize(700, 780)

        self.vars = {}
        self.captured_seqids = None   # seqids from the last Check
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
        self._row(tgt, "Main table", "table", 0, default=config.DEFAULT_TABLE)
        self._row(tgt, "Backup table", "backup_table", 1,
                  default=config.DEFAULT_BACKUP)
        self.vars["create_backup"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(tgt, text="Create backup table if it doesn't exist",
                        variable=self.vars["create_backup"]).grid(
            row=2, column=1, sticky="w", padx=8, pady=2)
        self._row(tgt, "Repush table", "repush_table", 3,
                  default=config.DEFAULT_REPUSH)
        self._row(tgt, "Batch size", "batch_size", 4,
                  default=config.DEFAULT_BATCH_SIZE, width=10)
        tgt.columnconfigure(1, weight=1)

        ttk.Button(parent, text="Save settings",
                   command=self._save_config).pack(anchor="w", padx=12, pady=8)
        ttk.Label(parent, foreground="#888", wraplength=680, justify="left",
                  text=("Connection + targets are saved to repush_config.json "
                        "next to this app. Password is only saved if 'Remember "
                        "password' is ticked.")).pack(anchor="w", padx=12)

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
        self._build_meter_row(params)

        ttk.Label(params, foreground="#888",
                  text="Dates: YYYY-MM-DD HH:MM").grid(
            row=6, column=1, sticky="w", padx=8)
        params.columnconfigure(1, weight=1)

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

    def _build_meter_row(self, parent):
        """Restrict to meternumbers - either from a DB table or an uploaded file."""
        mf = ttk.Frame(parent)
        mf.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4)
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
                                     command=self.do_repush, state="disabled")
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

    def _row(self, parent, label, key, row, default="", show=None, width=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e",
                                           padx=8, pady=4)
        self.vars[key] = tk.StringVar(value=default)
        kw = {"textvariable": self.vars[key]}
        if show:
            kw["show"] = show
        if width:
            kw["width"] = width
        e = ttk.Entry(parent, **kw)
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

    def _begin_determinate(self, action, total):
        """For Backup/Update/Repush: progress measured against the seqid list."""
        self._action = action
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(total, 1), value=0)
        self.progress_var.set(f"0 / {total:,}")
        self._start_timer()

    def _on_progress(self, done, total):
        """Called (via root.after) after each committed batch."""
        self.progress.configure(value=done)
        self.progress_var.set(f"{done:,} / {total:,}")
        self.log(f"  {self._action}: {done:,} / {total:,} done.")

    def _end_progress(self):
        self._timer_on = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if self.progress["maximum"]:
            self.progress.configure(value=self.progress["maximum"])
        self.progress_var.set("")

    def _batch_size(self):
        try:
            size = int(self.vars["batch_size"].get())
        except (ValueError, KeyError):
            size = int(config.DEFAULT_BATCH_SIZE)
        return max(size, 1)

    def _progress_cb(self):
        """A thread-safe callback that marshals batch progress onto the UI."""
        return lambda done, total: self.root.after(0, self._on_progress, done, total)

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
        if not cutoff:
            raise ValueError("createdat cutoff is empty - check RTC-to / SLA.")
        parse_dt(cutoff)
        return projectid, rtc_from, rtc_to, cutoff

    def _breach_where(self, projectid, rtc_from, rtc_to, cutoff):
        return build_breach_where(
            projectid, rtc_from, rtc_to, cutoff,
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

    # ===================== step 1: CHECK =====================
    def do_check(self):
        try:
            pid, rf, rt, cutoff = self._validate()
            where, params, preview = self._breach_where(pid, rf, rt, cutoff)
        except ValueError as e:
            messagebox.showwarning("Check inputs", str(e))
            return

        tbl = self.vars["table"].get().strip() or config.DEFAULT_TABLE
        self.log("-" * 60)
        self.log("CHECK:")
        self.log(f"select sequenceid from {tbl}\nwhere " + "\n  and ".join(preview))
        self._disable_all()
        self.status_var.set("Checking... (this can take several minutes)")
        self.result_var.set("...")
        self.captured_seqids = None
        self.backup_done = False
        self.update_done = False
        self._begin_indeterminate("CHECK")

        threading.Thread(target=self._do_check,
                         args=(self._conn_kw(), tbl, where, params),
                         daemon=True).start()

    def _do_check(self, conn_kw, table, where, params):
        try:
            seqids = db_ops.run_check(conn_kw, table, where, params)
            self.root.after(0, self._check_done, seqids)
        except Exception as e:
            self.root.after(0, self._on_error, str(e))

    def _check_done(self, seqids):
        self._end_progress()
        self.captured_seqids = seqids
        self.result_var.set(f"{len(seqids):,}")
        self.check_btn.configure(state="normal")
        self.log(f"  -> {len(seqids):,} breaching record(s) "
                 f"(found in {self._elapsed}s).")
        if seqids:
            self.backup_btn.configure(state="normal")
            self.status_var.set("Breaches found - run backup first.")
        else:
            self.status_var.set("No breaches. Nothing to fix.")

    # ===================== step 2: BACKUP =====================
    def do_backup(self):
        if not self.captured_seqids:
            messagebox.showinfo("Nothing to do", "Run Check first.")
            return
        backup = self.vars["backup_table"].get().strip() or config.DEFAULT_BACKUP
        if not messagebox.askyesno(
                "Confirm backup",
                f"This will back up {len(self.captured_seqids):,} row(s) "
                f"into '{backup}'.\n\nProceed?"):
            return

        self._disable_all()
        self.status_var.set("Backing up...")
        self._begin_determinate("BACKUP", len(self.captured_seqids))
        threading.Thread(
            target=self._do_backup,
            args=(self._conn_kw(),
                  self.vars["table"].get().strip() or config.DEFAULT_TABLE,
                  backup,
                  list(self.captured_seqids),
                  self.vars["create_backup"].get(),
                  self._batch_size()),
            daemon=True).start()

    def _do_backup(self, conn_kw, table, backup, seqids, create_backup, batch_size):
        try:
            count = db_ops.run_backup(conn_kw, table, backup, seqids,
                                      create_backup=create_backup,
                                      batch_size=batch_size,
                                      progress_cb=self._progress_cb())
            self.root.after(0, self._backup_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nBackup rolled back.")

    def _backup_done(self, count):
        self._end_progress()
        self.backup_done = True
        self.log(f"BACKUP: backed up {count:,} row(s).")
        self.status_var.set("Backup complete. Ready for update.")
        self.check_btn.configure(state="normal")
        self.backup_btn.configure(state="normal")
        self.update_btn.configure(state="normal")

    # ===================== step 3: UPDATE =====================
    def do_update(self):
        if not self.captured_seqids:
            messagebox.showinfo("Nothing to do", "Run Check first.")
            return
        if not self.backup_done:
            messagebox.showwarning("Backup required",
                                   "Run backup before updating createdat.")
            return
        if not messagebox.askyesno(
                "Confirm update",
                f"This will update createdat for {len(self.captured_seqids):,} "
                f"row(s).\n\nProceed?"):
            return

        self._disable_all()
        self.status_var.set("Updating createdat...")
        self._begin_determinate("UPDATE", len(self.captured_seqids))
        threading.Thread(
            target=self._do_update,
            args=(self._conn_kw(),
                  self.vars["table"].get().strip() or config.DEFAULT_TABLE,
                  list(self.captured_seqids),
                  self._batch_size()),
            daemon=True).start()

    def _do_update(self, conn_kw, table, seqids, batch_size):
        try:
            count = db_ops.run_update(conn_kw, table, seqids,
                                      batch_size=batch_size,
                                      progress_cb=self._progress_cb())
            self.root.after(0, self._update_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nUpdate rolled back.")

    def _update_done(self, count):
        self._end_progress()
        self.update_done = True
        self.log(f"UPDATE: updated createdat on {count:,} row(s).")
        self.status_var.set("Update complete. Ready for repush.")
        self.check_btn.configure(state="normal")
        self.update_btn.configure(state="normal")
        self.repush_btn.configure(state="normal")

    # ===================== step 4: REPUSH =====================
    def do_repush(self):
        if not self.captured_seqids:
            messagebox.showinfo("Nothing to do", "Run Check first.")
            return
        if not self.update_done:
            messagebox.showwarning("Update required",
                                   "Run update before repushing.")
            return
        repush = self.vars["repush_table"].get().strip() or config.DEFAULT_REPUSH
        if not messagebox.askyesno(
                "Confirm repush",
                f"This will insert {len(self.captured_seqids):,} row(s) into "
                f"'{repush}'.\n\nProceed?"):
            return

        self._disable_all()
        self.status_var.set("Repushing...")
        self._begin_determinate("REPUSH", len(self.captured_seqids))
        threading.Thread(
            target=self._do_repush,
            args=(self._conn_kw(),
                  self.vars["table"].get().strip() or config.DEFAULT_TABLE,
                  repush,
                  list(self.captured_seqids),
                  self._batch_size()),
            daemon=True).start()

    def _do_repush(self, conn_kw, table, repush, seqids, batch_size):
        try:
            count = db_ops.run_repush(conn_kw, table, repush, seqids,
                                      batch_size=batch_size,
                                      progress_cb=self._progress_cb())
            self.root.after(0, self._repush_done, count)
        except Exception as e:
            self.root.after(0, self._on_error, f"{e}\n\nRepush rolled back.")

    def _repush_done(self, count):
        self._end_progress()
        self.log(f"REPUSH: inserted {count:,} row(s) into repush table.")
        self.status_var.set("Repush complete.")
        self.check_btn.configure(state="normal")
        self.repush_btn.configure(state="normal")
        messagebox.showinfo("Done", f"Repush complete: {count:,} rows.")

    # ===================== errors / config =====================
    def _on_error(self, msg):
        self._end_progress()
        self.status_var.set("Error.")
        self.check_btn.configure(state="normal")
        self.log("ERROR: " + msg)
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
        self._toggle_meter()
        self._toggle_cutoff()
