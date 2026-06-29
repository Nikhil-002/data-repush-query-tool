"""
queries.py
==========
SQL builders only - every function returns a psycopg2 ``sql.SQL`` object and
does NOT touch the database. Keeping the SQL here (and away from both the UI and
the connection code) means you can read the exact statements in one place.

The four workflow steps map onto these builders:

  Step 1 CHECK   -> build_breach_where + build_count_query           (count only)
  Step 2 BACKUP  -> build_create_backup_table + build_truncate
                    + build_backup_insert      (temp2 holds ONLY this run)
  Step 3 UPDATE  -> build_update_createdat
  Step 4 REPUSH  -> build_truncate (by_sequence) + settings-row helpers
                    + build_repush_from_backup + build_minmax_createdat
                    + build_settings_update

Table names may be schema-qualified (e.g. "public.meter_blockloadprofile");
_ident() splits on the dot so each part is quoted correctly.
"""

from psycopg2 import sql


def _ident(name):
    """Build an SQL identifier, supporting schema-qualified names ('schema.tbl')."""
    parts = [p for p in str(name).split(".") if p]
    if not parts:
        raise ValueError("Empty table/identifier name.")
    return sql.Identifier(*parts)


# The "fix" applied to createdat in step 3: rtcdateat plus a small random
# offset (1-5 min, random sec/ms/us) so the row falls back inside the SLA.
_FIX_CREATEDAT = (
    "rtcdateat "
    "+ (INTERVAL '1 minute'      * (FLOOR(RANDOM() * 5) + 1)) "
    "+ (INTERVAL '1 second'      * FLOOR(RANDOM() * 60)) "
    "+ (INTERVAL '1 millisecond' * FLOOR(RANDOM() * 1000)) "
    "+ (INTERVAL '1 microsecond' * (FLOOR(RANDOM() * 999) + 1))"
)


# ----------------------------- generic helpers -----------------------------
def build_truncate(table):
    """Empty a table fast (transactional in Postgres)."""
    return sql.SQL("truncate table {}").format(_ident(table))


def build_count_rows(table):
    """Count all rows in a table (no filter)."""
    return sql.SQL("select count(*) from {}").format(_ident(table))


def build_minmax_createdat(table):
    """Return (min(createdat), max(createdat)) for a table."""
    return sql.SQL("select min(createdat), max(createdat) from {}").format(
        _ident(table))


# ------------------------------- step 1: CHECK ------------------------------
def build_breach_where(projectid, rtc_from, rtc_to, cutoff,
                       use_meter=False, meter_source="table",
                       meter_table=None, meter_values=None):
    """
    Build the WHERE clause that identifies SLA-breaching rows.

    A row breaches when its createdat is LATER than the cutoff deadline
    (cutoff = rtc_to + SLA hours), i.e. the data arrived too late.

    Returns (where_clause_sql, params, preview_text_lines).
    """
    if not projectid:
        raise ValueError("Project ID is required.")
    if rtc_from > rtc_to:
        raise ValueError("RTC from is after RTC to.")

    where = [sql.SQL("projectid = %s"),
             sql.SQL("rtcdateat >= %s"),
             sql.SQL("rtcdateat <= %s"),
             sql.SQL("createdat > %s")]
    params = [projectid, rtc_from, rtc_to, cutoff]
    preview = [f"projectid = {projectid}",
               f"rtcdateat >= '{rtc_from}'",
               f"rtcdateat <= '{rtc_to}'",
               f"createdat > '{cutoff}'"]

    if use_meter:
        if meter_source == "table":
            if not meter_table:
                raise ValueError("Meter filter enabled but meter table is empty.")
            where.append(sql.SQL(
                "meternumber in (select meternumber from {})").format(
                    _ident(meter_table)))
            preview.append(
                f"meternumber in (select meternumber from {meter_table})")
        elif meter_source == "file":
            if not meter_values:
                raise ValueError("Meter filter enabled but no meter numbers loaded.")
            where.append(sql.SQL("meternumber = any(%s)"))
            params.append(meter_values)
            preview.append(f"meternumber = any(<{len(meter_values)} meter numbers>)")
        else:
            raise ValueError("Unknown meter filter source.")

    return sql.SQL(" and ").join(where), params, preview


def build_count_query(table, where_clause):
    """Step 1: COUNT the breaching rows (fast - does not return the rows)."""
    return sql.SQL("select count(*) from {} where {}").format(
        _ident(table), where_clause)


# ------------------------------- step 2: BACKUP -----------------------------
def build_create_backup_table(backup_table, source_table):
    """Step 2a: create an empty backup table with the 4 columns we keep."""
    return sql.SQL(
        "create table if not exists {} as "
        "select sequenceid, meternumber, rtcdateat, createdat "
        "from {} where false"
    ).format(_ident(backup_table), _ident(source_table))


def build_backup_insert(backup_table, source_table, where_clause):
    """Step 2b: copy the breaching rows into the (freshly truncated) backup table."""
    return sql.SQL(
        "insert into {} (sequenceid, meternumber, rtcdateat, createdat) "
        "select sequenceid, meternumber, rtcdateat, createdat "
        "from {} where {}"
    ).format(_ident(backup_table), _ident(source_table), where_clause)


# ------------------------------- step 3: UPDATE -----------------------------
def build_update_createdat(source_table, backup_table, where_clause):
    """
    Step 3: rewrite createdat so the breaching rows fall back inside SLA.

    Fast AND safe:
      * the breach predicates (projectid / rtcdateat / createdat / meternumber)
        let Postgres use its indexes to find the rows - no giant id array is
        shipped from the app;
      * "sequenceid in (select sequenceid from <backup>)" restricts the update
        to exactly the rows that were backed up, so a row that arrived between
        Backup and Update (and is therefore NOT in the backup) is never touched.
    """
    return sql.SQL(
        "update {} set createdat = " + _FIX_CREATEDAT + " "
        "where {} and sequenceid in (select sequenceid from {})"
    ).format(_ident(source_table), where_clause, _ident(backup_table))


# ------------------------------- step 4: REPUSH -----------------------------
def build_repush_from_backup(repush_table, backup_table):
    """
    Step 4: copy the backed-up rows (temp2) into the per-sequence repush table,
    stamping the datarepushid (the only %s) onto every row.
    """
    return sql.SQL(
        "insert into {} (datarepushid, sequenceid, meternumber, rtcdateat, createdat) "
        "select %s, sequenceid, meternumber, rtcdateat, createdat "
        "from {}"
    ).format(_ident(repush_table), _ident(backup_table))


# ---- data_repush_settings (the single parent row that owns the datarepushid) ----
def build_max_datarepushid(settings_table):
    """Greatest datarepushid currently in the settings table (None if empty)."""
    return sql.SQL("select max(datarepushid) from {}").format(_ident(settings_table))


def build_settings_insert(settings_table):
    """
    Insert a fresh settings row and return its (auto-incremented) datarepushid.
    startdate/enddate are placeholders here (now()); they are overwritten by
    build_settings_update once the real min/max createdat is known.
    Params: (projectid, profilename).
    """
    return sql.SQL(
        "insert into {} (projectid, profilename, startdate, enddate, "
        "minsequenceid, maxsequenceid, isspecificseqrepush) "
        "values (%s, %s, now(), now(), 0, 0, true) "
        "returning datarepushid"
    ).format(_ident(settings_table))


def build_delete_other_settings(settings_table):
    """Delete every settings row except the one with the given datarepushid."""
    return sql.SQL("delete from {} where datarepushid <> %s").format(
        _ident(settings_table))


def build_settings_update(settings_table):
    """
    Update the single settings row with the run's window + metadata.
    Params: (projectid, profilename, startdate, enddate, datarepushid).
    """
    return sql.SQL(
        "update {} set projectid = %s, profilename = %s, "
        "startdate = %s, enddate = %s, "
        "minsequenceid = 0, maxsequenceid = 0, isspecificseqrepush = true "
        "where datarepushid = %s"
    ).format(_ident(settings_table))
