"""
db_ops.py
=========
The only module that opens a database connection. Each workflow step gets one
function here; all commit/rollback handling lives here so the UI never has to
think about transactions. Every step can take several minutes, so the UI shows
a "running..." bar + elapsed timer while these run on a background thread.

  * CHECK  - one COUNT(*); returns an int.
  * BACKUP - truncate the backup table, then INSERT ... SELECT the breaching
             rows; returns the row count.
  * UPDATE - one set-based UPDATE; returns the row count.
  * REPUSH - a single transaction that first (re)builds temp1 - the full good
             dataset (every packet for the meter list in the RTC window whose
             createdat now sits inside the createdat window, i.e. the original
             in-SLA rows plus the ones step 3 just pulled back inside SLA) -
             then truncates the per-sequence repush table, ensures one parent
             row in data_repush_settings (to get the datarepushid), copies
             temp1 into the per-sequence table, and writes temp1's createdat
             window back to the settings row.
"""

import psycopg2

from queries import (
    build_count_query,
    build_create_backup_table,
    build_truncate,
    build_backup_insert,
    build_update_createdat,
    build_count_rows,
    build_max_datarepushid,
    build_settings_insert,
    build_delete_other_settings,
    build_repush_from_backup,
    build_repush_window,
    build_settings_update,
    build_create_snapshot_like,
    build_snapshot_insert,
)

_CONNECT_TIMEOUT = 10
_SQL_LOG_LIMIT = 4000      # cap logged SQL length (a meter-file array can be huge)


def _connect(conn_kw):
    return psycopg2.connect(connect_timeout=_CONNECT_TIMEOUT, **conn_kw)


def _log_sql(cur, statement, params, sql_cb):
    """
    Render the EXACT statement (identifiers + parameter values resolved) using
    cursor.mogrify and hand it to sql_cb, so the UI can show precisely what is
    sent to Postgres before it runs. Long output is truncated.
    """
    if not sql_cb:
        return
    try:
        text = cur.mogrify(statement, params)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
    except Exception as e:
        text = f"<could not render SQL: {e}>"
    if len(text) > _SQL_LOG_LIMIT:
        text = text[:_SQL_LOG_LIMIT] + f"\n... [truncated, {len(text):,} chars total]"
    sql_cb(text)


# -------------------------------- step 1: CHECK -----------------------------
def run_check(conn_kw, table, where_clause, params, sql_cb=None):
    """
    Step 1: COUNT the rows that breach the SLA (read-only). Returns an int.
    """
    query = build_count_query(table, where_clause)
    with _connect(conn_kw) as conn:
        with conn.cursor() as cur:
            _log_sql(cur, query, params, sql_cb)
            cur.execute(query, params)
            return cur.fetchone()[0]


# -------------------------------- step 2: BACKUP ----------------------------
def run_backup(conn_kw, table, backup_table, where_clause, params,
               create_backup=True, sql_cb=None):
    """
    Step 2: (optionally create the backup table,) TRUNCATE it so it holds only
    this run, then copy the breaching rows in. Returns the row count.
    """
    conn = _connect(conn_kw)
    try:
        with conn.cursor() as cur:
            if create_backup:
                cur.execute(build_create_backup_table(backup_table, table))
            trunc = build_truncate(backup_table)
            _log_sql(cur, trunc, None, sql_cb)
            cur.execute(trunc)

            insert_stmt = build_backup_insert(backup_table, table, where_clause)
            _log_sql(cur, insert_stmt, params, sql_cb)
            cur.execute(insert_stmt, params)
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -------------------------------- step 3: UPDATE ----------------------------
def run_update(conn_kw, table, backup_table, where_clause, params, sql_cb=None):
    """
    Step 3: rewrite createdat for the breaching rows in one set-based statement.
    Uses the indexed breach predicates for speed and restricts to the backed-up
    sequenceids for safety (see build_update_createdat). Returns the row count.
    """
    stmt = build_update_createdat(table, backup_table, where_clause)
    conn = _connect(conn_kw)
    try:
        with conn.cursor() as cur:
            _log_sql(cur, stmt, params, sql_cb)
            cur.execute(stmt, params)
            count = cur.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -------------------------------- step 4: REPUSH ----------------------------
def _ensure_settings_row(cur, settings_table, projectid, profilename, sql_cb):
    """
    Make sure data_repush_settings has exactly ONE row and return its
    datarepushid. If empty, insert one (auto id). If several exist, keep the
    greatest datarepushid and delete the rest.
    """
    cur.execute(build_count_rows(settings_table))
    n = cur.fetchone()[0]

    if n == 0:
        insert_stmt = build_settings_insert(settings_table)
        _log_sql(cur, insert_stmt, (projectid, profilename), sql_cb)
        cur.execute(insert_stmt, (projectid, profilename))
        return cur.fetchone()[0]

    cur.execute(build_max_datarepushid(settings_table))
    datarepushid = cur.fetchone()[0]
    if n > 1:
        delete_stmt = build_delete_other_settings(settings_table)
        _log_sql(cur, delete_stmt, (datarepushid,), sql_cb)
        cur.execute(delete_stmt, (datarepushid,))
    return datarepushid


def run_repush(conn_kw, source_table, temp1_table, by_sequence_table,
               settings_table, projectid, profilename,
               temp1_where, temp1_params, create_temp1=True, sql_cb=None):
    """
    Step 4 (single transaction):
      1. (optionally create) + truncate temp1, then fill it from the main table
         with the full good dataset (temp1_where);
      2. truncate the per-sequence repush table;
      3. ensure one settings row + get its datarepushid;
      4. copy temp1 into the per-sequence table with that id;
      5. read min/max(createdat) + DDMM back from temp1;
      6. write projectid/profilename/window back to the settings row;
      7. append the rows into the per-day snapshot table repush_DDMM.

    Returns dict(datarepushid, temp1_count, inserted, startdate, enddate,
                 snapshot_table, snapshot_count).
    """
    conn = _connect(conn_kw)
    try:
        with conn.cursor() as cur:
            # 1. (re)build temp1 = the full good dataset to repush
            if create_temp1:
                cur.execute(build_create_backup_table(temp1_table, source_table))
            trunc_t1 = build_truncate(temp1_table)
            _log_sql(cur, trunc_t1, None, sql_cb)
            cur.execute(trunc_t1)

            fill_t1 = build_backup_insert(temp1_table, source_table, temp1_where)
            _log_sql(cur, fill_t1, temp1_params, sql_cb)
            cur.execute(fill_t1, temp1_params)
            temp1_count = cur.rowcount
            if temp1_count == 0:
                raise RuntimeError(
                    "temp1 is empty - the createdat/RTC/meter filter matched no "
                    "rows in the main table. Check the Run parameters.")

            # 2. clear the per-sequence (child) table
            trunc = build_truncate(by_sequence_table)
            _log_sql(cur, trunc, None, sql_cb)
            cur.execute(trunc)

            # 3. one parent settings row -> datarepushid
            datarepushid = _ensure_settings_row(
                cur, settings_table, projectid, profilename, sql_cb)

            # 4. copy temp1 into the per-sequence table, stamping the id
            repush_stmt = build_repush_from_backup(by_sequence_table, temp1_table)
            _log_sql(cur, repush_stmt, (datarepushid,), sql_cb)
            cur.execute(repush_stmt, (datarepushid,))
            inserted = cur.rowcount

            # 5. window from temp1: createdat min/max (for settings)
            #    + DDMM of earliest rtcdateat (names the snapshot table)
            cur.execute(build_repush_window(temp1_table))
            startdate, enddate, ddmm = cur.fetchone()

            # 6. write the window + metadata back to the single settings row
            update_stmt = build_settings_update(settings_table)
            update_params = (projectid, profilename, startdate, enddate, datarepushid)
            _log_sql(cur, update_stmt, update_params, sql_cb)
            cur.execute(update_stmt, update_params)

            # 7. append the rows into the per-day snapshot table repush_DDMM
            snapshot_table = f"repush_{ddmm}"
            create_snap = build_create_snapshot_like(snapshot_table, by_sequence_table)
            _log_sql(cur, create_snap, None, sql_cb)
            cur.execute(create_snap)
            snap_insert = build_snapshot_insert(snapshot_table, by_sequence_table)
            _log_sql(cur, snap_insert, None, sql_cb)
            cur.execute(snap_insert)
            snapshot_count = cur.rowcount
        conn.commit()
        return {"datarepushid": datarepushid, "temp1_count": temp1_count,
                "inserted": inserted, "startdate": startdate, "enddate": enddate,
                "snapshot_table": snapshot_table, "snapshot_count": snapshot_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
