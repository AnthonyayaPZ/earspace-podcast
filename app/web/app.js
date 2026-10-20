'use strict';
const $ = selector => document.querySelector(selector);
const state = {key: sessionStorage.getItem('earspace-key') || '', view: 'discover', language: 'zh',
  context: 'gym', offset: 0, limit: 12, connected: false, episode: null, segments: [],
  loopIndex: -1, lastSave: 0, loadId: 0, renderId: 0, sleepTimer: null, activeJobs: 0};
let toastTimer;

function h(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on')) element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') element.className = value;
    else element.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) if (child !== undefined && child !== null) {
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}
const safe = fn => async (...args) => { try { await fn(...args); } catch (error) { toast(error.message || '操作失败，请稍后重试'); } };
function showDialog(id) { if (id === 'import-dialog') $('#import-language').value = state.language; const d = $('#' + id); if (!d.open) d.showModal(); }
function notice(message = '') { $('#notice').textContent = message; $('#notice').hidden = !message; }
async function api(path, options = {}) {
  if (!state.key) { showDialog('connect-dialog'); throw new Error('请先连接服务'); }
  const response = await fetch('/api/v1' + path, {...options,
    headers: {'Authorization': 'Bearer ' + state.key, ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers},
    signal: AbortSignal.timeout(30000)});
  if (!response.ok) {
    let body; try { body = await response.json(); } catch { body = {}; }
    if (response.status === 401) { state.connected = false; $('#connection-dot').classList.remove('online'); showDialog('connect-dialog'); }
    const detail = Array.isArray(body.detail) ? body.detail.map(x => x.msg).join('；') : body.detail;
    const error = new Error(detail || '请求失败（' + response.status + '）'); error.status = response.status; throw error;
  }
  if (options.raw) return response;
  return response.json();
}
function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '待获取';
  return Math.floor(seconds / 3600) ? `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(Math.floor(seconds) % 60).padStart(2, '0')}`
    : `${Math.floor(seconds / 60)}:${String(Math.floor(seconds) % 60).padStart(2, '0')}`;
}
function empty(title, description, action, callback) {
  const node = h('div', {class: 'empty-state'}, h('div', {class: 'empty-symbol'}, '◖◗'), h('h3', {}, title), h('p', {}, description));
  if (action) node.append(h('button', {class: 'button primary', onclick: safe(callback)}, action));
  return node;
}
async function loadPreferences() {
  const profile = await api('/preferences');
  $('#interests').value = profile.interests.join('，');
  $('#preferred-channels').value = profile.preferred_channels.join(', ');
  $('#english-level').value = profile.english_level;
  $('#session-minutes').value = profile.session_minutes;
  const ratio = $('#discovery-ratio');
  if (![...ratio.options].some(x => Number(x.value) === profile.discovery_ratio)) ratio.add(new Option(`${Math.round(profile.discovery_ratio * 100)}%`, profile.discovery_ratio));
  ratio.value = profile.discovery_ratio;
  $('#profile-summary').textContent = profile.interests.slice(0, 3).join(' · ') + ` / ${profile.session_minutes} 分钟`;
}
async function connect() {
  await loadPreferences(); state.connected = true;
  $('#connection-dot').classList.add('online');
  $('#connect-dialog').close(); $('#connect-error').textContent = '';
  sessionStorage.setItem('earspace-key', state.key);
  await Promise.all([render(), pollJobs(), checkStatus()]);
}
async function checkStatus() {
  const status = await api('/status');
  const messages = [];
  if (!status.youtube_discovery_configured) messages.push('服务器尚未配置 YouTube API 密钥，配置后可发现真实推荐。你仍可用链接导入内容。');
  if (!status.workers_online) messages.push('暂未检测到在线 Worker，请在服务器启动 uv run --locked python -m app.worker。');
  if (!status.ffmpeg_available || !status.ffprobe_available) messages.push('服务器缺少 FFmpeg / ffprobe，媒体处理不可用。');
  notice(messages.join(' '));
}
async function refreshDiscovery() {
  const job = await api('/discovery/refresh', {method: 'POST'});
  toast(job.status === 'running' ? '正在发现适合你的内容' : '已加入内容发现队列');
  await pollJobs();
}
const viewTitles = {discover: '发现播客', library: '我的待听', continue: '继续收听', jobs: '处理任务'};
function setView(view) {
  state.view = view; state.offset = 0;
  document.querySelectorAll('[data-view]').forEach(x => x.classList.toggle('active', x.dataset.view === view));
  $('#breadcrumb').textContent = viewTitles[view];
  $('#section-title').textContent = {discover: '为你发现', library: '留给下一次好时光', continue: '接着上次，继续听', jobs: '内容准备中'}[view];
  $('#section-eyebrow').textContent = {discover: 'PICKED FOR YOUR CURIOSITY', library: 'SAVED FOR A GOOD MOMENT', continue: 'PICK UP WHERE YOU LEFT OFF', jobs: 'YOUR BACKGROUND TASKS'}[view];
  $('#language-tabs').hidden = view === 'jobs' || view === 'continue';
  $('#filter-row').hidden = view !== 'discover';
  safe(render)();
}
async function render() {
  const id = ++state.renderId;
  const container = $('#content-list');
  if (!state.connected) {
    container.replaceChildren(empty('给每次收听，一个新的开始', '连接服务，设置兴趣，发现适合你的中英文播客。这里不会填充虚构的推荐或下载记录。', '连接我的服务 ↗', () => showDialog('connect-dialog')));
    return;
  }
  let data;
  if (state.view === 'discover') data = await api(`/recommendations?language=${state.language}&context=${state.context}&limit=${state.limit}&offset=${state.offset}`);
  else if (state.view === 'library') data = await api(`/episodes?language=${state.language}&library=true&limit=${state.limit}&offset=${state.offset}`);
  else if (state.view === 'continue') data = await api('/listening/continue');
  else data = await api(`/jobs?limit=${state.limit}&offset=${state.offset}`);
  if (id !== state.renderId) return;
  if (!data.items.length) {
    const description = state.view === 'discover' ? '先设置感兴趣的主题，再更新推荐。发现任务完成后，真实节目会出现在这里。'
      : state.view === 'library' ? '发现喜欢的节目后加入待听，也可以直接导入 YouTube 链接。'
      : state.view === 'continue' ? '播放过的节目会保留进度，下次回来可以继续听。' : '下载、内容发现和转写任务会在这里显示进度。';
    container.replaceChildren(empty(state.view === 'discover' ? '你的下一段好播客，正在路上' : '这里还留着一些空位', description,
      state.view === 'discover' ? '发现适合我的播客 ↗' : '导入一段内容 ＋',
      state.view === 'discover' ? refreshDiscovery : () => showDialog('import-dialog')));
  } else container.replaceChildren(...data.items.map(state.view === 'jobs' ? jobCard : episodeCard));
  $('#prev-page').hidden = state.offset === 0;
  $('#next-page').hidden = state.view === 'continue' || state.offset + state.limit >= (data.total || 0);
  $('#page-info').textContent = data.total ? `${state.offset + 1}–${Math.min(state.offset + data.items.length, data.total)} / ${data.total}` : '';
  $('#ranking-note').textContent = state.view === 'discover' ? data.note || '' : '内容和播放进度保存在你的服务器。文件就绪后可在这里播放。';
}
function episodeCard(item) {
  const cover = h('div', {class: 'cover'}, h('div', {class: 'cover-letter'}, '◖◗'));
  if (item.thumbnail && /^https:\/\//.test(item.thumbnail)) {
    const img = h('img', {src: item.thumbnail, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer'});
    img.addEventListener('error', () => img.remove()); cover.replaceChildren(img);
  }
  cover.append(h('span', {class: 'duration'}, item.duration ? duration(item.duration) : '待获取'));
  const tags = h('div', {class: 'tags'}, (item.topics || []).slice(0, 2).map(t => h('span', {class: 'tag'}, t)));
  if (item.exploration) tags.append(h('span', {class: 'tag highlight'}, '探索新内容'));
  if (item.audio_available) tags.append(h('span', {class: 'tag'}, '音频已就绪'));
  if (item.language_source === 'estimated_from_search') tags.append(h('span', {class: 'tag highlight'}, '语言待确认'));
  if (item.language === 'en') {
    const level = item.difficulty?.level;
    tags.append(h('span', {class: 'tag'}, ({beginner: '估计难度：入门', intermediate: '估计难度：中级', advanced: '估计难度：进阶'})[level] || '难度待分析'));
  }
  const actions = h('div', {class: 'card-actions'});
  if (item.audio_available) actions.append(h('button', {class: 'button primary', onclick: safe(() => playEpisode(item))}, '▷ 开始收听'));
  else actions.append(h('button', {class: 'button primary', onclick: safe(async () => {
    await api(`/episodes/${item.id}/download?mode=audio`, {method: 'POST'}); toast('已加入音频下载队列'); await pollJobs();
  })}, '↓ 准备收听'));
  if (!item.library) actions.append(h('button', {class: 'button secondary', onclick: safe(async () => {
    await api(`/episodes/${item.id}/library`, {method: 'PUT'}); toast('已加入我的待听'); await render();
  })}, '＋ 待听'));
  else if (state.view === 'library') actions.append(h('button', {class: 'button secondary', onclick: safe(async () => {
    await api(`/episodes/${item.id}/library`, {method: 'DELETE'}); await render();
  })}, '移出'));
  if (item.audio_available) actions.append(h('button', {class: 'button secondary', onclick: safe(() => downloadMedia(item.id, 'audio'))}, '音频 ↓'));
  if (item.video_available) actions.append(h('button', {class: 'button secondary', onclick: safe(() => downloadMedia(item.id, 'video'))}, 'MP4 ↓'));
  else if (item.audio_available && state.view === 'library') actions.append(h('button', {class: 'button secondary', onclick: safe(async () => {
    await api(`/episodes/${item.id}/download?mode=video`, {method: 'POST'}); toast('已加入 MP4 下载队列'); await pollJobs();
  })}, '准备 MP4'));
  actions.append(h('div', {class: 'feedback-buttons'},
    h('button', {title: '喜欢这类内容', 'aria-label': '喜欢这类内容', onclick: safe(() => feedback(item.id, 'reaction', 'like'))}, '♡'),
    h('button', {title: '不感兴趣', 'aria-label': '不感兴趣', onclick: safe(() => feedback(item.id, 'reaction', 'dislike'))}, '⊘')));
  return h('article', {class: 'episode-card'}, cover, h('div', {class: 'card-body'},
    h('a', {class: 'channel', href: item.youtube_url, target: '_blank', rel: 'noopener noreferrer'}, (item.channel_title || 'YouTube') + ' ↗'),
    h('h3', {}, item.title), tags, h('p', {class: 'reason'}, (item.reasons || [item.transcript_warning || (item.position ? `上次听到 ${duration(item.position)}` : '为下一段属于自己的时间，留一段好内容。')])[0]), actions));
}
async function feedback(id, kind, value) {
  await api(`/episodes/${id}/feedback`, {method: 'PUT', body: JSON.stringify({kind, value})});
  toast(value === 'dislike' ? '会减少这类推荐' : '已记下你的偏好');
  await render();
}
function jobCard(item) {
  const kinds = {download: item.media_mode === 'audio' ? '音频下载' : '视频与音频下载', refresh: '发现新播客', transcribe: '语音转写'};
  const statuses = {queued: '等待中', running: '处理中', succeeded: '已完成', failed: '失败', cancelled: '已取消'};
  const card = h('article', {class: 'job-card'}, h('div', {}, h('strong', {}, kinds[item.kind] || item.kind),
    h('p', {}, item.episode_id ? `视频 ${item.episode_id} · ${item.stage}` : item.stage)), h('span', {class: 'job-status'}, statuses[item.status]));
  if (item.status === 'running') card.append(h('progress', {value: item.progress, max: 100, 'aria-label': '处理进度'}));
  if (item.error) card.append(h('p', {class: 'error'}, item.error));
  if (item.result?.warnings?.length) card.append(h('p', {class: 'error'}, item.result.warnings.join('；')));
  if (item.result?.warning) card.append(h('p', {class: 'error'}, item.result.warning));
  if (item.result?.discovered !== undefined) card.append(h('p', {}, `发现 ${item.result.discovered} 条内容`));
  const bottom = h('div', {class: 'job-bottom'}, h('small', {}, new Date(item.created_at * 1000).toLocaleString() + ` · 已尝试 ${item.attempts} 次`));
  const action = ['failed', 'cancelled'].includes(item.status) ? 'retry' : ['running', 'queued'].includes(item.status) ? 'cancel' : null;
  if (action) bottom.append(h('button', {class: 'button secondary', onclick: safe(async () => {
    await api(`/jobs/${item.id}/${action}`, {method: 'POST'}); await pollJobs(); await render();
  })}, action === 'retry' ? '重新尝试 ↻' : '取消任务'));
  card.append(bottom); return card;
}
let previousJobs = new Map();
async function pollJobs() {
  if (!state.connected) return;
  const data = await api('/jobs?limit=100');
  const active = data.items.filter(x => ['queued', 'running'].includes(x.status));
  state.activeJobs = active.length; $('#job-count').textContent = active.length || '';
  let changed = false;
  for (const job of data.items) {
    if (previousJobs.has(job.id) && previousJobs.get(job.id) !== job.status && ['succeeded', 'failed'].includes(job.status)) {
      changed = true; toast(job.status === 'succeeded' ? '一个后台任务已完成，内容已更新' : '一个任务处理失败，可在处理任务中查看原因');
    }
  }
  previousJobs = new Map(data.items.map(x => [x.id, x.status]));
  if (state.view === 'jobs' || changed) await render();
}
const audio = $('#audio'), video = $('#video');
const activeMedia = () => $('#video-dialog').open ? video : audio;
async function saveProgress(completed = false) {
  if (!state.episode || !state.connected || state.suppressMediaEvents || !Number.isFinite(activeMedia().currentTime)) return;
  state.lastSave = Date.now();
  await api(`/episodes/${state.episode.id}/progress`, {method: 'PUT', body: JSON.stringify({position: activeMedia().currentTime,
    completed: completed || activeMedia().ended, playback_rate: activeMedia().playbackRate})});
}
async function playEpisode(item) {
  const requestId = ++state.loadId;
  if (state.episode) await saveProgress();
  const [token, progress] = await Promise.all([api(`/episodes/${item.id}/playback-token?kind=audio`, {method: 'POST'}), api(`/episodes/${item.id}/progress`)]);
  if (requestId !== state.loadId) return;
  state.suppressMediaEvents = true;
  audio.pause(); state.episode = item; state.segments = []; state.loopIndex = -1;
  $('#player').hidden = false; $('#player-title').textContent = item.title; $('#player-channel').textContent = item.channel_title;
  $('#video-btn').hidden = !item.video_available;
  $('#speed').value = progress.playback_rate; audio.playbackRate = progress.playback_rate;
  $('#transcript-panel').hidden = true;
  audio.src = token.url;
  audio.onloadedmetadata = () => {
    audio.currentTime = progress.completed ? 0 : Math.min(progress.position, audio.duration || 0);
    state.suppressMediaEvents = false;
    audio.playbackRate = progress.playback_rate; audio.play().catch(() => toast('内容已就绪，点击播放开始收听'));
  };
  if ('mediaSession' in navigator && 'MediaMetadata' in window) {
    navigator.mediaSession.metadata = new MediaMetadata({title: item.title, artist: item.channel_title || '耳间'});
    navigator.mediaSession.setActionHandler('seekbackward', () => { audio.currentTime = Math.max(0, audio.currentTime - 15); });
    navigator.mediaSession.setActionHandler('seekforward', () => { audio.currentTime = Math.min(audio.duration, audio.currentTime + 15); });
  }
}
async function loadTranscript() {
  if (!state.episode) return;
  $('#transcript-panel').hidden = false; $('#transcript-source').textContent = '正在获取文稿…'; $('#transcript-lines').replaceChildren();
  const episodeId = state.episode.id;
  try {
    const data = await api(`/episodes/${episodeId}/transcript`);
    if (state.episode?.id !== episodeId) return;
    state.segments = data.segments;
    const names = {manual: '手动导入', youtube_manual: 'YouTube 人工字幕', youtube_auto: 'YouTube 自动字幕', whisper: 'Whisper 语音转写'};
    $('#transcript-source').textContent = `来源：${names[data.source] || data.source} · ${data.segments.length} 句。点击句子跳转播放。`;
    $('#transcript-lines').replaceChildren(...data.segments.map((segment, i) => h('button', {class: 'transcript-line', 'data-index': i,
      onclick: () => { state.loopIndex = i; activeMedia().currentTime = segment.start; activeMedia().play().catch(() => {}); }},
      h('small', {}, duration(segment.start)), h('span', {}, segment.text))));
    if (state.episode.language === 'en') {
      $('#transcript-lines').append(h('div', {class: 'tags'}, ['too_easy', 'just_right', 'too_hard'].map((value, i) => h('button', {
        class: 'button secondary', onclick: safe(() => feedback(episodeId, 'difficulty', value))}, ['太简单', '刚刚好', '太难了'][i]))));
    }
  } catch (error) {
    if (error.status !== 404) throw error;
    state.segments = []; $('#transcript-source').textContent = '这段内容还没有文稿。你可以导入带时间戳的 JSON 文稿，或在服务开启转写后从音频生成。';
  }
}
function syncTranscript() {
  const media = activeMedia();
  const loop = state.segments[state.loopIndex];
  if ($('#sentence-loop').checked && loop && media.currentTime >= loop.end) { media.currentTime = loop.start; return; }
  if (!state.segments.length || $('#transcript-panel').hidden) return;
  let low = 0, high = state.segments.length - 1, index = -1;
  while (low <= high) { const mid = (low + high) >> 1; if (state.segments[mid].start <= media.currentTime) {index = mid; low = mid + 1;} else high = mid - 1; }
  const old = $('#transcript-lines .active'); const current = $(`#transcript-lines [data-index="${index}"]`);
  if (old !== current) { old?.classList.remove('active'); current?.classList.add('active'); }
}
for (const media of [audio, video]) {
  media.addEventListener('timeupdate', () => { if (state.suppressMediaEvents) return; syncTranscript(); if (Date.now() - state.lastSave > 15000 && !media.paused) safe(saveProgress)(); });
  media.addEventListener('pause', () => { if (media === activeMedia()) safe(() => saveProgress())(); });
  media.addEventListener('ended', safe(async () => { await saveProgress(true); toast('这段收听完成，辛苦啦'); await render(); }));
  media.addEventListener('error', () => { if (media.src) toast('媒体加载失败，请检查网络，或重新打开节目获取播放链接'); });
}
async function downloadMedia(id, kind = 'audio') {
  const token = await api(`/episodes/${id}/playback-token?kind=${kind}`, {method: 'POST'});
  const link = h('a', {href: token.url, download: id + (kind === 'audio' ? '.m4a' : '.mp4')}); document.body.append(link); link.click(); link.remove();
}
document.querySelectorAll('[data-view]').forEach(x => x.addEventListener('click', () => setView(x.dataset.view)));
document.querySelectorAll('[data-language]').forEach(x => x.addEventListener('click', () => {
  state.language = x.dataset.language; state.offset = 0;
  document.querySelectorAll('[data-language]').forEach(y => y.classList.toggle('active', y === x)); safe(render)();
}));
document.querySelectorAll('[data-context]').forEach(x => x.addEventListener('click', () => {
  state.context = x.dataset.context; state.offset = 0;
  document.querySelectorAll('[data-context]').forEach(y => y.classList.toggle('active', y === x)); safe(render)();
}));
document.querySelectorAll('[data-close]').forEach(x => x.addEventListener('click', () => $('#' + x.dataset.close).close()));
$('#connection-btn').onclick = () => showDialog('connect-dialog');
$('#mobile-settings').onclick = () => showDialog(state.connected ? 'preferences-dialog' : 'connect-dialog');
$('#preferences-connection').onclick = () => { $('#preferences-dialog').close(); showDialog('connect-dialog'); };
$('#import-btn').onclick = () => showDialog('import-dialog');
$('#preferences-btn').onclick = () => state.connected ? showDialog('preferences-dialog') : showDialog('connect-dialog');
$('#refresh-btn').onclick = safe(refreshDiscovery);
$('#connect-form').onsubmit = async event => {
  event.preventDefault(); state.key = $('#api-key').value.trim();
  const button = event.submitter; button.disabled = true;
  try { await connect(); toast('已连接你的播客空间'); } catch (error) { $('#connect-error').textContent = error.message; }
  finally { button.disabled = false; }
};
$('#disconnect').onclick = () => {
  state.key = ''; state.connected = false; sessionStorage.removeItem('earspace-key'); audio.pause(); video.pause();
  audio.removeAttribute('src'); audio.load(); video.removeAttribute('src'); video.load();
  $('#connection-dot').classList.remove('online'); $('#connect-dialog').close(); $('#api-key').value = '';
  $('#player').hidden = true; $('#transcript-panel').hidden = true; state.episode = null; state.loadId++;
  previousJobs.clear(); $('#job-count').textContent = ''; notice(); safe(render)();
};
$('#connect-dialog').addEventListener('cancel', () => { if (!state.connected) toast('你可以稍后通过“服务连接”连接后端'); });
$('#preferences-form').onsubmit = safe(async event => {
  event.preventDefault(); const split = value => value.split(/[,，\n]/).map(x => x.trim()).filter(Boolean);
  await api('/preferences', {method: 'PUT', body: JSON.stringify({interests: split($('#interests').value),
    preferred_channels: split($('#preferred-channels').value), english_level: $('#english-level').value,
    session_minutes: Number($('#session-minutes').value), discovery_ratio: Number($('#discovery-ratio').value)})});
  $('#preferences-dialog').close(); await loadPreferences(); toast('偏好已保存，更新推荐可发现新内容'); await render();
});
$('#import-form').onsubmit = safe(async event => {
  event.preventDefault(); const body = {url: $('#video-url').value.trim(), language: $('#import-language').value, download: $('#download-now').checked, mode: $('#import-mode').value};
  await api('/episodes/import', {method: 'POST', body: JSON.stringify(body)});
  $('#import-dialog').close(); $('#video-url').value = ''; state.language = body.language;
  document.querySelectorAll('[data-language]').forEach(x => x.classList.toggle('active', x.dataset.language === body.language));
  setView('library'); toast(body.download ? '已加入待听，后台正在准备媒体' : '已加入待听'); await pollJobs();
});
$('#prev-page').onclick = safe(async () => { state.offset = Math.max(0, state.offset-state.limit); await render(); });
$('#next-page').onclick = safe(async () => { state.offset += state.limit; await render(); });
$('#back15').onclick = () => { activeMedia().currentTime = Math.max(0, activeMedia().currentTime - 15); };
$('#forward15').onclick = () => { activeMedia().currentTime = Math.min(activeMedia().duration || 0, activeMedia().currentTime + 15); };
$('#speed').onchange = () => { audio.playbackRate = video.playbackRate = Number($('#speed').value); safe(() => saveProgress())(); };
$('#sleep').onchange = () => {
  clearTimeout(state.sleepTimer); const minutes = Number($('#sleep').value);
  if (minutes) { state.sleepTimer = setTimeout(() => { audio.pause(); video.pause(); $('#sleep').value = '0'; toast('定时已到，休息一下吧'); }, minutes*60000); toast(`将在 ${minutes} 分钟后暂停播放`); }
};
$('#close-player').onclick = safe(async () => { await saveProgress(); audio.pause(); video.pause(); clearTimeout(state.sleepTimer); $('#sleep').value='0'; state.loadId++; state.episode = null; $('#player').hidden = true; $('#transcript-panel').hidden = true; });
$('#transcript-btn').onclick = safe(loadTranscript);
$('#close-transcript').onclick = () => { $('#transcript-panel').hidden = true; };
$('#sentence-loop').onchange = () => {
  if ($('#sentence-loop').checked && state.loopIndex < 0) toast('点击一句文稿，开始循环练习');
};
$('#export-transcript').onclick = safe(async () => {
  if (!state.episode) return;
  const response = await api(`/episodes/${state.episode.id}/transcript/export?format=txt`, {raw: true});
  const url = URL.createObjectURL(await response.blob());
  const link = h('a', {href: url, download: state.episode.id + '.txt'}); document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});
$('#transcribe-btn').onclick = safe(async () => {
  if (!state.episode) return; await api(`/episodes/${state.episode.id}/transcribe`, {method: 'POST'});
  toast('转写任务已加入队列'); await pollJobs();
});
$('#upload-transcript').onclick = () => { $('#transcript-file').click(); };
$('#transcript-file').onchange = safe(async () => {
  const file = $('#transcript-file').files[0]; if (!file || !state.episode) return;
  if (file.size > 8 * 1024 * 1024) throw new Error('文稿文件不能超过 8 MB');
  let data; try { data = JSON.parse(await file.text()); } catch { throw new Error('请上传 JSON：{"language":"en","segments":[{"start":0,"end":2,"text":"Hello"}]}'); }
  await api(`/episodes/${state.episode.id}/transcript`, {method: 'PUT', body: JSON.stringify(data)});
  $('#transcript-file').value = ''; toast('文稿已导入'); await loadTranscript();
});
$('#video-btn').onclick = safe(async () => {
  if (!state.episode) return;
  const token = await api(`/episodes/${state.episode.id}/playback-token?kind=video`, {method: 'POST'});
  const position = audio.currentTime; audio.pause(); video.src = token.url;
  video.onloadedmetadata = () => { video.currentTime = position; video.playbackRate = Number($('#speed').value); video.play().catch(() => {}); };
  showDialog('video-dialog');
});
$('#close-video').onclick = () => $('#video-dialog').close();
$('#video-dialog').addEventListener('close', () => { video.pause(); if (Number.isFinite(video.currentTime)) audio.currentTime = video.currentTime; });
document.addEventListener('visibilitychange', () => { if (document.hidden && state.episode) safe(() => saveProgress())(); });
window.addEventListener('pagehide', () => {
  if (!state.episode || !state.connected) return;
  fetch(`/api/v1/episodes/${state.episode.id}/progress`, {method: 'PUT', keepalive: true,
    headers: {'Authorization': 'Bearer ' + state.key, 'Content-Type': 'application/json'},
    body: JSON.stringify({position: activeMedia().currentTime || 0, completed: activeMedia().ended, playback_rate: activeMedia().playbackRate})}).catch(() => {});
});
setInterval(() => { if (state.connected && !document.hidden) pollJobs().catch(() => {}); }, 5000);
setInterval(() => { if (state.connected && !document.hidden) checkStatus().catch(() => {}); }, 30000);
if (state.key) safe(connect)(); else { render(); }
