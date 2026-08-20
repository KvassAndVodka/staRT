"""
Domain Models and Database Schema for staRT
Conforming to Section 8 of the Product and Technical Specification.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, JSON, ForeignKey,
    UniqueConstraint, CheckConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship
from pydantic import BaseModel, Field

Base = declarative_base()

def generate_uuid() -> str:
    return str(uuid.uuid4())

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ==========================================
# SQLAlchemy Database Models
# ==========================================

class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    title = Column(String(255), nullable=False, default="Untitled Session")
    source_url = Column(Text, nullable=False)
    source_type = Column(String(50), nullable=False, default="live")  # 'live', 'finite', 'upload'
    status = Column(String(50), nullable=False, default="queued")  # queued, connecting, live, finalizing, ready, failed, cancelled
    processing_mode = Column(String(50), nullable=False, default="normal")  # normal, catching_up, degraded, record_only, recovering_source
    language_mode = Column(String(50), nullable=False, default="auto-mixed")  # auto-mixed, auto, en, tl, ceb
    allowed_languages = Column(JSON, nullable=False, default=lambda: ["en", "tl", "ceb"])
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)
    audio_path = Column(Text, nullable=True)
    asr_model = Column(String(100), nullable=False, default="small")
    active_processing_revision = Column(String(50), nullable=False, default="sample-v2")
    actual_asr_device = Column(String(50), nullable=True)
    actual_compute_type = Column(String(50), nullable=True)
    diarization_model = Column(String(100), nullable=False, default="pyannote-community-1")
    last_durable_audio_ms = Column(Integer, nullable=False, default=0)
    committed_frontier_ms = Column(Integer, nullable=False, default=0)
    event_sequence = Column(Integer, nullable=False, default=0, server_default="0")
    event_replay_floor = Column(Integer, nullable=False, default=1, server_default="1")
    training_consent = Column(String(50), nullable=False, default="excluded")  # excluded, candidate, approved, withdrawn
    deleted_at = Column(DateTime, nullable=True)
    purge_after = Column(DateTime, nullable=True)
    error_code = Column(String(100), nullable=True)
    schema_version = Column(String(20), nullable=False, default="1.0")

    # Relationships
    audio_assets = relationship("AudioAssetModel", back_populates="session", cascade="all, delete-orphan")
    audio_fragments = relationship("AudioFragmentModel", back_populates="session", cascade="all, delete-orphan")
    timeline_gaps = relationship("TimelineGapModel", back_populates="session", cascade="all, delete-orphan")
    inference_windows = relationship("InferenceWindowModel", back_populates="session", cascade="all, delete-orphan")
    outbox_events = relationship("OutboxEventModel", back_populates="session", cascade="all, delete-orphan")
    speakers = relationship("SpeakerModel", back_populates="session", cascade="all, delete-orphan")
    words = relationship("WordModel", back_populates="session", cascade="all, delete-orphan")
    turns = relationship("TranscriptTurnModel", back_populates="session", cascade="all, delete-orphan")
    speaker_activities = relationship("SpeakerActivityModel", back_populates="session", cascade="all, delete-orphan")
    overlap_regions = relationship("OverlapRegionModel", back_populates="session", cascade="all, delete-orphan")
    review_issues = relationship("ReviewIssueModel", back_populates="session", cascade="all, delete-orphan")
    correction_events = relationship("CorrectionEventModel", back_populates="session", cascade="all, delete-orphan")


class AudioAssetModel(Base):
    __tablename__ = "audio_assets"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(50), nullable=False)  # master, playback, inference, fragment, separation_stem, export_mix
    status = Column(String(50), nullable=False, default="writing")  # writing, finalizing, ready, deleting, purged, corrupt
    path = Column(Text, nullable=False)
    container = Column(String(50), nullable=True)
    codec = Column(String(50), nullable=True)
    sample_rate_hz = Column(Integer, nullable=True)
    channels = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    derived_from_id = Column(String(36), nullable=True)
    provenance = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    deleted_at = Column(DateTime, nullable=True)
    schema_version = Column(String(20), nullable=False, default="1.0")

    session = relationship("SessionModel", back_populates="audio_assets")


class AudioFragmentModel(Base):
    __tablename__ = "audio_fragments"
    __table_args__ = (
        UniqueConstraint("session_id", "stream_epoch", "sequence", name="uq_audio_fragments_seq"),
        CheckConstraint("sample_start >= 0", name="chk_fragment_sample_start"),
        CheckConstraint("sample_end >= sample_start", name="chk_fragment_sample_end"),
        CheckConstraint("sample_count = sample_end - sample_start", name="chk_fragment_sample_count"),
        CheckConstraint("sample_rate_hz > 0", name="chk_fragment_sample_rate"),
        CheckConstraint("bytes_per_sample > 0", name="chk_fragment_bytes_per_sample"),
        CheckConstraint("status != 'durable' OR sha256 IS NOT NULL", name="chk_durable_fragment_sha"),
        Index("ix_audio_fragments_session_epoch_samples", "session_id", "stream_epoch", "sample_start", "sample_end"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    stream_epoch = Column(Integer, nullable=False, default=0)
    sample_start = Column(Integer, nullable=False, default=0)
    sample_end = Column(Integer, nullable=False, default=0)
    sample_count = Column(Integer, nullable=False, default=0)
    sample_rate_hz = Column(Integer, nullable=False, default=16000)
    bytes_per_sample = Column(Integer, nullable=False, default=2)
    source_start_ms = Column(Integer, nullable=False)
    source_end_ms = Column(Integer, nullable=False)
    wall_started_at = Column(DateTime, nullable=False, default=utc_now)
    wall_ended_at = Column(DateTime, nullable=True)
    # FFmpeg-normalized PCM PTS, with a time base of 1 / sample_rate_hz.
    source_pts_start = Column(Integer, nullable=True)
    source_pts_end = Column(Integer, nullable=True)
    path = Column(Text, nullable=False)
    sha256 = Column(String(64), nullable=True)
    status = Column(String(50), nullable=False, default="durable")  # writing, durable, corrupt, purged

    session = relationship("SessionModel", back_populates="audio_fragments")


class TimelineGapModel(Base):
    __tablename__ = "timeline_gaps"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    source_start_ms = Column(Integer, nullable=True)
    source_end_ms = Column(Integer, nullable=True)
    wall_started_at = Column(DateTime, nullable=False, default=utc_now)
    wall_ended_at = Column(DateTime, nullable=True)
    reason = Column(String(50), nullable=False)  # network, source_stall, expired_url, capture_failure
    recoverable = Column(Boolean, nullable=False, default=False)
    recovered = Column(Boolean, nullable=False, default=False)
    details = Column(JSON, nullable=True)

    session = relationship("SessionModel", back_populates="timeline_gaps")


class InferenceWindowModel(Base):
    __tablename__ = "inference_windows"
    __table_args__ = (
        UniqueConstraint("session_id", "model_profile_revision", "stream_epoch", "ordinal", name="uq_inference_windows_ord"),
        CheckConstraint("target_start_sample < target_end_sample", name="chk_target_sample_interval"),
        CheckConstraint("context_start_sample <= target_start_sample", name="chk_context_sample_start"),
        CheckConstraint("context_end_sample = target_end_sample", name="chk_context_sample_end"),
        CheckConstraint("sample_rate_hz > 0", name="chk_window_sample_rate"),
        Index("ix_inference_windows_claim", "session_id", "model_profile_revision", "status", "ordinal"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    stream_epoch = Column(Integer, nullable=False, default=0)
    ordinal = Column(Integer, nullable=False)
    target_start_sample = Column(Integer, nullable=False)
    target_end_sample = Column(Integer, nullable=False)
    context_start_sample = Column(Integer, nullable=False)
    context_end_sample = Column(Integer, nullable=False)
    sample_rate_hz = Column(Integer, nullable=False, default=16000)
    target_start_ms = Column(Integer, nullable=False)
    target_end_ms = Column(Integer, nullable=False)
    context_start_ms = Column(Integer, nullable=False)
    context_end_ms = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default="pending")  # pending, running, succeeded, failed
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    active_attempt_id = Column(String(36), nullable=True)
    committed_attempt_id = Column(String(36), nullable=True)
    input_manifest = Column(JSON, nullable=True)
    raw_hypotheses = Column(JSON, nullable=True)
    reconciler_snapshot = Column(JSON, nullable=True)
    model_profile_revision = Column(String(50), nullable=False, default="1.0")
    actual_device = Column(String(50), nullable=True)
    actual_compute_type = Column(String(50), nullable=True)
    error_code = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    session = relationship("SessionModel", back_populates="inference_windows")
    attempts = relationship("InferenceAttemptModel", back_populates="window", cascade="all, delete-orphan")


class InferenceAttemptModel(Base):
    __tablename__ = "inference_attempts"
    __table_args__ = (
        UniqueConstraint("window_id", "attempt_number", name="uq_inference_attempt_number"),
        Index("ix_inference_attempts_window_status", "window_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    window_id = Column(String(36), ForeignKey("inference_windows.id", ondelete="CASCADE"), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    worker_id = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="running")  # running, succeeded, failed, superseded
    input_manifest = Column(JSON, nullable=True)
    raw_hypotheses = Column(JSON, nullable=True)
    actual_device = Column(String(50), nullable=True)
    actual_compute_type = Column(String(50), nullable=True)
    error_code = Column(String(100), nullable=True)
    started_at = Column(DateTime, nullable=False, default=utc_now)
    completed_at = Column(DateTime, nullable=True)

    window = relationship("InferenceWindowModel", back_populates="attempts")


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),
        CheckConstraint("sequence > 0", name="chk_outbox_positive_sequence"),
        Index("ix_outbox_session_published", "session_id", "published_at", "created_at"),
        Index("uq_outbox_session_sequence", "session_id", "sequence", unique=True),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    window_id = Column(String(36), ForeignKey("inference_windows.id", ondelete="CASCADE"), nullable=True)
    idempotency_key = Column(String(255), nullable=False)
    event_type = Column(String(100), nullable=False)
    sequence = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    published_at = Column(DateTime, nullable=True)

    session = relationship("SessionModel", back_populates="outbox_events")


class SpeakerModel(Base):
    __tablename__ = "speakers"
    __table_args__ = (
        Index("uq_speakers_session_machine_label", "session_id", "machine_label", unique=True),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    machine_label = Column(String(50), nullable=False)  # e.g. "SPEAKER_00"
    display_name = Column(String(100), nullable=False)  # e.g. "Speaker 1" or user renamed "Dr. Smith"
    color = Column(String(50), nullable=False, default="#4f46e5")
    sort_order = Column(Integer, nullable=False, default=0)

    session = relationship("SessionModel", back_populates="speakers")
    words = relationship("WordModel", back_populates="speaker")
    turns = relationship("TranscriptTurnModel", back_populates="speaker")


class WordModel(Base):
    __tablename__ = "words"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    machine_text = Column(Text, nullable=False)
    edited_text = Column(Text, nullable=True)
    speaker_id = Column(String(36), ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True)
    stability = Column(String(50), nullable=False, default="committed")  # provisional, committed, finalized
    confidence = Column(Float, nullable=True)
    source_chunk_ids = Column(JSON, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    language = Column(String(20), nullable=True)
    language_confidence = Column(Float, nullable=True)
    wall_start_at = Column(DateTime, nullable=True)
    wall_end_at = Column(DateTime, nullable=True)

    session = relationship("SessionModel", back_populates="words")
    speaker = relationship("SpeakerModel", back_populates="words")


class TranscriptTurnModel(Base):
    __tablename__ = "transcript_turns"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    speaker_id = Column(String(36), ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    edited_text = Column(Text, nullable=True)  # Versioned turn-level user edit
    first_word_id = Column(String(36), nullable=True)
    last_word_id = Column(String(36), nullable=True)
    break_reason = Column(String(50), nullable=False, default="speaker_change")  # speaker_change, long_silence, user_edit, final_repair
    revision = Column(Integer, nullable=False, default=1)

    session = relationship("SessionModel", back_populates="turns")
    speaker = relationship("SpeakerModel", back_populates="turns")


class SpeakerActivityModel(Base):
    __tablename__ = "speaker_activities"
    __table_args__ = (
        Index("ix_speaker_activities_session_interval", "session_id", "start_ms", "end_ms"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    speaker_id = Column(String(36), ForeignKey("speakers.id", ondelete="CASCADE"), nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    confidence = Column(Float, nullable=True)
    stability = Column(String(50), nullable=False, default="provisional")  # provisional, committed, finalized
    overlap_group = Column(String(50), nullable=True)

    session = relationship("SessionModel", back_populates="speaker_activities")


class OverlapRegionModel(Base):
    __tablename__ = "overlap_regions"
    __table_args__ = (
        Index("ix_overlap_regions_session_interval", "session_id", "start_ms", "end_ms"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    speaker_activity_ids = Column(JSON, nullable=False, default=list)
    resolution_status = Column(String(50), nullable=False, default="detected")  # detected, mixed_only, separated_tentative, reviewed
    hypotheses = Column(JSON, nullable=False, default=list)  # list of {speaker_id, words, source, confidence}
    schema_version = Column(String(20), nullable=False, default="1.0")

    session = relationship("SessionModel", back_populates="overlap_regions")


class ReviewIssueModel(Base):
    __tablename__ = "review_issues"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)  # low_confidence, speaker_conflict, overlap, gap, boundary, language, glossary
    start_ms = Column(Integer, nullable=False)
    end_ms = Column(Integer, nullable=False)
    priority = Column(Float, nullable=False, default=0.5)
    evidence = Column(JSON, nullable=True)
    status = Column(String(50), nullable=False, default="open")  # open, resolved, accepted_as_is, not_a_problem
    resolved_by_event_id = Column(String(36), nullable=True)

    session = relationship("SessionModel", back_populates="review_issues")


class CorrectionEventModel(Base):
    __tablename__ = "correction_events"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    target_type = Column(String(50), nullable=False)  # word, turn, speaker, session
    target_id = Column(String(36), nullable=False)
    operation = Column(String(50), nullable=False)  # text_replace, split, merge, reassign, retime, overlap, rename
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    audio_start_ms = Column(Integer, nullable=True)
    audio_end_ms = Column(Integer, nullable=True)
    training_status = Column(String(50), nullable=False, default="excluded")  # excluded, candidate, approved, withdrawn
    created_at = Column(DateTime, nullable=False, default=utc_now)

    session = relationship("SessionModel", back_populates="correction_events")


class GlossaryModel(Base):
    __tablename__ = "glossaries"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(100), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    scope = Column(String(50), nullable=False, default="global")  # global, project, session
    session_id = Column(String(36), nullable=True)
    entries = Column(JSON, nullable=False, default=list)  # list of GlossaryEntry dicts
    created_at = Column(DateTime, nullable=False, default=utc_now)


class DatasetSnapshotModel(Base):
    __tablename__ = "dataset_snapshots"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    task = Column(String(50), nullable=False)  # clean_asr, code_switch_asr, diarization, overlap, vad
    manifest_path = Column(Text, nullable=False)
    source_event_hash = Column(String(64), nullable=False)
    split_policy = Column(JSON, nullable=False)
    statistics = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    schema_version = Column(String(20), nullable=False, default="1.0")


class ModelVersionModel(Base):
    __tablename__ = "model_versions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    task = Column(String(50), nullable=False)  # ASR, diarization, VAD
    base_model = Column(String(100), nullable=False)
    artifact_path = Column(Text, nullable=False)
    dataset_snapshot_id = Column(String(36), nullable=True)
    training_config = Column(JSON, nullable=True)
    metrics = Column(JSON, nullable=True)
    stage = Column(String(50), nullable=False, default="experiment")  # experiment, candidate, production, archived
    created_at = Column(DateTime, nullable=False, default=utc_now)
    backup_status = Column(String(50), nullable=False, default="not_backed_up")  # not_backed_up, queued, verified, failed
    schema_version = Column(String(20), nullable=False, default="1.0")

# ==========================================
# Pydantic Schemas for API Requests & Responses
# ==========================================

class WordSchema(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: Optional[str] = None
    stability: str = "committed"
    confidence: Optional[float] = None
    language: Optional[str] = None

class SpeakerSchema(BaseModel):
    id: str
    machine_label: str
    display_name: str
    color: str
    sort_order: int

class TurnSchema(BaseModel):
    id: str
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None
    speaker_color: Optional[str] = None
    start_ms: int
    end_ms: int
    text: str
    edited_text: Optional[str] = None
    words: List[WordSchema] = []
    break_reason: str = "speaker_change"

class AudioAssetSchema(BaseModel):
    id: str
    session_id: str
    kind: str
    status: str
    container: Optional[str] = None
    codec: Optional[str] = None
    sample_rate_hz: Optional[int] = None
    channels: Optional[int] = None
    duration_ms: Optional[int] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    derived_from_id: Optional[str] = None
    provenance: Optional[Dict[str, Any]] = None

class SpeakerActivitySchema(BaseModel):
    id: str
    speaker_id: str
    speaker_name: str
    speaker_color: str
    start_ms: int
    end_ms: int
    confidence: Optional[float] = None
    stability: str
    overlap_group: Optional[str] = None

class OverlapRegionSchema(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    speaker_activity_ids: List[str] = Field(default_factory=list)
    resolution_status: str
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list)
    schema_version: str

class SessionCreateRequest(BaseModel):
    url: str
    language_mode: str = "auto-mixed"
    asr_model: Optional[str] = None
    diarization_model: Optional[str] = None
    allowed_languages: List[str] = ["en", "tl", "ceb"]

class SessionSummarySchema(BaseModel):
    id: str
    title: str
    source_url: str
    source_type: str
    status: str
    processing_mode: str
    language_mode: str
    duration_ms: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    asr_model: str
    actual_asr_device: Optional[str] = None
    actual_compute_type: Optional[str] = None
    diarization_model: str
    speaker_count: int = 0
    deleted_at: Optional[datetime] = None

class SessionDetailSchema(SessionSummarySchema):
    speakers: List[SpeakerSchema] = []
    turns: List[TurnSchema] = []
    audio_assets: List[AudioAssetSchema] = []
    speaker_activities: List[SpeakerActivitySchema] = Field(default_factory=list)
    overlap_regions: List[OverlapRegionSchema] = Field(default_factory=list)
    audio_assets_count: int = 0
    last_durable_audio_ms: int = 0
    committed_frontier_ms: int = 0
    event_sequence: int = 0
    event_replay_floor: int = 1
    training_consent: str = "excluded"

class SpeakerRenameRequest(BaseModel):
    display_name: str
    color: Optional[str] = None

class TurnEditRequest(BaseModel):
    text: Optional[str] = None
    speaker_id: Optional[str] = None

class StorageSummarySchema(BaseModel):
    total_sessions: int
    active_sessions: int
    trashed_sessions: int
    total_audio_bytes: int
    total_export_bytes: int
