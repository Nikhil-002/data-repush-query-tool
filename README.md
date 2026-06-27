# Meter Query Tools

Internal desktop tools (Python + Tkinter) for working with the meter blockload
Postgres database.

## Tools

### Repush Tool (`Repush Tool/`)
Finds meter blockload rows that breached the SLA, backs them up, fixes their
`createdat`, and repushes them. Four-step workflow with progress reporting and
batched updates. See [`Repush Tool/README.txt`](Repush%20Tool/README.txt) for
full details.

Run: double-click `Repush Tool/Run Repush Tool.bat` (or `python main.py`).

### Meter Query Tool (`meter_query_tool.py`)
Standalone meter query utility. Run: double-click `Run Meter Query Tool.bat`.

## Requirements
```
pip install psycopg2-binary    # database access (both tools)
pip install openpyxl           # only needed for .xlsx meter-number uploads
```

## Notes
- Settings files (`*_config.json`) are **git-ignored** because they can contain
  database passwords. Each user keeps their own locally.
- Meter data files (`*.csv`, `*.xlsx`) are git-ignored too — they hold meter
  numbers and should not be committed.
