/* ============================================================
   Material Library — WORKFLOWS
   Real edit form + create form + approval workflow.
   Talks to app.js through a small ctx object so state stays shared.
   ============================================================ */
(function (global) {
  "use strict";

  var ctx = null;      // set by init: { ALL, DB, byUid, renderAll, toast, esc, swatchClass, onStage }
  var CRS = [];        // change requests
  var crSeq = 5;
  var CURRENT_USER = "AL";
  var formMode = "edit";   // 'edit' | 'create'
  var formUid = null;
  var revTab = "pending";
  var activeCrId = null;
  var checked = {};        // batch selection { crId:true }

  var LABELS = {
    name: "Material name", brand: "Brand", category: "Category", modelNo: "Model no.",
    family: "Family", supplier: "Supplier", unit: "Unit", unitCost: "Unit cost (ref.)",
    costBasis: "Cost basis", productUrl: "Product link", status: "Status",
    confidence: "Confidence", description: "Description", variants: "Variants", swatch: "Swatch"
  };
  var UNITS = ["ea", "sheet", "box", "roll", "m", "m2", "mm", "set"];
  var BASIS = ["ex GST", "inc GST"];
  var REJECT_PRESETS = [
    "Duplicate of an existing material",
    "Price needs supplier re-quote",
    "Category or attributes are incorrect",
    "Missing or invalid product link",
    "Supplier details not confirmed",
    "Not aligned with master data standard"
  ];
  var STATUSES = [{ v: "active", t: "Active" }, { v: "review", t: "Needs review" }, { v: "duplicate", t: "Possible duplicate" }];

  /* ---------- seed data ---------- */
  function seed() {
    CRS = [
      { id: "CR-0004", type: "update", uid: "mat_91d4e6b7c0352fa8", name: "Laminex 18mm Black MDF",
        requestedBy: "MK", requestedAt: "18 min ago", status: "approved",
        reason: "Supplier name corrected to match the master vendor list.",
        changes: [{ field: "supplier", from: "Laminex", to: "Laminex AU", by: "MK", at: "18 min ago" }],
        decidedBy: "AL", decidedAt: "6 min ago" },
      { id: "CR-0003", type: "update", uid: "mat_0a6c92f1e8d47b35", name: "Polytec 16mm Walnut Melamine",
        requestedBy: CURRENT_USER, requestedAt: "42 min ago", status: "pending",
        reason: "Price confirmed and data quality checked — ready to activate.",
        changes: [{ field: "status", from: "review", to: "active", by: "AL", at: "42 min ago" }, { field: "confidence", from: 92, to: 100, by: "AL", at: "42 min ago" }],
        decidedBy: null, decidedAt: null },
      { id: "CR-0002", type: "update", uid: "mat_4c88f019e2b7a6d3", name: "CLIP top Knock-in hinge NI",
        requestedBy: "JD", requestedAt: "2 h ago", status: "pending",
        reason: "Updated cost from supplier quote dated this week.",
        changes: [{ field: "unitCost", from: 8.9, to: 9.2, by: "JD", at: "2 h ago" }],
        decidedBy: null, decidedAt: null },
      { id: "CR-0001", type: "create", uid: "mat_new0000000000001", name: "Laminex 18mm White Melamine",
        requestedBy: "MK", requestedAt: "5 h ago", status: "pending",
        reason: "New board added to the supplier catalogue.",
        changes: [
          { field: "name", from: "—", to: "Laminex 18mm White Melamine", by: "MK", at: "5 h ago" },
          { field: "brand", from: "—", to: "Laminex", by: "MK", at: "5 h ago" },
          { field: "category", from: "—", to: "board.melamine.mdf", by: "MK", at: "5 h ago" },
          { field: "unit", from: "—", to: "sheet", by: "MK", at: "5 h ago" },
          { field: "unitCost", from: "—", to: 40.5, by: "MK", at: "5 h ago" }
        ],
        payload: {
          uid: "mat_new0000000000001", name: "Laminex 18mm White Melamine", brand: "Laminex",
          category: "board.melamine.mdf", categoryLabel: "Board / Melamine MDF", supplier: "Laminex",
          unit: "sheet", unitCost: 40.5, costBasis: "ex GST", variants: { thickness: "18 mm", finish: "White", size: "2440 × 1220" },
          confidence: 90, status: "review", updated: "just now", family: "Laminex Melamine", modelNo: "LM-18WH",
          swatch: "white", tags: ["white", "melamine", "18mm"], description: "White melamine board, 18 mm, newly added to the supplier range.",
          source: "manual", productUrl: "https://www.laminex.com.au/"
        },
        decidedBy: null, decidedAt: null },
      { id: "CR-0000", type: "update", uid: "mat_c4a8e7d13f0b9625", name: "Laminex 6mm Hardboard",
        requestedBy: "JD", requestedAt: "1 d ago", status: "rejected",
        reason: "Requested a lower unit cost for bulk purchasing.",
        changes: [{ field: "unitCost", from: 18.9, to: 15.5, by: "JD", at: "1 d ago" }],
        decidedBy: "AL", decidedAt: "1 d ago",
        rejectReason: "Cost below supplier's minimum — please re-quote before resubmitting." }
    ];
  }

  /* ---------- dom bootstrap ---------- */
  function ensureDom() {
    if (document.getElementById("wfOverlay")) return;
    var wrap = document.createElement("div");
    wrap.innerHTML =
      '<div class="overlay" id="wfOverlay"></div>' +
      '<div class="modal" id="wfFormModal" role="dialog" aria-modal="true">' +
        '<div class="mhead"><div class="mi"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg></div>' +
        '<div class="mt"><b id="wfFormTitle">Edit material</b><small id="wfFormSub">Changes are staged until approved</small></div>' +
        '<button class="mx" id="wfFormX">×</button></div>' +
        '<div class="mbody" id="wfFormBody"></div>' +
        '<div class="mfoot"><span class="chgcount" id="wfChgCount">0 fields changed</span><span class="spacer"></span>' +
        '<button class="btn" id="wfCancel">Cancel</button>' +
        '<button class="btn" id="wfDraft">Save as draft</button>' +
        '<button class="btn primary" id="wfSubmit">Submit for approval</button></div>' +
      '</div>' +
      '<div class="modal wide" id="wfRevModal" role="dialog" aria-modal="true">' +
        '<div class="mhead"><div class="mi"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2h9"/></svg></div>' +
        '<div class="mt"><b>Review changes</b><small id="wfRevSub">Approve or reject material change requests</small></div>' +
        '<button class="mx" id="wfRevX">×</button></div>' +
        '<div class="revTabs" id="wfRevTabs"></div>' +
        '<div class="revBody"><div class="revList" id="wfRevList"></div><div class="revDetail" id="wfRevDetail"></div></div>' +
        '<div class="mfoot"><span class="hint" id="wfRevHint">Select a request to review its changes.</span><span class="spacer"></span>' +
        '<button class="btn" id="wfBatch" disabled>Approve selected</button>' +
        '<button class="btn" id="wfReject">Reject</button>' +
        '<button class="btn primary" id="wfApprove">Approve</button></div>' +
      '</div>';
    while (wrap.firstChild) document.body.appendChild(wrap.firstChild);
    bindModalEvents();
  }
  function openOverlay() { document.getElementById("wfOverlay").classList.add("open"); }
  function closeAll() {
    document.getElementById("wfOverlay").classList.remove("open");
    document.getElementById("wfFormModal").classList.remove("open");
    document.getElementById("wfRevModal").classList.remove("open");
  }

  /* ---------- EDIT / CREATE FORM ---------- */
  function fieldHtml(f, val) {
    var v = val == null ? "" : String(val);
    var id = "wf_f_" + f.key;
    var input = "";
    if (f.type === "select") {
      var opts = "";
      if (f.options === "brands") opts = ctx.DB.meta.brands.map(function (o) { return '<option' + (o === v ? " selected" : "") + '>' + ctx.esc(o) + "</option>"; }).join("");
      else if (f.options === "suppliers") opts = ctx.DB.meta.suppliers.map(function (o) { return '<option' + (o === v ? " selected" : "") + '>' + ctx.esc(o) + "</option>"; }).join("");
      else if (f.options === "categories") opts = ctx.DB.meta.categories.map(function (o) { return '<option value="' + o + '"' + (o === v ? " selected" : "") + ">" + ctx.esc(o) + "</option>"; }).join("");
      else if (f.options === "units") opts = UNITS.map(function (o) { return '<option' + (o === v ? " selected" : "") + ">" + o + "</option>"; }).join("");
      else if (f.options === "basis") opts = BASIS.map(function (o) { return '<option' + (o === v ? " selected" : "") + ">" + o + "</option>"; }).join("");
      else if (f.options === "statuses") opts = STATUSES.map(function (o) { return '<option value="' + o.v + '"' + (o.v === v ? " selected" : "") + ">" + o.t + "</option>"; }).join("");
      input = '<select id="' + id + '" data-field="' + f.key + '">' + opts + "</select>";
    } else if (f.type === "textarea") {
      input = '<textarea id="' + id + '" data-field="' + f.key + '">' + ctx.esc(v) + "</textarea>";
    } else {
      var t = f.type === "number" ? "number" : (f.type === "url" ? "url" : "text");
      var step = f.type === "number" ? ' step="any" min="0"' : "";
      var ph = f.key === "productUrl" ? "https://…" : "";
      input = '<input type="' + t + '"' + step + ' id="' + id + '" data-field="' + f.key + '" value="' + ctx.esc(v) + '" placeholder="' + ph + '">';
    }
    var hint = f.key === "unitCost" ? '<span class="fhint">Reference only — subject to change over time</span>' :
               f.key === "productUrl" ? '<span class="fhint">Opens in a new tab from the detail view</span>' : "";
    return '<div class="field' + (f.full ? " full" : "") + '" data-wrap="' + f.key + '"><label for="' + id + '">' +
      ctx.esc(f.label) + (f.required ? ' <span class="req">*</span>' : "") + "</label>" + input + hint +
      '<span class="emsg">Required field</span></div>';
  }
  function variantRows(variants) {
    var keys = Object.keys(variants || {});
    if (!keys.length) keys = [""];
    return keys.map(function (k) {
      return '<div class="vrow"><input data-vk placeholder="Attribute (e.g. thickness)" value="' + ctx.esc(k) + '">' +
        '<input data-vv placeholder="Value (e.g. 18 mm)" value="' + ctx.esc((variants || {})[k] || "") + '">' +
        '<button class="rm" title="Remove">×</button></div>';
    }).join("");
  }
  var FIELDS = [
    { key: "name", label: "Material name", type: "text", required: true, full: true },
    { key: "brand", label: "Brand", type: "select", options: "brands" },
    { key: "category", label: "Category", type: "select", options: "categories" },
    { key: "modelNo", label: "Model no.", type: "text" },
    { key: "family", label: "Family", type: "text", full: true },
    { key: "supplier", label: "Supplier", type: "select", options: "suppliers" },
    { key: "unit", label: "Unit", type: "select", options: "units" },
    { key: "unitCost", label: "Unit cost (reference)", type: "number" },
    { key: "costBasis", label: "Cost basis", type: "select", options: "basis" },
    { key: "productUrl", label: "Product link (URL)", type: "url", full: true },
    { key: "status", label: "Status", type: "select", options: "statuses" },
    { key: "confidence", label: "Confidence (%)", type: "number" },
    { key: "description", label: "Description", type: "textarea", full: true }
  ];
  function openForm(uid) {
    ensureDom();
    var isCreate = uid == null;
    formMode = isCreate ? "create" : "edit";
    formUid = uid;
    var m = isCreate ? {
      name: "", brand: ctx.DB.meta.brands[0], category: ctx.DB.meta.categories[0], modelNo: "",
      family: "", supplier: ctx.DB.meta.suppliers[0], unit: "ea", unitCost: null, costBasis: "ex GST",
      productUrl: "", status: "review", confidence: 100, description: "", variants: {}
    } : ctx.byUid(uid);
    if (!m) return ctx.toast("Material not found");
    document.getElementById("wfFormTitle").textContent = isCreate ? "Add material" : "Edit material";
    document.getElementById("wfFormSub").textContent = isCreate
      ? "A new material request will be created (not written directly)"
      : m.uid + " · changes require approval before they take effect";
    var body = '<div class="formSec"><h4>Identity &amp; classification</h4><div class="grid2">' +
      FIELDS.slice(0, 5).map(function (f) { return fieldHtml(f, m[f.key]); }).join("") + "</div></div>" +
      '<div class="formSec"><h4>Commercial &amp; reference</h4><div class="grid2">' +
      FIELDS.slice(5, 10).map(function (f) { return fieldHtml(f, m[f.key]); }).join("") + "</div></div>" +
      '<div class="formSec"><h4>Data quality</h4><div class="grid2">' +
      FIELDS.slice(10, 12).map(function (f) { return fieldHtml(f, m[f.key]); }).join("") + "</div></div>" +
      '<div class="formSec"><h4>Variants</h4><div id="wfVariants">' + variantRows(m.variants) + "</div>" +
      '<button class="addRow" id="wfAddVariant">+ Add variant</button></div>' +
      '<div class="formSec"><h4>Description</h4>' + fieldHtml(FIELDS[12], m.description) + "</div>" +
      '<div class="formSec"><h4>Change preview <span style="text-transform:none;letter-spacing:0;font-weight:500">(updates as you type)</span></h4>' +
      '<div id="wfLiveDiff"></div></div>';
    document.getElementById("wfFormBody").innerHTML = body;
    document.getElementById("wfFormBody")._original = JSON.stringify(collectForm());
    document.getElementById("wfAddVariant").addEventListener("click", function () {
      var box = document.getElementById("wfVariants");
      var div = document.createElement("div");
      div.innerHTML = '<div class="vrow"><input data-vk placeholder="Attribute"><input data-vv placeholder="Value"><button class="rm" title="Remove">×</button></div>';
      box.appendChild(div.firstChild);
      bindVariantRemove();
    });
    bindVariantRemove();
    document.getElementById("wfChgCount").classList.remove("show");
    document.getElementById("wfDraft").style.display = isCreate ? "none" : "";
    openOverlay();
    document.getElementById("wfFormModal").classList.add("open");
    bindFormLive();
    updateChangeCount();
  }
  function bindVariantRemove() {
    $$wf("#wfVariants .rm").forEach(function (b) {
      b.onclick = function () { b.parentNode.remove(); updateChangeCount(); };
    });
  }
  function $$wf(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function collectForm() {
    var o = {};
    $$wf("#wfFormBody [data-field]").forEach(function (el) {
      var k = el.dataset.field;
      if (k === "unitCost") o[k] = el.value === "" ? null : parseFloat(el.value);
      else if (k === "confidence") o[k] = el.value === "" ? 100 : parseInt(el.value, 10);
      else o[k] = el.value;
    });
    var v = {};
    $$wf("#wfVariants .vrow").forEach(function (row) {
      var k = row.querySelector("[data-vk]").value.trim();
      var val = row.querySelector("[data-vv]").value.trim();
      if (k) v[k] = val;
    });
    o.variants = v;
    return o;
  }
  function bindFormLive() {
    $$wf("#wfFormBody [data-field], #wfVariants input").forEach(function (el) {
      el.addEventListener("input", updateChangeCount);
      el.addEventListener("change", updateChangeCount);
    });
  }
  function fmtVal(v) {
    if (v == null || v === "" || v === "—") return '<span class="dv-empty">—</span>';
    return ctx.esc(String(v));
  }
  /* per-change author badge ("who · when") used in both diff tables */
  function authorHtml(who, when) {
    var initials = String(who || "?").slice(0, 2).toUpperCase();
    var isMe = who === CURRENT_USER;
    return '<span class="author' + (isMe ? " me" : "") + '" title="Changed by ' + ctx.esc(who || "unknown") + '">' +
      '<span class="au-av">' + ctx.esc(initials) + "</span>" + ctx.esc(who || "unknown") +
      (when ? " · " + ctx.esc(when) : "") + "</span>";
  }
  function updateChangeCount() {
    var body = document.getElementById("wfFormBody");
    var orig = JSON.parse(body._original || "{}");
    var cur = collectForm();
    var changes = diffFrom(orig, cur);
    var badge = document.getElementById("wfChgCount");
    badge.textContent = changes.length + " field" + (changes.length === 1 ? "" : "s") + " changed";
    badge.classList.toggle("show", changes.length > 0);

    var box = document.getElementById("wfLiveDiff");
    if (!box) return;
    if (!changes.length) {
      box.innerHTML = '<div class="diff-empty">No changes yet — edit a field above to see before / after here.</div>';
      return;
    }
    box.innerHTML = '<table class="diff live"><thead><tr><th>Field</th><th>Before</th><th></th><th>After</th><th>Changed by</th></tr></thead><tbody>' +
      changes.map(function (c) {
        return "<tr><td class='df'>" + ctx.esc(LABELS[c.field] || c.field) + "</td>" +
          "<td class='old'>" + fmtVal(c.from) + "</td><td class='arw'>→</td>" +
          "<td class='new'>" + fmtVal(c.to) + "</td>" +
          "<td class='by-cell'>" + authorHtml(CURRENT_USER, "just now") + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  function validateForm() {
    var ok = true;
    $$wf("#wfFormBody .field").forEach(function (w) { w.classList.remove("err"); });
    var name = document.getElementById("wf_f_name");
    if (!name.value.trim()) { name.parentNode.classList.add("err"); ok = false; }
    var url = document.getElementById("wf_f_productUrl");
    if (url && url.value.trim()) {
      try { new URL(url.value.trim()); } catch (e) { url.parentNode.classList.add("err"); ok = false; }
    }
    return ok;
  }
  function diffFrom(orig, next) {
    var out = [];
    Object.keys(next).forEach(function (k) {
      if (k === "variants") {
        if (JSON.stringify(orig[k]) !== JSON.stringify(next[k])) {
          var ok = Object.keys(orig[k] || {}), nk = Object.keys(next[k] || {});
          var all = ok.concat(nk.filter(function (x) { return ok.indexOf(x) < 0; }));
          all.forEach(function (vk) {
            var a = (orig[k] || {})[vk], b = next[k][vk];
            if (a !== b) out.push({ field: "variants." + vk, from: a == null ? "—" : a, to: b == null ? "—" : b });
          });
        }
      } else if (JSON.stringify(orig[k]) !== JSON.stringify(next[k])) {
        out.push({ field: k, from: orig[k] == null || orig[k] === "" ? "—" : orig[k], to: next[k] == null || next[k] === "" ? "—" : next[k] });
      }
    });
    return out;
  }
  function submitForm(asDraft) {
    if (!validateForm()) { ctx.toast("Please fix the highlighted fields"); return; }
    var next = collectForm();
    if (formMode === "edit") {
      var m = ctx.byUid(formUid);
      var orig = {};
      Object.keys(next).forEach(function (k) { orig[k] = m[k]; });
      var changes = diffFrom(orig, next);
      changes.forEach(function (c) { c.by = CURRENT_USER; c.at = "just now"; });
      if (!changes.length && !asDraft) { ctx.toast("No changes to submit"); closeAll(); return; }
      if (asDraft) {
        ctx.onStage(formUid);
        ctx.toast("Saved as draft · " + formUid.slice(0, 14) + "… staged");
      } else {
        var id = nextId();
        CRS.unshift({ id: id, type: "update", uid: formUid, name: m.name, requestedBy: CURRENT_USER,
          requestedAt: "just now", status: "pending", reason: "Submitted from the edit form.",
          changes: changes, decidedBy: null, decidedAt: null });
        ctx.toast("Change request " + id + " submitted for approval");
      }
    } else {
      // create
      var uid = "mat_" + Math.random().toString(16).slice(2, 12) + "c" + Date.now().toString(16).slice(-4);
      var payload = Object.assign({}, next, {
        uid: uid, categoryLabel: categoryLabel(next.category), updated: "just now", source: "manual",
        tags: [], swatch: next.category.indexOf("board.") === 0 ? "wood-oak" : "metal-nickel"
      });
      var id2 = nextId();
      CRS.unshift({ id: id2, type: "create", uid: uid, name: next.name, requestedBy: CURRENT_USER,
        requestedAt: "just now", status: "pending", reason: "New material submitted from the add form.",
        changes: ["name", "brand", "category", "unit", "unitCost"].map(function (k) {
          return { field: k, from: "—", to: payload[k] == null ? "—" : payload[k], by: CURRENT_USER, at: "just now" };
        }), payload: payload, decidedBy: null, decidedAt: null });
      ctx.toast("Create request " + id2 + " submitted for approval");
    }
    closeAll();
    ctx.renderAll();
  }
  function categoryLabel(code) {
    var parts = String(code).split(".");
    var map = { board: "Board", hardware: "Hardware", melamine: "Melamine", mdf: "MDF", veneer: "Veneer",
      particleboard: "Particleboard", hardboard: "Hardboard", hinge: "Hinge", runner: "Runner", fitting: "Fitting" };
    return parts.map(function (p) { return map[p] || p; }).join(" / ");
  }
  function nextId() { return "CR-" + String(crSeq++).padStart(4, "0"); }

  /* ---------- APPROVAL ---------- */
  function openReview() {
    ensureDom();
    checked = {};
    revTab = "pending";
    var pending = CRS.filter(function (c) { return c.status === "pending"; });
    activeCrId = pending.length ? pending[0].id : (CRS[0] ? CRS[0].id : null);
    renderReview();
    openOverlay();
    document.getElementById("wfRevModal").classList.add("open");
  }
  function renderReview() {
    var counts = {
      pending: CRS.filter(function (c) { return c.status === "pending"; }).length,
      approved: CRS.filter(function (c) { return c.status === "approved"; }).length,
      rejected: CRS.filter(function (c) { return c.status === "rejected"; }).length,
      all: CRS.length
    };
    document.getElementById("wfRevSub").textContent = counts.pending + " pending · " + counts.approved + " approved · " + counts.rejected + " rejected";
    document.getElementById("wfRevTabs").innerHTML = ["pending", "approved", "rejected", "all"].map(function (t) {
      return '<button data-tab="' + t + '" class="' + (revTab === t ? "active" : "") + '">' +
        t.charAt(0).toUpperCase() + t.slice(1) + '<span class="n">' + counts[t] + "</span></button>";
    }).join("");
    $$wf("#wfRevTabs button").forEach(function (b) {
      b.onclick = function () {
        revTab = b.dataset.tab;
        var list = visibleCRs();
        activeCrId = list.length ? list[0].id : null;
        renderReview();
      };
    });
    var list = visibleCRs();
    document.getElementById("wfRevList").innerHTML = list.length ? list.map(function (c) {
      var sm = c.status === "pending" ? "warn" : (c.status === "approved" ? "ok" : "danger");
      var st = c.status === "pending" ? "Pending" : (c.status === "approved" ? "Approved" : "Rejected");
      var lastBy = (c.changes.length && c.changes[c.changes.length - 1].by) || c.requestedBy;
      var lastAt = (c.changes.length && c.changes[c.changes.length - 1].at) || c.requestedAt;
      return '<div class="crItem' + (activeCrId === c.id ? " active" : "") + '" data-cr="' + c.id + '">' +
        '<input type="checkbox" class="crcb" data-cr="' + c.id + '"' + (checked[c.id] ? " checked" : "") + '>' +
        '<div class="crmain"><div class="crtitle">' + ctx.esc(c.name) + "</div>" +
        '<div class="crmeta"><span class="badge ' + sm + '"><i></i>' + st + "</span>" +
        "<span>" + c.id + "</span><span>" + ctx.esc(c.requestedAt) + "</span></div>" +
        '<div class="crsummary"><span class="fldct">' + c.changes.length + " field" + (c.changes.length === 1 ? "" : "s") + "</span>" +
        "<span>last by <b>" + ctx.esc(lastBy) + "</b></span><span>" + ctx.esc(lastAt) + "</span></div></div></div>";
    }).join("") : '<div class="revEmpty">No ' + revTab + ' requests</div>';
    $$wf("#wfRevList .crItem").forEach(function (it) {
      it.onclick = function (e) {
        if (e.target.classList.contains("crcb")) return;
        activeCrId = it.dataset.cr; renderReview();
      };
    });
    $$wf("#wfRevList .crcb").forEach(function (cb) {
      cb.onchange = function () { checked[cb.dataset.cr] = cb.checked; renderReview(); };
    });
    renderReviewDetail();
  }
  function visibleCRs() {
    return CRS.filter(function (c) { return revTab === "all" || c.status === revTab; });
  }
  function renderReviewDetail() {
    var detail = document.getElementById("wfRevDetail");
    var cr = CRS.filter(function (c) { return c.id === activeCrId; })[0];
    var approve = document.getElementById("wfApprove");
    var reject = document.getElementById("wfReject");
    var batch = document.getElementById("wfBatch");
    var nChecked = Object.keys(checked).filter(function (k) { return checked[k] && (CRS.filter(function (c) { return c.id === k; })[0] || {}).status === "pending"; }).length;
    batch.disabled = nChecked === 0;
    batch.textContent = nChecked ? "Approve selected (" + nChecked + ")" : "Approve selected";
    if (!cr) {
      detail.innerHTML = '<div class="revEmpty">Select a request to review its changes.</div>';
      approve.disabled = reject.disabled = true;
      return;
    }
    approve.disabled = reject.disabled = cr.status !== "pending";
    var m = ctx.byUid(cr.uid) || cr.payload || {};
    var sm = cr.status === "pending" ? "warn" : (cr.status === "approved" ? "ok" : "danger");
    var st = cr.status === "pending" ? "Pending" : (cr.status === "approved" ? "Approved" : "Rejected");
    var diffRows = cr.changes.map(function (c) {
      return "<tr><td class='df'>" + ctx.esc(LABELS[c.field] || c.field) + "</td>" +
        "<td class='old'>" + ctx.esc(String(c.from)) + "</td><td class='arw'>→</td>" +
        "<td class='new'>" + ctx.esc(String(c.to)) + "</td>" +
        "<td class='by-cell'>" + authorHtml(c.by || cr.requestedBy, c.at || cr.requestedAt) + "</td></tr>";
    }).join("");
    var selfWarn = cr.requestedBy === CURRENT_USER && cr.status === "pending"
      ? '<div class="selfWarn"><span>ⓘ</span><div><b>Self-approval restricted.</b> You requested this change — in the real system another reviewer must approve it. For this prototype you may still approve to see the outcome.</div></div>'
      : "";
    var audit = '<div>' + ctx.esc(cr.requestedBy) + " submitted this request <span>· " + ctx.esc(cr.requestedAt) + "</span></div>";
    if (cr.decidedBy) audit += "<div>" + ctx.esc(cr.decidedBy) + " " + (cr.status === "approved" ? "approved" : "rejected") + " it <span>· " + ctx.esc(cr.decidedAt) + "</span></div>";
    if (cr.status === "rejected" && cr.rejectReason) {
      audit += '<div class="audit-reason"><span class="ar-label">Rejection reason</span><span class="ar-text">' +
        ctx.esc(cr.rejectReason) + "</span></div>";
    }
    detail.innerHTML =
      '<div class="rdHead"><div class="' + ctx.swatchClass(m) + '" style="width:46px;height:46px"></div>' +
      '<div><h3>' + ctx.esc(cr.name) + "</h3><div class='u'>" + ctx.esc(cr.uid) + "</div></div>" +
      '<div class="badges"><span class="badge ' + sm + '"><i></i>' + st + "</span>" +
      '<span class="tag">' + (cr.type === "create" ? "New material" : "Update") + "</span></div></div>" +
      '<div class="rdSec"><h4>Proposed changes (' + cr.changes.length + ") <span style='text-transform:none;letter-spacing:0;font-weight:500'>— who changed what &amp; when</span></h4>" +
      '<table class="diff"><thead><tr><th>Field</th><th>Current</th><th></th><th>Proposed</th><th>Changed by</th></tr></thead><tbody>' + diffRows + "</tbody></table></div>" +
      '<div class="rdSec"><h4>Reason</h4><div class="reasonBox">' + ctx.esc(cr.reason) + "</div></div>" +
      selfWarn +
      '<div class="rdSec"><h4>Audit trail</h4><div class="audit">' + audit + "</div></div>" +
      (cr.status === "pending" ? '<div class="rejBox" id="wfRejBox">' +
        '<h4 style="font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);margin:0 0 7px">Rejection reason</h4>' +
        '<select id="wfRejPreset"><option value="">Common reasons…</option>' +
        REJECT_PRESETS.map(function (p) { return '<option value="' + ctx.esc(p) + '">' + ctx.esc(p) + "</option>"; }).join("") +
        '<option value="__custom">Other (type below)</option></select>' +
        '<textarea id="wfRejReason" placeholder="Select a common reason, or explain why this change is rejected…"></textarea>' +
        '<div style="display:flex;justify-content:flex-end;margin-top:8px">' +
        '<button class="btn" id="wfRejConfirm" style="color:var(--danger);border-color:var(--danger)">Confirm rejection</button></div></div>' : "");
    if (cr.status === "pending") {
      document.getElementById("wfRejBox").style.display = "none";
      reject.onclick = function () {
        var box = document.getElementById("wfRejBox");
        box.style.display = box.style.display === "block" ? "none" : "block";
      };
      var preset = document.getElementById("wfRejPreset");
      var reasonBox = document.getElementById("wfRejReason");
      preset.onchange = function () {
        if (preset.value === "__custom") { reasonBox.value = ""; reasonBox.focus(); }
        else if (preset.value) reasonBox.value = preset.value;
        reasonBox.classList.remove("err");
      };
      reasonBox.oninput = function () { reasonBox.classList.remove("err"); };
      document.getElementById("wfRejConfirm").onclick = function () {
        var reason = (reasonBox.value || "").trim();
        if (!reason) { reasonBox.classList.add("err"); ctx.toast("Please choose or enter a rejection reason"); return; }
        decide(cr.id, "reject", reason);
      };
    }
  }
  function decide(id, action, reason) {
    var cr = CRS.filter(function (c) { return c.id === id; })[0];
    if (!cr || cr.status !== "pending") return;
    if (action === "approve") {
      if (cr.type === "create" && cr.payload) {
        ctx.ALL.push(cr.payload);
        ctx.DB.materials.push(cr.payload);
      } else {
        var m = ctx.byUid(cr.uid);
        if (m) cr.changes.forEach(function (c) { if (c.field.indexOf("variants.") === 0) {
          m.variants = m.variants || {}; m.variants[c.field.split(".")[1]] = c.to;
        } else m[c.field] = c.to; });
      }
      cr.status = "approved";
      cr.decidedBy = CURRENT_USER; cr.decidedAt = "just now";
      if (cr.type === "create") {
        ctx.DB.meta.categories.indexOf(cr.payload.category) < 0 && ctx.DB.meta.categories.push(cr.payload.category);
        ctx.DB.meta.brands.indexOf(cr.payload.brand) < 0 && ctx.DB.meta.brands.push(cr.payload.brand);
        ctx.DB.meta.suppliers.indexOf(cr.payload.supplier) < 0 && ctx.DB.meta.suppliers.push(cr.payload.supplier);
      }
      ctx.toast(cr.id + " approved · changes applied to both views");
    } else {
      cr.status = "rejected"; cr.decidedBy = CURRENT_USER; cr.decidedAt = "just now";
      cr.rejectReason = reason || "No reason provided";
      ctx.toast(cr.id + " rejected");
    }
    delete checked[id];
    ctx.renderAll();
    renderReview();
  }
  function bindModalEvents() {
    document.getElementById("wfFormX").onclick = closeAll;
    document.getElementById("wfRevX").onclick = closeAll;
    document.getElementById("wfOverlay").onclick = closeAll;
    document.getElementById("wfCancel").onclick = closeAll;
    document.getElementById("wfDraft").onclick = function () { submitForm(true); };
    document.getElementById("wfSubmit").onclick = function () { submitForm(false); };
    document.getElementById("wfApprove").onclick = function () { if (activeCrId) decide(activeCrId, "approve"); };
    document.getElementById("wfReject").onclick = function () {
      if (!activeCrId) return;
      var reason = (document.getElementById("wfRejReason") || {}).value;
      decide(activeCrId, "reject", (reason || "").trim());
    };
    document.getElementById("wfBatch").onclick = function () {
      var ids = Object.keys(checked).filter(function (k) { return checked[k]; });
      ids.forEach(function (id) { decide(id, "approve"); });
      ctx.toast(ids.length + " request(s) approved");
    };
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && (document.getElementById("wfOverlay").classList.contains("open"))) closeAll();
    });
  }

  global.Workflows = {
    init: function (c) { ctx = c; seed(); ensureDom(); },
    openEdit: function (uid) { openForm(uid); },
    openCreate: function () { openForm(null); },
    openReview: function () { openReview(); },
    pendingCount: function () { return CRS.filter(function (c) { return c.status === "pending"; }).length; },
    all: function () { return CRS; }
  };
})(window);
