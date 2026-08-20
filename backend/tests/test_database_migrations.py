"""Schema migration contracts for clean and retained SQLite databases."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.storage.database import engine, _migrate_schema, _unique_column_sets
from app.domain.models import Base


def _schema_contract(sync_connection):
    inspector = inspect(sync_connection)
    audio_constraints = {
        item.get("name") for item in inspector.get_unique_constraints("audio_fragments")
    }
    audio_constraints.update(
        item.get("name") for item in inspector.get_check_constraints("audio_fragments")
    )
    window_constraints = {
        item.get("name") for item in inspector.get_unique_constraints("inference_windows")
    }
    window_constraints.update(
        item.get("name") for item in inspector.get_check_constraints("inference_windows")
    )
    audio_asset_columns = {
        item["name"] for item in inspector.get_columns("audio_assets")
    }
    session_columns = {item["name"] for item in inspector.get_columns("sessions")}
    outbox_columns = {item["name"] for item in inspector.get_columns("outbox_events")}
    outbox_indexes = {item["name"] for item in inspector.get_indexes("outbox_events")}
    outbox_constraints = {
        item.get("name") for item in inspector.get_check_constraints("outbox_events")
    }
    return (
        audio_constraints,
        window_constraints,
        _unique_column_sets(sync_connection, "audio_fragments"),
        _unique_column_sets(sync_connection, "inference_windows"),
        audio_asset_columns,
        session_columns,
        outbox_columns,
        outbox_indexes,
        outbox_constraints,
    )


@pytest.mark.asyncio
async def test_clean_schema_contains_durable_constraints():
    async with engine.connect() as connection:
        (
            audio, windows, audio_keys, window_keys, asset_columns,
            session_columns, outbox_columns, outbox_indexes,
            outbox_constraints,
        ) = await connection.run_sync(_schema_contract)
    assert ("session_id", "stream_epoch", "sequence") in audio_keys
    assert "chk_fragment_sample_count" in audio
    assert "chk_durable_fragment_sha" in audio
    assert (
        "session_id",
        "model_profile_revision",
        "stream_epoch",
        "ordinal",
    ) in window_keys
    assert "chk_target_sample_interval" in windows
    assert "provenance" in asset_columns
    assert {"event_sequence", "event_replay_floor"}.issubset(session_columns)
    assert "sequence" in outbox_columns
    assert "uq_outbox_session_sequence" in outbox_indexes
    assert "chk_outbox_positive_sequence" in outbox_constraints


@pytest.mark.asyncio
async def test_retained_schema_is_rebuilt_and_fragment_samples_are_backfilled(tmp_path: Path):
    database_path = tmp_path / "legacy.db"
    fragment_path = tmp_path / "legacy.raw"
    fragment_bytes = b"\x01\x00" * 1001
    fragment_path.write_bytes(fragment_bytes)
    checksum = hashlib.sha256(fragment_bytes).hexdigest()
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    def create_legacy(sync_connection):
        Base.metadata.create_all(sync_connection)
        sync_connection.exec_driver_sql("ALTER TABLE audio_assets DROP COLUMN provenance")
        sync_connection.exec_driver_sql("ALTER TABLE sessions DROP COLUMN event_sequence")
        sync_connection.exec_driver_sql("ALTER TABLE sessions DROP COLUMN event_replay_floor")
        sync_connection.exec_driver_sql("DROP TABLE outbox_events")
        sync_connection.exec_driver_sql("DROP TABLE inference_attempts")
        sync_connection.exec_driver_sql("DROP TABLE inference_windows")
        sync_connection.exec_driver_sql("DROP TABLE audio_fragments")
        sync_connection.exec_driver_sql(
            """
            CREATE TABLE audio_fragments (
                id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                sequence INTEGER NOT NULL,
                source_start_ms INTEGER NOT NULL,
                source_end_ms INTEGER NOT NULL,
                wall_started_at DATETIME,
                wall_ended_at DATETIME,
                source_pts_start INTEGER,
                source_pts_end INTEGER,
                stream_epoch INTEGER NOT NULL DEFAULT 0,
                path TEXT NOT NULL,
                sha256 VARCHAR(64),
                status VARCHAR(50) NOT NULL
            )
            """
        )
        sync_connection.exec_driver_sql(
            """
            CREATE TABLE inference_windows (
                id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                stream_epoch INTEGER NOT NULL DEFAULT 0,
                ordinal INTEGER NOT NULL,
                target_start_ms INTEGER NOT NULL,
                target_end_ms INTEGER NOT NULL,
                context_start_ms INTEGER NOT NULL,
                context_end_ms INTEGER NOT NULL,
                status VARCHAR(50) NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                lease_owner VARCHAR(100),
                lease_expires_at DATETIME,
                input_manifest JSON,
                raw_hypotheses JSON,
                model_profile_revision VARCHAR(50) NOT NULL DEFAULT '1.0',
                actual_device VARCHAR(50),
                actual_compute_type VARCHAR(50),
                error_code VARCHAR(100),
                created_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME
            )
            """
        )
        sync_connection.exec_driver_sql(
            """
            CREATE TABLE outbox_events (
                id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                window_id VARCHAR(36),
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                event_type VARCHAR(100) NOT NULL,
                payload JSON NOT NULL,
                created_at DATETIME NOT NULL,
                published_at DATETIME
            )
            """
        )
        sync_connection.execute(text(
            "INSERT INTO sessions (id,title,source_url,source_type,status,processing_mode,"
            "language_mode,allowed_languages,created_at,updated_at,asr_model,"
            "active_processing_revision,diarization_model,last_durable_audio_ms,"
            "committed_frontier_ms,training_consent,schema_version) VALUES "
            "('legacy-session','Legacy','https://example.com/a','finite','failed','normal',"
            "'auto-mixed','[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'small','sample-v2',"
            "'pyannote-community-1',0,0,'excluded','1.0')"
        ))
        sync_connection.execute(text(
            "INSERT INTO audio_fragments "
            "(id,session_id,sequence,source_start_ms,source_end_ms,stream_epoch,path,sha256,status) "
            "VALUES ('legacy-fragment','legacy-session',0,0,62,0,:path,:sha,'durable')"
        ), {"path": str(fragment_path), "sha": checksum})
        sync_connection.execute(text(
            "INSERT INTO inference_windows "
            "(id,session_id,stream_epoch,ordinal,target_start_ms,target_end_ms,"
            "context_start_ms,context_end_ms,status,attempt_count,model_profile_revision) "
            "VALUES ('legacy-window','legacy-session',0,0,0,62,0,62,'pending',0,'1.0')"
        ))
        sync_connection.execute(text(
            "INSERT INTO outbox_events "
            "(id,session_id,window_id,idempotency_key,event_type,payload,created_at,published_at) "
            "VALUES "
            "('legacy-event-1','legacy-session','legacy-window','legacy:1','test.one','{}',"
            "'2026-01-01 00:00:00',CURRENT_TIMESTAMP),"
            "('legacy-event-2','legacy-session','legacy-window','legacy:2','test.two','{}',"
            "'2026-01-01 00:00:01',NULL)"
        ))

    async with legacy_engine.begin() as connection:
        await connection.run_sync(create_legacy)
        await connection.run_sync(_migrate_schema)
        await connection.run_sync(_migrate_schema)

    async with legacy_engine.connect() as connection:
        (
            audio_constraints,
            window_constraints,
            audio_keys,
            window_keys,
            asset_columns,
            session_columns,
            outbox_columns,
            outbox_indexes,
            outbox_constraints,
        ) = await connection.run_sync(_schema_contract)
        row = (await connection.execute(text(
            "SELECT sample_start,sample_end,sample_count,status,sha256 "
            "FROM audio_fragments WHERE id='legacy-fragment'"
        ))).one()
        version = (await connection.execute(text(
            "SELECT max(version) FROM schema_migrations"
        ))).scalar()
        event_rows = (await connection.execute(text(
            "SELECT id,sequence,payload FROM outbox_events ORDER BY sequence"
        ))).all()
        event_state = (await connection.execute(text(
            "SELECT event_sequence,event_replay_floor FROM sessions "
            "WHERE id='legacy-session'"
        ))).one()

    assert row.sample_start == 0
    assert row.sample_end == 1001
    assert row.sample_count == 1001
    assert row.status == "durable"
    assert row.sha256 == checksum
    assert ("session_id", "stream_epoch", "sequence") in audio_keys
    assert "chk_fragment_sample_count" in audio_constraints
    assert (
        "session_id",
        "model_profile_revision",
        "stream_epoch",
        "ordinal",
    ) in window_keys
    assert "chk_target_sample_interval" in window_constraints
    assert "provenance" in asset_columns
    assert {"event_sequence", "event_replay_floor"}.issubset(session_columns)
    assert "sequence" in outbox_columns
    assert "uq_outbox_session_sequence" in outbox_indexes
    assert "chk_outbox_positive_sequence" in outbox_constraints
    assert [(row.id, row.sequence) for row in event_rows] == [
        ("legacy-event-1", 1),
        ("legacy-event-2", 2),
    ]
    assert all(row.payload == "{}" for row in event_rows)
    assert event_state.event_sequence == 2
    assert event_state.event_replay_floor == 1
    assert version == 4
    await legacy_engine.dispose()
