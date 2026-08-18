"""
Tests for Word Continuity Reconciler & Turn Builder
"""
import pytest
from app.application.continuity import WordContinuityReconciler, normalize_token
from app.adapters.asr.faster_whisper_engine import ASRWordHypothesis

def test_normalize_token():
    assert normalize_token("Hello,") == "hello"
    assert normalize_token(" World! ") == "world"
    assert normalize_token("kamusta?") == "kamusta"

def test_reconciler_frontier_advancement():
    reconciler = WordContinuityReconciler("session-test-1")
    reconciler.stability_margin_ms = 3000  # 3s margin
    
    # Window 1: speech from 0s to 5s in audio spanning 0 to 6s
    hypotheses_w1 = [
        ASRWordHypothesis(100, 600, "Good", 0.9),
        ASRWordHypothesis(650, 1100, "morning", 0.95),
        ASRWordHypothesis(1200, 1800, "everyone,", 0.88),
        ASRWordHypothesis(2000, 2600, "welcome", 0.92),
        ASRWordHypothesis(2700, 3200, "to", 0.97),
        ASRWordHypothesis(3300, 3800, "class.", 0.94),
    ]
    
    newly_committed, provisionals = reconciler.reconcile_window(
        hypotheses_w1,
        current_audio_time_ms=6000,
        current_speaker_id="spk_1"
    )
    
    # Audio is at 6000ms, stability margin is 3000ms, candidate frontier is 3000ms
    # Words ending <= 3000ms should be committed ("Good", "morning", "everyone,", "welcome")
    committed_texts = [w.text for w in newly_committed]
    assert "Good" in committed_texts
    assert "morning" in committed_texts
    assert "everyone," in committed_texts
    assert "welcome" in committed_texts
    
    # Words ending > 3000ms should be provisional ("to", "class.")
    provisional_texts = [w.text for w in provisionals]
    assert "to" in provisional_texts
    assert "class." in provisional_texts
    assert reconciler.committed_frontier_ms == 2600

def test_reconciler_overlap_deduplication():
    reconciler = WordContinuityReconciler("session-test-2")
    reconciler.stability_margin_ms = 3000
    
    # Window 1: 0 -> 4000ms
    hypotheses_w1 = [
        ASRWordHypothesis(500, 900, "Magandang", 0.9),
        ASRWordHypothesis(950, 1500, "araw", 0.9),
        ASRWordHypothesis(1600, 2100, "sa", 0.9),
        ASRWordHypothesis(2200, 2800, "inyo.", 0.9),
    ]
    reconciler.reconcile_window(hypotheses_w1, current_audio_time_ms=5000)
    
    # Window 2: overlapping audio, repeats "sa", "inyo." and adds "kamusta"
    hypotheses_w2 = [
        ASRWordHypothesis(1600, 2100, "sa", 0.9),
        ASRWordHypothesis(2200, 2800, "inyo.", 0.9),
        ASRWordHypothesis(3000, 3600, "kamusta", 0.95),
        ASRWordHypothesis(3700, 4200, "kayo?", 0.9),
    ]
    newly_committed, provisionals = reconciler.reconcile_window(hypotheses_w2, current_audio_time_ms=8000)
    
    all_words = reconciler.get_all_words()
    all_texts = [w.text for w in all_words]
    
    # Count occurrences of words
    assert all_texts.count("Magandang") == 1
    assert all_texts.count("araw") == 1
    assert all_texts.count("sa") == 1
    assert all_texts.count("inyo.") == 1
    assert "kamusta" in all_texts
    assert "kayo?" in all_texts

def test_turn_grouping_no_false_splits():
    reconciler = WordContinuityReconciler("session-test-3")
    reconciler.silence_threshold_ms = 2000  # 2s silence splits turn
    
    hypotheses = [
        ASRWordHypothesis(0, 500, "This", 0.9),
        ASRWordHypothesis(550, 1000, "is", 0.9),
        ASRWordHypothesis(1050, 1600, "a", 0.9),
        ASRWordHypothesis(1650, 2200, "single", 0.9),
        ASRWordHypothesis(2250, 2800, "continuous", 0.9),
        ASRWordHypothesis(2850, 3400, "monologue.", 0.9),
    ]
    
    reconciler.reconcile_window(hypotheses, current_audio_time_ms=4000, current_speaker_id="spk_0")
    turns = reconciler.build_turns()
    
    # All speech is under the same speaker with short pauses -> exactly 1 turn
    assert len(turns) == 1
    assert turns[0].text == "This is a single continuous monologue."
