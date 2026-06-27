"""
Meter Blockload Profile Query Tool
----------------------------------
A small desktop app to run the repetitive count query against Postgres
without retyping it in TablePlus every time.

Lets you pick:
  - Project ID
  - RTC datetime range (rtcdateat)
  - Created datetime range (createdat)
  - Optional meternumber filter via a subquery table

Requires:  pip install psycopg2-binary
Run with:  python meter_query_tool.py
"""

import json
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:  # show a friendly message instead of a stack trace
    import sys
    import tkinter.messagebox as mb
    _r = tk.Tk()
    _r.withdraw()
    mb.showerror(
        "Missing dependency",
        "psycopg2 is not installed.\n\nOpen PowerShell and run:\n"
        "    pip install psycopg2-binary\n\nThen start the app again.",
    )
    sys.exit(1)


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "query_tool_config.json")

# Default table queried. Editable in the UI in case you reuse this for others.
DEFAULT_TABLE = "meter_blockloadprofile"
DT_HINT = "YYYY-MM-DD HH:MM   (leave blank to skip this bound)"


class App:
    def __init__(self, root):
        self.root = root
        root.title("Meter Blockload Profile - Count Tool")
        root.geometry("640x720")
        root.minsize(560, 640)

        self.vars = {}
        self._build_ui()
        self._load_config()

    # ----- UI -------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- Connection frame ---
        conn = ttk.LabelFrame(self.root, text="Database connection")
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

        # --- Query parameters frame ---
        params = ttk.LabelFrame(self.root, text="Query parameters")
        params.pack(fill="x", **pad)

        self._row(params, "Table", "table", 0, default=DEFAULT_TABLE)
        self._row(params, "Project ID", "projectid", 1, default="1", width=10)

        ttk.Label(params, text="RTC range (rtcdateat)",
                  font=("", 9, "bold")).grid(row=2, column=0, columnspan=2,
                                             sticky="w", padx=8, pady=(8, 0))
        self._row(params, "RTC from  >=", "rtc_from", 3)
        self._row(params, "RTC to    <=", "rtc_to", 4)

        ttk.Label(params, text="Created range (createdat)",
                  font=("", 9, "bold")).grid(row=5, column=0, columnspan=2,
                                             sticky="w", padx=8, pady=(8, 0))
        self._row(params, "Created from >=", "created_from", 6)
        self._row(params, "Created to   <=", "created_to", 7)

        ttk.Label(params, text=DT_HINT, foreground="#888").grid(
            row=8, column=1, sticky="w", padx=8)
        params.columnconfigure(1, weight=1)

        # --- Optional meternumber subquery ---
        mfilt = ttk.LabelFrame(self.root, text="Meter number filter (optional)")
        mfilt.pack(fill="x", **pad)

        self.vars["use_meter"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            mfilt,
            text="Restrict to meternumbers from a table",
            variable=self.vars["use_meter"],
            command=self._toggle_meter,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        ttk.Label(mfilt, text="Table").grid(row=1, column=0, sticky="e",
                                            padx=8, pady=4)
        self.vars["meter_table"] = tk.StringVar(value="june18")
        self.meter_entry = ttk.Entry(mfilt, textvariable=self.vars["meter_table"])
        self.meter_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        mfilt.columnconfigure(1, weight=1)
        self._toggle_meter()

        # --- Actions ---
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.run_btn = ttk.Button(actions, text="Run Count",
                                  command=self.run_count)
        self.run_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Save settings",
                   command=self._save_config).pack(side="left")

        # --- Result ---
        self.result_var = tk.StringVar(value="—")
        res = ttk.LabelFrame(self.root, text="Count")
        res.pack(fill="x", **pad)
        ttk.Label(res, textvariable=self.result_var,
                  font=("", 28, "bold")).pack(pady=10)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var,
                  foreground="#0a7").pack(fill="x", padx=12)

        # --- Generated SQL (for trust / copy into TablePlus) ---
        sqlf = ttk.LabelFrame(self.root, text="SQL executed")
        sqlf.pack(fill="both", expand=True, **pad)
        self.sql_text = tk.Text(sqlf, height=7, wrap="word")
        self.sql_text.pack(fill="both", expand=True, padx=6, pady=6)

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

    def _toggle_meter(self):
        state = "normal" if self.vars["use_meter"].get() else "disabled"
        self.meter_entry.configure(state=state)

    # ----- Query building -------------------------------------------------
    def _build_query(self):
        """Returns (composed_sql, params_list, preview_string)."""
        table = self.vars["table"].get().strip() or DEFAULT_TABLE
        projectid = self.vars["projectid"].get().strip()
        if not projectid:
            raise ValueError("Project ID is required.")

        where = [sql.SQL("projectid = %s")]
        params = [projectid]
        preview_parts = [f"projectid = {projectid}"]

        bounds = [
            ("rtcdateat", ">=", self.vars["rtc_from"].get().strip()),
            ("rtcdateat", "<=", self.vars["rtc_to"].get().strip()),
            ("createdat", ">=", self.vars["created_from"].get().strip()),
            ("createdat", "<=", self.vars["created_to"].get().strip()),
        ]
        for col, op, val in bounds:
            if val:
                where.append(sql.SQL("{} {} %s").format(
                    sql.Identifier(col), sql.SQL(op)))
                params.append(val)
                preview_parts.append(f"{col} {op} '{val}'")

        if self.vars["use_meter"].get():
            mtable = self.vars["meter_table"].get().strip()
            if not mtable:
                raise ValueError("Meter filter is enabled but table is empty.")
            where.append(sql.SQL(
                "meternumber in (select meternumber from {})").format(
                sql.Identifier(mtable)))
            preview_parts.append(
                f"meternumber in (select meternumber from {mtable})")

        query = sql.SQL("select count(*) from {} where {}").format(
            sql.Identifier(table),
            sql.SQL(" and ").join(where),
        )
        preview = (f"select count(*) from {table}\nwhere "
                   + "\n  and ".join(preview_parts))
        return query, params, preview

    # ----- Run ------------------------------------------------------------
    def run_count(self):
        try:
            query, params, preview = self._build_query()
        except ValueError as e:
            messagebox.showwarning("Check inputs", str(e))
            return

        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", preview)
        self.run_btn.configure(state="disabled")
        self.status_var.set("Running…")
        self.result_var.set("…")

        conn_kw = {
            "host": self.vars["host"].get().strip() or "localhost",
            "port": self.vars["port"].get().strip() or "5432",
            "dbname": self.vars["dbname"].get().strip(),
            "user": self.vars["user"].get().strip(),
            "password": self.vars["password"].get(),
        }
        threading.Thread(target=self._do_query,
                         args=(conn_kw, query, params), daemon=True).start()

    def _do_query(self, conn_kw, query, params):
        try:
            with psycopg2.connect(connect_timeout=10, **conn_kw) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    count = cur.fetchone()[0]
            self.root.after(0, self._on_success, count)
        except Exception as e:  # surface DB errors in the UI
            self.root.after(0, self._on_error, str(e))

    def _on_success(self, count):
        self.result_var.set(f"{count:,}")
        self.status_var.set("Done.")
        self.run_btn.configure(state="normal")

    def _on_error(self, msg):
        self.result_var.set("—")
        self.status_var.set("Error.")
        self.run_btn.configure(state="normal")
        messagebox.showerror("Query failed", msg)

    # ----- Config persistence --------------------------------------------
    def _save_config(self):
        data = {k: v.get() for k, v in self.vars.items()}
        if not self.vars["remember_pw"].get():
            data["password"] = ""
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.status_var.set(f"Settings saved to {CONFIG_PATH}")
        except OSError as e:
            messagebox.showerror("Save failed", str(e))

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for k, val in data.items():
            if k in self.vars:
                self.vars[k].set(val)
        self._toggle_meter()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
