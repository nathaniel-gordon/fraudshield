"""Database schema and its forward migrations.

Migrations only ever move forward and are applied inside one transaction each, so a
database is either fully at version N or fully at version N-1 — never half-migrated.
``PRAGMA user_version`` carries the number (it is a single integer in the file header,
so it cannot itself be lost) and ``schema_migrations`` records what was applied for a
human reading the file later.

The constraints here are not decoration; they are where several guarantees actually
live:

    events.seq PRIMARY KEY          the log is a total order with no duplicates
    UNIQUE(stream_id, version)      optimistic concurrency — two writers racing on one
                                    card cannot both win, the loser hits this index
    UNIQUE(hash)                    a chain link is unique to its position
    ux_events_txn_observed          one TransactionObserved per txn_id, so the derived
                                    ALERT-/CASE- identities can never collide
    processed_commands.command_id   command idempotency: the second submit of the same
                                    command cannot append a second copy
"""
from __future__ import annotations

import sqlite3
from typing import NamedTuple

from ..errors import SchemaTooNew


class Migration(NamedTuple):
    """One forward step. ``statements`` run in order inside a single transaction."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "event log, indexes and command idempotency",
        (
            """
            CREATE TABLE IF NOT EXISTS events (
                seq        INTEGER PRIMARY KEY,
                stream_id  TEXT    NOT NULL,
                version    INTEGER NOT NULL,
                type       TEXT    NOT NULL,
                prev_hash  TEXT    NOT NULL,
                hash       TEXT    NOT NULL,
                data       TEXT    NOT NULL,
                txn_id     INTEGER,
                case_id    TEXT,
                CHECK (seq > 0),
                CHECK (version > 0),
                CHECK (length(hash) = 64 AND length(prev_hash) = 64),
                UNIQUE (stream_id, version),
                UNIQUE (hash)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_events_type_seq ON events (type, seq)",
            "CREATE INDEX IF NOT EXISTS ix_events_stream_seq ON events (stream_id, seq)",
            "CREATE INDEX IF NOT EXISTS ix_events_txn ON events (txn_id) WHERE txn_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_events_case ON events (case_id) WHERE case_id IS NOT NULL",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_events_txn_observed
                ON events (txn_id) WHERE type = 'TransactionObserved'
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_commands (
                command_id   TEXT    PRIMARY KEY,
                command_type TEXT    NOT NULL,
                first_seq    INTEGER NOT NULL,
                last_seq     INTEGER NOT NULL,
                CHECK (last_seq >= first_seq)
            )
            """,
        ),
    ),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


def current_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def migrate(con: sqlite3.Connection) -> list[str]:
    """Bring a connection's database up to :data:`SCHEMA_VERSION`.

    Returns:
        The names of the migrations that were applied, empty when already current.

    Raises:
        SchemaTooNew: the file was written by a newer build; refusing to touch it is
            the only safe move, since this build does not know what the extra columns
            mean.
    """
    version = current_version(con)
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(
            "database schema is newer than this build understands",
            found=version,
            supported=SCHEMA_VERSION,
        )
    applied: list[str] = []
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                con.execute(statement)
            con.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            con.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            # PRAGMA does not accept a bound parameter; the value is our own integer.
            con.execute(f"PRAGMA user_version = {int(migration.version)}")
            con.commit()
        except Exception:
            con.rollback()
            raise
        applied.append(migration.name)
    return applied


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Migration", "current_version", "migrate"]
