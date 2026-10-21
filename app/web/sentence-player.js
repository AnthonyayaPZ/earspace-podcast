/* Shared by the browser player and Node's deterministic playback tests. */
(function (root) {
  'use strict';
  function indexAt(segments, time) {
    let low = 0, high = segments.length - 1, index = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (segments[mid].start <= time) { index = mid; low = mid + 1; }
      else high = mid - 1;
    }
    return index >= 0 && time < segments[index].end ? index : -1;
  }

  class SentenceLoop {
    // Browser timer methods require Window as their receiver, even when stored on this controller.
    constructor(getMedia, {setTimer = (callback, delay) => root.setTimeout(callback, delay),
      clearTimer = id => root.clearTimeout(id)} = {}) {
      this.getMedia = getMedia; this.setTimer = setTimer; this.clearTimer = clearTimer;
      this.segments = []; this.index = -1; this.enabled = false; this.timer = null; this.pendingSeek = null; this.buffering = false;
    }
    clear() { this.clearTimer(this.timer); this.timer = null; }
    reset() { this.clear(); this.segments = []; this.index = -1; this.enabled = false; this.pendingSeek = null; this.buffering = false; }
    get looping() { return this.enabled && !!this.segments[this.index]; }
    setSegments(segments) {
      this.clear(); this.segments = segments; this.pendingSeek = null;
      this.index = indexAt(segments, this.getMedia().currentTime);
      this.update();
    }
    setEnabled(enabled) {
      this.enabled = enabled;
      this.index = indexAt(this.segments, this.getMedia().currentTime);
      this.update();
    }
    jump(time) { this.pendingSeek = time; this.getMedia().currentTime = time; }
    select(index) {
      if (!this.segments[index]) return;
      this.index = index; this.jump(this.segments[index].start);
      this.getMedia().play().catch(() => {}); this.update();
    }
    handle(name, media) {
      if (media !== this.getMedia()) return false;
      if (name === 'waiting') this.buffering = true;
      if (name === 'playing' || name === 'emptied') this.buffering = false;
      if (name === 'seeking') {
        this.clear();
        if (this.pendingSeek === null || Math.abs(media.currentTime - this.pendingSeek) > .05) {
          this.index = indexAt(this.segments, media.currentTime);
        }
        this.pendingSeek = null;
        return false;
      }
      if (name === 'pause' || name === 'waiting' || name === 'emptied') { this.clear(); return false; }
      if (name === 'ended' && this.looping) {
        this.jump(this.segments[this.index].start); media.play().catch(() => {}); this.update(); return true;
      }
      this.update(); return false;
    }
    update() {
      this.clear();
      const media = this.getMedia();
      if (!this.enabled || this.buffering || media.paused || media.seeking || !Number.isFinite(media.currentTime)) return;
      if (this.index < 0) this.index = indexAt(this.segments, media.currentTime);
      const sentence = this.segments[this.index];
      if (!sentence) return;
      // Use media time on every wake-up: timers may run late when a tab is in the background.
      if (media.currentTime >= sentence.end) { this.jump(sentence.start); }
      const milliseconds = (sentence.end - media.currentTime) / (media.playbackRate || 1) * 1000;
      this.timer = this.setTimer(() => this.update(), Math.max(4, Math.min(500, milliseconds)));
    }
  }
  const api = {indexAt, SentenceLoop};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.EarspacePlayback = api;
})(globalThis);
