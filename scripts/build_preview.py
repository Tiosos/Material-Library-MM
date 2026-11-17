"""Build a self-contained, double-clickable preview of the Material Library UI.

Inlines the real stylesheet and a real data snapshot. All interaction is local
JavaScript: it is a faithful VISUAL snapshot, not a live client. Output goes to
the repository root as material-library-preview.html.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
css = (ROOT / "api/ui/styles.css").read_text(encoding="utf-8")
data = json.loads((ROOT / "preview_snapshot.json").read_text(encoding="utf-8"))
snapshot_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Material Library - Preview</title>
<style>
/* ===== inlined from api/ui/styles.css ===== */
__CSS__
/* ===== preview-only additions ===== */
.preview-note{max-width:1600px;margin:0 auto;padding:9px 20px;background:var(--accent-soft);
  border-bottom:1px solid var(--accent);color:var(--accent);font-size:12px;display:flex;
  gap:10px;align-items:center;flex-wrap:wrap}
.preview-note b{font-weight:700}
.preview-note .spacer{margin-left:auto}
.preview-note a{color:var(--accent)}
.brand small{color:var(--accent);font-weight:600}
td.editable{cursor:default}
.tab{cursor:pointer}
</style>
</head>
<body>
<div class="preview-note">
  <b>Static preview</b>
  <span>This page is a self-contained snapshot. All data is real but frozen; buttons filter and
    render locally - they do not call the live API or write to the database.</span>
  <span class="spacer"></span>
  <span id="export-stamp"></span>
</div>
<header>
  <div class="hrow">
    <div class="brand"><span class="dot"></span>Material Library <small>preview</small></div>
    <nav class="tabs" aria-label="Sections">
      <button type="button" class="tab active" data-tab="edit">Edit</button>
      <button type="button" class="tab" data-tab="add">Add</button>
      <button type="button" class="tab" data-tab="review">Review</button>
    </nav>
    <div class="identity">
      <label>actor <input id="actor" type="text" value="bob" placeholder="server default"></label>
      <span id="actor-note" class="hint">drives own badges</span>
      <label>token <input type="password" value="preview" disabled placeholder="n/a"></label>
      <span class="hint">not used</span>
    </div>
  </div>
</header>

<main id="edit-view">
  <aside class="sidebar" aria-label="Facets">
    <div class="side-block"><h3>Category</h3><div id="facet-category" class="facets"></div></div>
    <div class="side-block"><h3>Brand</h3><div id="facet-brand" class="facets"></div></div>
    <div class="side-block"><h3>Supplier</h3><div id="facet-supplier" class="facets"></div></div>
  </aside>
  <section class="content">
    <div class="querybar">
      <div class="searchwrap"><span class="icon" aria-hidden="true">&#9905;</span>
        <input id="q" type="search" maxlength="200" placeholder="Search materials" autocomplete="off"></div>
      <label>sort <select id="sort"><option value="">relevance</option><option value="name">name</option>
        <option value="unit_cost">unit_cost</option><option value="updated_at">updated_at</option></select></label>
      <label>order <select id="order"><option value="asc">asc</option><option value="desc">desc</option></select></label>
      <button type="button" id="search" class="btn primary">Search</button>
    </div>
    <div class="resultbar">
      <span id="total" class="total"></span>
      <span id="level" class="pill">browse</span>
      <span class="pill">level-accuracy 100%</span>
    </div>
    <div class="tablewrap"><table id="results">
      <thead><tr><th>name</th><th>category</th><th>brand</th><th>supplier</th>
        <th>unit</th><th class="num">unit_cost</th><th>updated_at</th><th class="num">variants</th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
    <div class="pager"><button type="button" class="btn" disabled>Previous</button>
      <span id="page-label" class="hint"></span><button type="button" class="btn" disabled>Next</button></div>
  </section>
</main>

<section id="add-view" class="add-view" hidden>
  <div class="add-head">
    <div><h2>Add a material</h2>
      <p class="hint">Every new material is filed as a request. Another actor approves it before a
        worker creates the row; nothing is written directly.</p></div>
    <div class="add-head-actions"><label>reopen pending request
      <input id="add-reopen-id" type="number" placeholder="request id"></label>
      <button type="button" id="add-reopen" class="btn">Reopen</button></div>
  </div>
  <div class="add-grid">
    <div class="add-form" id="add-form"><div id="add-fields"></div></div>
    <aside class="add-preview"><h3>Preview</h3>
      <div id="add-preview-table" class="preview-table"></div>
      <details id="add-json-details"><summary>raw JSON</summary><pre id="add-preview-json"></pre></details>
    </aside>
  </div>
  <div class="add-footer">
    <label>reason <textarea id="add-reason" rows="2" maxlength="500"
      placeholder="why this material is being added"></textarea></label>
    <span id="add-status" class="hint">Submit is disabled in the static preview.</span>
    <button type="button" class="btn primary" disabled>Submit for approval</button>
  </div>
</section>

<section id="review-view" class="review-view" hidden>
  <div class="review-toolbar">
    <div class="filter-chips" aria-label="Queue type">
      <button type="button" class="chip active" data-kind="edits">edits</button>
      <button type="button" class="chip" data-kind="creates">creates</button>
    </div>
    <div class="filter-chips" aria-label="Queue status">
      <button type="button" class="chip active" data-status="actionable">actionable</button>
      <button type="button" class="chip" data-status="pending">pending</button>
      <button type="button" class="chip" data-status="approved">approved</button>
      <button type="button" class="chip" data-status="all">all</button>
    </div>
    <div class="review-actions"><span id="queue-total" class="total"></span>
      <button type="button" class="btn primary" disabled>Approve selected</button>
      <button type="button" class="btn" disabled>Reject selected</button></div>
  </div>
  <div class="tablewrap"><table id="queue">
    <thead><tr><th aria-label="select"></th><th>id</th><th>material</th><th>paths</th>
      <th>status</th><th>requested by</th><th>age</th><th>decision</th></tr></thead>
    <tbody id="queue-rows"></tbody>
  </table></div>
</section>

<aside id="detail-drawer" class="drawer" hidden>
  <div class="drawer-head"><h2 id="detail-title">Request</h2>
    <button type="button" id="detail-close" class="btn small">Close</button></div>
  <div id="detail-badges" class="badges"></div>
  <div id="detail-body"></div>
  <div class="drawer-actions"><button type="button" class="btn primary" disabled>Approve</button>
    <button type="button" class="btn" disabled>Reject</button></div>
</aside>

<footer>
  <span id="footer-status">Ready</span>
  <span class="hint">Static snapshot - no data was written and no API was called.</span>
</footer>

<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script>
const SNAP = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

$('#export-stamp').textContent = 'Snapshot: ' + SNAP.meta.materials + ' materials, exported ' +
  SNAP.exported_at.slice(0,16).replace('T',' ');

/* ---------- tabs ---------- */
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const which = t.dataset.tab;
  $('#edit-view').hidden = which !== 'edit';
  $('#add-view').hidden = which !== 'add';
  $('#review-view').hidden = which !== 'review';
  $('#detail-drawer').hidden = true;
}));

/* ---------- Edit: facets + search ---------- */
const rows = SNAP.results;
const facetState = {brand:new Set(), category:new Set(), supplier:new Set()};
function groupFacets(name){
  const m = {};
  SNAP.facets.filter(f => f.facet === name).forEach(f => { m[f.value]=f.count; });
  return m;
}
function renderFacets(name, host){
  const counts = groupFacets(name);
  host.innerHTML = Object.entries(counts).map(([v,c]) =>
    `<button type="button" class="facet" data-facet="${esc(name)}" data-value="${esc(v)}">
       <span>${esc(v)}</span><span class="count">${c}</span></button>`).join('') || '<p class="facet-empty">none</p>';
  host.querySelectorAll('.facet').forEach(b => b.addEventListener('click', () => {
    const set = facetState[name], key = b.dataset.value;
    if (set.has(key)) set.delete(key); else set.add(key);
    b.classList.toggle('selected', set.has(key));
    renderResults();
  }));
}
renderFacets('brand', $('#facet-brand'));
renderFacets('category', $('#facet-category'));
renderFacets('supplier', $('#facet-supplier'));

function renderResults(){
  const q = $('#q').value.trim().toLowerCase();
  const sort = $('#sort').value, order = $('#order').value;
  let list = rows.filter(r => {
    if (q && ![r.name,r.category,r.brand,r.supplier].some(v => String(v||'').toLowerCase().includes(q))) return false;
    for (const f of ['brand','category','supplier']) {
      if (facetState[f].size && !facetState[f].has(r[f])) return false;
    }
    return true;
  });
  if (sort) list = [...list].sort((a,b) => {
    const x=a[sort], y=b[sort];
    const c = (x==null?1:0)-(y==null?1:0) || (x??''<y??''?-1:x??''>y??''?1:0);
    return order==='desc' ? -c : c;
  });
  $('#total').textContent = list.length + ' results of ' + SNAP.search_total + ' in the snapshot';
  $('#page-label').textContent = 'showing ' + list.length + ' of 12 exported rows';
  $('#rows').innerHTML = list.map(r => `<tr>
    <td class="name">${esc(r.name)}<br><span class="uid">${esc(r.material_uid)}</span></td>
    <td>${esc(r.category)}</td><td>${esc(r.brand)}</td><td>${esc(r.supplier)}</td>
    <td>${esc(r.unit)}</td><td class="num">${r.unit_cost==null?'-':esc(r.unit_cost)}</td>
    <td>${esc(String(r.updated_at).slice(0,10))}</td>
    <td class="num">${r.variant_count||''}</td></tr>`).join('')
    || '<tr><td colspan="8" class="empty">No materials match this filter</td></tr>';
}
$('#search').addEventListener('click', renderResults);
$('#q').addEventListener('input', renderResults);
$('#sort').addEventListener('change', renderResults);
$('#order').addEventListener('change', renderResults);
renderResults();

/* ---------- Add: schema-driven form + proposed preview ---------- */
const GROUPS = [
  {title:'Required', fields:[{p:'category_code',l:'Category',e:'category'},{p:'name',l:'Name',e:'textarea',req:1}]},
  {title:'Classification', fields:[{p:'product_type',l:'Product type',e:'text'},{p:'model_no',l:'Model no',e:'text'},
    {p:'unit',l:'Unit',e:'unit'},{p:'lead_time_days',l:'Lead time (days)',e:'integer'}]},
  {title:'Commercial', fields:[{p:'unit_cost',l:'Unit cost',e:'number'},{p:'currency',l:'Currency',e:'text',ph:'AUD'},
    {p:'cost_basis',l:'Cost basis',e:'basis'}]},
  {title:'Text', fields:[{p:'description',l:'Description',e:'textarea'},{p:'product_url',l:'Product URL',e:'text'},
    {p:'image_url',l:'Image URL',e:'text'},{p:'synonyms',l:'Synonyms (comma separated)',e:'tokens'},
    {p:'tags',l:'Tags (comma separated)',e:'tokens'}]},
  {title:'Source (optional)', fields:[{p:'source_system',l:'Source system',e:'text'},{p:'source_id',l:'Source id',e:'text'}]},
  {title:'Attributes', fields:[], dynamic:true},
];
function opts(e){
  if (e==='category') return SNAP.schema.categories;
  if (e==='unit') return SNAP.schema.units;
  if (e==='basis') return SNAP.schema.cost_bases;
  return null;
}
function attrFields(cat){
  const c = SNAP.schema.catalogs.find(x => x.category_code === cat);
  return (c ? c.attributes : []).map(a => ({p:'attributes.'+a.key, l:a.key, e:a.editor||'text', dyn:true}));
}
function inputFor(f){
  const o=opts(f.e);
  if (o) return `<select data-path="${f.p}"><option value="">(none)</option>${o.map(v=>`<option>${esc(v)}</option>`).join('')}</select>`;
  if (f.e==='textarea') return `<textarea data-path="${f.p}" rows="2" maxlength="1000"></textarea>`;
  return `<input data-path="${f.p}" type="${(f.e==='number'||f.e==='integer')?'number':'text'}" ${f.ph?`placeholder="${f.ph}"`:''}>`;
}
function renderAddForm(){
  let html='';
  for (const g of GROUPS){
    const fields = g.dynamic ? attrFields($('#add-fields [data-path="category_code"]')?.value||'') : g.fields;
    html += `<fieldset class="add-group"><legend>${g.title}</legend>`;
    for (const f of fields) html += `<div class="add-row"><label>${f.l}${f.req?'<span class="req"> *</span>':''}</label>${inputFor(f)}</div>`;
    html += '</fieldset>';
  }
  $('#add-fields').innerHTML = html;
  $('#add-fields').querySelectorAll('input,textarea,select').forEach(el => el.addEventListener('input', preview));
  const cat = $('#add-fields [data-path="category_code"]');
  if (cat) cat.addEventListener('change', () => {
    const vals = collect(); renderAddForm(); Object.entries(vals).forEach(([k,v])=>{const el=$(`#add-fields [data-path="${k}"]`); if(el) el.value=v;});
    preview();
  });
  preview();
}
function collect(){ const o={}; $('#add-fields').querySelectorAll('[data-path]').forEach(el=>o[el.dataset.path]=el.value); return o; }
function norm(p,raw,e){
  if (e==='tokens'||p==='synonyms'||p==='tags'){const t=String(raw||'').split(',').map(x=>x.trim()).filter(Boolean);return t.length?t:null;}
  if (e==='number'||e==='integer'){if(raw==='')return null;const n=Number(raw);return Number.isFinite(n)?n:null;}
  const t=String(raw??''); return t.trim()===''?null:t;
}
function proposed(){
  const v=collect(), out={}, attrs={};
  for (const g of GROUPS) for (const f of g.fields){
    if (g.dynamic) continue;
    const val=norm(f.p,v[f.p],f.e); if (val!==null) out[f.p]=val;
  }
  for (const f of attrFields(v.category_code||'')){
    const val=norm(f.p,v[f.p],f.e); if (val!==null) attrs[f.p.replace('attributes.','')]=val;
  }
  if (Object.keys(attrs).length) out.attributes=attrs;
  return out;
}
function preview(){
  const p=proposed();
  const html = Object.entries(p).map(([k,val]) =>
    `<div class="detail-block"><h3>${esc(k)}</h3><div class="detail-diff">
      <span class="path">${esc(k)}</span><span class="after">${esc(typeof val==='object'?JSON.stringify(val):val)}</span>
    </div></div>`).join('') || '<p class="empty">Fill a field to see the proposed row.</p>';
  $('#add-preview-table').innerHTML = html;
  $('#add-preview-json').textContent = JSON.stringify(p,null,2);
}
renderAddForm();

/* ---------- Review: queues + detail drawer ---------- */
let kind='edits', qstatus='actionable';
const ACTIONABLE=new Set(['pending','approved','failed']);
function age(iso){ if(!iso)return '-'; const s=Math.max(0,Math.floor((Date.now()-new Date(iso))/1000));
  if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m'; if(s<86400)return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d'; }
function queueRows(){
  let src = kind==='creates' ? SNAP.create_rows : SNAP.cr_rows;
  return src.filter(r => qstatus==='all' || (qstatus==='actionable'?ACTIONABLE.has(r.status):r.status===qstatus));
}
function titleOf(r){ return kind==='creates' ? (r.proposed&&r.proposed.name||'(unnamed)') : r.material_name; }
function fieldsOf(r){ return kind==='creates' ? Object.keys(r.proposed||{}) : (r.paths||[]); }
function renderQueue(){
  const list=queueRows();
  $('#queue-total').textContent=list.length+' requests';
  $('#queue-rows').innerHTML=list.map(r=>{
    const own = $('#actor').value.trim() && $('#actor').value.trim()===r.requested_by;
    return `<tr data-id="${r.id}">
      <td><input type="checkbox" ${own?'disabled title="cannot approve own request"':''}></td>
      <td>${r.id}</td><td>${esc(titleOf(r))}</td><td>${esc(fieldsOf(r).join(', '))}</td>
      <td class="status-${esc(r.status)}">${esc(r.status)}</td><td>${esc(r.requested_by)}</td>
      <td>${age(r.requested_at)}</td><td>${esc(r.decision_reason||r.decided_at||'-')}</td></tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">No requests in this view</td></tr>';
  $('#queue-rows').querySelectorAll('tr[data-id]').forEach(tr =>
    tr.addEventListener('click',()=>openDetail(Number(tr.dataset.id))));
}
function openDetail(id){
  const r=(kind==='creates'?SNAP.create_rows:SNAP.cr_rows).find(x=>x.id===id); if(!r)return;
  $('#detail-title').textContent=`${kind==='creates'?'Create':'Request'} ${id} - ${titleOf(r)}`;
  const badges=[];
  if ($('#actor').value.trim()===r.requested_by) badges.push('own');
  if (r.decision_reason||r.decided_at) badges.push('decided');
  $('#detail-badges').innerHTML=badges.map(b=>`<span class="badge ${b}">${b}</span>`).join('');
  let body='';
  if (kind==='creates'){
    body+='<div class="detail-block"><h3>Proposed</h3>';
    for (const [k,v] of Object.entries(r.proposed||{})) body+=`<div class="detail-diff"><span class="path">${esc(k)}</span><span class="after">${esc(typeof v==='object'?JSON.stringify(v):v)}</span></div>`;
    body+='</div>';
  } else {
    body+='<div class="detail-block"><h3>Request</h3><pre>'+esc(JSON.stringify(r,null,2))+'</pre></div>';
  }
  body+='<div class="detail-block"><h3>Decision</h3>';
  for (const [k,v] of [['status',r.status],['decided_by',r.decided_by],['decided_at',r.decided_at],['decision_reason',r.decision_reason],['error',r.error]])
    body+=`<div class="detail-kv"><span class="key">${k}</span><span class="value">${v==null?'-':esc(v)}</span></div>`;
  body+='</div>';
  $('#detail-body').innerHTML=body;
  $('#detail-drawer').hidden=false;
}
$('#detail-close').addEventListener('click',()=>$('#detail-drawer').hidden=true);
$$('.chip[data-kind]').forEach(c=>c.addEventListener('click',()=>{
  $$('.chip[data-kind]').forEach(x=>x.classList.remove('active')); c.classList.add('active');
  kind=c.dataset.kind; renderQueue();
}));
$$('.chip[data-status]').forEach(c=>c.addEventListener('click',()=>{
  $$('.chip[data-status]').forEach(x=>x.classList.remove('active')); c.classList.add('active');
  qstatus=c.dataset.status; renderQueue();
}));
$('#actor').addEventListener('input',renderQueue);
renderQueue();
</script>
</body>
</html>
"""

out = (HTML.replace("__CSS__", css).replace("__SNAPSHOT__", snapshot_json))
target = ROOT / "material-library-preview.html"
target.write_text(out, encoding="utf-8", newline="\n")
print("wrote", target, len(out), "bytes")
