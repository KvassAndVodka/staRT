"""Async SQLite database setup and explicit schema migrations."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Any

from sqlalchemy import DateTime, JSON, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.config import settings
from app.domain.models import Base


SCHEMA_VERSION = 5

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Make declared ON DELETE rules effective on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


def _constraint_names(conn: Connection, table_name: str) -> set[str]:
    inspector = inspect(conn)
    names = {
        item.get("name")
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    }
    names.update(
        item.get("name")
        for item in inspector.get_check_constraints(table_name)
        if item.get("name")
    )
    return names


def _unique_column_sets(conn: Connection, table_name: str) -> set[tuple[str, ...]]:
    """Return physical unique keys, including SQLite auto-indexed constraints.

    SQLAlchemy's SQLite reflection can intermittently omit a named UNIQUE
    constraint after a drop/create cycle even though PRAGMA reports the backing
    auto-index. Migration decisions must use the physical key, not its label.
    """
    inspector = inspect(conn)
    keys = {
        tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
        if item.get("column_names")
    }
    if conn.dialect.name != "sqlite":
        return keys

    escaped_table = table_name.replace("'", "''")
    for index_row in conn.exec_driver_sql(
        f"PRAGMA index_list('{escaped_table}')"
    ):
        if not index_row[2] or index_row[3] == "pk":
            continue
        index_name = str(index_row[1]).replace("'", "''")
        columns = tuple(
            row[2]
            for row in conn.exec_driver_sql(f"PRAGMA index_info('{index_name}')")
            if row[2] is not None
        )
        if columns:
            keys.add(columns)
    return keys


def _assert_no_duplicate_keys(rows: list[dict[str, Any]], fields: tuple[str, ...], label: str) -> None:
    seen: set[tuple[Any, ...]] = set()
    duplicates: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row.get(field) for field in fields)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        sample = sorted(duplicates, key=repr)[:5]
        raise RuntimeError(f"Cannot migrate {label}: duplicate durable keys found: {sample}")


def _coerce_rebuilt_row(table, row: dict[str, Any]) -> dict[str, Any]:
    """Convert raw SQLite values before inserting through typed SQLAlchemy columns."""
    converted = dict(row)
    for column in table.columns:
        value = converted.get(column.name)
        if value is None:
            continue
        if isinstance(column.type, DateTime) and isinstance(value, str):
            converted[column.name] = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        elif isinstance(column.type, JSON) and isinstance(value, str):
            converted[column.name] = json.loads(value)
    return converted


def _rebuild_audio_fragments(conn: Connection) -> None:
    inspector = inspect(conn)
    if "audio_fragments" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("audio_fragments")}
    constraints = _constraint_names(conn, "audio_fragments")
    required_columns = {
        "sample_start",
        "sample_end",
        "sample_count",
        "sample_rate_hz",
        "bytes_per_sample",
    }
    required_constraints = {"chk_fragment_sample_count", "chk_durable_fragment_sha"}
    unique_keys = _unique_column_sets(conn, "audio_fragments")
    if (
        required_columns.issubset(columns)
        and required_constraints.issubset(constraints)
        and ("session_id", "stream_epoch", "sequence") in unique_keys
    ):
        return

    rows = [dict(row._mapping) for row in conn.execute(text(
        "SELECT * FROM audio_fragments ORDER BY session_id, stream_epoch, sequence"
    ))]
    _assert_no_duplicate_keys(rows, ("session_id", "stream_epoch", "sequence"), "audio_fragments")

    converted: list[dict[str, Any]] = []
    frontiers: dict[tuple[str, int], int] = {}
    target_columns = {column.name for column in Base.metadata.tables["audio_fragments"].columns}

    for source in rows:
        row = {key: value for key, value in source.items() if key in target_columns}
        group = (source["session_id"], int(source.get("stream_epoch") or 0))
        frontier = frontiers.get(group, 0)
        bytes_per_sample = int(source.get("bytes_per_sample") or 2)
        sample_rate = int(source.get("sample_rate_hz") or settings.INFERENCE_SAMPLE_RATE)
        path = Path(source["path"])
        checksum = source.get("sha256")
        status = source.get("status") or "writing"

        existing_count = int(source.get("sample_count") or 0)
        existing_start = int(source.get("sample_start") or 0)
        existing_end = int(source.get("sample_end") or 0)
        existing_valid = (
            existing_count > 0
            and existing_end - existing_start == existing_count
            and existing_start >= frontier
        )

        verified = False
        sample_count = existing_count if existing_valid else 0
        if path.is_file() and bytes_per_sample > 0:
            data = path.read_bytes()
            aligned = len(data) % bytes_per_sample == 0
            computed = hashlib.sha256(data).hexdigest() if aligned else None
            checksum_matches = checksum is None or checksum == computed
            if aligned and checksum_matches:
                verified = True
                sample_count = len(data) // bytes_per_sample
                checksum = computed

        sample_start = existing_start if existing_valid and verified else frontier
        sample_end = sample_start + sample_count

        if not verified:
            status = "corrupt"
            sample_count = 0
            sample_start = frontier
            sample_end = frontier

        row.update({
            "stream_epoch": group[1],
            "sample_start": sample_start,
            "sample_end": sample_end,
            "sample_count": sample_count,
            "sample_rate_hz": sample_rate,
            "bytes_per_sample": bytes_per_sample,
            "sha256": checksum,
            "status": status,
            "wall_started_at": source.get("wall_started_at") or datetime.now(timezone.utc),
        })
        converted.append(row)
        frontiers[group] = sample_end

    legacy_name = "audio_fragments_legacy_v1"
    if legacy_name in inspector.get_table_names():
        raise RuntimeError(f"Refusing migration: leftover table {legacy_name} exists")
    conn.exec_driver_sql(f"ALTER TABLE audio_fragments RENAME TO {legacy_name}")
    table = Base.metadata.tables["audio_fragments"]
    table.create(conn)
    if converted:
        conn.execute(table.insert(), [_coerce_rebuilt_row(table, row) for row in converted])
    conn.exec_driver_sql(f"DROP TABLE {legacy_name}")


def _rebuild_inference_windows(conn: Connection) -> None:
    inspector = inspect(conn)
    if "inference_windows" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("inference_windows")}
    constraints = _constraint_names(conn, "inference_windows")
    required_columns = {
        "target_start_sample",
        "target_end_sample",
        "context_start_sample",
        "context_end_sample",
        "sample_rate_hz",
        "active_attempt_id",
        "committed_attempt_id",
        "reconciler_snapshot",
    }
    required_constraints = {"chk_target_sample_interval"}
    unique_keys = _unique_column_sets(conn, "inference_windows")
    if (
        required_columns.issubset(columns)
        and required_constraints.issubset(constraints)
        and (
            "session_id",
            "model_profile_revision",
            "stream_epoch",
            "ordinal",
        ) in unique_keys
    ):
        return

    rows = [dict(row._mapping) for row in conn.execute(text(
        "SELECT * FROM inference_windows ORDER BY session_id, stream_epoch, ordinal"
    ))]
    _assert_no_duplicate_keys(
        rows,
        ("session_id", "model_profile_revision", "stream_epoch", "ordinal"),
        "inference_windows",
    )

    target_columns = {column.name for column in Base.metadata.tables["inference_windows"].columns}
    converted: list[dict[str, Any]] = []
    for source in rows:
        row = {key: value for key, value in source.items() if key in target_columns}
        rate = int(source.get("sample_rate_hz") or settings.INFERENCE_SAMPLE_RATE)
        target_start = int(source.get("target_start_sample") or int(source["target_start_ms"] * rate / 1000))
        target_end = int(source.get("target_end_sample") or int(source["target_end_ms"] * rate / 1000))
        context_start = int(source.get("context_start_sample") or int(source["context_start_ms"] * rate / 1000))
        if target_end <= target_start:
            raise RuntimeError(f"Cannot migrate zero-length inference window {source['id']}")
        row.update({
            "sample_rate_hz": rate,
            "target_start_sample": target_start,
            "target_end_sample": target_end,
            "context_start_sample": min(context_start, target_start),
            "context_end_sample": target_end,
            "active_attempt_id": source.get("active_attempt_id"),
            "committed_attempt_id": source.get("committed_attempt_id"),
            "reconciler_snapshot": source.get("reconciler_snapshot"),
            "created_at": source.get("created_at") or datetime.now(timezone.utc),
        })
        converted.append(row)

    legacy_name = "inference_windows_legacy_v1"
    if legacy_name in inspector.get_table_names():
        raise RuntimeError(f"Refusing migration: leftover table {legacy_name} exists")
    conn.exec_driver_sql(f"ALTER TABLE inference_windows RENAME TO {legacy_name}")
    table = Base.metadata.tables["inference_windows"]
    table.create(conn)
    if converted:
        conn.execute(table.insert(), [_coerce_rebuilt_row(table, row) for row in converted])
    conn.exec_driver_sql(f"DROP TABLE {legacy_name}")


def _migrate_event_stream(conn: Connection) -> None:
    """Add session counters and rebuild retained outbox rows with strict order."""
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "sessions" not in tables:
        return

    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "event_sequence" not in session_columns:
        conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0"
        ))
    if "event_replay_floor" not in session_columns:
        conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN event_replay_floor INTEGER NOT NULL DEFAULT 1"
        ))
    if "outbox_events" not in tables:
        return

    outbox_inspector = inspect(conn)
    reflected_columns = outbox_inspector.get_columns("outbox_events")
    outbox_columns = {column["name"] for column in reflected_columns}
    sequence_column = next(
        (column for column in reflected_columns if column["name"] == "sequence"),
        None,
    )
    constraints = _constraint_names(conn, "outbox_events")
    unique_keys = _unique_column_sets(conn, "outbox_events")
    needs_rebuild = (
        sequence_column is None
        or bool(sequence_column.get("nullable"))
        or "chk_outbox_positive_sequence" not in constraints
        or ("session_id", "sequence") not in unique_keys
    )

    rows = [dict(row._mapping) for row in conn.execute(text(
        "SELECT * FROM outbox_events ORDER BY session_id, created_at, id"
    ))]
    _assert_no_duplicate_keys(rows, ("idempotency_key",), "outbox events")
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(row["session_id"], []).append(row)

    converted: list[dict[str, Any]] = []
    target_columns = {
        column.name for column in Base.metadata.tables["outbox_events"].columns
    }
    stream_state: dict[str, tuple[int, int]] = {}
    for session_id, session_rows in by_session.items():
        existing = [
            int(row["sequence"])
            for row in session_rows
            if "sequence" in outbox_columns and row.get("sequence") is not None
        ]
        if any(sequence <= 0 for sequence in existing) or len(existing) != len(set(existing)):
            raise RuntimeError(
                f"Cannot migrate event stream for session {session_id}: invalid sequences"
            )
        next_sequence = max(existing, default=0)
        assigned_sequences = list(existing)
        for source in session_rows:
            row = {key: value for key, value in source.items() if key in target_columns}
            sequence = source.get("sequence") if "sequence" in outbox_columns else None
            if sequence is None:
                next_sequence += 1
                sequence = next_sequence
                assigned_sequences.append(sequence)
            row["sequence"] = int(sequence)
            converted.append(row)
        floor = min(assigned_sequences, default=next_sequence + 1)
        stream_state[session_id] = (next_sequence, floor)

    if needs_rebuild:
        legacy_name = "outbox_events_legacy_v3"
        if legacy_name in tables:
            raise RuntimeError(f"Refusing migration: leftover table {legacy_name} exists")
        for index in outbox_inspector.get_indexes("outbox_events"):
            name = index.get("name")
            if name:
                escaped_name = str(name).replace('"', '""')
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{escaped_name}"')
        conn.exec_driver_sql(f"ALTER TABLE outbox_events RENAME TO {legacy_name}")
        table = Base.metadata.tables["outbox_events"]
        table.create(conn)
        if converted:
            conn.execute(table.insert(), [_coerce_rebuilt_row(table, row) for row in converted])
        conn.exec_driver_sql(f"DROP TABLE {legacy_name}")

    for session_id, (latest, floor) in stream_state.items():
        conn.execute(text(
            "UPDATE sessions SET "
            "event_sequence=MAX(event_sequence, :latest), "
            "event_replay_floor=:floor "
            "WHERE id=:session_id"
        ), {
            "latest": latest,
            "floor": floor,
            "session_id": session_id,
        })


def _migrate_speaker_pipeline_indexes(conn: Connection) -> None:
    """Add durable identity and interval lookup indexes for final diarization."""
    tables = set(inspect(conn).get_table_names())
    if "speakers" in tables:
        duplicate = conn.execute(text(
            "SELECT session_id,machine_label,COUNT(*) AS count "
            "FROM speakers GROUP BY session_id,machine_label HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate is not None:
            raise RuntimeError(
                "Cannot migrate speakers: duplicate session machine labels found: "
                f"{(duplicate.session_id, duplicate.machine_label)}"
            )
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_speakers_session_machine_label "
            "ON speakers(session_id,machine_label)"
        ))
    if "speaker_activities" in tables:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_speaker_activities_session_interval "
            "ON speaker_activities(session_id,start_ms,end_ms)"
        ))
    if "overlap_regions" in tables:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_overlap_regions_session_interval "
            "ON overlap_regions(session_id,start_ms,end_ms)"
        ))


def _migrate_schema(conn: Connection) -> None:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())

    if "sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("sessions")}
        if "actual_asr_device" not in columns:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN actual_asr_device VARCHAR(50)"))
        if "actual_compute_type" not in columns:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN actual_compute_type VARCHAR(50)"))
        if "active_processing_revision" not in columns:
            conn.execute(text(
                "ALTER TABLE sessions ADD COLUMN active_processing_revision "
                "VARCHAR(50) NOT NULL DEFAULT 'sample-v2'"
            ))

    if "audio_assets" in tables:
        columns = {column["name"] for column in inspector.get_columns("audio_assets")}
        if "provenance" not in columns:
            conn.execute(text("ALTER TABLE audio_assets ADD COLUMN provenance JSON"))

    _rebuild_audio_fragments(conn)
    _rebuild_inference_windows(conn)
    _migrate_event_stream(conn)
    _migrate_speaker_pipeline_indexes(conn)
    Base.metadata.create_all(conn)
    conn.execute(
        text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
        {"version": SCHEMA_VERSION},
    )


async def init_db() -> None:
    """Apply ordered, validated migrations. Errors deliberately fail startup."""
    async with engine.begin() as conn:
        await conn.run_sync(_migrate_schema)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
