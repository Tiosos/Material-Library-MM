// Edit grid, staging and review sheet (docs/ui-bulk-edit.md section 7, step 5).
// Every edit becomes a material_change_request; this module never issues a
// direct write to material. Pure helpers are exported so their invariants can
// be pinned by tests without a JS harness (U11).

import { state, api } from './app.js';

const STAGE_KEY = 'ml.staged';
const MAX_CHARS = 1000;

// Fields editable in the grid. attributes.<key> is decided by the schema's
// attribute_keys allowlist and its editor hint, not by a hard-coded list here.
const GRID_FIELDS = ['name', 'product_type', 'unit', 'unit_cost', 'product_url', 'synonyms'];

export const staged = new Map();   // material_uid -> Map(path -> {before, after})

let schema = null;                 // the /v1/writeback/schema payload
let editorContext = null;          // {uid, path, editor, before, apply}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------
export function normaliseValue(path, raw, editor) {
  // Clearing is a first-class edit: a blank text field stages null, never "".
  if (editor === 'tokens') {
    const tokens = String(raw || '').split(',').map((x) => x.trim()).filter(Boolean);
    return tokens.length ? tokens : null;
  }
  if (editor === 'number' || editor === 'integer') {
    if (raw === '' || raw == null) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : undefined;
  }
  if (path === 'synonyms') {
    const tokens = String(raw || '').split(',').map((x) => x.trim()).filter(Boolean);
    return tokens.length ? tokens : null;
  }
  const text = String(raw ?? '');
  return text.trim() === '' ? null : text;   // padding kept as sent (U19)
}

export function validateStaged(path, value, editor) {
  if (value === null) return null;
  if (editor === 'number' || editor === 'integer') {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'must be a number';
    if (value < 0) return 'must not be negative';
    if (editor === 'integer' && !Number.isInteger(value)) return 'must be a whole number';
    if (path === 'unit_cost' && value >= 10000000000) return 'unit_cost is too large';
    return null;
  }
  if (Array.isArray(value)) {
    if (value.length && value.every((v) => typeof v === 'string')) return null;
    return 'each token must be a string';
  }
  if (typeof value !== 'string') return 'must be text';
  if (Array.from(value).length > MAX_CHARS) return `at most ${MAX_CHARS} characters`;
  if (path === 'product_url' && value && !/^https?:\/\//.test(value)) {
    return 'must start with http:// or https://';
  }
  return null;
}

export function stagedMaterials() {
  return [...staged.entries()].filter(([, diffs]) => diffs.size > 0);
}

export function stagingSummary() {
  const groups = stagedMaterials();
  const changes = groups.reduce((n, [, diffs]) => n + diffs.size, 0);
  return `${changes} change${changes === 1 ? '' : 's'} across ${groups.length} material${groups.length === 1 ? '' : 's'}`;
}

export function groupStaged(reason) {
  // One request per material, all of its staged paths in one diffs array (U2).
  return stagedMaterials().map(([material_uid, diffs]) => ({
    material_uid,
    diffs: [...diffs.entries()].map(([path, entry]) => ({
      path, before: entry.before, after: entry.after,
    })),
    reason: reason || undefined,
  }));
}

export function hasLineBreak(value) {
  return typeof value === 'string' && /[\r\n]/.test(value);
}

// ---------------------------------------------------------------------------
// Persistence: sessionStorage mirror, not localStorage (survives refresh only)
// ---------------------------------------------------------------------------
export function saveStaged() {
  const plain = [...staged.entries()].map(([uid, diffs]) => [uid, [...diffs.entries()]]);
  try { sessionStorage.setItem(STAGE_KEY, JSON.stringify(plain)); } catch { /* private mode */ }
}

export function restoreStaged() {
  let plain;
  try { plain = JSON.parse(sessionStorage.getItem(STAGE_KEY) || '[]'); } catch { return 0; }
  if (!Array.isArray(plain)) return 0;
  let restored = 0;
  for (const [uid, entries] of plain) {
    if (!Array.isArray(entries)) continue;
    const map = new Map();
    for (const [path, entry] of entries) {
      if (entry && 'before' in entry && 'after' in entry) map.set(path, entry);
    }
    if (map.size) { staged.set(uid, map); restored += map.size; }
  }
  return restored;
}

export function clearStaged() {
  staged.clear();
  saveStaged();
  updateStagingBar();
}

// ---------------------------------------------------------------------------
// Schema-driven editors
// ---------------------------------------------------------------------------
export async function loadSchema() {
  const payload = await api('/v1/writeback/schema');
  schema = payload;
  const head = document.getElementById('result-head');
  const extras = ['product_type', 'synonyms'];
  for (const cat of schema.catalogs) {
    for (const attribute of cat.attributes) {
      const path = `attributes.${attribute.key}`;
      if (!extras.includes(path)) extras.push(path);
    }
  }
  for (const path of extras) {
    const th = document.createElement('th');
    th.textContent = path.startsWith('attributes.') ? path.slice('attributes.'.length) : path;
    th.dataset.field = path;
    head.appendChild(th);
  }
  document.dispatchEvent(new CustomEvent('ml:rows-rendered',
                                         { detail: state.lastPayload || { results: [] } }));
  return payload;
}

function editorFor(path, categoryCode) {
  if (path.startsWith('attributes.')) {
    const cat = schema && schema.catalogs.find((c) => c.category_code === categoryCode);
    if (!cat) return null;
    const key = path.slice('attributes.'.length);
    const attribute = cat.attributes.find((a) => a.key === key);
    return attribute ? attribute.editor : null;
  }
  const field = schema && schema.fields.find((f) => f.path === path);
  return field ? field.editor : null;
}

function allEditablePaths() {
  // Column headers are the union of what any category can edit; per row the
  // cells that this row's category does not allow are simply left unpainted.
  const paths = [...GRID_FIELDS];
  for (const cat of (schema && schema.catalogs) || []) {
    for (const a of cat.attributes) {
      const path = `attributes.${a.key}`;
      if (!paths.includes(path)) paths.push(path);
    }
  }
  return paths;
}

// ---------------------------------------------------------------------------
// Grid painting
// ---------------------------------------------------------------------------
function valueForColumn(row, path) {
  if (path === 'synonyms') return (row.synonyms || []).join(', ');
  if (path.startsWith('attributes.')) {
    const key = path.slice('attributes.'.length);
    const value = row.attributes && row.attributes[key];
    return value == null ? '' : String(value);
  }
  const value = row[path];
  return value == null ? '' : String(value);
}

function paintGrid(payload) {
  if (!schema) return;
  const paths = allEditablePaths();
  const head = document.getElementById('result-head');
  head.querySelectorAll('th[data-field]').forEach((th) => {
    th.hidden = !paths.includes(th.dataset.field);
  });
  for (const row of (payload && payload.results) || []) {
    const tr = document.querySelector(`tr[data-uid="${row.material_uid}"]`);
    if (!tr) continue;
    const cellMap = staged.get(row.material_uid);
    for (const path of paths) {
      // A path not allowed for this row's category has no cell at all.
      if (!editorFor(path, row.category)) continue;
      let td = tr.querySelector(`td[data-path="${CSS.escape(path)}"]`);
      if (!td) {
        td = document.createElement('td');
        td.dataset.path = path;
        td.className = 'editable';
        tr.appendChild(td);
      }
      const entry = cellMap && cellMap.get(path);
      td.classList.toggle('dirty', Boolean(entry));
      td.replaceChildren();
      const shown = entry ? entry.after : valueForColumn(row, path);
      td.textContent = shown == null ? '' : String(shown);
      if (typeof shown === 'string' && /[\r\n]/.test(shown)) {
        const marker = document.createElement('span');
        marker.className = 'linecount';
        marker.textContent = `¶ ${shown.split(/\r?\n/).length} lines`;
        td.appendChild(marker);
      }
      td.title = entry ? 'staged - click to change' : 'click to edit';
      td.onclick = () => openEditor(row, path);
    }
  }
}

// Give each result row a data-uid so the edit module can find it. app.js set
// no such attribute; the uid is already rendered in a child div.
function tagRows() {
  for (const tr of document.querySelectorAll('#rows tr')) {
    const uid = tr.querySelector('.uid');
    if (uid && !tr.dataset.uid) tr.dataset.uid = uid.textContent.trim();
  }
}

// ---------------------------------------------------------------------------
// Editor popover
// ---------------------------------------------------------------------------
async function openEditor(row, path) {
  const editor = editorFor(path, row.category);
  if (!editor) return;
  let before = valueForColumn(row, path);
  // Synonyms and attributes are not in the list payload: fetch the detail only
  // when the person actually opens that cell.
  if (path === 'synonyms' || path.startsWith('attributes.')) {
    try {
      const detail = await api(`/v1/materials/${encodeURIComponent(row.material_uid)}`);
      before = path === 'synonyms'
        ? (detail.synonyms || []).join(', ')
        : String((detail.attributes || {})[path.slice('attributes.'.length)] ?? '');
    } catch (exc) {
      document.getElementById('footer-status').textContent = `detail failed: ${exc.message}`;
      return;
    }
  }
  const entry = staged.get(row.material_uid) && staged.get(row.material_uid).get(path);
  const current = entry ? String(entry.after ?? '') : String(before ?? '');
  editorContext = { uid: row.material_uid, path, editor, before };

  document.getElementById('editor-title').textContent = `Edit ${path}`;
  const body = document.getElementById('editor-body');
  body.replaceChildren();
  const label = document.createElement('label');
  label.className = 'field-label';
  label.textContent = editor === 'textarea' ? 'Line breaks are kept verbatim' : path;

  let input;
  if (editor === 'select') {
    input = document.createElement('select');
    for (const unit of (schema && schema.units) || []) {
      const option = document.createElement('option');
      option.value = unit.code;
      option.textContent = `${unit.code} - ${unit.name}`;
      input.appendChild(option);
    }
  } else if (editor === 'textarea') {
    input = document.createElement('textarea');
    input.rows = 6;
  } else {
    input = document.createElement('input');
    input.type = editor === 'number' || editor === 'integer' ? 'number' : 'text';
    if (editor === 'number') input.step = '0.0001';
  }
  input.value = current;
  label.appendChild(input);
  body.appendChild(label);

  const counter = document.getElementById('editor-counter');
  const updateCounter = () => {
    const length = Array.from(input.value).length;
    counter.textContent = `${length} / ${MAX_CHARS}`;
    const over = length > MAX_CHARS;
    counter.classList.toggle('over', over);
    document.getElementById('editor-apply').disabled = over;
  };
  input.addEventListener('input', updateCounter);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeEditor();
    if (event.key === 'Enter' && editor !== 'textarea') {
      event.preventDefault();
      applyEditor(input.value);
    }
    if (event.key === 'Enter' && editor === 'textarea' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      applyEditor(input.value);
    }
  });
  document.getElementById('editor-apply').onclick = () => applyEditor(input.value);
  updateCounter();
  document.getElementById('editor-popover').hidden = false;
  input.focus();
}

function closeEditor() {
  document.getElementById('editor-popover').hidden = true;
  editorContext = null;
}

function applyEditor(raw) {
  if (!editorContext) return;
  const { uid, path, editor, before } = editorContext;
  const after = normaliseValue(path, raw, editor);
  const error = validateStaged(path, after, editor);
  if (error) {
    document.getElementById('footer-status').textContent = `${path}: ${error}`;
    return;
  }
  if (!staged.has(uid)) staged.set(uid, new Map());
  const map = staged.get(uid);
  if (after === null && String(before ?? '') === '') map.delete(path);
  else map.set(path, { before: before === '' ? null : before, after });
  saveStaged();
  closeEditor();
  updateStagingBar();
  document.dispatchEvent(new CustomEvent('ml:rows-rendered',
                                         { detail: state.lastPayload || { results: [] } }));
}

function updateStagingBar() {
  const bar = document.getElementById('staging-bar');
  bar.hidden = stagedMaterials().length === 0;
  document.getElementById('staging-summary').textContent = stagingSummary();
}

// ---------------------------------------------------------------------------
// Review sheet
// ---------------------------------------------------------------------------
function renderReview() {
  const body = document.getElementById('review-body');
  body.replaceChildren();
  const groups = stagedMaterials();
  if (!groups.length) return;
  for (const [uid, diffs] of groups) {
    const group = document.createElement('div');
    group.className = 'review-group';
    const heading = document.createElement('h3');
    heading.textContent = diffs.size > 1
      ? `${uid} - ${diffs.size} fields`
      : uid;
    group.appendChild(heading);
    for (const [path, entry] of diffs) {
      const line = document.createElement('div');
      line.className = 'diff-line';
      const name = document.createElement('span');
      name.textContent = path;
      const before = document.createElement('span');
      before.className = 'before';
      before.textContent = entry.before == null ? '(empty)' : String(entry.before);
      const arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = '->';
      const after = document.createElement('span');
      after.className = 'after';
      after.textContent = entry.after == null ? '(clear)' : String(entry.after);
      line.append(name, before, arrow, after);
      if (hasLineBreak(entry.after)) {
        const flag = document.createElement('span');
        flag.className = 'nl-flag';
        flag.textContent = 'contains a line break; upstream acceptance is unknown';
        line.appendChild(flag);
      }
      group.appendChild(line);
    }
    body.appendChild(group);
  }
  const needsReason = groups.length > 5;
  document.getElementById('reason-hint').textContent = needsReason
    ? `${groups.length} materials: a reason is required`
    : 'optional for 5 materials or fewer';
  syncConfirm();
}

function syncConfirm() {
  const needsReason = stagedMaterials().length > 5;
  const reason = document.getElementById('batch-reason').value.trim();
  document.getElementById('confirm-batch').disabled = needsReason && !reason;
}

async function confirmBatch() {
  const reason = document.getElementById('batch-reason').value.trim();
  const materials = groupStaged(reason);
  const button = document.getElementById('confirm-batch');
  button.disabled = true;
  const body = document.getElementById('review-body');
  body.replaceChildren();
  const note = document.createElement('p');
  note.className = 'hint';
  note.textContent = 'Submitting...';
  body.appendChild(note);
  try {
    const report = await api('/v1/change-requests/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_system: 'joineryflow', reason, materials }),
    });
    body.replaceChildren();
    const summary = document.createElement('p');
    summary.textContent = `${report.submitted} submitted, ${report.failed} failed`;
    body.appendChild(summary);
    for (const result of report.results) {
      const line = document.createElement('div');
      line.className = `review-result${result.ok ? '' : ' failed'}`;
      line.textContent = result.ok
        ? `${result.material_uid}: ${result.status} (request ${result.id})`
        : `${result.material_uid}: ${result.code} - kept in staging`;
      body.appendChild(line);
      if (!result.ok) {
        // keep the failed row staged so the person can fix or drop it
        if (!staged.has(result.material_uid)) staged.set(result.material_uid, new Map());
      } else {
        staged.delete(result.material_uid);
      }
    }
    saveStaged();
    updateStagingBar();
    document.dispatchEvent(new CustomEvent('ml:rows-rendered',
                                           { detail: state.lastPayload || { results: [] } }));
  } catch (exc) {
    body.replaceChildren();
    const error = document.createElement('p');
    error.className = 'warning';
    error.textContent = `submit failed: ${exc.message}`;
    body.appendChild(error);
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function wireEdit() {
  document.addEventListener('ml:rows-rendered', (event) => {
    tagRows();
    paintGrid(event.detail);
  });
  document.getElementById('editor-cancel').addEventListener('click', closeEditor);
  document.getElementById('discard-all').addEventListener('click', () => {
    if (window.confirm('Discard all staged changes?')) clearStaged();
  });
  document.getElementById('reload').addEventListener('click', () => {
    document.getElementById('search').click();
  });
  document.getElementById('review').addEventListener('click', () => {
    renderReview();
    document.getElementById('review-sheet').showModal();
  });
  document.getElementById('review-close').addEventListener('click', () => {
    document.getElementById('review-sheet').close();
  });
  document.getElementById('batch-reason').addEventListener('input', syncConfirm);
  document.getElementById('confirm-batch').addEventListener('click', confirmBatch);

  const restored = restoreStaged();
  if (restored) {
    const banner = document.getElementById('restore-banner');
    banner.hidden = false;
    banner.textContent = `${restored} staged change${restored === 1 ? '' : 's'} restored from this tab.`;
  }
  updateStagingBar();
  loadSchema().catch((exc) => {
    document.getElementById('footer-status').textContent = `schema failed: ${exc.message}`;
  });
}

wireEdit();
