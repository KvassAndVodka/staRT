"""
Tests for Exporter Formats
"""
import json
from app.adapters.exporters.export_service import ExportService

SAMPLE_TURNS = [
    {
        "id": "turn-1",
        "speaker_id": "spk-1",
        "speaker_name": "Teacher",
        "start_ms": 1000,
        "end_ms": 4500,
        "text": "Maayong buntag sa tanan.",
        "words": []
    },
    {
        "id": "turn-2",
        "speaker_id": "spk-2",
        "speaker_name": "Student",
        "start_ms": 5000,
        "end_ms": 7200,
        "text": "Good morning ma'am!",
        "words": []
    }
]

def test_txt_export():
    txt = ExportService.export_txt("Physics Lecture", SAMPLE_TURNS)
    assert "Physics Lecture" in txt
    assert "Teacher [00:01]:" in txt
    assert "Maayong buntag sa tanan." in txt
    assert "Student [00:05]:" in txt

def test_markdown_export():
    md = ExportService.export_markdown("Physics Lecture", SAMPLE_TURNS)
    assert "# Physics Lecture" in md
    assert "### **Teacher** `00:01`" in md
    assert "Maayong buntag sa tanan." in md

def test_srt_export():
    srt = ExportService.export_srt(SAMPLE_TURNS)
    assert "1\n00:00:01,000 --> 00:00:04,500\n[Teacher] Maayong buntag sa tanan." in srt
    assert "2\n00:00:05,000 --> 00:00:07,200\n[Student] Good morning ma'am!" in srt

def test_vtt_export():
    vtt = ExportService.export_vtt(SAMPLE_TURNS)
    assert "WEBVTT" in vtt
    assert "00:00:01.000 --> 00:00:04.500\n<v Teacher>Maayong buntag sa tanan.</v>" in vtt
    assert "00:00:05.000 --> 00:00:07.200\n<v Student>Good morning ma'am!</v>" in vtt

def test_json_export():
    json_str = ExportService.export_json({"id": "s1", "title": "Physics Lecture"}, SAMPLE_TURNS, [])
    parsed = json.loads(json_str)
    assert parsed["schema_version"] == "1.0"
    assert parsed["session"]["title"] == "Physics Lecture"
    assert len(parsed["turns"]) == 2
