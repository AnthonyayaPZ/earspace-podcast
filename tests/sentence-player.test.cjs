const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {indexAt, SentenceLoop} = require('../app/web/sentence-player.js');
const segments = [{start: 1, end: 3}, {start: 3, end: 5}, {start: 7, end: 10}];

test('browser timers retain their Window receiver on player reset and loop scheduling', () => {
  // Node timers accept an arbitrary receiver; browser Web IDL timers do not.
  const page = vm.createContext({});
  vm.runInContext(`
    var scheduled = 0, cleared = 0;
    globalThis.setTimeout = function(callback, delay) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      scheduled++; return scheduled;
    };
    globalThis.clearTimeout = function(id) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      cleared++;
    };
  `, page);
  vm.runInContext(fs.readFileSync(require.resolve('../app/web/sentence-player.js'), 'utf8'), page);
  vm.runInContext(`
    var media = {currentTime: 1, paused: false, playbackRate: 1, seeking: false};
    var player = new EarspacePlayback.SentenceLoop(() => media);
    player.reset();
    player.setSegments([{start: 1, end: 3}]);
    player.setEnabled(true);
    media.paused = true; player.handle('pause', media);
    player.reset();
  `, page);
  assert.equal(page.scheduled, 1);
  assert.ok(page.cleared >= 4);
});

function fixture() {
  const audio = {currentTime: 1, playbackRate: 1, paused: false, seeking: false,
    play() { this.paused = false; return Promise.resolve(); }};
  const video = {...audio}; let active = audio, timer, delay;
  const loop = new SentenceLoop(() => active, {
    setTimer(fn, ms) { timer = fn; delay = ms; return 1; }, clearTimer() { timer = null; }
  });
  loop.setSegments(segments);
  return {audio, video, loop, wake: () => { const fn = timer; timer = null; fn?.(); },
    delay: () => delay, pending: () => !!timer, switch: () => { active = video; }};
}

test('half-open sentence ranges do not highlight gaps or the preceding sentence', () => {
  assert.equal(indexAt(segments, 0), -1); assert.equal(indexAt(segments, 1), 0);
  assert.equal(indexAt(segments, 3), 1); assert.equal(indexAt(segments, 5.5), -1);
  assert.equal(indexAt(segments, 10), -1);
});
test('loop uses the full merged range and recovers from a late timer', () => {
  const {audio, loop, wake} = fixture(); loop.setEnabled(true);
  audio.currentTime = 2.9; loop.handle('timeupdate', audio); assert.equal(audio.currentTime, 2.9);
  audio.currentTime = 3.2; wake(); assert.equal(audio.currentTime, 1);
  audio.currentTime = 3.4; loop.handle('timeupdate', audio); assert.equal(audio.currentTime, 1);
});
test('timer is rate-aware and stops while paused or buffering', () => {
  const {audio, loop, delay, pending} = fixture(); loop.setEnabled(true);
  audio.currentTime = 2.8; audio.playbackRate = 2; loop.handle('ratechange', audio);
  assert.ok(Math.abs(delay() - 100) < .001);
  audio.paused = true; loop.handle('pause', audio); assert.equal(pending(), false);
  audio.paused = false; loop.handle('playing', audio); assert.equal(pending(), true);
  loop.handle('waiting', audio); assert.equal(pending(), false);
  loop.handle('timeupdate', audio); assert.equal(pending(), false);
  loop.handle('playing', audio); assert.equal(pending(), true);
});
test('manual seeking retargets instead of trapping the user in the old sentence', () => {
  const {audio, loop} = fixture(); loop.setEnabled(true);
  audio.currentTime = 8; loop.handle('seeking', audio); loop.handle('seeked', audio);
  assert.equal(loop.index, 2); assert.equal(audio.currentTime, 8);
  audio.currentTime = 10; loop.handle('timeupdate', audio); assert.equal(audio.currentTime, 7);
  loop.handle('seeking', audio); assert.equal(loop.index, 2);
  audio.currentTime = 6; loop.handle('seeking', audio); loop.handle('seeked', audio);
  assert.equal(loop.index, -1); assert.equal(audio.currentTime, 6);
  audio.currentTime = 7.1; loop.handle('timeupdate', audio); assert.equal(loop.index, 2);
});
test('last sentence loops on ended, and disabling restores normal completion', () => {
  const {audio, loop} = fixture(); audio.currentTime = 9; loop.setEnabled(true);
  audio.currentTime = 10; audio.paused = true;
  assert.equal(loop.looping, true); // The pause/ended sequence must not mark a looping episode complete.
  assert.equal(loop.handle('ended', audio), true); assert.equal(audio.currentTime, 7);
  assert.equal(audio.paused, false); loop.setEnabled(false);
  assert.equal(loop.handle('ended', audio), false);
  assert.equal(loop.looping, false);
});
test('rebuild replaces stale indices, video switching ignores inactive media, reset stops timers', () => {
  const {audio, video, loop, switch: switchMedia, pending} = fixture(); loop.setEnabled(true);
  loop.setSegments([{start: 0, end: 5}]); assert.equal(loop.index, 0);
  switchMedia(); video.currentTime = 2; loop.handle('playing', video);
  audio.currentTime = 20; loop.handle('ended', audio); assert.equal(video.currentTime, 2);
  loop.reset(); assert.equal(pending(), false); assert.equal(loop.index, -1); assert.equal(loop.enabled, false);
});
