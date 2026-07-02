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
1. CHECK   - COUNTS the rows that breached the SLA (it does not pull the rows
             back). Two mutually-exclusive createdat modes:
               * default (tick OFF): createdat > cutoff
                 (cutoff = rtc_to + SLA hours) - the data arrived too late.
               * tick "Also limit createdat (step 1)" ON: the cutoff test is
                 DROPPED and replaced by your own createdat > / createdat <
                 bounds (each used only if filled). At least one is required.

2. BACKUP  - TRUNCATES the backup table (so it holds only this run) and copies
             the breaching rows in, keeping:
                 sequenceid, meternumber, rtcdateat, createdat

3. UPDATE  - rewrites createdat on the breaching rows to
                 rtcdateat + a small random offset (1-5 min)
             so they fall back inside the SLA. The update uses the indexed
             breach predicates (projectid / rtcdateat / createdat / meternumber)
             for speed AND restricts to "sequenceid in (select sequenceid from
             <backup>)" so it can only touch rows that were actually backed up -
             a row that arrives between Backup and Update is never modified.

4. REPUSH  - one transaction that, in order:
               a. (re)builds temp1 - the FULL good dataset to repush. Unlike
                  temp2 (only the LATE rows), temp1 is every packet for the meter
                  list in the RTC window whose createdat lands inside the
                  Createdat window:
                      projectid = <Run param>
                      rtcdateat BETWEEN <RTC from> AND <RTC to>
                      createdat BETWEEN <Createdat from> AND <Createdat to>
                      meternumber in (select meternumber from <meter table>)
                  i.e. the rows that were already in-SLA (LS) PLUS the ones
                  step 3 just pulled back inside SLA (their rewritten createdat
                  now falls in this window). temp1 is created-if-missing then
                  TRUNCATED so it holds only this run;
               b. TRUNCATES the per-sequence repush table
                  (data_repush_settings_by_sequence);
               c. makes sure data_repush_settings has exactly ONE row and takes
                  its datarepushid (creates a row if the table is empty; if it
                  has several, keeps the greatest id and deletes the rest) - so
                  you DON'T type the datarepushid;
               d. copies temp1 into the per-sequence table, stamping that
                  datarepushid onto every row (on conflict do nothing);
               e. reads min(createdat)/max(createdat) back from temp1;
               f. updates the single data_repush_settings row:
                  projectid (from Run params), profilename (Repush settings box),
                  startdate = min, enddate = max, minsequenceid = 0,
                  maxsequenceid = 0, isspecificseqrepush = true;
               g. APPENDS the per-sequence rows into a per-day snapshot table
                  named repush_DDMM, where DD/MM is the day+month of the EARLIEST
                  rtcdateat (e.g. a window starting 17 Jun -> repush_1706). The
                  table is created if missing; re-running the same day appends.
             You enter the Profile name + the Createdat window; the rest is
             derived. (temp1 reads the main table directly, so run Update first
             so the just-fixed createdats are reflected.)

Each step is gated: Backup unlocks after Check, Update after Backup. Repush can
run on its own (it rebuilds temp1 from the current Run parameters). Backup,
Update and Repush ask for confirmation first.

Every button prints the EXACT SQL it runs into the "SQL / log" panel (rendered
with the real table names and parameter values already filled in), so you can
confirm each statement before trusting the result.


PROGRESS ON LONG-RUNNING STEPS
------------------------------
These queries can take several minutes. All four steps run as single set-based
statements (or one short transaction), so each shows an animated "running in
background..." bar plus a live elapsed-time counter; the result (count / rows
affected / datarepushid + window) appears when the database returns.


TABLE NAMES / "relation does not exist"
---------------------------------------
If Check fails with: relation "<name>" does not exist - the table isn't found
in the database/schema you are connected to. Check:
  * the Database / User on the Setup tab point at the right DB
  * the Main table name is spelled correctly
  * if the table lives in a schema, qualify it, e.g.  public.meter_blockloadprofile
    (a dotted name is quoted per-part: "public"."meter_blockloadprofile")
The same applies to the Backup, Repush and meter Table names.


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
