"""
Word Continuity Reconciler & Transcript Turn Builder.
Conforming to Section 5.2 and Section 6.2 of the spec.
Deduplicates overlap words, maintains monotonic committed frontier, and groups words into semantic speaker turns.
"""
import uuid
import re
from typing import List, Dict, Any, Optional, Tuple
from app.adapters.asr.faster_whisper_engine import ASRWordHypothesis
from app.config import settings

def normalize_token(text: str) -> str:
    """Lowercase and strip punctuation for token sequence alignment."""
    return re.sub(r"[^\w\s]", "", text).strip().lower()

class ReconciledWord:
    def __init__(
        self,
        id: str,
        start_ms: int,
        end_ms: int,
        text: str,
        speaker_id: Optional[str] = None,
        stability: str = "provisional",  # provisional, committed, finalized
        confidence: float = 0.8,
        language: Optional[str] = "en"
    ):
        self.id = id
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.text = text
        self.speaker_id = speaker_id
        self.stability = stability
        self.confidence = confidence
        self.language = language

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "stability": self.stability,
            "confidence": round(self.confidence, 3),
            "language": self.language
        }

class TranscriptTurnData:
    def __init__(
        self,
        id: str,
        speaker_id: Optional[str],
        start_ms: int,
        end_ms: int,
        words: List[ReconciledWord],
        break_reason: str = "speaker_change"
    ):
        self.id = id
        self.speaker_id = speaker_id
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.words = words
        self.break_reason = break_reason

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self, speakers_map: Dict[str, Any] = None) -> Dict[str, Any]:
        spk_info = (speakers_map or {}).get(self.speaker_id, {})
        return {
            "id": self.id,
            "speaker_id": self.speaker_id,
            "speaker_name": spk_info.get("display_name", "Speaker 1"),
            "speaker_color": spk_info.get("color", "#4f46e5"),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "break_reason": self.break_reason,
            "words": [w.to_dict() for w in self.words]
        }

class WordContinuityReconciler:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.committed_words: List[ReconciledWord] = []
        self.provisional_words: List[ReconciledWord] = []
        self.committed_frontier_ms: int = 0
        self.stability_margin_ms: int = int(settings.STABILITY_MARGIN_SEC * 1000)
        self.silence_threshold_ms: int = int(settings.TURN_SILENCE_THRESHOLD_SEC * 1000)
        self.default_speaker_id: str = "spk_0"
        self.active_stream_epoch: Optional[int] = None

    def begin_stream_epoch(self, stream_epoch: int) -> List[ReconciledWord]:
        """Close the revisable tail before processing audio after a discontinuity."""
        if self.active_stream_epoch is None:
            self.active_stream_epoch = stream_epoch
            return []
        if stream_epoch < self.active_stream_epoch:
            raise ValueError(
                f"Stream epoch moved backwards from {self.active_stream_epoch} to {stream_epoch}"
            )
        if stream_epoch == self.active_stream_epoch:
            return []

        committed_at_boundary = list(self.provisional_words)
        for word in committed_at_boundary:
            word.stability = "committed"
            self.committed_words.append(word)
            self.committed_frontier_ms = max(self.committed_frontier_ms, word.end_ms)
        self.provisional_words = []
        self.active_stream_epoch = stream_epoch
        return committed_at_boundary

    def reconcile_window(
        self,
        new_hypotheses: List[ASRWordHypothesis],
        current_audio_time_ms: int,
        current_speaker_id: Optional[str] = None
    ) -> Tuple[List[ReconciledWord], List[ReconciledWord]]:
        """
        Reconciles new hypotheses from the latest inference window with previously committed words.
        Returns: (newly_committed_words, all_current_provisional_words)
        """
        if not new_hypotheses:
            # Advance frontier if audio moved far ahead of speech
            new_frontier = max(0, current_audio_time_ms - self.stability_margin_ms)
            if new_frontier > self.committed_frontier_ms:
                self.committed_frontier_ms = new_frontier
            return [], self.provisional_words

        spk_id = current_speaker_id or self.default_speaker_id
        
        # Calculate new candidate committed frontier
        candidate_frontier_ms = max(0, current_audio_time_ms - self.stability_margin_ms)
        
        # Filter hypotheses that start after the existing committed frontier
        # Hypotheses before the frontier are used to align, but not re-committed
        reconciled_stream: List[ReconciledWord] = []
        
        # Find where new hypotheses overlap with the committed tail
        # We look at the last few committed words to avoid duplicates
        committed_tail = self.committed_words[-5:] if self.committed_words else []
        
        start_idx = 0
        if committed_tail and new_hypotheses:
            last_committed = committed_tail[-1]
            # Skip words that clearly end before the last committed word
            for i, hyp in enumerate(new_hypotheses):
                if hyp.start_ms >= last_committed.end_ms - 200:  # 200ms tolerance
                    # Check token similarity to prevent edge duplicates
                    if hyp.start_ms <= last_committed.end_ms + 200:
                        if normalize_token(hyp.text) == normalize_token(last_committed.text):
                            continue  # Duplicate token at boundary, skip
                    start_idx = i
                    break
            else:
                # All hypotheses are before the last committed word
                start_idx = len(new_hypotheses)

        new_words_to_process = new_hypotheses[start_idx:]
        
        newly_committed: List[ReconciledWord] = []
        new_provisionals: List[ReconciledWord] = []
        
        for hyp in new_words_to_process:
            word_obj = ReconciledWord(
                id=str(uuid.uuid4()),
                start_ms=hyp.start_ms,
                end_ms=hyp.end_ms,
                text=hyp.text,
                speaker_id=spk_id,
                confidence=hyp.confidence,
                language=hyp.language
            )
            
            # If word is before the candidate frontier, mark as committed
            if hyp.end_ms <= candidate_frontier_ms:
                word_obj.stability = "committed"
                self.committed_words.append(word_obj)
                newly_committed.append(word_obj)
                if hyp.end_ms > self.committed_frontier_ms:
                    self.committed_frontier_ms = hyp.end_ms
            else:
                word_obj.stability = "provisional"
                new_provisionals.append(word_obj)
                
        self.provisional_words = new_provisionals
        return newly_committed, self.provisional_words

    def get_all_words(self) -> List[ReconciledWord]:
        """Returns all words (committed + provisional) sorted by timestamp."""
        return self.committed_words + self.provisional_words

    def build_turns(self, speakers_map: Dict[str, Any] = None) -> List[TranscriptTurnData]:
        """
        Groups all current words into semantic speaker turns.
        Splits turns on speaker change or long silence (>2.0s), NOT on window boundaries.
        """
        all_words = self.get_all_words()
        if not all_words:
            return []
            
        turns: List[TranscriptTurnData] = []
        current_turn_words: List[ReconciledWord] = [all_words[0]]
        current_speaker = all_words[0].speaker_id
        
        for w in all_words[1:]:
            prev_w = current_turn_words[-1]
            time_gap_ms = w.start_ms - prev_w.end_ms
            
            is_speaker_change = (w.speaker_id != current_speaker)
            is_long_silence = (time_gap_ms >= self.silence_threshold_ms)
            
            if is_speaker_change or is_long_silence:
                reason = "speaker_change" if is_speaker_change else "long_silence"
                turn = TranscriptTurnData(
                    id=str(uuid.uuid4()),
                    speaker_id=current_speaker,
                    start_ms=current_turn_words[0].start_ms,
                    end_ms=current_turn_words[-1].end_ms,
                    words=list(current_turn_words),
                    break_reason=reason
                )
                turns.append(turn)
                current_turn_words = [w]
                current_speaker = w.speaker_id
            else:
                current_turn_words.append(w)
                
        if current_turn_words:
            turn = TranscriptTurnData(
                id=str(uuid.uuid4()),
                speaker_id=current_speaker,
                start_ms=current_turn_words[0].start_ms,
                end_ms=current_turn_words[-1].end_ms,
                words=list(current_turn_words),
                break_reason="ongoing"
            )
            turns.append(turn)
            
        return turns

    def finalize_all(self):
        """Called when stream ends: commit all remaining provisional words."""
        for w in self.provisional_words:
            w.stability = "finalized"
            self.committed_words.append(w)
        if self.committed_words:
            self.committed_frontier_ms = self.committed_words[-1].end_ms
        self.provisional_words = []
