Repush Tool
===========
Fixes meter blockload rows that breached the SLA, then repushes them - all
against Postgres, without having to create a meter table first.

HOW TO RUN
----------
Double-click "Run Repush Tool.bat"  (or run:  python main.py)

Requirements:
    pip install psycopg2-binary       (always needed)
    pip install openpyxl              (only if you load .xlsx meter files)


THE FOUR STEPS (one button each, run in order)
----------------------------------------------
1. CHECK   - counts the rows that breached the SLA and remembers their
             sequenceids. A row breaches when:
                 createdat > cutoff      (cutoff = rtc_to + SLA hours)
             i.e. the data arrived later than the SLA deadline.

2. BACKUP  - copies the breaching rows into the backup table, keeping:
                 sequenceid, meternumber, rtcdateat, createdat

3. UPDATE  - rewrites createdat on the breaching rows to
                 rtcdateat + a small random offset (1-5 min)
             so they fall back inside the SLA.

4. REPUSH  - inserts the fixed rows (sequenceid, meternumber, rtcdateat,
             createdat) into the repush table.

Each step is gated: Backup unlocks after Check, Update after Backup, Repush
after Update. Update and Repush ask for confirmation first.


PROGRESS ON LONG-RUNNING STEPS
------------------------------
These queries can take several minutes. The window shows what is happening:

  * CHECK  - a single query that can't be measured, so it shows an animated
             "running in background..." bar plus a live elapsed-time counter.
             The breach count appears as soon as the database returns.

  * BACKUP / UPDATE / REPUSH - these work off the captured seqid list, so they
             run in BATCHES of "Batch size" seqids (default 5000, set on the
             Setup tab). After each batch is committed the progress bar moves
             and a log line is printed, e.g.
                 UPDATE: 15,000 / 50,000 done.
             so you always know how far along it is. Because each batch is
             committed as it finishes, progress is saved as it goes.


METER NUMBERS: TABLE OR FILE
----------------------------
Tick "Restrict to meternumbers" and choose:
  * from table - reads meternumbers from a DB table you name (e.g. june18)
  * from file  - upload a .csv / .txt / .xlsx / .xls list (first column).
                 No DB table needed.


FILE LAYOUT (each file does one job)
------------------------------------
  main.py      - entry point + psycopg2 dependency check
  app.py       - the window (widgets + button handlers); delegates everything
  config.py    - default table names + load/save of repush_config.json
  parsing.py   - parse dates + read meter-number files (csv/txt/xlsx)
  queries.py   - all the SQL statements, in one place
  db_ops.py    - the only module that connects to the database

Settings are saved to repush_config.json next to these files. The password is
only saved if "Remember password" is ticked.
