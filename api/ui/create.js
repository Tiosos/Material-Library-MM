// Add tab (docs/ui-create-material.md step 4): a schema-driven form for a new
// material, a grouped preview with the raw JSON collapsed, in-place editing of
// a pending request, and submit through the five T3-style routes. Pure helpers
// are exported for source-level assertions and unit tests.

import { api } from './app.js';
import { normaliseValue, validateStaged } from './edit.js';

// The field list mirrors ml_create_material_validate()'s allowlist. Kept as data
// so the form, the preview and the tests read the same definition.
export const FIELD_GROUPS = [
  { title: 'Required', fields: [
    { path: 'category_code', label: 'Category', editor: 'category' },
    { path: 'name', label: 'Name', editor: 'textarea', required: true },
  ] },
  { title: 'Classification', fields: [
    { path: 'product_type', label: 'Product type', editor: 'text' },
    { path: 'model_no', label: 'Model no', editor: 'text' },
    { path: 'brand_id', label: 'Brand id', editor: 'integer' },
    { path: 'default_supplier_id', label: 'Supplier id', editor: 'integer' },
    { path: 'default_supplier_ref', label: 'Supplier ref', editor: 'text' },
    { path: 'family_id', label: 'Family id', editor: 'text' },
    { path: 'unit', label: 'Unit', editor: 'unit' },
  ] },
  { title: 'Commercial', fields: [
    { path: 'unit_cost', label: 'Unit cost', editor: 'number' },
    { path: 'currency', label: 'Currency', editor: 'text', placeholder: 'AUD' },
    { path: 'cost_basis', label: 'Cost basis', editor: 'cost_basis' },
    { path: 'lead_time_days', label: 'Lead time (days)', editor: 'integer' },
  ] },
  { title: 'Text', fields: [
    { path: 'description', label: 'Description', editor: 'textarea' },
    { path: 'product_url', label: 'Product URL', editor: 'url' },
    { path: 'image_url', label: 'Image URL', editor: 'url' },
    { path: 'synonyms', label: 'Synonyms (comma separated)', editor: 'tokens' },
    { path: 'tags', label: 'Tags (comma separated)', editor: 'tokens' },
  ] },
  { title: 'Source (optional)', fields: [
    { path: 'source_system', label: 'Source system', editor: 'text' },
    { path: 'source_id', label: 'Source id', editor: 'text' },
    { path: 'source_payload', label: 'Source payload (JSON)', editor: 'json' },
    { path: 'imported_at', label: 'Imported at (ISO)', editor: 'text' },
  ] },
  { title: 'Attributes', fields: [], dynamic: true },
];

const COST_BASES = ['ex_gst', 'inc_gst', 'ex_tax', 'inc_tax'];

let schema = null;
let editingId = null;              // non-null while reopening a pending request
let attributeFields = [];          // rebuilt when the category changes

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/** Build a proposed row from raw form values; empty fields become null, which
 *  the caller drops. Attributes are nested under the `attributes` key. */
export function buildProposed(values, dynamicFields = []) {
  const proposed = {};
  const all = [...FIELD_GROUPS.flatMap((g) => g.fields), ...dynamicFields];
  for (const field of all) {
    if (field.dynamic) continue;
    if (field.path === 'attributes') continue;
    let raw = values[field.path];
    if (field.editor === 'json') {
      if (String(raw ?? '').trim() === '') continue;
      try { raw = JSON.parse(String(raw)); }
      catch { throw new Error(`${field.path}: invalid JSON`); }
      proposed[field.path] = raw;
      continue;
    }
    const value = normaliseValue(field.path, raw, field.editor);
    if (value !== null && value !== undefined) proposed[field.path] = value;
  }
  const attrs = {};
  for (const field of dynamicFields) {
    const value = normaliseValue(field.path, values[field.path], field.editor);
    if (value !== null && value !== undefined) attrs[field.path.replace(/^attributes\./, '')] = value;
  }
  if (Object.keys(attrs).length) proposed.attributes = attrs;
  // Required checks before anything is sent; SQL repeats them as the guard.
  if (!String(proposed.category_code ?? '').trim()) throw new Error('category_code is required');
  if (!String(proposed.name ?? '').trim()) throw new Error('name is required');
  return proposed;
}

/** Validate every proposed value with the same rules the edit grid uses.
 *  Returns the first error string, or null. */
export function validateProposed(proposed) {
  for (const [path, value] of Object.entries(proposed)) {
    const editor = editorForPath(path);
    const error = validateStaged(path, value, editor);
    if (error) return `${path}: ${error}`;
  }
  return null;
}

export function editorForPath(path) {
  if (path === 'attributes') return null;
  const field = FIELD_GROUPS.flatMap((g) => g.fields).find((f) => f.path === path);
  return field ? field.editor : null;
}

/** Group proposed key/values by FIELD_GROUPS for the human-readable preview. */
export function groupProposed(proposed, dynamicFields = []) {
  const groups = FIELD_GROUPS.map((g) => ({ title: g.title, rows: [] }));
  const attrGroup = groups[groups.length - 1];
  for (const group of FIELD_GROUPS) {
    const target = group.dynamic ? attrGroup : groups.find((g) => g.title === group.title);
    const paths = group.dynamic ? dynamicFields.map((f) => f.path) : group.fields.map((f) => f.path);
    for (const path of paths) {
      if (!(path in proposed)) continue;
      if (group.dynamic) {
        target.rows.push({ path, value: proposed[path] });
      } else {
        target.rows.push({ path, value: proposed[path.replace(/^attributes\./, '')] });
      }
    }
  }
  if ('attributes' in proposed) {
    for (const [key, value] of Object.entries(proposed.attributes)) {
      attrGroup.rows.push({ path: `attributes.${key}`, value });
    }
  }
  return groups.filter((g) => g.rows.length);
}

// ---------------------------------------------------------------------------
// Form rendering
// ---------------------------------------------------------------------------

async function loadSchema() {
  if (schema) return schema;
  schema = await api('/v1/writeback/schema');
  return schema;
}

function optionsFor(editor) {
  if (editor === 'category') {
    return [...new Set((schema.catalogs || []).map((c) => c.category_code))].sort();
  }
  if (editor === 'unit') return (schema.units || []).map((u) => u.code);
  if (editor === 'cost_basis') return COST_BASES;
  return null;
}

function makeInput(field) {
  let input;
  const options = optionsFor(field.editor);
  if (options) {
    input = document.createElement('select');
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = '(none)';
    input.appendChild(blank);
    for (const value of options) {
      const option = document.createElement('option');
      option.value = value; option.textContent = value;
      input.appendChild(option);
    }
  } else if (field.editor === 'textarea') {
    input = document.createElement('textarea');
    input.rows = 2; input.maxLength = 1000;
  } else {
    input = document.createElement('input');
    input.type = field.editor === 'number' || field.editor === 'integer' ? 'number' : 'text';
    if (field.editor === 'number') input.step = '0.0001';
    if (field.maxLength) input.maxLength = field.maxLength;
  }
  input.dataset.path = field.path;
  if (field.placeholder) input.placeholder = field.placeholder;
  return input;
}

function renderForm() {
  const host = document.getElementById('add-fields');
  host.replaceChildren();
  for (const group of FIELD_GROUPS) {
    const fieldset = document.createElement('fieldset');
    fieldset.className = 'add-group';
    const legend = document.createElement('legend');
    legend.textContent = group.title;
    fieldset.appendChild(legend);
    const fields = group.dynamic ? attributeFields : group.fields;
    for (const field of fields) {
      const row = document.createElement('div');
      row.className = 'add-row';
      const label = document.createElement('label');
      label.textContent = field.label;
      if (field.required) {
        const star = document.createElement('span');
        star.className = 'req'; star.textContent = ' *';
        label.appendChild(star);
      }
      row.append(label, makeInput(field));
      fieldset.appendChild(row);
    }
    host.appendChild(fieldset);
  }
  // Category selection rebuilds the attribute fields for that category.
  const category = host.querySelector('[data-path="category_code"]');
  category.addEventListener('change', () => {
    const cat = (schema.catalogs || []).find((c) => c.category_code === category.value);
    attributeFields = (cat && cat.attributes || []).map((a) => ({
      path: `attributes.${a.key}`, label: a.key, editor: a.editor || 'text', dynamic: true,
    }));
    const values = collectValues();
    renderForm();
    applyValues(values);
    refreshPreview();
  });
}

function collectValues() {
  const values = {};
  document.querySelectorAll('#add-fields [data-path]').forEach((el) => {
    values[el.dataset.path] = el.value;
  });
  return values;
}

function applyValues(values) {
  document.querySelectorAll('#add-fields [data-path]').forEach((el) => {
    if (values[el.dataset.path] !== undefined) el.value = values[el.dataset.path];
  });
}

function currentProposed() {
  return buildProposed(collectValues(), attributeFields);
}

function renderPreview(proposed) {
  const table = document.getElementById('add-preview-table');
  table.replaceChildren();
  const groups = groupProposed(proposed, attributeFields);
  if (!groups.length) {
    const empty = document.createElement('p');
    empty.className = 'empty'; empty.textContent = 'Fill a field to see the proposed row.';
    table.appendChild(empty);
  }
  for (const group of groups) {
    const block = document.createElement('div');
    block.className = 'detail-block';
    const heading = document.createElement('h3'); heading.textContent = group.title;
    block.appendChild(heading);
    for (const row of group.rows) {
      const line = document.createElement('div');
      line.className = 'detail-diff';
      const path = document.createElement('span'); path.className = 'path'; path.textContent = row.path;
      const value = document.createElement('span'); value.className = 'after';
      value.textContent = typeof row.value === 'object' ? JSON.stringify(row.value) : String(row.value);
      line.append(path, value);
      block.appendChild(line);
    }
    table.appendChild(block);
  }
  document.getElementById('add-preview-json').textContent = JSON.stringify(proposed, null, 2);
}

function refreshPreview() {
  const status = document.getElementById('add-status');
  try {
    renderPreview(currentProposed());
    // Clear only a plain validation message, never a submit result block.
    if (!status.querySelector('.result-block') && !status.textContent.startsWith('editing ')) {
      status.textContent = '';
    }
  } catch (exc) {
    status.textContent = exc.message;
  }
}

// ---------------------------------------------------------------------------
// Submit and reopen
// ---------------------------------------------------------------------------

async function submit() {
  const status = document.getElementById('add-status');
  let proposed;
  try {
    proposed = currentProposed();
  } catch (exc) {
    status.textContent = exc.message;
    return;
  }
  const error = validateProposed(proposed);
  if (error) { status.textContent = error; return; }
  const reason = document.getElementById('add-reason').value.trim();
  const button = document.getElementById('add-submit');
  button.disabled = true;
  status.textContent = editingId ? 'Updating...' : 'Submitting...';
  try {
    const path = editingId
      ? `/v1/material-create-requests/${editingId}`
      : '/v1/material-create-requests';
    const report = await api(path, {
      method: editingId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposed, reason: reason || undefined }),
    });
    const box = document.createElement('div');
    box.className = 'result-block ok';
    box.textContent = `request ${report.id}: ${report.status} - another actor must approve it`;
    status.replaceChildren(box);
    editingId = null;
    document.getElementById('add-reopen-id').value = '';
    document.getElementById('add-reason').value = '';
  } catch (exc) {
    const box = document.createElement('div');
    box.className = 'result-block bad';
    box.textContent = `${exc.message} - nothing was retried`;
    status.replaceChildren(box);
  } finally {
    button.disabled = false;
    button.textContent = 'Submit for approval';
  }
}

async function reopen() {
  const status = document.getElementById('add-status');
  const id = document.getElementById('add-reopen-id').value.trim();
  if (!/^\d+$/.test(id)) { status.textContent = 'enter a numeric request id'; return; }
  try {
    const row = await api(`/v1/material-create-requests/${id}`);
    if (row.status !== 'pending') {
      status.textContent = `request ${id} is ${row.status}; only pending requests can be edited`;
      return;
    }
    editingId = Number(id);
    const proposed = row.proposed || {};
    // Category must be set first so attribute fields exist before values land.
    const values = {};
    for (const group of FIELD_GROUPS) {
      for (const field of group.fields) {
        if (field.path in proposed) values[field.path] =
          Array.isArray(proposed[field.path]) ? proposed[field.path].join(', ') :
          typeof proposed[field.path] === 'object' ? JSON.stringify(proposed[field.path]) :
          String(proposed[field.path]);
      }
    }
    const category = values.category_code;
    const cat = (schema.catalogs || []).find((c) => c.category_code === category);
    attributeFields = (cat && cat.attributes || []).map((a) => ({
      path: `attributes.${a.key}`, label: a.key, editor: a.editor || 'text', dynamic: true,
    }));
    renderForm();
    applyValues(values);
    for (const [key, value] of Object.entries(proposed.attributes || {})) {
      const el = document.querySelector(`#add-fields [data-path="attributes.${key}"]`);
      if (el) el.value = Array.isArray(value) ? value.join(', ') : String(value);
    }
    document.getElementById('add-reason').value = row.reason || '';
    document.getElementById('add-submit').textContent = 'Save changes';
    status.textContent = `editing request ${id} (pending) - the id stays the same`;
    refreshPreview();
  } catch (exc) {
    status.textContent = exc.message;
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function wireCreate() {
  document.getElementById('add-submit').addEventListener('click', submit);
  document.getElementById('add-reopen').addEventListener('click', reopen);
  document.getElementById('add-fields').addEventListener('input', refreshPreview);
  document.addEventListener('ml:tab-changed', async (event) => {
    if (event.detail.tab !== 'add') return;
    try {
      await loadSchema();
      attributeFields = [];
      renderForm();
      refreshPreview();
    } catch (exc) {
      document.getElementById('add-status').textContent = `schema failed: ${exc.message}`;
    }
  });
}

wireCreate();
