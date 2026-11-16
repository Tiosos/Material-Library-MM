// Review tab (docs/ui-bulk-edit.md section 8, step 6): queue, detail badges,
// single and bulk decisions. Only the existing HTTP routes are used; no direct
// write and no new SQL. Pure helpers are exported for source-level assertions.

import { state, api } from './app.js';

const BULK_LIMIT = 25;
const selected = new Set();
let status = 'actionable';
let queue = null;             // last queue payload
let current = null;           // detail row currently open
let detail = null;            // /v1/change-requests/{id} payload
let decisionMode = null;      // {ids, decision}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------
export function computeBadges(row, live, actor) {
  const badges = [];
  const base = detailBase();
  // base.values holds only the diffed paths; compare those keys, not the whole
  // document (which has many fields the request never touched).
  if (base && live && Object.keys(base).some((key) => !deepEqual(base[key], live[key]))) {
    badges.push('stale');
  }
  if (row.status === 'archived' || row.material_status === 'archived'
      || row.status === 'superseded' || row.material_status === 'superseded') {
    badges.push('inactive');
  }
  if (actor && actor === row.requested_by) badges.push('own');
  return badges;
}

// The base is set by openDetail(); kept module-local so computeBadges stays
// callable from tests with (row, live, actor).
let detailBaseValues = null;
export function detailBase() { return detailBaseValues; }
export function setDetailBase(values) { detailBaseValues = values; }

export function ageText(iso, now = Date.now()) {
  if (!iso) return '-';
  const seconds = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export function deepEqual(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}

export function selectionState(ids, actor, rows) {
  // Rows filed by the current actor are unchecked by default and marked own (8.5).
  const own = ids.filter((id) => {
    const row = rows.find((r) => r.id === id);
    return row && actor && row.requested_by === actor;
  });
  const selectable = ids.filter((id) => !own.includes(id));
  const tooMany = selectable.length > BULK_LIMIT;
  return {
    own, selectable,
    initial: tooMany ? [] : selectable,
    hint: tooMany
      ? `${selectable.length} selected, ${BULK_LIMIT} per action`
      : own.length ? `${own.length} own request${own.length === 1 ? '' : 's'} unchecked` : '',
  };
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------
async function loadQueue() {
  const params = new URLSearchParams({ status, limit: '50' });
  if (queue && queue.next_cursor) params.set('cursor', queue.next_cursor);
  try {
    queue = await api(`/v1/change-requests?${params.toString()}`);
    renderQueue();
  } catch (exc) {
    document.getElementById('footer-status').textContent = `queue failed: ${exc.message}`;
  }
}

function renderQueue() {
  const body = document.getElementById('queue-rows');
  body.replaceChildren();
  const rows = queue.rows || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8; td.className = 'empty'; td.textContent = 'No requests in this view';
    tr.appendChild(td); body.appendChild(tr);
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    tr.dataset.id = String(row.id);

    const checkTd = document.createElement('td');
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = selected.has(row.id);
    const actor = document.getElementById('actor').value.trim();
    if (actor && actor === row.requested_by) {
      check.disabled = true;
      check.title = 'a submitter cannot approve their own request';
    }
    check.onclick = (event) => event.stopPropagation();
    check.onchange = () => {
      if (check.checked) selected.add(row.id); else selected.delete(row.id);
      updateSelection();
    };
    checkTd.appendChild(check);
    tr.appendChild(checkTd);

    for (const value of [row.id, row.material_name, (row.paths || []).join(', ')]) {
      const td = document.createElement('td');
      td.textContent = String(value ?? '');
      tr.appendChild(td);
    }
    const statusTd = document.createElement('td');
    statusTd.className = `status-${row.status}`;
    statusTd.textContent = row.status;
    tr.appendChild(statusTd);
    for (const value of [row.requested_by, ageText(row.requested_at),
                         row.decision_reason || row.decision_at || '-']) {
      const td = document.createElement('td');
      td.textContent = String(value ?? '');
      tr.appendChild(td);
    }
    tr.onclick = () => openDetail(row);
    body.appendChild(tr);
  }
  document.getElementById('queue-total').textContent = `${queue.total} requests`;
  document.getElementById('queue-prev').disabled = !queue.offset;
  document.getElementById('queue-next').disabled = !queue.next_cursor;
  updateSelection();
}

function updateSelection() {
  const rows = (queue && queue.rows) || [];
  const actor = document.getElementById('actor').value.trim();
  const idList = rows.map((r) => r.id);
  const state = selectionState(idList, actor, rows);
  // Drop own rows from the live selection and the checkboxes they own.
  for (const id of state.own) {
    selected.delete(id);
    const ownRow = rows.find((r) => r.id === id);
    if (ownRow) {
      const box = document.querySelector(`tr[data-id="${id}"] input[type=checkbox]`);
      if (box) { box.checked = false; box.disabled = true; }
    }
  }
  const approve = document.getElementById('approve-selected');
  const reject = document.getElementById('reject-selected');
  approve.disabled = selected.size === 0 || selected.size > BULK_LIMIT;
  reject.disabled = approve.disabled;
  const tooMany = selected.size > BULK_LIMIT;
  document.getElementById('selection-hint').textContent = tooMany
    ? `${selected.size} selected, ${BULK_LIMIT} per action`
    : state.hint;
}

// ---------------------------------------------------------------------------
// Detail drawer
// ---------------------------------------------------------------------------
async function openDetail(row) {
  current = row;
  try {
    detail = await api(`/v1/change-requests/${row.id}`);
  } catch (exc) {
    document.getElementById('footer-status').textContent = `detail failed: ${exc.message}`;
    return;
  }
  document.getElementById('detail-title').textContent =
    `Request ${row.id} - ${row.material_name}`;
  setDetailBase(detail.base && detail.base.values);

  const badgeHost = document.getElementById('detail-badges');
  badgeHost.replaceChildren();
  let live = null;
  try {
    live = await api(`/v1/materials/${encodeURIComponent(row.material_uid)}`);
  } catch { /* an unreadable detail cannot confirm stale; leave the badge off */ }
  for (const badge of computeBadges(row, live && live.attributes ? live : live, actorName())) {
    const span = document.createElement('span');
    span.className = `badge ${badge}`;
    span.textContent = badge;
    badgeHost.appendChild(span);
  }
  if (row.decision_reason || row.decided_at) {
    const span = document.createElement('span');
    span.className = 'badge decided';
    span.textContent = 'decided';
    badgeHost.appendChild(span);
  }

  const body = document.getElementById('detail-body');
  body.replaceChildren();
  body.appendChild(block('Diffs', (detail.diffs || []).map((diff) => {
    const line = document.createElement('div');
    line.className = 'detail-diff';
    const path = document.createElement('span'); path.className = 'path'; path.textContent = diff.path;
    const before = document.createElement('span'); before.className = 'before';
    before.textContent = diff.before == null ? '(empty)' : String(diff.before);
    const arrow = document.createElement('span'); arrow.textContent = '->';
    const after = document.createElement('span'); after.className = 'after';
    after.textContent = diff.after == null ? '(clear)' : String(diff.after);
    line.append(path, before, arrow, after);
    return line;
  })));
  body.appendChild(keyValues('Base', detail.base && detail.base.values));
  if (live) body.appendChild(keyValues('Live', live));
  body.appendChild(keyValues('PATCH preview', detail.plan && detail.plan.body));
  const decision = document.createElement('div');
  decision.className = 'detail-block';
  const heading = document.createElement('h3'); heading.textContent = 'Decision';
  decision.appendChild(heading);
  for (const [key, value] of [
    ['decided_by', row.decided_by], ['decided_at', row.decided_at],
    ['decision_reason', row.decision_reason],
  ]) {
    const kv = document.createElement('div'); kv.className = 'detail-kv';
    const k = document.createElement('span'); k.className = 'key'; k.textContent = key;
    const v = document.createElement('span'); v.className = 'value';
    v.textContent = value == null ? '-' : String(value);
    kv.append(k, v); decision.appendChild(kv);
  }
  body.appendChild(decision);

  const own = actorName() && actorName() === row.requested_by;
  const approve = document.getElementById('detail-approve');
  approve.disabled = Boolean(own);
  approve.title = own ? 'a submitter cannot approve their own request' : '';
  document.getElementById('detail-drawer').hidden = false;
}

function actorName() { return document.getElementById('actor').value.trim(); }

function block(title, nodes) {
  const box = document.createElement('div'); box.className = 'detail-block';
  const h = document.createElement('h3'); h.textContent = title;
  box.appendChild(h);
  for (const node of nodes) box.appendChild(node);
  return box;
}

function keyValues(title, obj) {
  const nodes = [];
  for (const [key, value] of Object.entries(obj || {})) {
    if (key === 'attributes' || key === 'measures' || key === 'synonyms') continue;
    const kv = document.createElement('div'); kv.className = 'detail-kv';
    const k = document.createElement('span'); k.className = 'key'; k.textContent = key;
    const v = document.createElement('span'); v.className = 'value';
    v.textContent = value == null ? '-' : String(value);
    kv.append(k, v); nodes.push(kv);
  }
  return block(title, nodes);
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------
function openDecision(ids, decision) {
  decisionMode = { ids, decision };
  document.getElementById('decision-title').textContent =
    `${decision === 'approve' ? 'Approve' : 'Reject'} ${ids.length} request${ids.length === 1 ? '' : 's'}`;
  const body = document.getElementById('decision-body');
  body.replaceChildren();
  const rows = (queue && queue.rows) || [];
  for (const id of ids) {
    const row = rows.find((r) => r.id === id);
    const line = document.createElement('div');
    line.className = 'review-result';
    line.textContent = row
      ? `${id} - ${row.material_name} (${(row.paths || []).length} field${(row.paths || []).length === 1 ? '' : 's'})`
      : String(id);
    body.appendChild(line);
  }
  document.getElementById('decision-reason').value = '';
  document.getElementById('decision-hint').textContent =
    ids.length > 1 ? 'a reason is required for a batch decision' : 'a reason helps the audit trail';
  document.getElementById('decision-sheet').showModal();
}

async function confirmDecision() {
  if (!decisionMode) return;
  const reason = document.getElementById('decision-reason').value.trim();
  if (decisionMode.ids.length > 1 && !reason) {
    document.getElementById('footer-status').textContent = 'reason_required';
    return;
  }
  const { ids, decision } = decisionMode;
  const body = document.getElementById('decision-body');
  body.replaceChildren();
  body.appendChild(Object.assign(document.createElement('p'),
                                 { className: 'hint', textContent: 'Deciding...' }));
  try {
    if (ids.length === 1) {
      await api(`/v1/change-requests/${ids[0]}/${decision}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason || undefined }),
      });
      body.replaceChildren();
      body.appendChild(Object.assign(document.createElement('div'),
        { className: 'review-result', textContent: `${ids[0]}: ${decision}d` }));
    } else {
      const report = await api('/v1/change-requests/decide-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, ids, reason }),
      });
      body.replaceChildren();
      body.appendChild(Object.assign(document.createElement('p'),
        { className: 'hint', textContent: `${report.decided} decided, ${report.failed} failed` }));
      for (const result of report.results) {
        const line = document.createElement('div');
        line.className = `review-result${result.ok ? '' : ' failed'}`;
        line.textContent = result.ok
          ? `${result.id}: ${result.status} at ${result.decided_at}`
          : `${result.id}: ${result.code} - still in queue`;
        body.appendChild(line);
      }
    }
    selected.clear();
    document.getElementById('detail-drawer').hidden = true;
    await loadQueue();
  } catch (exc) {
    body.replaceChildren();
    body.appendChild(Object.assign(document.createElement('p'),
      { className: 'warning', textContent: `${decision} failed: ${exc.message}` }));
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function wireReview() {
  for (const chip of document.querySelectorAll('.chip[data-status]')) {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      status = chip.dataset.status;
      queue = null;
      selected.clear();
      loadQueue();
    });
  }
  document.getElementById('queue-prev').addEventListener('click', () => {
    // The cursor is forward-only; Previous refetches from page 0.
    queue = null; loadQueue();
  });
  document.getElementById('queue-next').addEventListener('click', loadQueue);
  document.getElementById('approve-selected').addEventListener('click', () => {
    openDecision([...selected], 'approve');
  });
  document.getElementById('reject-selected').addEventListener('click', () => {
    openDecision([...selected], 'reject');
  });
  document.getElementById('detail-close').addEventListener('click', () => {
    document.getElementById('detail-drawer').hidden = true;
  });
  document.getElementById('detail-approve').addEventListener('click', () => {
    if (current) openDecision([current.id], 'approve');
  });
  document.getElementById('detail-reject').addEventListener('click', () => {
    if (current) openDecision([current.id], 'reject');
  });
  document.getElementById('decision-close').addEventListener('click', () => {
    document.getElementById('decision-sheet').close();
  });
  document.getElementById('decision-confirm').addEventListener('click', confirmDecision);

  document.addEventListener('ml:tab-changed', (event) => {
    if (event.detail.tab === 'review' && !queue) loadQueue();
  });
  document.addEventListener('ml:rows-rendered', () => {
    // A submit may have created requests the reviewer wants to see.
    const pill = document.getElementById('review-count');
    pill.hidden = false;
    pill.textContent = 'review refreshed on open';
  });
}

wireReview();
