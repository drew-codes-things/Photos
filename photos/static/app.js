'use strict';

const VIEW_TITLES = { timeline: 'Photos', favourites: 'Favourites', albums: 'Albums', bin: 'Bin', album: 'Album' };
const EMPTY_TEXT = {
  timeline: 'Photos and videos you upload appear here, newest first.',
  favourites: 'Tap the heart on a photo to keep it here.',
  albums: 'Albums you make appear here.',
  bin: 'The bin is empty.',
  album: 'This album is empty. Select photos in the timeline and add them here.',
  search: 'Nothing matches that search.',
};
const TAKEN_SOURCE_TEXT = {
  photo: "from the photo's own details",
  video: "from the video's own details",
  file: "from the file's date",
  upload: 'when it was uploaded (the file had no date)',
};
const PAGE_SIZE = 150;
const SEARCH_DELAY_MS = 300;
const TOAST_MS = 4000;
const STORAGE_REFRESH_MS = 60000;
const LONG_PRESS_MS = 450;
const SWIPE_MIN_PX = 60;
const VIDEO_CONTROLS_PX = 72;
const UPLOADS_AT_ONCE = 2;
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];
const RETRY_LIMIT = 40;
const REFRESH_AFTER_UPLOADS = 6;
const GIGABYTE = 1024 ** 3;
const MEGABYTE = 1024 ** 2;
const STOP_STATUSES = [400, 401, 403, 404, 409, 410, 411, 413, 415, 422, 507];
const REFUSED_STATUSES = [400, 413, 415, 422];
const MOVE_TOLERANCE_PX = 10;
const NEAR_TOP_PX = 300;
const ICONS = {
  heart: 'M12 20s-7-4.4-9.2-8.6C1.4 8.7 3 5 6.6 5c2 0 3.4 1.1 4.1 2.4h2.6C14 6.1 15.4 5 17.4 5 21 5 22.6 8.7 21.2 11.4 19 15.6 12 20 12 20z',
  album: 'M4 5h16v14H4zM4 15l4.5-4.5 4 4 2.5-2.5L20 17M15 9h.01',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  restore: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5',
  forever: 'M4 7h16M6 7l1 13h10l1-13M9 7V4h6v3M10 11l4 5M14 11l-4 5',
  download: 'M12 4v11M7 10l5 5 5-5M5 20h14',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5h.01',
  close: 'M6 6l12 12M18 6 6 18',
  prev: 'M15 5l-7 7 7 7',
  next: 'M9 5l7 7-7 7',
  play: 'M8 5v14l11-7z',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  calendar: 'M4 6h16v14H4zM4 10h16M8 3v4M16 3v4',
  remove: 'M4 5h16v14H4zM9 12h6',
  down: 'M6 9l6 6 6-6',
  up: 'M6 15l6-6 6 6',
};

const state = {
  view: 'timeline', albumId: null, albumName: '', query: '', from: '', to: '', group: 'day',
  items: [], cursor: null, total: 0, loading: false, loadedOnce: false, generation: 0, csrf: '', session: null,
  selecting: false, selected: new Set(), viewerIndex: -1, viewerPushed: false, albums: [], storage: null,
};
const byId = (id) => document.getElementById(id);
const dayFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
const monthFormat = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' });
const fullFormat = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const shortFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const numberFormat = new Intl.NumberFormat();

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

function isSignInRedirect(response) {
  return response.redirected && new URL(response.url).pathname.startsWith('/signin');
}

function mediaUrl(item, kind) {
  return `/media/${item.id}/${kind}?v=${encodeURIComponent(item.version)}`;
}

async function api(method, path, body) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  if (isSignInRedirect(response)) throw new ApiError('You are signed out. Reload the page to sign in again.', 401);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data && data.error ? data.error : `the server answered ${response.status}`;
    throw new ApiError(message, response.status, data);
  }
  return data;
}

let toastTimer = 0;
function showToast(message, isError = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}

function reportError(error) {
  if (error instanceof ApiError && error.status === 401) {
    showToast('You are signed out. Reload the page to sign in again.', true);
    return;
  }
  showToast(error.message || String(error), true);
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `icon icon-${name}`);
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function iconButton(name, label, onClick, pressed) {
  const button = element('button', 'icon-btn');
  button.type = 'button';
  button.setAttribute('aria-label', label);
  button.title = label;
  if (pressed !== undefined) button.setAttribute('aria-pressed', String(pressed));
  button.append(icon(name));
  button.addEventListener('click', onClick);
  return button;
}

function fillIcon(id, name) {
  byId(id).replaceChildren(icon(name));
}

function formatBytes(bytes) {
  if (bytes >= GIGABYTE) return `${(bytes / GIGABYTE).toFixed(1)} GB`;
  if (bytes >= MEGABYTE) return `${(bytes / MEGABYTE).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function formatDuration(seconds) {
  const whole = Math.round(seconds || 0);
  const minutes = Math.floor(whole / 60);
  const hours = Math.floor(minutes / 60);
  const twoDigits = (value) => String(value).padStart(2, '0');
  if (hours) return `${hours}:${twoDigits(minutes % 60)}:${twoDigits(whole % 60)}`;
  return `${minutes}:${twoDigits(whole % 60)}`;
}

function wallClock(text) {
  const [day, time = '00:00:00'] = text.split('T');
  const [year, month, date] = day.split('-').map(Number);
  const [hour, minute, second] = time.split(':').map(Number);
  return new Date(year, month - 1, date, hour, minute, second);
}

function localIso(milliseconds) {
  const moment = new Date(milliseconds);
  const twoDigits = (value) => String(value).padStart(2, '0');
  return `${moment.getFullYear()}-${twoDigits(moment.getMonth() + 1)}-${twoDigits(moment.getDate())}T`
    + `${twoDigits(moment.getHours())}:${twoDigits(moment.getMinutes())}:${twoDigits(moment.getSeconds())}`;
}

function groupKey(item) {
  return state.group === 'month' ? item.takenAt.slice(0, 7) : item.takenAt.slice(0, 10);
}

function groupTitle(key) {
  if (state.group === 'month') return monthFormat.format(wallClock(`${key}-01`));
  return dayFormat.format(wallClock(key));
}

function isSearching() {
  return Boolean(state.query || state.from || state.to);
}

function listQuery(cursor) {
  const params = new URLSearchParams({ view: state.view, limit: String(PAGE_SIZE) });
  if (state.view === 'album') params.set('album', String(state.albumId));
  if (state.query) params.set('q', state.query);
  if (state.from) params.set('from', state.from);
  if (state.to) params.set('to', state.to);
  if (cursor) params.set('cursor', cursor);
  return `/api/media?${params}`;
}

function resetTimeline() {
  state.generation += 1;
  state.items = [];
  state.cursor = null;
  state.total = 0;
  state.loading = false;
  state.loadedOnce = false;
  byId('timeline').replaceChildren();
}

async function loadMore() {
  if (state.loading || state.view === 'albums') return;
  if (state.loadedOnce && !state.cursor) return;
  const generation = state.generation;
  state.loading = true;
  byId('loading').hidden = state.items.length > 0;
  try {
    const page = await api('GET', listQuery(state.cursor));
    if (generation !== state.generation) return;
    const start = state.items.length;
    state.items.push(...page.items);
    state.cursor = page.nextCursor;
    state.total = page.total;
    state.loadedOnce = true;
    appendTiles(page.items, start);
    renderCounts();
  } catch (error) {
    reportError(error);
  } finally {
    if (generation === state.generation) {
      state.loading = false;
      byId('loading').hidden = true;
      if (state.cursor && sentinelIsNear()) loadMore();
    }
  }
}

function sentinelIsNear() {
  return byId('sentinel').getBoundingClientRect().top < window.innerHeight * 2;
}

function renderCounts() {
  const count = byId('view-count');
  count.textContent = state.total ? `${numberFormat.format(state.total)} ${state.total === 1 ? 'item' : 'items'}` : '';
  const empty = byId('empty-state');
  empty.hidden = state.items.length > 0;
  empty.textContent = isSearching() ? EMPTY_TEXT.search : EMPTY_TEXT[state.view];
}

function lastSection() {
  const sections = byId('timeline').children;
  return sections.length ? sections[sections.length - 1] : null;
}

function makeSection(key) {
  const section = element('section', 'day');
  section.dataset.key = key;
  const head = element('div', 'day-head');
  head.append(element('h2', 'day-title', groupTitle(key)));
  const pick = element('button', 'day-pick', 'Select all');
  pick.type = 'button';
  pick.addEventListener('click', () => selectSection(key));
  head.append(pick);
  section.append(head, element('div', `grid ${state.group === 'month' ? 'compact' : ''}`));
  byId('timeline').append(section);
  return section;
}

function appendTiles(items, startIndex) {
  items.forEach((item, offset) => {
    const key = groupKey(item);
    let section = lastSection();
    if (!section || section.dataset.key !== key) section = makeSection(key);
    section.querySelector('.grid').append(makeTile(item, startIndex + offset));
  });
}

function tileImage(item) {
  if (!item.hasPreview) {
    const blank = element('div', 'no-preview');
    blank.append(element('strong', '', item.format.toUpperCase()), element('span', '', 'No preview'));
    return blank;
  }
  const image = element('img');
  image.src = mediaUrl(item, 'thumb');
  image.alt = item.name;
  image.loading = 'lazy';
  image.decoding = 'async';
  return image;
}

function makeTile(item, index) {
  const tile = element('button', 'tile');
  tile.type = 'button';
  tile.dataset.index = String(index);
  tile.dataset.id = String(item.id);
  tile.setAttribute('aria-label', `${item.kind === 'video' ? 'Video' : 'Photo'} ${item.name}, ${shortFormat.format(wallClock(item.takenAt))}`);
  tile.append(tileImage(item));
  if (item.kind === 'video') {
    const badge = element('span', 'badge video-badge');
    badge.append(icon('play'), document.createTextNode(item.duration ? formatDuration(item.duration) : 'Video'));
    tile.append(badge);
  }
  if (item.favourite) {
    const heart = element('span', 'badge fav-badge');
    heart.append(icon('heart'));
    tile.append(heart);
  }
  const tick = element('span', 'tick');
  tick.append(icon('check'));
  tile.append(tick);
  tile.classList.toggle('selected', state.selected.has(item.id));
  bindTile(tile);
  return tile;
}

function refreshTile(item) {
  const index = state.items.findIndex((candidate) => candidate.id === item.id);
  const tile = document.querySelector(`.tile[data-id="${item.id}"]`);
  if (index < 0 || !tile) return;
  tile.replaceWith(makeTile(item, index));
}

function bindTile(tile) {
  let pressTimer = 0;
  let pressed = false;
  let startX = 0;
  let startY = 0;
  tile.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'mouse') return;
    pressed = false;
    startX = event.clientX;
    startY = event.clientY;
    pressTimer = setTimeout(() => {
      pressed = true;
      startSelecting();
      toggleSelected(Number(tile.dataset.id));
    }, LONG_PRESS_MS);
  });
  const cancelPress = () => clearTimeout(pressTimer);
  tile.addEventListener('pointerup', cancelPress);
  tile.addEventListener('pointercancel', cancelPress);
  tile.addEventListener('pointermove', (event) => {
    if (Math.hypot(event.clientX - startX, event.clientY - startY) > MOVE_TOLERANCE_PX) cancelPress();
  });
  tile.addEventListener('contextmenu', (event) => { if (pressed) event.preventDefault(); });
  tile.addEventListener('click', (event) => {
    if (pressed) { pressed = false; return; }
    const id = Number(tile.dataset.id);
    if (state.selecting || event.shiftKey || event.ctrlKey || event.metaKey) {
      startSelecting();
      toggleSelected(id);
      return;
    }
    openViewer(Number(tile.dataset.index));
  });
}

function startSelecting() {
  if (state.selecting) return;
  state.selecting = true;
  document.body.classList.add('selecting');
  byId('select-toggle').setAttribute('aria-pressed', 'true');
  renderSelection();
}

function stopSelecting() {
  state.selecting = false;
  state.selected.clear();
  document.body.classList.remove('selecting');
  byId('select-toggle').setAttribute('aria-pressed', 'false');
  document.querySelectorAll('.tile.selected').forEach((tile) => tile.classList.remove('selected'));
  renderSelection();
}

function toggleSelected(id) {
  if (state.selected.has(id)) state.selected.delete(id);
  else state.selected.add(id);
  const tile = document.querySelector(`.tile[data-id="${id}"]`);
  if (tile) tile.classList.toggle('selected', state.selected.has(id));
  renderSelection();
}

function selectSection(key) {
  startSelecting();
  const section = document.querySelector(`.day[data-key="${key}"]`);
  const ids = [...section.querySelectorAll('.tile')].map((tile) => Number(tile.dataset.id));
  const allChosen = ids.every((id) => state.selected.has(id));
  ids.forEach((id) => {
    if (allChosen) state.selected.delete(id);
    else state.selected.add(id);
    section.querySelector(`.tile[data-id="${id}"]`).classList.toggle('selected', !allChosen);
  });
  renderSelection();
}

function actionButton(label, onClick, variant = 'ghost') {
  const button = element('button', `btn ${variant}`, label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function selectionActions() {
  const ids = () => [...state.selected];
  if (state.view === 'bin') {
    return [actionButton('Restore', () => bulk('restore', ids())), actionButton('Delete forever', () => deleteForever(ids()), '')];
  }
  const actions = [
    actionButton(state.view === 'favourites' ? 'Unfavourite' : 'Favourite', () => bulk(state.view === 'favourites' ? 'unfavourite' : 'favourite', ids())),
    actionButton('Add to album', () => pickAlbum(ids())),
  ];
  if (state.view === 'album') actions.push(actionButton('Remove from album', () => removeFromAlbum(ids())));
  actions.push(actionButton('Move to bin', () => bulk('trash', ids()), ''));
  return actions;
}

function renderSelection() {
  const bar = byId('selection-bar');
  bar.hidden = !state.selecting;
  if (!state.selecting) return;
  const count = state.selected.size;
  byId('selection-count').textContent = count ? `${count} selected` : 'Tap photos to select them';
  const actions = selectionActions();
  actions.forEach((button) => { button.disabled = count === 0; });
  byId('selection-actions').replaceChildren(...actions);
}

function dropFromView(ids) {
  const gone = new Set(ids);
  state.items = state.items.filter((item) => !gone.has(item.id));
  state.total = Math.max(0, state.total - gone.size);
  rebuildTimeline();
}

function rebuildTimeline() {
  byId('timeline').replaceChildren();
  appendTiles(state.items, 0);
  renderCounts();
}

const BULK_DONE_TEXT = {
  favourite: (count) => `${count} added to favourites`,
  unfavourite: (count) => `${count} removed from favourites`,
  trash: (count) => `${count} moved to the bin`,
  restore: (count) => `${count} restored`,
  delete: (count) => `${count} deleted forever`,
};

function applyBulkLocally(action, ids) {
  const leavesView = action === 'trash' || action === 'restore' || action === 'delete'
    || (action === 'unfavourite' && state.view === 'favourites');
  if (leavesView) {
    dropFromView(ids);
    return;
  }
  const chosen = new Set(ids);
  state.items.filter((item) => chosen.has(item.id)).forEach((item) => {
    item.favourite = action === 'favourite';
    refreshTile(item);
  });
}

async function bulk(action, ids) {
  if (!ids.length) return;
  try {
    const result = await api('POST', '/api/media/bulk', { action, ids });
    applyBulkLocally(action, ids);
    showToast(BULK_DONE_TEXT[action](result.changed));
    if (state.selecting) stopSelecting();
    if (action === 'trash' || action === 'delete') loadStorage();
  } catch (error) {
    reportError(error);
  }
}

function deleteForever(ids) {
  const noun = ids.length === 1 ? 'this item' : `these ${ids.length} items`;
  if (!window.confirm(`Delete ${noun} forever? This cannot be undone.`)) return Promise.resolve();
  return bulk('delete', ids);
}

async function removeFromAlbum(ids) {
  try {
    const result = await api('POST', `/api/albums/${state.albumId}/remove`, { ids });
    dropFromView(ids);
    showToast(`${result.removed} removed from ${state.albumName}`);
    stopSelecting();
  } catch (error) {
    reportError(error);
  }
}

async function loadAlbums() {
  const data = await api('GET', '/api/albums');
  state.albums = data.albums;
  return state.albums;
}

function albumCard(album) {
  const card = element('button', 'album-card');
  card.type = 'button';
  const cover = element('div', 'album-cover');
  if (album.coverId) {
    const image = element('img');
    image.src = `/media/${album.coverId}/thumb`;
    image.alt = '';
    image.loading = 'lazy';
    image.addEventListener('error', () => image.remove());
    cover.append(image);
  } else {
    cover.append(icon('album'));
  }
  card.append(cover, element('strong', 'album-name', album.name), element('span', 'faint', `${numberFormat.format(album.count)} ${album.count === 1 ? 'item' : 'items'}`));
  card.addEventListener('click', () => openAlbum(album));
  return card;
}

async function renderAlbums() {
  const holder = byId('albums');
  try {
    const albums = await loadAlbums();
    if (state.view !== 'albums') return;
    holder.replaceChildren(...albums.map(albumCard));
    byId('view-count').textContent = albums.length ? `${albums.length} ${albums.length === 1 ? 'album' : 'albums'}` : '';
    const empty = byId('empty-state');
    empty.hidden = albums.length > 0;
    empty.textContent = EMPTY_TEXT.albums;
  } catch (error) {
    reportError(error);
  }
}

function openAlbum(album) {
  state.albumId = album.id;
  state.albumName = album.name;
  setView('album');
}

function askName(title, initial) {
  const dialog = byId('name-dialog');
  const input = byId('name-dialog-input');
  byId('name-dialog-title').textContent = title;
  input.value = initial || '';
  return new Promise((resolve) => {
    const finish = (value) => {
      dialog.removeEventListener('close', onClose);
      resolve(value);
    };
    const onClose = () => finish(dialog.returnValue === 'save' ? input.value.trim() : null);
    dialog.addEventListener('close', onClose);
    dialog.returnValue = '';
    dialog.showModal();
    input.focus();
    input.select();
  });
}

async function createAlbum() {
  const name = await askName('New album', '');
  if (!name) return;
  try {
    const album = await api('POST', '/api/albums', { name });
    showToast(`Album ${album.name} made`);
    if (state.view === 'albums') renderAlbums();
  } catch (error) {
    reportError(error);
  }
}

async function renameAlbum() {
  const name = await askName('Rename album', state.albumName);
  if (!name || name === state.albumName) return;
  try {
    const album = await api('PATCH', `/api/albums/${state.albumId}`, { name });
    state.albumName = album.name;
    byId('view-title').textContent = album.name;
  } catch (error) {
    reportError(error);
  }
}

async function deleteAlbum() {
  if (!window.confirm(`Delete the album ${state.albumName}? The photos in it stay in your library.`)) return;
  try {
    await api('DELETE', `/api/albums/${state.albumId}`);
    showToast(`Album ${state.albumName} deleted`);
    setView('albums');
  } catch (error) {
    reportError(error);
  }
}

let pickerIds = [];
async function pickAlbum(ids) {
  if (!ids.length) return;
  pickerIds = ids;
  const choices = byId('album-choices');
  choices.replaceChildren(element('p', 'faint', 'Loading albums...'));
  byId('album-picker-new').value = '';
  byId('album-picker').showModal();
  try {
    const albums = await loadAlbums();
    const buttons = albums.map((album) => {
      const button = actionButton(`${album.name} (${album.count})`, () => addToAlbum(album.id));
      button.classList.add('album-choice');
      return button;
    });
    choices.replaceChildren(...(buttons.length ? buttons : [element('p', 'faint', 'No albums yet. Name a new one below.')]));
  } catch (error) {
    reportError(error);
  }
}

async function addToAlbum(albumId) {
  byId('album-picker').close();
  try {
    const result = await api('POST', `/api/albums/${albumId}/add`, { ids: pickerIds });
    showToast(`${result.added} added to ${result.album.name}`);
    if (state.selecting) stopSelecting();
    if (state.viewerIndex >= 0) renderViewerInfo();
  } catch (error) {
    reportError(error);
  }
}

async function createAndAdd(event) {
  event.preventDefault();
  const name = byId('album-picker-new').value.trim();
  if (!name) {
    showToast('Type a name for the new album, or pick one above.', true);
    return;
  }
  try {
    const album = await api('POST', '/api/albums', { name });
    await addToAlbum(album.id);
  } catch (error) {
    reportError(error);
  }
}

function setView(view) {
  state.view = view;
  if (view !== 'album') {
    state.albumId = null;
    state.albumName = '';
  }
  if (state.selecting) stopSelecting();
  document.querySelectorAll('.view-btn').forEach((button) => {
    const current = button.dataset.view === view || (view === 'album' && button.dataset.view === 'albums');
    button.setAttribute('aria-current', String(current));
  });
  const isAlbums = view === 'albums';
  const isAlbum = view === 'album';
  byId('view-title').textContent = isAlbum ? state.albumName : VIEW_TITLES[view];
  byId('albums').hidden = !isAlbums;
  byId('timeline').hidden = isAlbums;
  byId('group-switch').hidden = isAlbums;
  byId('select-toggle').hidden = isAlbums;
  byId('album-new').hidden = !isAlbums;
  byId('album-rename').hidden = !isAlbum;
  byId('album-delete').hidden = !isAlbum;
  byId('album-back').hidden = !isAlbum;
  byId('bin-line').hidden = view !== 'bin';
  byId('view-count').textContent = '';
  byId('empty-state').hidden = true;
  resetTimeline();
  if (isAlbums) {
    byId('albums').replaceChildren();
    renderAlbums();
    return;
  }
  loadMore();
}

function reloadView() {
  setView(state.view);
}

async function loadStorage() {
  try {
    const space = await api('GET', '/api/storage');
    state.storage = space;
    renderStorage(space);
  } catch (error) {
    if (error.status === 401) reportError(error);
  }
}

function renderStorage(space) {
  const usedFraction = space.totalBytes ? (space.totalBytes - space.freeBytes) / space.totalBytes : 0;
  const bar = byId('storage-bar');
  bar.style.width = `${Math.min(100, Math.round(usedFraction * 100))}%`;
  bar.classList.toggle('full', !space.uploadsAllowed);
  byId('storage-library').textContent = `Library: ${formatBytes(space.libraryBytes)} in ${numberFormat.format(space.count)} ${space.count === 1 ? 'item' : 'items'}`;
  byId('storage-disk').textContent = `Disk: ${formatBytes(space.freeBytes)} free of ${formatBytes(space.totalBytes)}`;
  const held = space.reservedBytes ? ` (${formatBytes(space.reservedBytes)} is held for uploads still arriving)` : '';
  byId('storage-floor').textContent = `Uploads stop when less than ${formatBytes(space.minFreeBytes)} would be free, so there is room for ${formatBytes(space.roomBytes)} more${held}.`;
  const banner = byId('space-banner');
  banner.hidden = space.uploadsAllowed;
  banner.textContent = `The disk is nearly full: ${formatBytes(space.freeBytes)} is free and uploads are paused while less than ${formatBytes(space.minFreeBytes)} would be left. Empty the bin or free space on the server to upload again.`;
  byId('upload-label').classList.toggle('disabled', !space.uploadsAllowed);
  byId('upload-input').disabled = !space.uploadsAllowed;
}

function currentItem() {
  return state.items[state.viewerIndex];
}

function openViewer(index) {
  if (!state.items[index]) return;
  state.viewerIndex = index;
  byId('viewer').hidden = false;
  document.body.classList.add('viewing');
  if (!state.viewerPushed) {
    history.pushState({ viewer: true }, '');
    state.viewerPushed = true;
  }
  renderViewer();
}

function closeViewer(fromHistory = false) {
  if (byId('viewer').hidden) return;
  byId('viewer-stage').replaceChildren();
  byId('viewer').hidden = true;
  byId('viewer-info').hidden = true;
  document.body.classList.remove('viewing');
  const tile = document.querySelector(`.tile[data-index="${state.viewerIndex}"]`);
  state.viewerIndex = -1;
  if (state.viewerPushed && !fromHistory) {
    state.viewerPushed = false;
    history.back();
  }
  state.viewerPushed = false;
  if (tile) tile.focus({ preventScroll: false });
}

function stageContent(item) {
  if (item.kind === 'video') {
    const video = element('video', 'viewer-media');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    if (item.hasPreview) video.poster = mediaUrl(item, 'preview');
    video.src = mediaUrl(item, 'play');
    video.addEventListener('error', () => {
      const note = element('div', 'no-preview big');
      note.append(element('strong', '', 'This browser cannot play this video'), element('span', '', `It is a ${item.format.toUpperCase()} file. Download it to watch it on this device.`), downloadLink(item, 'btn send'));
      video.replaceWith(note);
    });
    return video;
  }
  if (!item.hasPreview) {
    const note = element('div', 'no-preview big');
    note.append(element('strong', '', `No preview for this ${item.format.toUpperCase()} file`), element('span', '', 'The original is stored safely. Download it to open it.'), downloadLink(item, 'btn send'));
    return note;
  }
  const image = element('img', 'viewer-media');
  image.src = mediaUrl(item, 'preview');
  image.alt = item.name;
  image.draggable = false;
  return image;
}

function downloadLink(item, className) {
  const link = element('a', className, 'Download original');
  link.href = `/media/${item.id}/original`;
  link.setAttribute('download', '');
  return link;
}

function viewerActions(item) {
  const download = element('a', 'icon-btn');
  download.href = `/media/${item.id}/original`;
  download.setAttribute('download', '');
  download.setAttribute('aria-label', 'Download original');
  download.title = 'Download original';
  download.append(icon('download'));
  const info = iconButton('info', 'Details', toggleInfo, !byId('viewer-info').hidden);
  if (state.view === 'bin') {
    return [info, download, iconButton('restore', 'Restore', () => viewerBulk('restore')), iconButton('forever', 'Delete forever', () => viewerBulk('delete'))];
  }
  const favourite = iconButton('heart', item.favourite ? 'Remove from favourites' : 'Add to favourites', () => viewerBulk(item.favourite ? 'unfavourite' : 'favourite'), item.favourite);
  favourite.classList.add('fav');
  return [info, favourite, iconButton('album', 'Add to album', () => pickAlbum([item.id])), download, iconButton('trash', 'Move to bin', () => viewerBulk('trash'))];
}

function renderViewer() {
  const item = currentItem();
  if (!item) {
    closeViewer();
    return;
  }
  byId('viewer-date').textContent = fullFormat.format(wallClock(item.takenAt));
  byId('viewer-name').textContent = item.name;
  byId('viewer-actions').replaceChildren(...viewerActions(item));
  byId('viewer-stage').replaceChildren(stageContent(item));
  byId('viewer-prev').disabled = state.viewerIndex <= 0;
  byId('viewer-next').disabled = state.viewerIndex >= state.items.length - 1 && !state.cursor;
  if (!byId('viewer-info').hidden) renderViewerInfo();
  preloadNeighbours();
}

function preloadNeighbours() {
  [state.viewerIndex - 1, state.viewerIndex + 1].forEach((index) => {
    const neighbour = state.items[index];
    if (neighbour && neighbour.kind === 'image' && neighbour.hasPreview) new Image().src = mediaUrl(neighbour, 'preview');
  });
}

async function stepViewer(direction) {
  const target = state.viewerIndex + direction;
  if (target < 0) return;
  if (target >= state.items.length) {
    if (!state.cursor) return;
    await loadMore();
    if (target >= state.items.length) return;
  }
  state.viewerIndex = target;
  renderViewer();
}

function toggleInfo() {
  const panel = byId('viewer-info');
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderViewerInfo();
  byId('viewer-actions').querySelectorAll('[aria-label="Details"]').forEach((button) => button.setAttribute('aria-pressed', String(!panel.hidden)));
}

async function renderViewerInfo() {
  const item = currentItem();
  if (!item) return;
  const details = byId('viewer-details');
  const rows = [
    ['Taken', `${fullFormat.format(wallClock(item.takenAt))}, ${TAKEN_SOURCE_TEXT[item.takenSource]}`],
    ['File', item.name],
    ['Type', `${item.format.toUpperCase()} ${item.kind}`],
    ['Size', formatBytes(item.size)],
  ];
  if (item.width && item.height) rows.push(['Pixels', `${item.width} x ${item.height}`]);
  if (item.duration) rows.push(['Length', formatDuration(item.duration)]);
  if (item.purgeAt) rows.push(['Deleted for good', shortFormat.format(new Date(item.purgeAt * 1000))]);
  const render = (extra) => details.replaceChildren(...[...rows, ...extra].flatMap(([term, value]) => [element('dt', '', term), element('dd', '', value)]));
  render([]);
  try {
    const full = await api('GET', `/api/media/${item.id}`);
    if (currentItem() !== item) return;
    render([['Albums', full.albums.length ? full.albums.map((album) => album.name).join(', ') : 'None']]);
  } catch (error) {
    reportError(error);
  }
}

async function viewerBulk(action) {
  const item = currentItem();
  if (!item) return;
  if (action === 'delete' && !window.confirm('Delete this item forever? This cannot be undone.')) return;
  try {
    await api('POST', '/api/media/bulk', { action, ids: [item.id] });
    showToast(BULK_DONE_TEXT[action](1));
    if (action === 'favourite' || action === 'unfavourite') {
      item.favourite = action === 'favourite';
      if (state.view === 'favourites' && !item.favourite) {
        afterViewerRemoval(item);
        return;
      }
      refreshTile(item);
      renderViewer();
      return;
    }
    afterViewerRemoval(item);
    loadStorage();
  } catch (error) {
    reportError(error);
  }
}

function afterViewerRemoval(item) {
  const index = state.viewerIndex;
  dropFromView([item.id]);
  if (!state.items.length) {
    closeViewer();
    return;
  }
  state.viewerIndex = Math.min(index, state.items.length - 1);
  renderViewer();
}

function isOnVideoControls(event) {
  const video = event.target.closest('video');
  return Boolean(video) && event.clientY > video.getBoundingClientRect().bottom - VIDEO_CONTROLS_PX;
}

function bindViewerGestures() {
  const stage = byId('viewer-stage');
  let startX = 0;
  let startY = 0;
  let tracking = false;
  stage.addEventListener('pointerdown', (event) => {
    if (isOnVideoControls(event)) return;
    tracking = true;
    startX = event.clientX;
    startY = event.clientY;
  });
  stage.addEventListener('pointerup', (event) => {
    if (!tracking) return;
    tracking = false;
    const deltaX = event.clientX - startX;
    const deltaY = event.clientY - startY;
    if (Math.abs(deltaX) > SWIPE_MIN_PX && Math.abs(deltaX) > Math.abs(deltaY)) stepViewer(deltaX < 0 ? 1 : -1);
    else if (deltaY > SWIPE_MIN_PX * 2 && Math.abs(deltaY) > Math.abs(deltaX)) closeViewer();
  });
  stage.addEventListener('pointercancel', () => { tracking = false; });
  document.addEventListener('keydown', (event) => {
    if (byId('viewer').hidden || document.querySelector('dialog[open]')) return;
    if (event.key === 'ArrowRight') stepViewer(1);
    else if (event.key === 'ArrowLeft') stepViewer(-1);
    else if (event.key === 'Escape') closeViewer();
  });
  window.addEventListener('popstate', () => {
    if (!byId('viewer').hidden) closeViewer(true);
  });
}

const uploads = { jobs: [], running: 0, finishedSinceRefresh: 0, wakers: new Set(), collapsed: false };

const JOB_TEXT = {
  queued: () => 'Waiting',
  uploading: (job) => `${Math.floor((job.sent / job.file.size) * 100)}%`,
  finishing: () => 'Making a preview',
  done: () => 'Added',
  duplicate: () => 'Already in your library',
  restored: () => 'Was in the bin, now back',
  waiting: (job) => `Connection lost, trying again (${job.attempts})`,
  failed: (job) => job.error,
};

function waitForRetry(attempt) {
  const delay = RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)];
  return new Promise((resolve) => {
    const wake = () => {
      clearTimeout(timer);
      uploads.wakers.delete(wake);
      resolve();
    };
    const timer = setTimeout(wake, delay);
    uploads.wakers.add(wake);
  });
}

function wakeWaitingUploads() {
  [...uploads.wakers].forEach((wake) => wake());
}

function sendChunk(job, uploadId, offset, blob) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', `/api/uploads/${uploadId}`);
    request.setRequestHeader('X-CSRF-Token', state.csrf);
    request.setRequestHeader('Upload-Offset', String(offset));
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.responseType = 'json';
    request.upload.addEventListener('progress', (event) => {
      job.sent = offset + event.loaded;
      renderJob(job);
    });
    request.addEventListener('load', () => {
      const data = request.response || {};
      const redirected = request.responseURL && new URL(request.responseURL).pathname.startsWith('/signin');
      if (redirected) reject(new ApiError('You are signed out. Reload the page to sign in again.', 401));
      else if (request.status >= 200 && request.status < 300) resolve(data);
      else reject(new ApiError(data.error || `the server answered ${request.status}`, request.status, data));
    });
    request.addEventListener('error', () => reject(new ApiError('the connection dropped', 0)));
    request.addEventListener('timeout', () => reject(new ApiError('the connection timed out', 0)));
    request.send(blob);
  });
}

function isRefusal(error) {
  return error instanceof ApiError && REFUSED_STATUSES.includes(error.status);
}

function isPermanent(error) {
  return error instanceof ApiError && STOP_STATUSES.includes(error.status);
}

async function withRetries(job, step) {
  for (;;) {
    try {
      const result = await step();
      job.attempts = 0;
      return result;
    } catch (error) {
      if (isPermanent(error) || job.attempts >= RETRY_LIMIT) throw error;
      job.attempts += 1;
      job.status = 'waiting';
      renderJob(job);
      await waitForRetry(job.attempts - 1);
      job.status = 'uploading';
    }
  }
}

async function sendFile(job) {
  const file = job.file;
  const chunkBytes = state.session.chunkBytes;
  const started = await withRetries(job, () => api('POST', '/api/uploads', {
    name: file.name, size: file.size, fingerprint: `${file.name}|${file.size}|${file.lastModified}`,
    fileTime: file.lastModified ? localIso(file.lastModified) : null,
  }));
  job.uploadId = started.id;
  let offset = started.received;
  job.sent = offset;
  while (offset < file.size) {
    const chunk = file.slice(offset, Math.min(file.size, offset + chunkBytes));
    const answer = await withRetries(job, async () => {
      try {
        return await sendChunk(job, started.id, offset, chunk);
      } catch (error) {
        if (error.status === 409 && typeof error.data.received === 'number') return { received: error.data.received };
        throw error;
      }
    });
    offset = answer.received;
    job.sent = offset;
    renderJob(job);
  }
  job.status = 'finishing';
  renderJob(job);
  return withRetries(job, () => api('POST', `/api/uploads/${started.id}/finish`));
}

async function runJob(job) {
  job.status = 'uploading';
  job.attempts = 0;
  renderJob(job);
  try {
    const result = await sendFile(job);
    job.status = result.restored ? 'restored' : (result.duplicate ? 'duplicate' : 'done');
    job.sent = job.file.size;
    uploads.finishedSinceRefresh += 1;
  } catch (error) {
    job.status = 'failed';
    job.final = isRefusal(error);
    job.error = error.status === 0 ? 'The connection kept dropping. Tap Retry when you are back online.' : error.message;
    if (job.final) releaseUpload(job);
  }
  renderJob(job);
}

function releaseUpload(job) {
  const uploadId = job.uploadId;
  if (!uploadId) return;
  job.uploadId = null;
  api('DELETE', `/api/uploads/${uploadId}`).catch((error) => {
    if (error.status !== 404) console.warn(`could not let go of upload ${uploadId}: ${error.message}`);
  });
}

async function pumpUploads() {
  while (uploads.running < UPLOADS_AT_ONCE) {
    const job = uploads.jobs.find((candidate) => candidate.status === 'queued');
    if (!job) break;
    uploads.running += 1;
    runJob(job).finally(() => {
      uploads.running -= 1;
      afterJob();
      pumpUploads();
    });
  }
  renderUploadSummary();
}

function viewCanRefresh() {
  return ['timeline', 'album', 'favourites'].includes(state.view) && state.viewerIndex < 0 && !state.selecting;
}

function afterJob() {
  if (!uploads.finishedSinceRefresh) return;
  const busy = uploadsBusy();
  const nearTop = window.scrollY < NEAR_TOP_PX;
  if (busy && !(uploads.finishedSinceRefresh >= REFRESH_AFTER_UPLOADS && nearTop)) return;
  uploads.finishedSinceRefresh = 0;
  loadStorage();
  if (viewCanRefresh() && (nearTop || !busy)) reloadView();
}

function addFiles(files) {
  if (state.storage && !state.storage.uploadsAllowed) {
    showToast('Uploads are paused because the disk is nearly full.', true);
    return;
  }
  [...files].forEach((file) => {
    const job = { file, status: 'queued', sent: 0, attempts: 0, error: '', final: false, row: null, uploadId: null };
    if (file.size === 0) {
      job.status = 'failed';
      job.final = true;
      job.error = 'This file is empty.';
    } else if (state.session && file.size > state.session.maxFileBytes) {
      job.status = 'failed';
      job.final = true;
      job.error = `Larger than the ${formatBytes(state.session.maxFileBytes)} limit.`;
    }
    uploads.jobs.push(job);
    byId('upload-list').append(makeJobRow(job));
  });
  byId('upload-panel').hidden = false;
  pumpUploads();
}

function makeJobRow(job) {
  const row = element('li', 'upload-row');
  const name = element('span', 'upload-name', job.file.name);
  const status = element('span', 'upload-status');
  const bar = element('div', 'upload-bar');
  bar.append(element('span'));
  const retry = actionButton('Retry', () => {
    job.status = 'queued';
    job.error = '';
    renderJob(job);
    pumpUploads();
  });
  retry.classList.add('retry');
  row.append(name, status, retry, bar);
  job.row = row;
  renderJob(job);
  return row;
}

function renderJob(job) {
  if (!job.row) return;
  const percent = job.file.size ? Math.min(100, (job.sent / job.file.size) * 100) : 0;
  job.row.dataset.status = job.status;
  job.row.querySelector('.upload-status').textContent = JOB_TEXT[job.status](job);
  job.row.querySelector('.upload-bar span').style.width = `${percent}%`;
  job.row.querySelector('.retry').hidden = job.status !== 'failed' || job.final;
  renderUploadSummary();
}

function renderUploadSummary() {
  const jobs = uploads.jobs;
  const finished = jobs.filter((job) => ['done', 'duplicate', 'restored'].includes(job.status)).length;
  const failed = jobs.filter((job) => job.status === 'failed').length;
  const busy = jobs.length - finished - failed;
  const duplicates = jobs.filter((job) => job.status === 'duplicate').length;
  let summary = `Uploading ${finished + 1 > jobs.length ? jobs.length : finished + 1} of ${jobs.length}`;
  if (!busy) summary = `${finished} of ${jobs.length} uploaded${duplicates ? `, ${duplicates} already here` : ''}${failed ? `, ${failed} failed` : ''}`;
  byId('upload-summary').textContent = summary;
  byId('upload-close').hidden = busy > 0;
  const totalBytes = jobs.reduce((sum, job) => sum + job.file.size, 0);
  const sentBytes = jobs.reduce((sum, job) => sum + (['done', 'duplicate', 'restored'].includes(job.status) ? job.file.size : job.sent), 0);
  byId('upload-total-bar').style.width = `${totalBytes ? Math.min(100, (sentBytes / totalBytes) * 100) : 0}%`;
}

function uploadsBusy() {
  return uploads.jobs.some((job) => ['queued', 'uploading', 'finishing', 'waiting'].includes(job.status));
}

function closeUploadPanel() {
  if (uploadsBusy()) return;
  uploads.jobs.filter((job) => job.status === 'failed').forEach(releaseUpload);
  uploads.jobs = [];
  byId('upload-list').replaceChildren();
  byId('upload-panel').hidden = true;
}

function toggleUploadList() {
  uploads.collapsed = !uploads.collapsed;
  byId('upload-panel').classList.toggle('collapsed', uploads.collapsed);
  byId('upload-collapse').setAttribute('aria-expanded', String(!uploads.collapsed));
  byId('upload-collapse').replaceChildren(icon(uploads.collapsed ? 'up' : 'down'));
}

let searchTimer = 0;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = byId('search').value.trim();
    if (state.view === 'albums') setView('timeline');
    else reloadView();
  }, SEARCH_DELAY_MS);
}

function onDatesChanged() {
  state.from = byId('date-from').value;
  state.to = byId('date-to').value;
  if (state.from && state.to && state.from > state.to) {
    showToast('The from date is after the to date.', true);
    return;
  }
  if (state.view === 'albums') setView('timeline');
  else reloadView();
}

function toggleFilters() {
  const filters = byId('filters');
  filters.hidden = !filters.hidden;
  byId('filter-toggle').setAttribute('aria-expanded', String(!filters.hidden));
  if (!filters.hidden) byId('date-from').focus();
}

function clearDates() {
  byId('date-from').value = '';
  byId('date-to').value = '';
  onDatesChanged();
}

function setGroup(group) {
  state.group = group;
  document.querySelectorAll('#group-switch button').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.group === group)));
  rebuildTimeline();
}

function bindControls() {
  document.querySelectorAll('.view-btn').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  document.querySelectorAll('#group-switch button').forEach((button) => button.addEventListener('click', () => setGroup(button.dataset.group)));
  byId('search').addEventListener('input', onSearchInput);
  byId('filter-toggle').addEventListener('click', toggleFilters);
  byId('date-from').addEventListener('change', onDatesChanged);
  byId('date-to').addEventListener('change', onDatesChanged);
  byId('filters-clear').addEventListener('click', clearDates);
  byId('upload-input').addEventListener('change', (event) => {
    addFiles(event.target.files);
    event.target.value = '';
  });
  byId('select-toggle').addEventListener('click', () => (state.selecting ? stopSelecting() : startSelecting()));
  byId('selection-cancel').addEventListener('click', stopSelecting);
  byId('album-new').addEventListener('click', createAlbum);
  byId('album-rename').addEventListener('click', renameAlbum);
  byId('album-delete').addEventListener('click', deleteAlbum);
  byId('album-back').addEventListener('click', () => setView('albums'));
  byId('bin-empty').addEventListener('click', emptyBin);
  byId('viewer-close').addEventListener('click', () => closeViewer());
  byId('viewer-prev').addEventListener('click', () => stepViewer(-1));
  byId('viewer-next').addEventListener('click', () => stepViewer(1));
  byId('album-picker-form').addEventListener('submit', createAndAdd);
  byId('album-picker-cancel').addEventListener('click', () => byId('album-picker').close());
  byId('name-dialog-form').addEventListener('submit', (event) => {
    event.preventDefault();
    byId('name-dialog').close(byId('name-dialog-input').value.trim() ? 'save' : '');
  });
  byId('name-dialog-cancel').addEventListener('click', () => byId('name-dialog').close(''));
  byId('upload-close').addEventListener('click', closeUploadPanel);
  byId('upload-collapse').addEventListener('click', toggleUploadList);
  window.addEventListener('online', wakeWaitingUploads);
  window.addEventListener('beforeunload', (event) => {
    if (!uploadsBusy()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadMore();
  }, { rootMargin: '1200px 0px' }).observe(byId('sentinel'));
  bindViewerGestures();
}

async function emptyBin() {
  if (!window.confirm('Delete everything in the bin forever? This cannot be undone.')) return;
  try {
    const result = await api('POST', '/api/bin/empty');
    showToast(`${result.removed} deleted forever`);
    reloadView();
    loadStorage();
  } catch (error) {
    reportError(error);
  }
}

function fillIcons() {
  fillIcon('filter-toggle', 'calendar');
  fillIcon('viewer-close', 'close');
  fillIcon('viewer-prev', 'prev');
  fillIcon('viewer-next', 'next');
  fillIcon('upload-close', 'close');
  fillIcon('upload-collapse', 'down');
}

async function start() {
  fillIcons();
  bindControls();
  try {
    state.session = await api('GET', '/api/session');
    state.csrf = state.session.csrfToken;
    byId('bin-text').textContent = `Items in the bin are deleted for good after ${state.session.binDays} days.`;
  } catch (error) {
    reportError(error);
    return;
  }
  setView('timeline');
  loadStorage();
  setInterval(loadStorage, STORAGE_REFRESH_MS);
}

start();
