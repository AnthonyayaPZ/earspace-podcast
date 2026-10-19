import html
import json
import re


def parse_json3(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = []
    for event in data.get("events", []):
        text = html.unescape("".join(s.get("utf8", "") for s in event.get("segs", [])))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start = max(0, event.get("tStartMs", 0) / 1000)
        end = start + max(.01, event.get("dDurationMs", 1000) / 1000)
        # Rolling automatic captions may repeat the previous event's prefix.
        if segments and text == segments[-1]["text"]:
            segments[-1]["end"] = max(end, segments[-1]["end"])
            continue
        segments.append({"start": start, "end": end, "text": text})
    return sorted(segments, key=lambda s: s["start"])


def vtt(segments):
    def timestamp(seconds):
        total = round(seconds * 1000)
        hours, total = divmod(total, 3600000)
        minutes, total = divmod(total, 60000)
        seconds, milliseconds = divmod(total, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"
    return "WEBVTT\n\n" + "\n\n".join(
        f"{timestamp(s['start'])} --> {timestamp(s['end'])}\n{html.escape(s['text'])}" for s in segments) + "\n"
