"""
main.py
=======
Entry point for the Repush Tool. Double-click "Run Repush Tool.bat" or run:

    python main.py

It checks that psycopg2 is installed (showing a friendly message if not) and
then opens the window defined in app.py.
"""

import sys
import tkinter as tk
from tkinter import messagebox


def main():
    # Check the database driver before importing anything that needs it.
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "psycopg2 is not installed.\n\nOpen PowerShell and run:\n"
            "    pip install psycopg2-binary\n\nThen start the app again.",
        )
        sys.exit(1)

    from app import App  # imported here so the driver check runs first
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
