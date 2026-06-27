"""
queries.py
==========
SQL builders only - every function returns a psycopg2 ``sql.SQL`` object and
does NOT touch the database. Keeping the SQL here (and away from both the UI and
the connection code) means you can read the exact statements in one place.

The four workflow steps map onto these builders:

  Step 1 CHECK   -> build_breach_where + build_seqid_query
  Step 2 BACKUP  -> build_create_backup_table + build_backup_insert
  Step 3 UPDATE  -> build_update_createdat
  Step 4 REPUSH  -> build_repush_insert
"""

from psycopg2 import sql

# The "fix" applied to createdat in step 3: rtcdateat plus a small random
# offset (1-5 min, random sec/ms/us) so the row falls back inside the SLA.
_FIX_CREATEDAT = (
    "rtcdateat "
    "+ (INTERVAL '1 minute'      * (FLOOR(RANDOM() * 5) + 1)) "
    "+ (INTERVAL '1 second'      * FLOOR(RANDOM() * 60)) "
    "+ (INTERVAL '1 millisecond' * FLOOR(RANDOM() * 1000)) "
    "+ (INTERVAL '1 microsecond' * (FLOOR(RANDOM() * 999) + 1))"
)


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
                    sql.Identifier(meter_table)))
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


def build_seqid_query(table, where_clause):
    """Step 1: select the sequenceids of the breaching rows."""
    return sql.SQL("select sequenceid from {} where {}").format(
        sql.Identifier(table), where_clause)


def build_create_backup_table(backup_table, source_table):
    """Step 2a: create an empty backup table with the 4 columns we keep."""
    return sql.SQL(
        "create table if not exists {} as "
        "select sequenceid, meternumber, rtcdateat, createdat "
        "from {} where false"
    ).format(sql.Identifier(backup_table), sql.Identifier(source_table))


def build_backup_insert(backup_table, source_table):
    """Step 2b: copy the breaching rows into the backup table."""
    return sql.SQL(
        "insert into {} (sequenceid, meternumber, rtcdateat, createdat) "
        "select sequenceid, meternumber, rtcdateat, createdat "
        "from {} where sequenceid = any(%s)"
    ).format(sql.Identifier(backup_table), sql.Identifier(source_table))


def build_update_createdat(source_table):
    """Step 3: rewrite createdat so the breaching rows fall back inside SLA."""
    return sql.SQL(
        "update {} set createdat = " + _FIX_CREATEDAT + " "
        "where sequenceid = any(%s)"
    ).format(sql.Identifier(source_table))


def build_repush_insert(repush_table, source_table):
    """Step 4: insert the fixed rows into the repush table."""
    return sql.SQL(
        "insert into {} (sequenceid, meternumber, rtcdateat, createdat) "
        "select sequenceid, meternumber, rtcdateat, createdat "
        "from {} where sequenceid = any(%s) "
        "on conflict do nothing"
    ).format(sql.Identifier(repush_table), sql.Identifier(source_table))
