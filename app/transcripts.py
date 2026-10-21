"""Reproducible sentence views over preserved source caption cues.

This is punctuation and timing processing, not speech recognition or alignment.
Legacy cue timings cannot tell us where a word was spoken inside a cue.
"""
import bisect
import hashlib
import html
import json
import math
import re
import time

SENTENCE_VERSION = "sentences-v1"
_CJK = r"\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af"
_TOKEN = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e"}
_CLOSERS = '\"\'”’」』）》】)］]'


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_json3(path):
    """Keep source cues and available fragment offsets; never deduplicate by text alone."""
    data = json.loads(path.read_text(encoding="utf-8"))
    cues = []
    for event in data.get("events", []):
        parts = event.get("segs", [])
        text = _clean(html.unescape("".join(s.get("utf8", "") for s in parts)))
        if not text:
            continue
        start = max(0, event.get("tStartMs", 0) / 1000)
        end = start + max(.01, event.get("dDurationMs", 1000) / 1000)
        cue = {"start": start, "end": end, "text": text}
        fragments = [(s, _clean(html.unescape(s.get("utf8", "")))) for s in parts]
        fragments = [(s, t) for s, t in fragments if t]
        offsets = [s.get("tOffsetMs", 0 if i == 0 else None) for i, (s, _) in enumerate(fragments)]
        if (len(offsets) > 1 and all(_number(v) for v in offsets)
                and offsets == sorted(offsets) and 0 <= offsets[0] < offsets[-1] < (end - start) * 1000):
            cue["words"] = [{"text": t, "start": start + offsets[i] / 1000,
                             "end": start + offsets[i + 1] / 1000 if i + 1 < len(offsets) else end}
                            for i, (_, t) in enumerate(fragments)]
        cues.append(cue)
    return sorted(cues, key=lambda s: s["start"])


def _separator(left, right):
    if not left or not right or right[0] in ",.!?;:，。！？；：、" + _CLOSERS:
        return ""
    if re.match(rf"[{_CJK}]", left[-1]) and re.match(rf"[{_CJK}]", right[0]):
        return ""
    return " "


def _terminal_boundaries(text):
    """English/CJK punctuation with decimal, title, initial and acronym guards."""
    i = 0
    while i < len(text):
        char = text[i]
        if char not in ".!?。！？":
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end] in ".!?。！？":
            end += 1
        after = end
        while after < len(text) and text[after] in _CLOSERS:
            after += 1
        following = after
        while following < len(text) and text[following].isspace():
            following += 1
        next_char = text[following] if following < len(text) else ""
        if char == ".":
            token = re.search(r"([A-Za-z][A-Za-z.]*)\.$", text[max(0, i-32):i + 1])
            word = token[1] if token else ""
            decimal = i > 0 and i + 1 < len(text) and text[i-1].isdigit() and text[i+1].isdigit()
            interior = i + 1 < len(text) and text[i+1].isalnum()
            abbreviation = (word.lower() in _ABBREVIATIONS
                            or (word.lower() in ("no", "fig") and next_char.isdigit())
                            or bool(re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", word))
                            or (len(word) == 1 and word.isupper()))
            ellipsis = end - i > 1 and next_char.islower()
            if decimal or interior or (abbreviation and next_char) or ellipsis:
                i = end
                continue
        yield after
        i = after


def sentence_view(raw, language="en", source="youtube_auto", duration=0):
    """Join/split without rewriting words; bound unpunctuated passages as fragments.

    Clip overlapping display tails to the next cue start before interpolation.
    Only same-start rolling revisions are collapsed; actual repetitions survive.
    """
    cues, invalid, revisions = [], 0, 0
    for item in raw:
        start, end, text = item.get("start"), item.get("end"), item.get("text", "")
        if not (_number(start) and _number(end) and end > start >= 0 and isinstance(text, str) and _clean(text)):
            invalid += 1
            continue
        if duration > 0 and start >= duration:
            invalid += 1
            continue
        cues.append({**item, "text": _clean(text), "end": min(end, duration) if duration > 0 else end})
    cues.sort(key=lambda x: x["start"])
    normalized = []
    for cue in cues:
        if normalized and cue["start"] == normalized[-1]["start"]:
            previous = normalized[-1]
            if cue["text"].startswith(previous["text"]):
                normalized[-1] = cue
                revisions += 1
            elif previous["text"].startswith(cue["text"]):
                revisions += 1
            else:
                normalized[-1] = {"start": cue["start"], "end": max(cue["end"], previous["end"]),
                                  "text": previous["text"] + _separator(previous["text"], cue["text"]) + cue["text"]}
        else:
            normalized.append(cue)

    chunks, spans, forced = [], [], {}
    length, last_text = 0, ""
    for i, cue in enumerate(normalized):
        text, start = cue["text"], cue["start"]
        end = min(cue["end"], normalized[i + 1]["start"]) if i + 1 < len(normalized) else cue["end"]
        separator = _separator(last_text, text)
        chunks.extend((separator, text))
        length += len(separator)
        base = length
        tokens = list(_TOKEN.finditer(text))
        weights = [max(1, len(m[0].strip(".,!?;:，。！？；："))) for m in tokens]
        total, used = sum(weights), 0
        local_spans = []
        for m, weight in zip(tokens, weights):
            local_spans.append((base + m.start(), base + m.end(), start + (end-start)*used/total,
                                start + (end-start)*(used+weight)/total, "estimated"))
            used += weight
        words = cue.get("words", [])
        if words:
            word_spans, offset = [], 0
            for word in words:
                wt = _clean(word.get("text", ""))
                position = text.find(wt, offset) if wt else -1
                ws, we = word.get("start"), word.get("end")
                if (position < 0 or text[offset:position].strip() or not (_number(ws) and _number(we))
                        or not start <= ws < we or ws >= end or (word_spans and ws < word_spans[-1][3])):
                    word_spans = []
                    break
                word_spans.append((base + position, base + position + len(wt), ws, min(end, we), "caption_words"))
                offset = position + len(wt)
            if word_spans and not text[offset:].strip():
                local_spans = word_spans
        spans.extend(local_spans)
        length += len(text)
        last_text = text
        if source == "manual":
            forced[length] = "manual"
        elif i + 1 < len(normalized) and normalized[i+1]["start"] - end >= 1.5:
            forced[length] = "pause"

    text = "".join(chunks)
    if not spans:
        return [], {"version": SENTENCE_VERSION, "raw_count": len(raw), "sentence_count": 0,
                    "fragment_count": 0, "timing": "estimated", "invalid_cues": invalid, "rolling_revisions": revisions}
    ends = [s[1] for s in spans]

    def clock(position, is_end=False):
        index = bisect.bisect_left(ends, position) if is_end else bisect.bisect_right(ends, position)
        span = spans[min(index, len(spans)-1)]
        ratio = max(0, min(1, (position-span[0]) / (span[1]-span[0])))
        return span[2] + ratio * (span[3]-span[2]), span

    natural = set(_terminal_boundaries(text))
    events = sorted(set(ends) | natural | set(forced) | {len(text)})
    result, cursor, previous = [], 0, 0
    max_chars = 240 if language == "zh" else 800

    def emit(stop, reason):
        nonlocal cursor
        begin = cursor
        while begin < stop and text[begin].isspace():
            begin += 1
        trimmed = stop
        while trimmed > begin and text[trimmed-1].isspace():
            trimmed -= 1
        if trimmed > begin:
            start, first = clock(begin)
            end, last = clock(trimmed, True)
            timing = "caption_words" if first[4] == last[4] == "caption_words" and begin == first[0] and trimmed == last[1] else "estimated"
            result.append({"start": round(start, 6), "end": round(max(start + .000001, end), 6),
                           "text": text[begin:trimmed], "kind": "sentence" if reason in ("punctuation", "manual") else "fragment",
                           "boundary": reason, "timing": timing})
        cursor = stop

    for position in events:
        if previous > cursor and (position-cursor > max_chars or clock(position, True)[0] - clock(cursor)[0] > 45):
            emit(previous, "length")
        if position in natural:
            emit(position, "punctuation")
        elif position in forced:
            emit(position, forced[position])
        previous = position
    emit(len(text), "end")
    qualities = {s["timing"] for s in result}
    return result, {"version": SENTENCE_VERSION, "raw_count": len(raw), "sentence_count": len(result),
                    "fragment_count": sum(s["kind"] == "fragment" for s in result),
                    "timing": next(iter(qualities)) if len(qualities) == 1 else "mixed",
                    "invalid_cues": invalid, "rolling_revisions": revisions}


def save_transcript(connection, episode_id, transcript):
    """Publish raw source and invalidate its derived view in the caller's transaction."""
    connection.execute("INSERT OR REPLACE INTO transcripts (episode_id,segments,source,language) VALUES (?,?,?,?)",
                       (episode_id, json.dumps(transcript["segments"], ensure_ascii=False), transcript["source"], transcript["language"]))
    connection.execute("DELETE FROM transcript_sentences WHERE episode_id=?", (episode_id,))


def read_transcript(db, episode_id, *, view="sentences", rebuild=False):
    # Serializing with source publication prevents a stale rebuild replacing a new import.
    with db.connect(immediate=view == "sentences") as c:
        row = c.execute("SELECT t.*,e.duration FROM transcripts t JOIN episodes e ON e.id=t.episode_id WHERE episode_id=?", (episode_id,)).fetchone()
        if not row:
            return None
        result = {"episode_id": episode_id, "language": row["language"], "source": row["source"], "view": view}
        if view == "raw":
            return {**result, "segments": json.loads(row["segments"])}
        fingerprint = hashlib.sha256(json.dumps([row["segments"], row["source"], row["language"], row["duration"], SENTENCE_VERSION], ensure_ascii=False).encode()).hexdigest()
        cached = c.execute("SELECT * FROM transcript_sentences WHERE episode_id=?", (episode_id,)).fetchone()
        changed = not cached or cached["fingerprint"] != fingerprint
        if rebuild or changed:
            segments, metadata = sentence_view(json.loads(row["segments"]), row["language"], row["source"], row["duration"])
            c.execute("INSERT OR REPLACE INTO transcript_sentences VALUES (?,?,?,?,?)",
                      (episode_id, fingerprint, json.dumps(segments, ensure_ascii=False), json.dumps(metadata), time.time()))
        else:
            segments, metadata = json.loads(cached["segments"]), json.loads(cached["metadata"])
        return {**result, "segments": segments, "processing": metadata, **({"changed": changed} if rebuild else {})}


def vtt(segments):
    def timestamp(seconds):
        total = round(seconds * 1000)
        hours, total = divmod(total, 3600000)
        minutes, total = divmod(total, 60000)
        seconds, milliseconds = divmod(total, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"
    return "WEBVTT\n\n" + "\n\n".join(
        f"{timestamp(s['start'])} --> {timestamp(s['end'])}\n{html.escape(s['text'])}" for s in segments) + "\n"
