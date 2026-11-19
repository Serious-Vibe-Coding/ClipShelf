'use strict';

const $ = (selector) => document.querySelector(selector);
const state = { clips: [], subject: '', q: '', month: '', subjects: [], stats: null, loaded: false, editId: null, file: null, busy: false, playing: null, request: null, toastTimer: null, user: null, epoch: 0, authMode: 'login', authBusy: false, logoutPending: false, uploadRequest: null, mutation: null };
const icon = (name) => { const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('aria-hidden', 'true'); const use = document.createElementNS(svg.namespaceURI, 'use'); use.setAttribute('href', `#i-${name}`); svg.append(use); return svg; };
const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const bytes = (n) => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`;
const recordedDate = (date) => date ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(`${date}T12:00:00`)) : 'Date not recorded';
const monthLabel = (month) => new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' }).format(new Date(`${month}-01T12:00:00`));
const uploadDate = (date) => new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(date));

async function jsonResponse(response) {
  let result;
  try { result = await response.json(); } catch { throw new Error('The local server returned an unreadable response. Please try again.'); }
  if (!response.ok) { const error = new Error(result.error || 'Something went wrong. Please try again.'); error.status = response.status; throw error; }
  return result;
}

async function loadClips() {
  if (!state.user) return;
  const epoch = state.epoch;
  if (state.request) state.request.abort();
  const request = new AbortController(); state.request = request;
  $('#clip-grid').setAttribute('aria-busy', 'true');
  $('#library-error').hidden = true;
  const params = new URLSearchParams();
  if (state.q) params.set('q', state.q);
  if (state.subject) params.set('subject', state.subject);
  if (state.month) params.set('month', state.month);
  try {
    const data = await jsonResponse(await fetch(`/api/clips?${params}`, { signal: request.signal }));
    if (state.request !== request || epoch !== state.epoch) return;
    state.clips = data.clips; state.subjects = data.subjects; state.stats = data.stats; state.loaded = true;
    renderNavigation(); renderMonths(data.months); renderClips();
  } catch (error) {
    if (error.name === 'AbortError' || epoch !== state.epoch) return;
    if (error.status === 401) { applyUser(null, 'Please sign in to open your library.'); return; }
    $('#load-error-text').textContent = `Couldn’t load your library. ${error.message}`;
    $('#library-error').hidden = false;
    $('#empty-library').hidden = true; $('#no-results').hidden = true; $('#getting-started').hidden = true;
    $('#clip-grid').replaceChildren(); $('#result-count').textContent = '—'; $('#results-note').textContent = 'Library unavailable';
  } finally { if (state.request === request) $('#clip-grid').setAttribute('aria-busy', 'false'); }
}

function renderNavigation() {
  const total = state.stats.total_clips;
  $('#nav-total').textContent = total;
  $('#storage-used').textContent = total ? `${bytes(state.stats.total_bytes)} saved locally` : 'Local library';
  $('#all-clips').classList.toggle('active', !state.subject);
  $('#all-clips').setAttribute('aria-pressed', String(!state.subject));
  const list = $('#subject-list'); list.replaceChildren();
  if (!state.subjects.length) list.append(element('p', 'sidebar-hint', 'Your subjects will appear here.'));
  for (const subject of state.subjects) {
    const button = element('button', `nav-item${state.subject === subject.name ? ' active' : ''}`);
    button.setAttribute('aria-pressed', String(state.subject === subject.name));
    button.append(element('span', 'subject-dot'), element('span', '', subject.name), element('span', 'count', subject.count));
    button.addEventListener('click', () => { state.subject = subject.name; loadClips(); });
    list.append(button);
  }
  const datalist = $('#subjects'); datalist.replaceChildren();
  for (const subject of state.subjects) { const option = element('option'); option.value = subject.name; datalist.append(option); }
}

function renderMonths(months) {
  const select = $('#month-filter'); select.replaceChildren();
  const choices = [['', 'Any recording month'], ...months.map((month) => [month, monthLabel(month)]), ['undated', 'Date not recorded']];
  if (state.month && !choices.some(([value]) => value === state.month)) choices.splice(1, 0, [state.month, monthLabel(state.month)]);
  for (const [value, label] of choices) { const option = element('option', '', label); option.value = value; select.append(option); }
  select.value = state.month;
}

function renderClips() {
  const grid = $('#clip-grid'); grid.replaceChildren();
  const filtered = Boolean(state.q || state.subject || state.month);
  $('#results-heading').firstChild.textContent = `${state.subject || 'All clips'} `;
  $('#result-count').textContent = state.clips.length;
  $('#clear-filters').hidden = !filtered;
  $('#results-note').hidden = filtered;
  $('#results-note').textContent = state.stats.total_clips ? 'A collection of your moments' : 'Your collection starts here';
  $('#empty-library').hidden = state.stats.total_clips > 0 || filtered;
  $('#getting-started').hidden = state.stats.total_clips > 0 || filtered;
  $('#no-results').hidden = !filtered || state.clips.length > 0;
  const clips = [...state.clips];
  if ($('#sort').value === 'title') clips.sort((a, b) => a.title.localeCompare(b.title));
  if ($('#sort').value === 'recorded') clips.sort((a, b) => (b.recording_date || '').localeCompare(a.recording_date || '') || b.uploaded_at.localeCompare(a.uploaded_at));
  for (const clip of clips) {
    const card = element('article', 'clip-card');
    const preview = element('button', 'preview-button'); preview.setAttribute('aria-label', `Play ${clip.title}`);
    const video = element('video', 'clip-preview'); video.src = `${clip.video_url}#t=0.1`; video.preload = 'metadata'; video.muted = true; video.playsInline = true; video.setAttribute('aria-hidden', 'true'); video.tabIndex = -1;
    const play = element('span', 'play-overlay'); const circle = element('span'); circle.append(icon('play')); play.append(circle); preview.append(video, play);
    preview.addEventListener('click', () => openPlayer(clip));
    const body = element('div', 'clip-card-body'); const top = element('div', 'clip-card-top');
    const edit = element('button', 'icon-button'); edit.setAttribute('aria-label', `Edit ${clip.title}`); edit.append(icon('edit')); edit.addEventListener('click', () => openForm(clip));
    top.append(element('span', 'subject-tag', clip.subject), edit);
    const title = element('button', 'clip-title-button'); title.append(element('h3', '', clip.title)); title.addEventListener('click', () => openPlayer(clip));
    body.append(top, title);
    if (clip.notes) body.append(element('p', 'clip-note', clip.notes));
    card.append(preview, body); grid.append(card);
  }
}

function resetFilters() { state.q = ''; state.subject = ''; state.month = ''; $('#search').value = ''; $('#month-filter').value = ''; loadClips(); }
let searchTimer;
$('#search').addEventListener('input', () => { clearTimeout(searchTimer); state.q = $('#search').value.trim(); searchTimer = setTimeout(loadClips, 160); });
$('#all-clips').addEventListener('click', resetFilters);
$('#month-filter').addEventListener('change', () => { state.month = $('#month-filter').value; loadClips(); });
$('#sort').addEventListener('change', renderClips);
$('#clear-filters').addEventListener('click', resetFilters);
$('#reset-search').addEventListener('click', resetFilters);
$('#retry-load').addEventListener('click', loadClips);

function formError(message) { $('#form-error').textContent = message; $('#form-error').hidden = !message; }
function chooseFile(file) {
  if (!file) return;
  state.file = null;
  $('#file-label').textContent = 'Choose a clip or drop it here';
  $('#file-description').textContent = 'MP4 or MOV video · Up to 250 MB';
  if (!/\.(mp4|mov)$/i.test(file.name)) { formError('Please choose an MP4 or MOV video.'); return; }
  if (file.size > 250 * 1024 * 1024) { formError('This clip is too large. Choose an MP4 or MOV of 250 MB or smaller.'); return; }
  if (!file.size) { formError('This file is empty. Please choose an MP4 or MOV video.'); return; }
  state.file = file; formError('');
  $('#file-label').textContent = file.name; $('#file-description').textContent = `${bytes(file.size)} · Click to choose a different clip`;
}

function openForm(clip = null) {
  if (!state.user) return;
  state.editId = clip ? clip.id : null; state.file = null;
  $('#clip-form').reset(); formError(''); $('#upload-progress').hidden = true; $('#file-section').hidden = Boolean(clip);
  $('#form-heading').textContent = clip ? 'Edit clip details' : 'Add a clip';
  $('#form-eyebrow').textContent = clip ? 'A LITTLE MORE CONTEXT' : 'A NEW MEMORY';
  $('#save-clip').replaceChildren(document.createTextNode(clip ? 'Save changes' : 'Add to library'), icon('arrow'));
  $('#file-label').textContent = 'Choose a clip or drop it here'; $('#file-description').textContent = 'MP4 or MOV video · Up to 250 MB';
  $('#clip-title').value = clip?.title || ''; $('#clip-subject').value = clip?.subject || ''; $('#clip-date').value = clip?.recording_date || ''; $('#clip-notes').value = clip?.notes || '';
  $('#clip-dialog').showModal();
  (clip ? $('#clip-title') : $('#file-drop')).focus();
}
document.querySelectorAll('[data-add]').forEach((button) => button.addEventListener('click', () => openForm()));
document.querySelectorAll('.close-dialog').forEach((button) => button.addEventListener('click', () => { if (!state.busy) $('#clip-dialog').close(); }));
$('#clip-dialog').addEventListener('cancel', (event) => { if (state.busy) event.preventDefault(); });
$('#file-drop').addEventListener('click', () => $('#clip-file').click());
$('#clip-file').addEventListener('change', () => chooseFile($('#clip-file').files[0]));
for (const eventName of ['dragenter', 'dragover']) $('#file-drop').addEventListener(eventName, (event) => { event.preventDefault(); if (!state.busy) $('#file-drop').classList.add('dragover'); });
for (const eventName of ['dragleave', 'drop']) $('#file-drop').addEventListener(eventName, (event) => { event.preventDefault(); $('#file-drop').classList.remove('dragover'); });
$('#file-drop').addEventListener('drop', (event) => { if (state.busy) return; if (event.dataTransfer.files.length !== 1) { formError('Please add one MP4 or MOV clip at a time.'); return; } chooseFile(event.dataTransfer.files[0]); });

function upload(metadata) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest(); request.open('POST', '/api/clips'); request.timeout = 300000;
    state.uploadRequest = request;
    request.setRequestHeader('Content-Type', /\.mov$/i.test(state.file.name) ? 'video/quicktime' : 'video/mp4'); request.setRequestHeader('X-Clip-Metadata', encodeURIComponent(JSON.stringify(metadata)));
    request.upload.addEventListener('progress', (event) => { if (!event.lengthComputable) return; const percent = Math.round(event.loaded / event.total * 100); $('#progress-value').textContent = `${percent}%`; $('#progress-bar').value = percent; $('#progress-text').textContent = percent === 100 ? 'Saving clip and details…' : 'Uploading to your local library…'; });
    request.addEventListener('load', () => { let result; try { result = JSON.parse(request.responseText); } catch { reject(new Error('The server returned an unreadable response. Check your library before trying again.')); return; } if (request.status >= 200 && request.status < 300) resolve(result); else { const error = new Error(result.error || 'The clip could not be saved. Please try again.'); error.status = request.status; reject(error); } });
    request.addEventListener('abort', () => { const error = new Error('Upload cancelled.'); error.name = 'AbortError'; reject(error); });
    request.addEventListener('error', () => reject(new Error('Can’t reach the local server. Check that ClipShelf is running, then try again.')));
    request.addEventListener('timeout', () => reject(new Error('The upload took too long. Check your library before trying again.')));
    request.send(state.file);
  });
}

$('#clip-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (state.busy || !state.user) return;
  const epoch = state.epoch;
  const metadata = { title: $('#clip-title').value.trim(), subject: $('#clip-subject').value.trim(), recording_date: $('#clip-date').value || null, notes: $('#clip-notes').value.trim() };
  if (!metadata.title || !metadata.subject) { formError('Please add a title and subject.'); return; }
  if (!state.editId && !state.file) { formError('Choose an MP4 or MOV video to add to your shelf.'); $('#file-drop').focus(); return; }
  state.busy = true; formError('');
  const isEdit = Boolean(state.editId);
  $('#clip-form').querySelectorAll('input,textarea,button').forEach((field) => { field.disabled = true; });
  if (!isEdit) { $('#upload-progress').hidden = false; $('#progress-bar').value = 0; $('#progress-value').textContent = '0%'; }
  try {
    state.mutation = new AbortController();
    if (isEdit) await jsonResponse(await fetch(`/api/clips/${state.editId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(metadata), signal: state.mutation.signal }));
    else { metadata.original_filename = state.file.name; await upload(metadata); }
    if (epoch !== state.epoch) return;
    $('#clip-dialog').close();
    await loadClips();
    toast(isEdit ? 'Clip details saved.' : 'Your clip is on the shelf.');
  } catch (error) { if (epoch !== state.epoch || error.name === 'AbortError') return; if (error.status === 401) applyUser(null, 'Please sign in again.'); else formError(error.message); }
  finally { if (epoch === state.epoch) { state.busy = false; state.uploadRequest = null; state.mutation = null; $('#clip-form').querySelectorAll('input,textarea,button').forEach((field) => { field.disabled = false; }); $('#upload-progress').hidden = true; } }
});

function openPlayer(clip) {
  if (!state.user) return;
  state.playing = clip; $('#player-title').textContent = clip.title; $('#player-notes').textContent = clip.notes; $('#player-notes').hidden = !clip.notes;
  const meta = $('#player-meta'); meta.replaceChildren(element('span', 'subject-tag', clip.subject), element('span', '', `${clip.recording_date ? 'Recorded ' : ''}${recordedDate(clip.recording_date)}`));
  $('#player-file').textContent = `${clip.original_filename} · ${bytes(clip.size_bytes)} · Added ${uploadDate(clip.uploaded_at)}`;
  $('#playback-error').hidden = true; $('#player').src = clip.video_url; $('#player-dialog').showModal();
  $('#player').play().catch(() => { if ($('#player').error) $('#playback-error').hidden = false; });
}
$('#player').addEventListener('error', () => { if ($('#player-dialog').open && $('#player').getAttribute('src')) $('#playback-error').hidden = false; });
$('#close-player').addEventListener('click', () => $('#player-dialog').close());
$('#player-dialog').addEventListener('close', () => { $('#player').pause(); $('#player').removeAttribute('src'); $('#player').load(); });
$('#edit-playing').addEventListener('click', () => { const clip = state.playing; $('#player-dialog').close(); openForm(clip); });
function toast(message) { clearTimeout(state.toastTimer); $('#toast span').textContent = message; $('#toast').hidden = false; state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4500); }

function clearPrivateLibrary() {
  state.epoch += 1;
  clearTimeout(searchTimer); clearTimeout(state.toastTimer);
  state.request?.abort(); state.uploadRequest?.abort(); state.mutation?.abort();
  state.request = null; state.uploadRequest = null; state.mutation = null;
  for (const video of document.querySelectorAll('video')) { video.pause(); video.removeAttribute('src'); video.load(); }
  $('#clip-dialog').close(); $('#player-dialog').close();
  $('#clip-form').reset();
  $('#clip-form').querySelectorAll('input,textarea,button').forEach((field) => { field.disabled = false; });
  state.clips = []; state.subjects = []; state.stats = null; state.loaded = false; state.playing = null; state.file = null; state.editId = null; state.busy = false;
  state.q = ''; state.subject = ''; state.month = '';
  for (const selector of ['#clip-grid', '#subject-list', '#subjects', '#player-meta']) $(selector).replaceChildren();
  for (const selector of ['#player-title', '#player-notes', '#player-file', '#account-name', '#form-error', '#file-label', '#file-description']) $(selector).textContent = '';
  $('#search').value = ''; $('#sort').value = 'recent'; renderMonths([]);
  $('#account-name').removeAttribute('title');
  $('#results-heading').firstChild.textContent = 'All clips ';
  $('#results-note').textContent = ''; $('#results-note').hidden = false; $('#clear-filters').hidden = true;
  $('#all-clips').classList.add('active'); $('#all-clips').setAttribute('aria-pressed', 'true');
  $('#nav-total').textContent = '0'; $('#result-count').textContent = '0'; $('#storage-used').textContent = 'Local library';
  $('#toast').hidden = true; $('#library-error').hidden = true; $('#upload-progress').hidden = true;
  $('#empty-library').hidden = true; $('#no-results').hidden = true; $('#getting-started').hidden = true;
}

function authError(message) { $('#auth-error').textContent = message; $('#auth-error').hidden = !message; }

function applyUser(user, message = '') {
  clearPrivateLibrary(); state.user = user;
  $('#auth-loading').hidden = true; $('#auth-screen').hidden = Boolean(user); $('#app-shell').hidden = !user;
  $('.skip-link').hidden = !user;
  $('#auth-password').value = '';
  authError(message);
  if (user) { $('#account-name').textContent = user.name; $('#account-name').title = user.email; loadClips(); }
}

function setAuthMode(mode) {
  state.authMode = mode; const signup = mode === 'signup';
  $('#auth-heading').textContent = signup ? 'Create your account' : 'Sign in';
  $('#auth-description').textContent = signup ? 'Start with an empty shelf, just for you.' : 'Welcome back to your library.';
  $('#auth-name-field').hidden = !signup; $('#auth-name').disabled = !signup; $('#auth-name').required = signup;
  $('#auth-password').autocomplete = signup ? 'new-password' : 'current-password';
  $('#auth-password').minLength = signup ? 6 : 1; $('#auth-password-help').hidden = !signup; $('#auth-password').value = '';
  $('#auth-submit').replaceChildren(document.createTextNode(signup ? 'Create account' : 'Sign in'), icon('arrow'));
  $('#auth-switch-label').textContent = signup ? 'Already have an account?' : 'New to ClipShelf?';
  $('#auth-toggle').textContent = signup ? 'Sign in instead' : 'Create account';
  authError('');
}

let sessionRequest = 0;
async function refreshSession() {
  if (state.logoutPending || state.authBusy) return;
  const request = ++sessionRequest;
  try {
    const result = await jsonResponse(await fetch('/api/auth/session', {cache:'no-store'}));
    if (request !== sessionRequest || state.authBusy || state.logoutPending) return;
    if (state.user?.id !== result.user?.id || !state.loaded) applyUser(result.user);
  } catch (error) {
    if (request === sessionRequest && !state.authBusy && !state.logoutPending) applyUser(null, `Couldn’t connect to ClipShelf. ${error.message}`);
  }
}

const accountChannel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('clipshelf-account') : null;
accountChannel?.addEventListener('message', () => { clearPrivateLibrary(); $('#app-shell').hidden = true; refreshSession(); });
$('#auth-toggle').addEventListener('click', () => { if (!state.authBusy) setAuthMode(state.authMode === 'login' ? 'signup' : 'login'); });
$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (state.authBusy) return;
  state.authBusy = true; sessionRequest += 1; authError('');
  const payload = {email:$('#auth-email').value.trim(), password:$('#auth-password').value};
  if (state.authMode === 'signup') payload.name = $('#auth-name').value.trim();
  const mode = state.authMode;
  $('#auth-form').querySelectorAll('input,button').forEach((field) => { field.disabled = true; }); $('#auth-toggle').disabled = true;
  try {
    const result = await jsonResponse(await fetch(`/api/auth/${mode}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}));
    applyUser(result.user); accountChannel?.postMessage('changed');
  } catch (error) { authError(error.message); }
  finally { state.authBusy = false; $('#auth-form').querySelectorAll('input,button').forEach((field) => { field.disabled = false; }); $('#auth-name').disabled = state.authMode !== 'signup'; $('#auth-toggle').disabled = false; }
});
async function signOut() {
  if (state.authBusy) return;
  state.authBusy = true; sessionRequest += 1;
  clearPrivateLibrary(); $('#app-shell').hidden = true; $('#auth-loading').hidden = false;
  try {
    await jsonResponse(await fetch('/api/auth/logout', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));
    state.logoutPending = false; $('#retry-sign-out').hidden = true;
    $('#auth-form').querySelectorAll('input,button').forEach((field) => { field.disabled = false; }); $('#auth-toggle').disabled = false;
    $('#auth-form').reset(); setAuthMode('login'); applyUser(null); accountChannel?.postMessage('changed'); $('#auth-email').focus();
  } catch (error) {
    state.logoutPending = true;
    applyUser(null, `Sign-out could not be confirmed. Retry when ClipShelf is running. ${error.message}`);
    $('#auth-form').querySelectorAll('input,button').forEach((field) => { field.disabled = true; }); $('#auth-toggle').disabled = true; $('#retry-sign-out').hidden = false;
  }
  finally { state.authBusy = false; }
}
$('#sign-out').addEventListener('click', signOut);
$('#retry-sign-out').addEventListener('click', signOut);
window.addEventListener('pageshow', (event) => { if (event.persisted) { clearPrivateLibrary(); $('#app-shell').hidden = true; refreshSession(); } });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && !state.authBusy) refreshSession(); });
setAuthMode('login');
refreshSession();
