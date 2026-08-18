"""
Transcript Export Service.
Generates TXT, Markdown, SRT, WebVTT (with voice spans), and Lossless JSON.
Conforming to Section 4.4 and Section 5.5 of the spec.
"""
import json
from typing import List, Dict, Any, Optional

def ms_to_srt_time(ms: int) -> str:
    """Format milliseconds as HH:MM:SS,mmm for SRT."""
    total_seconds = ms / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(ms % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

def ms_to_vtt_time(ms: int) -> str:
    """Format milliseconds as HH:MM:SS.mmm for WebVTT."""
    total_seconds = ms / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(ms % 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"

def ms_to_display_time(ms: int) -> str:
    """Format milliseconds as MM:SS or HH:MM:SS for plain text/markdown."""
    total_seconds = int(ms / 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

class ExportService:
    @staticmethod
    def export_txt(session_title: str, turns: List[Dict[str, Any]], include_timestamps: bool = True, include_speakers: bool = True) -> str:
        lines = [f"{session_title}\n", "=" * len(session_title), "\n"]
        for turn in turns:
            spk = turn.get("speaker_name", "Speaker")
            time_str = f"[{ms_to_display_time(turn['start_ms'])}]" if include_timestamps else ""
            
            header_parts = []
            if include_speakers:
                header_parts.append(spk)
            if include_timestamps:
                header_parts.append(time_str)
                
            if header_parts:
                lines.append(f"{' '.join(header_parts)}:\n")
            lines.append(f"{turn['text']}\n\n")
        return "".join(lines)

    @staticmethod
    def export_markdown(session_title: str, turns: List[Dict[str, Any]], include_timestamps: bool = True, include_speakers: bool = True) -> str:
        lines = [f"# {session_title}\n\n"]
        for turn in turns:
            spk = turn.get("speaker_name", "Speaker")
            time_str = f"`{ms_to_display_time(turn['start_ms'])}`" if include_timestamps else ""
            
            if include_speakers and include_timestamps:
                lines.append(f"### **{spk}** {time_str}\n\n")
            elif include_speakers:
                lines.append(f"### **{spk}**\n\n")
            elif include_timestamps:
                lines.append(f"**{time_str}**\n\n")
                
            lines.append(f"{turn['text']}\n\n")
        return "".join(lines)

    @staticmethod
    def export_srt(turns: List[Dict[str, Any]], include_speakers: bool = True) -> str:
        """
        SubRip format. Subtitle cues are created per turn or segmented for readability.
        """
        cues = []
        cue_idx = 1
        for turn in turns:
            spk = turn.get("speaker_name", "Speaker")
            start_str = ms_to_srt_time(turn["start_ms"])
            end_str = ms_to_srt_time(turn["end_ms"])
            text = turn["text"]
            if include_speakers:
                text = f"[{spk}] {text}"
                
            cues.append(f"{cue_idx}\n{start_str} --> {end_str}\n{text}\n")
            cue_idx += 1
        return "\n".join(cues)

    @staticmethod
    def export_vtt(turns: List[Dict[str, Any]], include_speakers: bool = True) -> str:
        """
        WebVTT format with voice spans <v Speaker>Text</v>.
        """
        cues = ["WEBVTT\n"]
        for turn in turns:
            spk = turn.get("speaker_name", "Speaker")
            start_str = ms_to_vtt_time(turn["start_ms"])
            end_str = ms_to_vtt_time(turn["end_ms"])
            text = turn["text"]
            if include_speakers:
                cue_body = f"<v {spk}>{text}</v>"
            else:
                cue_body = text
            cues.append(f"{start_str} --> {end_str}\n{cue_body}\n")
        return "\n".join(cues)

    @staticmethod
    def export_json(session_data: Dict[str, Any], turns: List[Dict[str, Any]], speakers: List[Dict[str, Any]]) -> str:
        """
        Lossless structured JSON export.
        """
        payload = {
            "schema_version": "1.0",
            "session": session_data,
            "speakers": speakers,
            "turns": turns
        }
        return json.dumps(payload, indent=2, default=str)
