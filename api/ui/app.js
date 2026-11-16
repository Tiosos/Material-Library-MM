// Query UI, step 4: search, facets, structured filters, saved views, read-only
// result table and paging. No build step (U11); this file is served as-is.
// Step 5 adds the grid editors on top of the same query object.

const TOKEN_KEY = 'ml.token';
const ACTOR_KEY = 'ml.actor';
const VIEWS_KEY = 'ml.views';

const SORT_FIELDS = new Set(['name', 'unit_cost', 'updated_at', 'source_id']);
const FILTER_FIELDS = new Set([
  'status', 'unit_cost', 'category', 'brand', 'supplier',
  'name', 'product_type', 'unit', 'product_url', 'synonyms',
]);
const NUMERIC_FIELDS = new Set(['unit_cost']);
const FACETS = ['category', 'brand', 'supplier'];

export const state = {
  query: {
    q: '',
    facets: { category: '', brand: '', supplier: '' },
    filters: [],
    sort: { field: '', order: 'asc' },
    page: { limit: 50, cursor: null },
  },
  cursors: [],       // one opaque cursor per page visited, so Previous works
  page: 0,
  lastPayload: null,
  warnings: [],
};

// ---------------------------------------------------------------------------
// Pure query compiler (docs/ui-bulk-edit.md 6.1). Unknown fields are dropped
// and reported rather than silently widening the result set.
// ---------------------------------------------------------------------------
export function compileQuery(query) {
  const params = new URLSearchParams();
  const warnings = [];
  if (query.q) params.set('q', query.q);

  for (const facet of FACETS) {
    const value = query.facets && query.facets[facet];
    if (value) params.set(facet, value);
  }

  for (const filter of query.filters || []) {
    const field = filter.field;
    if (!FILTER_FIELDS.has(field) && !(field || '').startsWith('attributes.')) {
      warnings.push(`unknown filter field: ${field}`);
      continue;
    }
    if (field === 'status') {
      if (!['active', 'archived', 'superseded', 'all'].includes(filter.value)) {
        warnings.push(`unknown status: ${filter.value}`);
        continue;
      }
      params.set('status', filter.value);
    } else if (NUMERIC_FIELDS.has(field)) {
      const value = Number(filter.value);
      if (!Number.isFinite(value)) {
        warnings.push(`invalid number for ${field}: ${filter.value}`);
        continue;
      }
      const key = filter.op === 'lte' ? 'unit_cost_max' : 'unit_cost_min';
      params.set(key, String(value));
    } else if ((field || '').startsWith('attributes.')) {
      // Equality on one attribute key merges into the shared attrs object.
      const existing = params.get('attrs');
      let attrs = {};
      if (existing) { try { attrs = JSON.parse(existing); } catch { attrs = {}; } }
      attrs[field.slice('attributes.'.length)] = filter.value;
      params.set('attrs', JSON.stringify(attrs));
    } else if (FACETS.includes(field)) {
      params.set(field, String(filter.value));
    } else {
      warnings.push(`no server-side filter for ${field}`);
    }
  }

  if (query.sort && query.sort.field) {
    if (SORT_FIELDS.has(query.sort.field)) {
      params.set('sort', query.sort.field);
      params.set('order', query.sort.order === 'desc' ? 'desc' : 'asc');
    } else {
      warnings.push(`unknown sort field: ${query.sort.field}`);
    }
  }
  params.set('limit', String((query.page && query.page.limit) || 50));
  if (query.page && query.page.cursor) params.set('cursor', query.page.cursor);
  return { params, warnings };
}

function readStored(key) {
  try { return localStorage.getItem(key) || ''; } catch { return ''; }
}
function writeStored(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}
function readViews() {
  try {
    const parsed = JSON.parse(localStorage.getItem(VIEWS_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}
function writeViews(views) {
  writeStored(VIEWS_KEY, JSON.stringify(views));
}

function token() { return document.getElementById('token').value.trim(); }
function actor() { return document.getElementById('actor').value.trim(); }

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const a = actor();
  if (a) headers['X-Actor'] = a;
  const response = await fetch(path, { ...options, headers });
  let body;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const error = body && body.error;
    throw new Error(error ? `${error.code}: ${error.message}` : `HTTP ${response.status}`);
  }
  return body;
}

function setStatus(text) { document.getElementById('footer-status').textContent = text; }
function showWarnings(warnings) {
  const box = document.getElementById('warning');
  box.hidden = warnings.length === 0;
  box.textContent = warnings.join(' · ');
}

// ---------------------------------------------------------------------------
// Facets and views
// ---------------------------------------------------------------------------
async function loadFacets() {
  const { params } = compileQuery({ ...state.query, page: { limit: 1, cursor: null } });
  params.delete('cursor');
  params.delete('limit');
  try {
    const body = await api(`/v1/facets?${params.toString()}`);
    const grouped = { category: [], brand: [], supplier: [] };
    for (const row of body.facets || []) {
      if (grouped[row.facet]) grouped[row.facet].push(row);
    }
    for (const name of FACETS) renderFacet(name, grouped[name] || []);
  } catch (exc) {
    setStatus(`facets: ${exc.message}`);
  }
}

function renderFacet(name, rows) {
  const host = document.getElementById(`facet-${name}`);
  host.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'facet-empty';
    empty.textContent = 'none';
    host.appendChild(empty);
    return;
  }
  for (const row of rows) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'facet';
    if (state.query.facets[name] === row.value) button.classList.add('selected');
    const label = document.createElement('span');
    label.textContent = row.value || '(none)';
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(row.count);
    button.append(label, count);
    button.addEventListener('click', () => {
      state.query.facets[name] = state.query.facets[name] === row.value ? '' : row.value;
      state.query.page.cursor = null;
      state.page = 0;
      state.cursors = [];
      runSearch();
    });
    host.appendChild(button);
  }
}

function renderViews() {
  const host = document.getElementById('view-list');
  host.replaceChildren();
  const views = readViews();
  if (!views.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = 'No saved views yet.';
    host.appendChild(empty);
    return;
  }
  views.forEach((view, index) => {
    const row = document.createElement('div');
    row.className = 'view';
    const apply = document.createElement('button');
    apply.type = 'button';
    apply.className = 'link';
    apply.textContent = view.name;
    apply.addEventListener('click', () => {
      state.query = JSON.parse(JSON.stringify(view.query));
      state.query.page.cursor = null;
      state.page = 0;
      state.cursors = [];
      syncForm();
      runSearch();
    });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'delete';
    del.textContent = 'delete';
    del.addEventListener('click', () => {
      const next = readViews();
      next.splice(index, 1);
      writeViews(next);
      renderViews();
    });
    row.append(apply, del);
    host.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Filter rows
// ---------------------------------------------------------------------------
function addFilterRow(filter = { field: 'unit_cost', op: 'gte', value: '' }) {
  const row = document.createElement('div');
  row.className = 'filter-row';
  const field = document.createElement('select');
  for (const name of ['status', 'unit_cost', 'category', 'brand', 'supplier', 'name', 'attributes.colour']) {
    const option = document.createElement('option');
    option.value = name; option.textContent = name;
    field.appendChild(option);
  }
  field.value = filter.field;
  const op = document.createElement('select');
  for (const name of ['gte', 'lte', 'eq']) {
    const option = document.createElement('option');
    option.value = name; option.textContent = name;
    op.appendChild(option);
  }
  op.value = filter.op || 'eq';
  const value = document.createElement('input');
  value.type = 'text';
  value.value = filter.value == null ? '' : String(filter.value);
  value.placeholder = 'value';
  const remove = document.createElement('button');
  remove.type = 'button'; remove.className = 'remove'; remove.textContent = 'x';
  const sync = () => {
    const index = [...document.querySelectorAll('.filter-row')].indexOf(row);
    if (index >= 0) {
      state.query.filters[index] = { field: field.value, op: op.value, value: value.value };
    }
  };
  field.addEventListener('change', sync);
  op.addEventListener('change', sync);
  value.addEventListener('input', sync);
  remove.addEventListener('click', () => {
    const index = [...document.querySelectorAll('.filter-row')].indexOf(row);
    if (index >= 0) state.query.filters.splice(index, 1);
    row.remove();
  });
  row.append(field, op, value, remove);
  document.getElementById('filter-rows').appendChild(row);
}

// ---------------------------------------------------------------------------
// Results and paging
// ---------------------------------------------------------------------------
function renderRows(payload) {
  const tbody = document.getElementById('rows');
  tbody.replaceChildren();
  const rows = payload.results || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8; td.className = 'empty'; td.textContent = 'No results';
    tr.appendChild(td); tbody.appendChild(tr);
    return;
  }
  const columns = ['name', 'category', 'brand', 'supplier', 'unit',
                   'unit_cost', 'updated_at', 'variant_count'];
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const column of columns) {
      const td = document.createElement('td');
      if (column === 'name') {
        td.className = 'name';
        td.textContent = row.name || '';
        const uid = document.createElement('div');
        uid.className = 'uid';
        uid.textContent = row.material_uid;
        td.appendChild(uid);
      } else if (column === 'unit_cost') {
        td.className = 'num';
        td.textContent = row.unit_cost == null ? '-' : String(row.unit_cost);
      } else if (column === 'variant_count') {
        td.className = 'num';
        td.textContent = String(row.variant_count || 0);
      } else {
        td.textContent = row[column] == null ? '' : String(row[column]);
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function renderPage(payload) {
  state.lastPayload = payload;
  renderRows(payload);
  document.getElementById('total').textContent = `${payload.total} materials`;
  document.getElementById('level').textContent = payload.level || '';
  document.getElementById('status-pill').textContent = payload.status || '';
  const start = Number(payload.offset || 0) + 1;
  const end = Number(payload.offset || 0) + (payload.results || []).length;
  document.getElementById('page-label').textContent = `${start}-${end} of ${payload.total}`;
  document.getElementById('prev').disabled = state.page === 0;
  document.getElementById('next').disabled = !payload.next_cursor;
  document.getElementById('footer-status').textContent = `ready in ${payload.took_ms || 0} ms`;
  // The edit module listens and repaints staged cells; app.js does not import it.
  document.dispatchEvent(new CustomEvent('ml:rows-rendered', { detail: payload }));
}

async function runSearch() {
  const compiled = compileQuery(state.query);
  showWarnings(compiled.warnings);
  setStatus('searching...');
  try {
    const body = await api(`/v1/materials?${compiled.params.toString()}`);
    renderPage(body);
    await loadFacets();
  } catch (exc) {
    setStatus(`search failed: ${exc.message}`);
  }
}

function syncForm() {
  document.getElementById('q').value = state.query.q || '';
  document.getElementById('sort').value = state.query.sort.field || '';
  document.getElementById('order').value = state.query.sort.order || 'asc';
  document.getElementById('filter-rows').replaceChildren();
  for (const filter of state.query.filters) addFilterRow(filter);
  document.querySelectorAll('.facet').forEach((button) => {
    const name = button.parentElement.id.replace('facet-', '');
    button.classList.toggle('selected', state.query.facets[name] === button.firstChild.textContent);
  });
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function wire() {
  const tokenInput = document.getElementById('token');
  const actorInput = document.getElementById('actor');
  tokenInput.value = readStored(TOKEN_KEY);
  actorInput.value = readStored(ACTOR_KEY);
  tokenInput.addEventListener('input', () => writeStored(TOKEN_KEY, tokenInput.value.trim()));
  actorInput.addEventListener('input', () => {
    writeStored(ACTOR_KEY, actorInput.value.trim());
    document.getElementById('actor-note').textContent =
      actorInput.value.trim() ? 'sent as X-Actor' : 'server default until typed';
  });

  document.getElementById('search').addEventListener('click', () => {
    state.query.q = document.getElementById('q').value.trim();
    state.query.sort.field = document.getElementById('sort').value;
    state.query.sort.order = document.getElementById('order').value;
    state.query.page.cursor = null;
    state.page = 0;
    state.cursors = [];
    runSearch();
  });
  document.getElementById('q').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') document.getElementById('search').click();
  });

  document.getElementById('filters-toggle').addEventListener('click', () => {
    const panel = document.getElementById('filters-panel');
    panel.hidden = !panel.hidden;
  });
  document.getElementById('views-toggle').addEventListener('click', () => {
    const panel = document.getElementById('views-panel');
    panel.hidden = !panel.hidden;
    renderViews();
  });
  document.getElementById('add-filter').addEventListener('click', () => addFilterRow());
  document.getElementById('save-view').addEventListener('click', () => {
    const name = window.prompt('View name');
    if (!name) return;
    const views = readViews();
    views.push({ name, query: JSON.parse(JSON.stringify(state.query)) });
    writeViews(views);
    renderViews();
  });

  document.getElementById('prev').addEventListener('click', () => {
    if (state.page === 0) return;
    state.page -= 1;
    state.query.page.cursor = state.cursors[state.page] || null;
    runSearch();
  });
  document.getElementById('next').addEventListener('click', () => {
    const cursor = state.lastPayload && state.lastPayload.next_cursor;
    if (!cursor) return;
    state.cursors[state.page] = cursor;
    state.page += 1;
    state.query.page.cursor = cursor;
    runSearch();
  });

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      if (tab.dataset.tab === 'review') {
        setStatus('Review tab lands in step 6');
      }
    });
  });

  // Auth is optional on the server: a 401 from /v1/meta means a token is needed.
  fetch('/v1/meta').then((response) => {
    document.getElementById('auth-note').textContent =
      response.ok ? 'auth off' : 'token required';
  }).catch(() => {
    document.getElementById('auth-note').textContent = 'auth unknown';
  });
}

wire();
runSearch();
