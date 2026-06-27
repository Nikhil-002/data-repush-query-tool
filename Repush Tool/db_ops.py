"""
db_ops.py
=========
The only module that opens a database connection. Each of the four workflow
steps gets one small function here. They take a connection dict + the values
they need, run the matching SQL from queries.py, and return a simple result
(a list of seqids, or a row count). All commit/rollback handling lives here so
the UI never has to think about transactions.

Because Check / Backup / Update / Repush can each take several minutes, the
three steps that work off a seqid list (backup, update, repush) run in
BATCHES: a chunk of seqids at a time, committing after each chunk and calling
``progress_cb(done, total)`` so the UI can show how far along it is.

These functions are meant to be run on a background thread by the UI.
"""

import psycopg2

from queries import (
    build_seqid_query,
    build_create_backup_table,
    build_backup_insert,
    build_update_createdat,
    build_repush_insert,
)

_CONNECT_TIMEOUT = 10
DEFAULT_BATCH_SIZE = 5000


def _connect(conn_kw):
    return psycopg2.connect(connect_timeout=_CONNECT_TIMEOUT, **conn_kw)


def _chunks(seq, size):
    """Yield successive `size`-length slices of `seq`."""
    size = max(int(size), 1)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run_check(conn_kw, table, where_clause, params):
    """
    Step 1: return the list of sequenceids that breach the SLA (read-only).

    This is a single query that may take several minutes; there is nothing to
    batch, so the UI shows an indeterminate "running" indicator instead.
    """
    query = build_seqid_query(table, where_clause)
    with _connect(conn_kw) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [r[0] for r in cur.fetchall()]


def _run_batched(conn_kw, seqids, statement, batch_size, progress_cb,
                 setup_statement=None):
    """
    Run `statement` (a builder result using `any(%s)`) over `seqids` in batches,
    committing each batch and reporting progress. Returns total affected rows.

    `setup_statement` (optional) is run + committed once before the batches,
    used by backup to create the target table.
    """
    total = len(seqids)
    affected = 0
    done = 0
    conn = _connect(conn_kw)
    try:
        with conn.cursor() as cur:
            if setup_statement is not None:
                cur.execute(setup_statement)
                conn.commit()
            for chunk in _chunks(seqids, batch_size):
                cur.execute(statement, (chunk,))
                affected += cur.rowcount
                conn.commit()
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
        return affected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_backup(conn_kw, table, backup_table, seqids, create_backup=True,
               batch_size=DEFAULT_BATCH_SIZE, progress_cb=None):
    """Step 2: copy the breaching rows into the backup table, in batches."""
    setup = build_create_backup_table(backup_table, table) if create_backup else None
    return _run_batched(
        conn_kw, seqids,
        build_backup_insert(backup_table, table),
        batch_size, progress_cb, setup_statement=setup)


def run_update(conn_kw, table, seqids,
               batch_size=DEFAULT_BATCH_SIZE, progress_cb=None):
    """Step 3: rewrite createdat for the breaching rows, in batches."""
    return _run_batched(
        conn_kw, seqids,
        build_update_createdat(table),
        batch_size, progress_cb)


def run_repush(conn_kw, table, repush_table, seqids,
               batch_size=DEFAULT_BATCH_SIZE, progress_cb=None):
    """Step 4: insert the fixed rows into the repush table, in batches."""
    return _run_batched(
        conn_kw, seqids,
        build_repush_insert(repush_table, table),
        batch_size, progress_cb)
