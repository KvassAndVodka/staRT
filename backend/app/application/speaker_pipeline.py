"""Transactional final speaker attribution and overlap reconstruction."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment
from sqlalchemy import asc, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.models import (
    OverlapRegionModel,
    SessionModel,
    SpeakerActivityModel,
    SpeakerModel,
    TranscriptTurnModel,
    WordModel,
)
from app.ports.diarization import DiarizationError, DiarizationSegment


SPEAKER_COLORS = (
    "#4f46e5",
    "#0891b2",
    "#059669",
    "#d97706",
    "#dc2626",
    "#9333ea",
    "#db2777",
    "#4d7c0f",
)


@dataclass(frozen=True)
class _NormalizedSegment:
    machine_label: str
    start_ms: int
    end_ms: int
    confidence: float | None


@dataclass(frozen=True)
class _TimelineSlice:
    start_ms: int
    end_ms: int
    active: tuple[tuple[str, float | None], ...]


@dataclass(frozen=True)
class FinalSpeakerResult:
    speaker_count: int
    activity_count: int
    overlap_count: int
    unresolved_word_count: int


def _intersection_ms(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> int:
    return max(0, min(first_end, second_end) - max(first_start, second_start))


def _merge_confidence(first: float | None, second: float | None) -> float | None:
    values = [value for value in (first, second) if value is not None]
    return min(values) if values else None


def _normalize_segments(
    segments: Iterable[DiarizationSegment],
    duration_ms: int,
) -> list[_NormalizedSegment]:
    if duration_ms < 0:
        raise DiarizationError("Session duration cannot be negative")

    grouped: dict[str, list[DiarizationSegment]] = {}
    for segment in segments:
        label = segment.machine_label.strip()
        if not label or len(label) > 50:
            raise DiarizationError("Diarization labels must contain 1 to 50 characters")
        if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
            raise DiarizationError(f"Invalid diarization interval for {label}")
        if segment.end_ms > duration_ms:
            raise DiarizationError(
                f"Diarization interval for {label} exceeds the session duration"
            )
        if segment.confidence is not None and (
            not math.isfinite(segment.confidence)
            or not 0.0 <= segment.confidence <= 1.0
        ):
            raise DiarizationError(f"Invalid diarization confidence for {label}")
        grouped.setdefault(label, []).append(segment)

    normalized: list[_NormalizedSegment] = []
    for label, label_segments in sorted(grouped.items()):
        current: _NormalizedSegment | None = None
        for segment in sorted(label_segments, key=lambda item: (item.start_ms, item.end_ms)):
            candidate = _NormalizedSegment(
                machine_label=label,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                confidence=segment.confidence,
            )
            if current is not None and candidate.start_ms <= current.end_ms:
                current = _NormalizedSegment(
                    machine_label=label,
                    start_ms=current.start_ms,
                    end_ms=max(current.end_ms, candidate.end_ms),
                    confidence=_merge_confidence(current.confidence, candidate.confidence),
                )
            else:
                if current is not None:
                    normalized.append(current)
                current = candidate
        if current is not None:
            normalized.append(current)
    return sorted(normalized, key=lambda item: (item.start_ms, item.end_ms, item.machine_label))


def _build_timeline(segments: Sequence[_NormalizedSegment]) -> list[_TimelineSlice]:
    boundaries = sorted({point for segment in segments for point in (segment.start_ms, segment.end_ms)})
    timeline: list[_TimelineSlice] = []
    for start_ms, end_ms in zip(boundaries, boundaries[1:]):
        active = tuple(sorted(
            (
                (segment.machine_label, segment.confidence)
                for segment in segments
                if segment.start_ms < end_ms and segment.end_ms > start_ms
            ),
            key=lambda item: item[0],
        ))
        if not active:
            continue
        if timeline and timeline[-1].end_ms == start_ms and timeline[-1].active == active:
            previous = timeline[-1]
            timeline[-1] = _TimelineSlice(previous.start_ms, end_ms, active)
        else:
            timeline.append(_TimelineSlice(start_ms, end_ms, active))
    return timeline


class FinalSpeakerPipeline:
    """Replace final speaker truth in one caller-owned database transaction."""

    async def apply(
        self,
        db: AsyncSession,
        session_id: str,
        segments: Sequence[DiarizationSegment],
        *,
        duration_ms: int,
    ) -> FinalSpeakerResult:
        session = await db.get(SessionModel, session_id)
        if session is None:
            raise DiarizationError(f"Session {session_id} does not exist")

        normalized = _normalize_segments(segments, duration_ms)
        timeline = _build_timeline(normalized)
        speaker_map = await self._map_speakers(db, session_id, normalized)

        await db.execute(
            delete(OverlapRegionModel).where(OverlapRegionModel.session_id == session_id)
        )
        await db.execute(
            delete(SpeakerActivityModel).where(SpeakerActivityModel.session_id == session_id)
        )
        await db.execute(
            delete(TranscriptTurnModel).where(TranscriptTurnModel.session_id == session_id)
        )

        activity_count = 0
        overlap_count = 0
        for item in timeline:
            overlap_id = str(uuid.uuid4()) if len(item.active) > 1 else None
            activity_ids: list[str] = []
            for label, confidence in item.active:
                activity_id = str(uuid.uuid4())
                activity_ids.append(activity_id)
                db.add(SpeakerActivityModel(
                    id=activity_id,
                    session_id=session_id,
                    speaker_id=speaker_map[label].id,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    confidence=confidence,
                    stability="finalized",
                    overlap_group=overlap_id,
                ))
                activity_count += 1
            if overlap_id is not None:
                db.add(OverlapRegionModel(
                    id=overlap_id,
                    session_id=session_id,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    speaker_activity_ids=activity_ids,
                    resolution_status="mixed_only",
                    hypotheses=[],
                    schema_version="1.0",
                ))
                overlap_count += 1

        words_result = await db.execute(
            select(WordModel)
            .where(WordModel.session_id == session_id)
            .order_by(asc(WordModel.start_ms), asc(WordModel.end_ms), asc(WordModel.id))
        )
        words = words_result.scalars().all()
        unresolved = self._attribute_words(words, timeline, speaker_map)
        self._rebuild_turns(db, session_id, words)
        await db.flush()
        return FinalSpeakerResult(
            speaker_count=len(speaker_map),
            activity_count=activity_count,
            overlap_count=overlap_count,
            unresolved_word_count=unresolved,
        )

    async def _map_speakers(
        self,
        db: AsyncSession,
        session_id: str,
        segments: Sequence[_NormalizedSegment],
    ) -> dict[str, SpeakerModel]:
        labels = sorted({segment.machine_label for segment in segments})
        speakers_result = await db.execute(
            select(SpeakerModel)
            .where(SpeakerModel.session_id == session_id)
            .order_by(asc(SpeakerModel.sort_order), asc(SpeakerModel.id))
        )
        existing = speakers_result.scalars().all()
        activities_result = await db.execute(
            select(SpeakerActivityModel)
            .where(SpeakerActivityModel.session_id == session_id)
        )
        old_activities = activities_result.scalars().all()

        scores: dict[tuple[str, str], int] = {}
        for segment in segments:
            for activity in old_activities:
                overlap = _intersection_ms(
                    segment.start_ms,
                    segment.end_ms,
                    activity.start_ms,
                    activity.end_ms,
                )
                if overlap:
                    key = (segment.machine_label, activity.speaker_id)
                    scores[key] = scores.get(key, 0) + overlap

        assigned_labels: set[str] = set()
        assigned_speakers: set[str] = set()
        mapping: dict[str, SpeakerModel] = {}
        if labels and existing and scores:
            weights = np.array([
                [scores.get((label, speaker.id), 0) for speaker in existing]
                for label in labels
            ], dtype=np.int64)
            label_indexes, speaker_indexes = linear_sum_assignment(weights, maximize=True)
            for label_index, speaker_index in zip(label_indexes, speaker_indexes):
                if weights[label_index, speaker_index] <= 0:
                    continue
                label = labels[int(label_index)]
                speaker = existing[int(speaker_index)]
                mapping[label] = speaker
                assigned_labels.add(label)
                assigned_speakers.add(speaker.id)

        for label in labels:
            if label in assigned_labels:
                continue
            exact = next(
                (
                    speaker for speaker in existing
                    if speaker.id not in assigned_speakers and speaker.machine_label == label
                ),
                None,
            )
            if exact is not None:
                mapping[label] = exact
                assigned_labels.add(label)
                assigned_speakers.add(exact.id)

        if not old_activities:
            available = [speaker for speaker in existing if speaker.id not in assigned_speakers]
            for label, speaker in zip(
                (label for label in labels if label not in assigned_labels),
                available,
            ):
                mapping[label] = speaker
                assigned_labels.add(label)
                assigned_speakers.add(speaker.id)

        final_labels = set(labels)
        for speaker in existing:
            mapped_label = next(
                (label for label, mapped in mapping.items() if mapped.id == speaker.id),
                None,
            )
            if speaker.machine_label in final_labels and mapped_label != speaker.machine_label:
                speaker.machine_label = f"LEGACY_{speaker.id}"[:50]
            elif mapped_label is not None and speaker.machine_label != mapped_label:
                speaker.machine_label = f"REMAP_{speaker.id}"[:50]
        await db.flush()

        next_sort_order = max((speaker.sort_order for speaker in existing), default=-1) + 1
        for label in labels:
            speaker = mapping.get(label)
            if speaker is None:
                speaker = SpeakerModel(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    machine_label=label,
                    display_name=f"Speaker {next_sort_order + 1}",
                    color=SPEAKER_COLORS[next_sort_order % len(SPEAKER_COLORS)],
                    sort_order=next_sort_order,
                )
                next_sort_order += 1
                db.add(speaker)
                mapping[label] = speaker
            else:
                speaker.machine_label = label
        await db.flush()
        active_speaker_ids = [speaker.id for speaker in mapping.values()]
        delete_stale = (
            delete(SpeakerModel)
            .where(SpeakerModel.session_id == session_id)
        )
        if active_speaker_ids:
            delete_stale = delete_stale.where(SpeakerModel.id.not_in(active_speaker_ids))
        await db.execute(delete_stale)
        await db.flush()
        return mapping

    @staticmethod
    def _attribute_words(
        words: Sequence[WordModel],
        timeline: Sequence[_TimelineSlice],
        speaker_map: dict[str, SpeakerModel],
    ) -> int:
        unresolved = 0
        for word in words:
            relevant = [
                item for item in timeline
                if _intersection_ms(word.start_ms, word.end_ms, item.start_ms, item.end_ms)
            ]
            if any(len(item.active) > 1 for item in relevant):
                word.speaker_id = None
                unresolved += 1
                continue
            scores: dict[str, int] = {}
            for item in relevant:
                label = item.active[0][0]
                scores[label] = scores.get(label, 0) + _intersection_ms(
                    word.start_ms,
                    word.end_ms,
                    item.start_ms,
                    item.end_ms,
                )
            if not scores:
                word.speaker_id = None
                unresolved += 1
                continue
            label = min(scores, key=lambda item: (-scores[item], item))
            word.speaker_id = speaker_map[label].id
            word.stability = "finalized"
        return unresolved

    @staticmethod
    def _rebuild_turns(
        db: AsyncSession,
        session_id: str,
        words: Sequence[WordModel],
    ) -> None:
        if not words:
            return
        silence_threshold_ms = int(settings.TURN_SILENCE_THRESHOLD_SEC * 1000)
        groups: list[tuple[list[WordModel], str]] = []
        current = [words[0]]
        current_reason = "final_repair"
        for word in words[1:]:
            previous = current[-1]
            speaker_change = word.speaker_id != previous.speaker_id
            long_silence = word.start_ms - previous.end_ms > silence_threshold_ms
            if speaker_change or long_silence:
                groups.append((current, current_reason))
                current = [word]
                current_reason = "speaker_change" if speaker_change else "long_silence"
            else:
                current.append(word)
        groups.append((current, current_reason))

        for group, reason in groups:
            db.add(TranscriptTurnModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                speaker_id=group[0].speaker_id,
                start_ms=group[0].start_ms,
                end_ms=group[-1].end_ms,
                first_word_id=group[0].id,
                last_word_id=group[-1].id,
                break_reason=reason,
            ))
