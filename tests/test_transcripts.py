import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.transcripts import parse_json3, read_transcript, save_transcript, sentence_view


def cue(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


def assert_ranges(segments, duration=None):
    assert all(0 <= s["start"] < s["end"] for s in segments)
    assert all(a["end"] <= b["start"] for a, b in zip(segments, segments[1:]))
    if duration:
        assert all(s["end"] <= duration for s in segments)


def test_merge_across_cues_split_inside_cues_and_clip_display_overlap():
    raw = [cue(.1, 2.2, "There has been a lot of"), cue(1.237, 3.521, "misinformation about"),
           cue(2.558, 3.742, "AI."), cue(3.287, 5.711, "This is Andrew. He was"),
           cue(4.749, 7.164, "the co-founder of Google"), cue(6.203, 8.559, "Brain and Coursera. His"),
           cue(7.588, 10, "course is popular.")]
    result, meta = sentence_view(raw)
    assert [s["text"] for s in result] == ["There has been a lot of misinformation about AI.",
        "This is Andrew.", "He was the co-founder of Google Brain and Coursera.", "His course is popular."]
    assert result[0]["start"] == .1 and result[0]["end"] == result[1]["start"] == 3.287
    assert meta["raw_count"] == 7 and meta["timing"] == "estimated"
    assert_ranges(result)


@pytest.mark.parametrize("text,expected", [
    ('Dr. Ng met Mr. Smith in the U.S. at 3.14 p.m. today. No. We agreed.',
     ['Dr. Ng met Mr. Smith in the U.S. at 3.14 p.m. today.', 'No.', 'We agreed.']),
    ('J. Smith said, "Great!" Is it? Yes!', ['J. Smith said, "Great!"', 'Is it?', 'Yes!']),
    ('See e.g. Fig. 2 for version 3.14. Next one.', ['See e.g. Fig. 2 for version 3.14.', 'Next one.']),
    ('Wait... maybe later. Okay.', ['Wait... maybe later.', 'Okay.']),
])
def test_english_punctuation_guards(text, expected):
    result, _ = sentence_view([cue(0, 12, text)])
    assert [s["text"] for s in result] == expected
    assert_ranges(result)


def test_chinese_and_mixed_text_quotes_without_inserting_spaces_between_characters():
    result, _ = sentence_view([cue(0, 2, '他说：“今天学习'), cue(2, 5, '人工智能。”然后练习 English。可以吗？当然！')], 'zh')
    assert [s["text"] for s in result] == ['他说：“今天学习人工智能。”', '然后练习 English。', '可以吗？', '当然！']
    assert_ranges(result)


def test_only_same_anchor_revisions_deduplicate_and_repetition_survives():
    result, meta = sentence_view([cue(0, 2, 'Hello'), cue(0, 3, 'Hello world.'), cue(3, 4, 'Yes.'), cue(4, 5, 'Yes.')])
    assert [s['text'] for s in result] == ['Hello world.', 'Yes.', 'Yes.']
    assert meta['rolling_revisions'] == 1
    assert_ranges(result)


def test_pause_and_long_unpunctuated_text_are_explicit_fragments_not_fake_sentences():
    result, meta = sentence_view([cue(0, 1, 'unfinished'), cue(4, 6, 'another fragment')])
    assert [s['boundary'] for s in result] == ['pause', 'end']
    assert meta['fragment_count'] == 2
    raw = [cue(i, i+1, f'word{i}') for i in range(160)]
    result, meta = sentence_view(raw)
    assert len(result) > 1 and all(s['kind'] == 'fragment' for s in result)
    assert ' '.join(s['text'] for s in result) == ' '.join(c['text'] for c in raw)
    assert all(s['end']-s['start'] <= 45 for s in result)
    assert_ranges(result)


def test_word_offsets_are_preserved_and_used_instead_of_character_estimates(tmp_path):
    path = tmp_path / 'caption.json3'
    path.write_text(json.dumps({'events': [{'tStartMs': 1000, 'dDurationMs': 9000, 'segs': [
        {'utf8': 'Hello.'}, {'utf8': ' Another', 'tOffsetMs': 1000}, {'utf8': ' sentence.', 'tOffsetMs': 7000}]}]}))
    raw = parse_json3(path)
    assert raw[0]['words'][1]['start'] == 2
    result, meta = sentence_view(raw)
    assert [s['text'] for s in result] == ['Hello.', 'Another sentence.']
    assert result[0]['end'] == result[1]['start'] == 2
    assert meta['timing'] == 'caption_words'
    assert_ranges(result)


def test_invalid_and_out_of_media_cues_do_not_break_playback():
    result, meta = sentence_view([cue(-1, 2, 'bad'), cue(0, float('nan'), 'bad'), cue(1, 2, 'Yes.'),
                                 cue(2, 12, 'Finish.'), cue(20, 22, 'out')], duration=5)
    assert meta['invalid_cues'] == 3 and len(result) == 2
    assert_ranges(result, 5)
    assert result[-1]['end'] == 5


def test_manual_imports_retain_explicit_units_without_terminal_punctuation():
    result, _ = sentence_view([cue(0, 2, 'Hello <everyone>'), cue(2, 4, 'Welcome. Hi!')], source='manual')
    assert [s['text'] for s in result] == ['Hello <everyone>', 'Welcome.', 'Hi!']


def test_legacy_rebuild_api_is_reversible_idempotent_and_exports_sentences(client, db, seed):
    episode_id = seed()
    raw = [cue(0, 1.5, 'Hello'), cue(1, 2.5, 'world. Next sentence.')]
    # Old schema/source rows need no media job, no external API and no destructive migration.
    with db.connect() as c:
        c.execute('INSERT INTO transcripts VALUES (?,?,?,?)', (episode_id, json.dumps(raw), 'youtube_auto', 'en'))
    url = f'/api/v1/episodes/{episode_id}/transcript'
    first = client.post(url + '/rebuild').json()
    assert first['changed'] and first['processing']['raw_count'] == 2
    assert [s['text'] for s in first['segments']] == ['Hello world.', 'Next sentence.']
    assert client.post(url + '/rebuild').json()['changed'] is False
    assert client.get(url + '?view=raw').json()['segments'] == raw
    assert client.get(url).json()['segments'] == first['segments']
    assert client.get(url + '/export?format=txt').text == 'Hello world.\nNext sentence.'
    assert client.get(url + '/export?format=txt&view=raw').text == 'Hello\nworld. Next sentence.'
    assert client.get(url + '/export?format=vtt').text.count(' --> ') == 2
    with db.connect() as c:
        assert json.loads(c.execute('SELECT segments FROM transcripts WHERE episode_id=?', (episode_id,)).fetchone()[0]) == raw
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert client.post('/api/v1/episodes/00000000000/transcript/rebuild').status_code == 404
    assert client.get(url + '?view=unknown').status_code == 422


def test_cache_invalidates_for_new_import_and_duration_changes(client, db, seed):
    episode_id = seed()
    url = f'/api/v1/episodes/{episode_id}/transcript'
    old = {'language': 'en', 'segments': [cue(0, 2, 'Old.')]}
    assert client.put(url, json=old).status_code == 200
    client.get(url)
    new = {'language': 'en', 'segments': [cue(0, 3, 'New. Two sentences!')]}
    assert client.put(url, json=new).status_code == 200
    rebuilt = client.get(url).json()
    assert [s['text'] for s in rebuilt['segments']] == ['New.', 'Two sentences!']
    with db.connect() as c:
        c.execute('UPDATE episodes SET duration=1 WHERE id=?', (episode_id,))
    assert_ranges(client.get(url).json()['segments'], 1)


def test_rebuild_and_source_publication_cannot_leave_a_stale_cache(db, seed):
    episode_id = seed()
    def publish(number):
        with db.connect(immediate=True) as c:
            save_transcript(c, episode_id, {'segments': [cue(0, 2, f'Version {number}.')], 'source': 'manual', 'language': 'en'})
    publish(0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(publish, i) if i % 2 else pool.submit(read_transcript, db, episode_id, rebuild=True) for i in range(1, 16)]
        for future in futures:
            future.result()
    raw = read_transcript(db, episode_id, view='raw')['segments']
    assert read_transcript(db, episode_id)['segments'][0]['text'] == raw[0]['text']
