/* ============================================================
   Material Library — APP LOGIC
   Both modes read from the same MATERIAL_DB and share STATE.
   ============================================================ */
(function () {
  "use strict";

  var DB = window.MATERIAL_DB;
  var ALL = DB.materials.slice();

  // shared state across both modes
  var STATE = {
    mode: "admin",
    query: "",
    filters: {},          // { category:'...', brand:'...', supplier:'...' }
    sort: "relevance",
    selectedUid: null,    // currently opened material
    selectedRows: {},     // admin staging selection { uid:true }
    favorites: {},        // browse saved { uid:true }
    sheetOpen: false
  };

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  /* ---------- helpers ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function swatchClass(m) { return "swatch sw-" + (m.swatch || "metal-nickel"); }
  function statusMeta(s) {
    return ({
      active:    { cls: "ok",     label: "Active" },
      review:    { cls: "warn",   label: "Needs review" },
      duplicate: { cls: "danger", label: "Duplicate?" }
    })[s] || { cls: "ok", label: s };
  }
  function priceHtml(m) {
    return m.unitCost == null
      ? '<span class="price na">On request</span>'
      : '<span class="price">$' + m.unitCost.toFixed(2) + ' <small>/' + esc(m.unit) + '</small></span>';
  }

  /* Smart Size label: boards = thickness + sheet size, hardware = model + key dimension. */
  function sizeLabel(m) {
    var v = m.variants || {};
    var cat = m.category || "";
    var parts = [];
    if (cat.indexOf("board.") === 0 || cat === "edging") {
      // panels & edging → dimensions
      if (v.thickness) parts.push(v.thickness);
      if (v.size) parts.push(v.size);
      if (v.width) parts.push(v.width + (v.length ? " × " + v.length : ""));
      if (parts.length === 0 && v.volume) parts.push(v.volume);
    } else {
      // hardware / fittings / adhesive → model no. + key spec
      if (m.modelNo) parts.push(m.modelNo);
      ["length", "opening", "angle", "size", "load", "volume", "fixing", "boss", "finish"].some(function (k) {
        if (v[k]) { parts.push(v[k]); return true; }
        return false;
      });
    }
    return parts.length ? parts.join(" · ") : "—";
  }

  /* Product link block used in both detail views. */
  function productLinkHtml(m) {
    var url = m.productUrl || "";
    var host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch (e) { host = url; }
    return '<div class="sec"><h4>Product link</h4>' +
      '<a class="product-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' +
      '<span class="pl-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7"/></svg></span>' +
      '<span class="pl-text"><b>' + esc(host || "Open product page") + '</b><small>Reference page · opens in new tab</small></span>' +
      '<span class="pl-arrow">↗</span></a></div>';
  }
  function highlight(name, q) {
    if (!q) return esc(name);
    var i = name.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return esc(name);
    return esc(name.slice(0, i)) + "<mark style='background:var(--accent-soft);color:var(--accent-d);border-radius:3px'>" +
      esc(name.slice(i, i + q.length)) + "</mark>" + esc(name.slice(i + q.length));
  }
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  /* ---------- filtering / sorting ---------- */
  function getFiltered() {
    var q = STATE.query.trim().toLowerCase();
    var list = ALL.filter(function (m) {
      // text search across name/brand/supplier/category/tags
      if (q) {
        var hay = [m.name, m.brand, m.supplier, m.category, m.categoryLabel,
          (m.tags || []).join(" "), (m.variants && Object.values(m.variants).join(" "))].join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      // facet filters (AND across groups)
      for (var k in STATE.filters) {
        if (STATE.filters[k] && String(m[k]) !== String(STATE.filters[k])) return false;
      }
      return true;
    });
    var s = STATE.sort;
    if (s === "name") list.sort(function (a, b) { return a.name.localeCompare(b.name); });
    else if (s === "cost") list.sort(function (a, b) { return (a.unitCost == null ? 1e9 : a.unitCost) - (b.unitCost == null ? 1e9 : b.unitCost); });
    else if (s === "confidence") list.sort(function (a, b) { return b.confidence - a.confidence; });
    else if (s === "updated") list.sort(function (a, b) { return parseInt(b.updated) || 0 - (parseInt(a.updated) || 0); });
    return list;
  }
  function countFor(facetKey, value) {
    return ALL.filter(function (m) {
      var othersMatch = true;
      for (var k in STATE.filters) {
        if (k === facetKey) continue;
        if (STATE.filters[k] && String(m[k]) !== String(STATE.filters[k])) othersMatch = false;
      }
      return othersMatch && String(m[facetKey]) === String(value);
    }).length;
  }
  function byUid(uid) {
    return ALL.filter(function (m) { return m.uid === uid; })[0];
  }

  /* ---------- FACET SIDEBAR ---------- */
  function renderFacets() {
    var groups = [
      { key: "category", title: "Category", values: DB.meta.categories, label: function (v) {
          return ({ "board.melamine.mdf": "Board / Melamine", "board.veneer": "Board / Veneer",
            "board.particleboard": "Board / Particle", "board.hardboard": "Board / Hardboard",
            "hardware.hinge": "Hardware / Hinge", "hardware.runner": "Hardware / Runner",
            "edging": "Edging", "adhesive": "Adhesive", "hardware.fitting": "Hardware / Fitting" })[v] || v;
        } },
      { key: "brand", title: "Brand", values: DB.meta.brands },
      { key: "supplier", title: "Supplier", values: DB.meta.suppliers },
      { key: "status", title: "Data quality", values: ["active", "review", "duplicate"], label: function (v) {
          return ({ active: "Active", review: "Needs review", duplicate: "Possible duplicate" })[v];
        } }
    ];
    var html = "";
    groups.forEach(function (g) {
      html += '<div class="fgroup"><h4>' + esc(g.title) + '</h4>';
      g.values.forEach(function (v) {
        var on = String(STATE.filters[g.key] || "") === String(v);
        var label = g.label ? g.label(v) : v;
        html += '<div class="facet' + (on ? " on" : "") + '" data-key="' + g.key + '" data-val="' + esc(v) + '">' +
          '<span class="cb">' + (on ? '<svg viewBox="0 0 12 12" fill="none"><path d="M2 6l2.5 2.5L10 3" stroke="#fff" stroke-width="2"/></svg>' : "") + '</span>' +
          '<span class="nm">' + esc(label) + '</span><span class="ct">' + countFor(g.key, v) + '</span></div>';
      });
      html += "</div>";
    });
    $("#facetGroups").innerHTML = html;
    $$("#facetGroups .facet").forEach(function (el) {
      el.addEventListener("click", function () {
        var k = el.dataset.key, v = el.dataset.val;
        STATE.filters[k] = (String(STATE.filters[k] || "") === v) ? null : v;
        renderAll();
      });
    });
  }

  /* ---------- FILTER CHIPS ---------- */
  function renderChips(container, withAdd) {
    var parts = [];
    Object.keys(STATE.filters).forEach(function (k) {
      var v = STATE.filters[k];
      if (!v) return;
      parts.push('<span class="chip">' + esc(k) + ": " + esc(v) +
        ' <span class="x" data-key="' + k + '"><svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 3l6 6M9 3l-6 6"/></svg></span></span>');
    });
    if (withAdd && parts.length === 0) parts.push('<span class="chip add">+ Add filter</span>');
    container.innerHTML = parts.join("");
    $$(".chip .x", container).forEach(function (el) {
      el.addEventListener("click", function () { STATE.filters[el.dataset.key] = null; renderAll(); });
    });
  }

  /* ---------- ADMIN TABLE ---------- */
  function renderAdmin(list) {
    $("#adminCount").textContent = list.length;
    $("#adminRange").textContent = list.length ? "1–" + Math.min(list.length, 36) + " of " + list.length : "0 results";
    var nSel = Object.keys(STATE.selectedRows).filter(function (k) { return STATE.selectedRows[k]; }).length;
    $("#selPill").textContent = nSel + " selected";
    $("#selPill").classList.toggle("show", nSel > 0);
    $("#stageCount").textContent = nSel + " selected";

    var rows = list.map(function (m) {
      var sm = statusMeta(m.status);
      var isSel = !!STATE.selectedRows[m.uid];
      return '<tr data-uid="' + m.uid + '" class="' + (isSel ? "sel" : "") + '">' +
        '<td><span class="cb2" data-cb="' + m.uid + '"></span></td>' +
        '<td><div class="mat"><div class="' + swatchClass(m) + '"></div><div class="txt">' +
          '<div class="nm">' + highlight(m.name, STATE.query) + '</div><div class="uid">' + m.uid + '</div></div></div></td>' +
        '<td><span class="tag">' + esc(m.category) + '</span></td>' +
        '<td>' + esc(m.brand) + '</td>' +
        '<td><span class="size-cell">' + esc(sizeLabel(m)) + '</span></td>' +
        '<td><div class="conf"><span class="bar"><i style="width:' + m.confidence + '%"></i></span>' + m.confidence + '%</div></td>' +
        '<td><span class="badge ' + sm.cls + '"><i></i>' + sm.label + '</span></td>' +
        '<td>' + esc(m.updated) + '</td></tr>';
    }).join("");
    $("#adminRows").innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--ink-3);padding:28px">No materials match your filters.</td></tr>';

    $$("#adminRows tr[data-uid]").forEach(function (tr) {
      tr.addEventListener("click", function (e) {
        if (e.target.dataset.cb) return; // handled below
        openDetail(tr.dataset.uid);
      });
    });
    $$("#adminRows .cb2").forEach(function (cb) {
      cb.addEventListener("click", function (e) {
        e.stopPropagation();
        var uid = cb.dataset.cb;
        STATE.selectedRows[uid] = !STATE.selectedRows[uid];
        renderAdmin(getFiltered());
      });
    });
  }

  /* ---------- DETAIL PANEL (admin) ---------- */
  function detailHtml(m) {
    var sm = statusMeta(m.status);
    var vHtml = Object.keys(m.variants || {}).map(function (k) {
      return '<span class="vchip"><b>' + esc(k) + '</b> &nbsp; ' + esc(m.variants[k]) + '</span>';
    }).join("");
    return '<div class="dhero"><div class="' + swatchClass(m) + '"></div><div>' +
      '<h2>' + esc(m.name) + '</h2><div class="u">' + m.uid + '</div>' +
      '<div class="badges"><span class="badge ' + sm.cls + '"><i></i>' + sm.label + '</span>' +
      '<span class="badge ok"><i></i>' + m.confidence + '% confidence</span></div></div></div>' +
      '<div class="dtabs"><a class="active">Details</a><a>Variants</a><a>History</a><a>Relations</a></div>' +
      '<div class="sec"><h4>Classification</h4><div class="kv">' +
      '<span class="k">Category</span><span class="v">' + esc(m.categoryLabel) + '</span>' +
      '<span class="k">Brand</span><span class="v">' + esc(m.brand) + '</span>' +
      '<span class="k">Model no.</span><span class="v">' + esc(m.modelNo) + '</span>' +
      '<span class="k">Family</span><span class="v">' + esc(m.family) + '</span></div></div>' +
      '<div class="sec"><h4>Variants</h4><div class="vgroup">' + (vHtml || '<span class="tag">—</span>') + '</div></div>' +
      '<div class="sec"><h4>Size &amp; supply</h4><div class="size-hero">' + esc(sizeLabel(m)) + '</div>' +
      '<div class="kv" style="margin-top:10px"><span class="k">Supplier</span><span class="v">' + esc(m.supplier) + '</span>' +
      '<span class="k">Unit</span><span class="v">' + esc(m.unit) + '</span></div>' +
      '<div class="price-ref"><span class="pr-lbl">Unit cost (reference only · subject to change)</span>' +
      '<span class="pr-val">' + (m.unitCost == null ? "On request" : "$" + m.unitCost.toFixed(2) + " / " + esc(m.unit)) + '</span></div></div>' +
      supplierQuickHtml(m) +
      productLinkHtml(m) +
      '<div class="sec"><h4>Provenance</h4><div class="kv">' +
      '<span class="k">Source</span><span class="v">' + esc(m.source) + '</span>' +
      '<span class="k">Confidence</span><span class="v" style="color:var(--teal)">' + m.confidence + '%</span>' +
      '<span class="k">Updated</span><span class="v">' + esc(m.updated) + '</span></div></div>' +
      '<div class="sec"><h4>Recent activity</h4><div class="timeline">' +
      '<div>Created from supplier feed <span>· ' + esc(m.updated) + '</span></div>' +
      '<div>Mapped to canonical family <span>· ' + esc(m.updated) + '</span></div></div></div>';
  }
  function openDetail(uid) {
    var m = byUid(uid);
    if (!m) return;
    STATE.selectedUid = uid;
    $("#detailBody").innerHTML = detailHtml(m);
    bindSupplierQuick("#detailBody");
    $$("#detailPanel tbody tr").forEach(function () {}); // no-op
    $$("#adminRows tr").forEach(function (tr) { tr.classList.toggle("sel", tr.dataset.uid === uid && !STATE.selectedRows[uid] ? false : tr.classList.contains("sel")); });
  }

  /* ---------- BROWSE CATEGORIES ---------- */
  function renderBrowseCats() {
    var cats = [
      { v: null, label: "All materials", icon: "▦" },
      { v: "board.melamine.mdf", label: "Boards", icon: "▤" },
      { v: "hardware.hinge", label: "Hardware", icon: "⚙" },
      { v: "hardware.runner", label: "Runners", icon: "▭" },
      { v: "edging", label: "Edging", icon: "◧" },
      { v: "adhesive", label: "Adhesive", icon: "◈" }
    ];
    $("#browseCats").innerHTML = cats.map(function (c) {
      var on = c.v === null ? !STATE.filters.category : STATE.filters.category === c.v;
      var count = c.v === null ? ALL.length : countFor("category", c.v);
      return '<div class="cat' + (on ? " on" : "") + '" data-v="' + (c.v || "") + '">' +
        '<span class="ic" style="background:var(--accent-soft)">' + c.icon + '</span>' + c.label +
        '<span class="n">' + count + '</span></div>';
    }).join("");
    $$("#browseCats .cat").forEach(function (el) {
      el.addEventListener("click", function () {
        STATE.filters.category = el.dataset.v || null;
        renderAll();
      });
    });
  }

  /* ---------- GALLERY ---------- */
  function renderGallery(list) {
    $("#browseCount").textContent = list.length;
    var html = list.map(function (m) {
      var sm = statusMeta(m.status);
      var fav = !!STATE.favorites[m.uid];
      var varTags = Object.keys(m.variants || {}).slice(0, 2).map(function (k) {
        return '<span class="v">' + esc(m.variants[k]) + '</span>';
      }).join("");
      return '<div class="card' + (STATE.selectedUid === m.uid && STATE.sheetOpen ? " sel" : "") + '" data-uid="' + m.uid + '">' +
        '<div class="' + swatchClass(m) + '"><span class="badge">' + esc(m.categoryLabel.split(" / ")[0].toUpperCase()) + '</span>' +
        '<span class="fav' + (fav ? " on" : "") + '" data-fav="' + m.uid + '"><svg viewBox="0 0 24 24" fill="' + (fav ? "currentColor" : "none") + '" stroke="currentColor" stroke-width="1.8"><path d="M12 21s-8-4.5-8-10a4.5 4.5 0 018-2.8A4.5 4.5 0 0120 11c0 5.5-8 10-8 10z"/></svg></span></div>' +
        '<div class="cbody"><div class="br">' + esc(m.brand) + '</div><h3>' + esc(m.name) + '</h3>' +
        '<div class="cmeta">' + varTags + '</div>' +
        '<div class="cfoot"><span class="card-size" title="Size">' + esc(sizeLabel(m)) + '</span>' +
        '<span class="st ' + (sm.cls === "ok" ? "" : sm.cls) + '"><i></i>' + sm.label + '</span></div></div></div>';
    }).join("");
    $("#gallery").innerHTML = html || '<div style="color:var(--ink-3);padding:28px;grid-column:1/-1">No materials match your filters.</div>';

    $$("#gallery .card").forEach(function (card) {
      card.addEventListener("click", function (e) {
        if (e.target.closest("[data-fav]")) return;
        openSheet(card.dataset.uid);
      });
    });
    $$("#gallery [data-fav]").forEach(function (f) {
      f.addEventListener("click", function (e) {
        e.stopPropagation();
        var uid = f.dataset.fav;
        STATE.favorites[uid] = !STATE.favorites[uid];
        renderGallery(getFiltered());
        toast(STATE.favorites[uid] ? "Saved to your list" : "Removed from your list");
      });
    });
  }

  /* ---------- DETAIL SHEET (browse) ---------- */
  function sheetHtml(m) {
    var sm = statusMeta(m.status);
    var opts = Object.keys(m.variants || {}).map(function (k) {
      return '<span class="k">' + esc(k) + '</span><span class="v">' + esc(m.variants[k]) + '</span>';
    }).join("");
    return '<div class="bigsw ' + swatchClass(m) + '" style="border-radius:14px;border:0"><span class="tagn">' + esc(m.name) + '</span></div>' +
      '<h2>' + esc(m.name) + '</h2><div class="u">' + m.uid + '</div>' +
      '<div class="pillrow"><span class="badge ' + sm.cls + '"><i></i>' + sm.label + '</span>' +
      '<span class="badge ok"><i></i>' + m.confidence + '% verified</span>' +
      '<span class="tag">' + esc(m.categoryLabel) + '</span></div>' +
      '<div class="sh-sec"><h4>Options &amp; variants</h4><div class="kv">' + (opts || '<span class="k">—</span><span class="v">Standard</span>') + '</div></div>' +
      '<div class="sh-sec"><h4>Size &amp; supply</h4><div class="size-hero">' + esc(sizeLabel(m)) + '</div>' +
      '<div class="kv" style="margin-top:10px"><span class="k">Supplier</span><span class="v">' + esc(m.supplier) + '</span>' +
      '<span class="k">Unit</span><span class="v">' + esc(m.unit) + '</span></div>' +
      '<div class="price-ref"><span class="pr-lbl">Unit cost (reference only · subject to change)</span>' +
      '<span class="pr-val">' + (m.unitCost == null ? "On request" : "$" + m.unitCost.toFixed(2) + " / " + esc(m.unit)) + '</span></div></div>' +
      '<div class="sheet-product">' + supplierQuickHtml(m).replace('<div class="sec">', '<div>') + '</div>' +
      '<div class="sheet-product">' + productLinkHtml(m).replace('<div class="sec">', '<div>') + '</div>' +
      '<div class="sh-sec"><h4>About this material</h4><div style="font-size:12px;color:var(--ink-2);line-height:1.6">' + esc(m.description) + '</div></div>' +
      '<div class="sh-sec"><h4>Where it is used</h4><div class="kv">' +
      '<span class="k">Family</span><span class="v">' + esc(m.family) + '</span>' +
      '<span class="k">Model no.</span><span class="v">' + esc(m.modelNo) + '</span></div></div>';
  }
  function openSheet(uid) {
    var m = byUid(uid);
    if (!m) return;
    STATE.selectedUid = uid;
    STATE.sheetOpen = true;
    $("#sheetBody").innerHTML = sheetHtml(m);
    bindSupplierQuick("#sheetBody");
    $("#sheetFav").textContent = STATE.favorites[uid] ? "♥ Saved" : "♡ Save";
    $("#sheet").classList.add("open");
    renderGallery(getFiltered());
  }
  function closeSheet() {
    STATE.sheetOpen = false;
    $("#sheet").classList.remove("open");
    renderGallery(getFiltered());
  }

  /* ---------- render all ---------- */
  function renderAll() {
    var list = getFiltered();
    renderFacets();
    renderChips($("#adminChips"), true);
    renderChips($("#browseChips"), false);
    renderAdmin(list);
    renderBrowseCats();
    renderGallery(list);
    // stats: pending approval queue (live) vs data-quality review status
    var pending = window.Workflows ? window.Workflows.pendingCount() : 0;
    var reviewStatus = ALL.filter(function (m) { return m.status === "review"; }).length;
    $("#reviewCount").textContent = pending;
    $("#openReviews").textContent = pending;
    $("#bReview").textContent = reviewStatus;
    if ($("#bPending")) $("#bPending").textContent = pending;
    if (STATE.selectedUid) {
      var m = byUid(STATE.selectedUid);
      if (m) { $("#detailBody").innerHTML = detailHtml(m); bindSupplierQuick("#detailBody"); }
    }
    renderSuppliers();
  }

  /* ---------- SUPPLIERS (direct edit — no approval queue) ---------- */
  function linkedCount(name) {
    return ALL.filter(function (m) { return m.supplier === name; }).length;
  }
  function findSupplier(name) {
    return (DB.suppliers || []).filter(function (s) { return s.name === name; })[0] || null;
  }
  /* Supplier contact quick-card used in both material detail views. */
  function supplierQuickHtml(m) {
    var s = findSupplier(m.supplier);
    if (!s) {
      return '<div class="sec"><h4>Supplier contact</h4><div class="sqNone">' +
        'No supplier record for “' + esc(m.supplier || "—") + '”. ' +
        '<button class="sqLink" data-gosup="new">Add supplier →</button></div></div>';
    }
    var initials = String(s.name || "?").split(/\s+/).slice(0, 2).map(function (w) { return w.charAt(0); }).join("").toUpperCase();
    return '<div class="sec"><h4>Supplier contact <span class="sqTag">linked</span></h4>' +
      '<div class="supQuick" data-sup="' + s.id + '">' +
        '<div class="sqTop"><span class="sqAv">' + esc(initials) + '</span>' +
          '<div class="sqId"><b>' + esc(s.name) + '</b><small>' + s.id + ' · ' + esc(s.country || "—") + '</small></div>' +
          '<button class="sqGo" data-gosup="' + s.id + '">View →</button></div>' +
        '<div class="sqGrid"><div><span>Contact</span><b>' + esc(s.contact || "—") + '</b></div>' +
          '<div><span>Lead time</span><b>' + (s.leadTimeDays != null ? s.leadTimeDays + " days" : "—") + '</b></div>' +
          '<div class="sqFull"><span>Email</span><b>' + (s.email ? '<a href="mailto:' + esc(s.email) + '">' + esc(s.email) + '</a>' : "—") + '</b></div>' +
          '<div class="sqFull"><span>Payment terms</span><b>' + esc(s.paymentTerms || "—") + '</b></div></div>' +
      '</div></div>';
  }
  function bindSupplierQuick(rootSel) {
    $$(rootSel + " [data-gosup]").forEach(function (b) {
      b.onclick = function () {
        var id = b.dataset.gosup;
        if (id === "new") { switchMode("suppliers"); openSupplierForm(null); return; }
        var s = (DB.suppliers || []).filter(function (x) { return x.id === id; })[0];
        if (s) {
          STATE.query = s.name; $("#globalSearch").value = s.name;
          switchMode("suppliers"); toast("Showing supplier: " + s.name);
        }
      };
    });
  }
  function renderSuppliers() {
    var sups = DB.suppliers || [];
    var q = STATE.query.trim().toLowerCase();
    var shown = q ? sups.filter(function (s) {
      return [s.name, s.id, s.contact, s.email, s.country, s.paymentTerms, s.notes].join(" ").toLowerCase().indexOf(q) >= 0;
    }) : sups;
    var totalLinked = sups.reduce(function (s, x) { return s + linkedCount(x.name); }, 0);
    var unassigned = ALL.filter(function (m) { return !m.supplier || m.supplier === "(none)"; }).length;
    var countries = sups.map(function (s) { return s.country; }).filter(function (v, i, a) { return a.indexOf(v) === i; });
    var avgLead = sups.length ? Math.round(sups.reduce(function (s, x) { return s + (Number(x.leadTimeDays) || 0); }, 0) / sups.length) : 0;
    $("#supStats").innerHTML =
      '<div class="supStat"><div class="k">Suppliers</div><div class="v a">' + sups.length + '</div><div class="sub">Active master records</div></div>' +
      '<div class="supStat"><div class="k">Linked materials</div><div class="v g">' + totalLinked + '</div><div class="sub">of ' + ALL.length + ' catalogue records</div></div>' +
      '<div class="supStat"><div class="k">Unassigned</div><div class="v w">' + unassigned + '</div><div class="sub">Materials with no supplier</div></div>' +
      '<div class="supStat"><div class="k">Avg. lead time</div><div class="v">' + avgLead + ' <span style="font-size:12px;color:var(--ink-3);font-weight:500">days</span></div><div class="sub">' + countries.length + ' countries</div></div>';
    $("#supCount").textContent = shown.length + (q && shown.length !== sups.length ? " / " + sups.length : "");
    if (!shown.length) {
      $("#supRows").innerHTML = '<tr><td colspan="7" class="supEmpty">' +
        (q ? "No suppliers match “" + esc(STATE.query) + "”." : "No suppliers yet — add one to get started.") + '</td></tr>';
      return;
    }
    $("#supRows").innerHTML = shown.map(function (s) {
      var n = linkedCount(s.name);
      return '<tr><td><div class="sname">' + esc(s.name) + '</div><div class="sid">' + s.id + '</div></td>' +
        '<td><div class="ctact">' + esc(s.contact || "—") + '</div><div class="em">' + esc(s.email || "") + '</div></td>' +
        '<td>' + esc(s.country || "—") + '</td>' +
        '<td>' + (s.leadTimeDays != null ? s.leadTimeDays + " days" : "—") + '</td>' +
        '<td>' + esc(s.paymentTerms || "—") + '</td>' +
        '<td><span class="linkn' + (n === 0 ? " zero" : "") + '">' + n + " material" + (n === 1 ? "" : "s") + '</span></td>' +
        '<td style="text-align:right"><button class="rowbtn" data-edit-sup="' + s.id + '">Edit</button></td></tr>';
    }).join("");
    $$("#supRows [data-edit-sup]").forEach(function (b) {
      b.addEventListener("click", function () { openSupplierForm(b.dataset.editSup); });
    });
  }
  var SUP_FIELDS = [
    { key: "name", label: "Supplier name", type: "text", required: true, full: true },
    { key: "contact", label: "Contact person", type: "text" },
    { key: "email", label: "Email", type: "email" },
    { key: "country", label: "Country", type: "text" },
    { key: "leadTimeDays", label: "Lead time (days)", type: "number" },
    { key: "paymentTerms", label: "Payment terms", type: "text" },
    { key: "website", label: "Website", type: "url", full: true },
    { key: "notes", label: "Notes", type: "textarea", full: true }
  ];
  function openSupplierForm(id) {
    var editing = !!id;
    var s = editing ? (DB.suppliers.filter(function (x) { return x.id === id; })[0]) : null;
    if (editing && !s) return toast("Supplier not found");
    $("#supModalTitle").textContent = editing ? "Edit supplier" : "Add supplier";
    var html = '<div class="grid2">' + SUP_FIELDS.map(function (f) {
      var v = editing ? (s[f.key] == null ? "" : s[f.key]) : (f.key === "country" ? "Australia" : (f.key === "paymentTerms" ? "30 days" : ""));
      var req = f.required ? ' <span class="req">*</span>' : "";
      var input;
      if (f.type === "textarea") input = '<textarea data-sf="' + f.key + '">' + esc(v) + "</textarea>";
      else input = '<input type="' + (f.type === "number" ? "number" : f.type) + '"' + (f.type === "number" ? ' min="0"' : "") + ' data-sf="' + f.key + '" value="' + esc(v) + '">';
      return '<div class="field' + (f.full ? " full" : "") + '" data-swrap="' + f.key + '"><label>' + f.label + req + "</label>" + input +
        '<span class="emsg">Required field</span></div>';
    }).join("") + "</div>";
    if (editing) {
      var n = linkedCount(s.name);
      html += '<div class="price-ref" style="margin-top:4px"><span class="pr-lbl">Linked materials (computed from catalogue)</span><span class="pr-val">' + n + " material" + (n === 1 ? "" : "s") + '</span></div>';
    }
    $("#supFormBody").innerHTML = html;
    $("#supFormBody")._editId = id || null;
    $("#supModal").classList.add("open");
    $("#wfOverlay").classList.add("open");
  }
  function closeSupplierForm() {
    $("#supModal").classList.remove("open");
    if (!$("#wfFormModal").classList.contains("open") && !$("#wfRevModal").classList.contains("open")) $("#wfOverlay").classList.remove("open");
  }
  function saveSupplier() {
    var get = function (k) { return $("#supFormBody").querySelector('[data-sf="' + k + '"]').value.trim(); };
    $$("#supFormBody .field").forEach(function (w) { w.classList.remove("err"); });
    if (!get("name")) {
      $("#supFormBody").querySelector('[data-swrap="name"]').classList.add("err");
      return toast("Supplier name is required");
    }
    var id = $("#supFormBody")._editId;
    var data = {
      name: get("name"), contact: get("contact"), email: get("email"), country: get("country"),
      leadTimeDays: get("leadTimeDays") === "" ? null : parseInt(get("leadTimeDays"), 10),
      paymentTerms: get("paymentTerms"), website: get("website"), notes: get("notes")
    };
    if (id) {
      var s = DB.suppliers.filter(function (x) { return x.id === id; })[0];
      var oldName = s.name;
      Object.keys(data).forEach(function (k) { s[k] = data[k]; });
      // rename propagation: if the supplier name changed, update linked materials and meta
      if (oldName !== data.name) {
        ALL.forEach(function (m) { if (m.supplier === oldName) m.supplier = data.name; });
        var mi = DB.meta.suppliers.indexOf(oldName);
        if (mi >= 0) DB.meta.suppliers[mi] = data.name;
      }
      toast("Supplier " + data.name + " updated");
    } else {
      var newId = "SUP-" + String(DB.suppliers.length + 1).padStart(3, "0");
      DB.suppliers.unshift(Object.assign({ id: newId }, data));
      if (DB.meta.suppliers.indexOf(data.name) < 0) DB.meta.suppliers.push(data.name);
      toast("Supplier " + data.name + " added");
    }
    closeSupplierForm();
    renderAll();
  }
  $("#addSupplier").addEventListener("click", function () { openSupplierForm(null); });
  $("#supModalX").addEventListener("click", closeSupplierForm);
  $("#supCancel").addEventListener("click", closeSupplierForm);
  $("#supSave").addEventListener("click", saveSupplier);
  $("#supExport").addEventListener("click", function () { toast("Exported " + DB.suppliers.length + " suppliers"); });

  /* ---------- events ---------- */
  function switchMode(mode) {
    STATE.mode = mode;
    var isSup = mode === "suppliers";
    $$("#modeSwitch button").forEach(function (b) { b.classList.toggle("active", !isSup && b.dataset.mode === mode); });
    $("#pane-admin").classList.toggle("active", mode === "admin");
    $("#pane-browse").classList.toggle("active", mode === "browse");
    $("#pane-suppliers").classList.toggle("active", isSup);
    $$(".topnav a").forEach(function (a) {
      a.classList.toggle("active", a.dataset.nav === (isSup ? "Suppliers" : "Materials"));
    });
    if (mode === "admin") closeSheet();
    if (isSup) renderSuppliers();
  }
  $$("#modeSwitch button").forEach(function (b) {
    b.addEventListener("click", function () { switchMode(b.dataset.mode); });
  });

  function bindSearch(input) {
    input.addEventListener("input", function () {
      STATE.query = input.value;
      $("#globalSearch").value = input.value;
      renderAll();
    });
  }
  bindSearch($("#globalSearch"));
  bindSearch($("#adminSearch"));

  $("#adminSort").addEventListener("change", function (e) { STATE.sort = e.target.value; renderAll(); });
  $("#browseSort").addEventListener("change", function (e) { STATE.sort = e.target.value; renderAll(); });

  $("#clearFilters").addEventListener("click", function () { STATE.filters = {}; STATE.query = ""; $("#globalSearch").value = ""; $("#adminSearch").value = ""; renderAll(); });
  $("#closeDetail").addEventListener("click", function () { $("#detailPanel").style.visibility = "hidden"; });
  $("#closeSheet").addEventListener("click", closeSheet);

  $("#clearStage").addEventListener("click", function () { STATE.selectedRows = {}; renderAll(); toast("Staged edits cleared"); });
  $("#reviewChanges").addEventListener("click", function () { window.Workflows.openReview(); });
  $("#addMaterial").addEventListener("click", function () { window.Workflows.openCreate(); });
  $("#exportBtn").addEventListener("click", function () { toast("Exported " + getFiltered().length + " records"); });
  $("#crBtn").addEventListener("click", function () {
    if (!STATE.selectedUid) return toast("Open a material first");
    window.Workflows.openEdit(STATE.selectedUid);
  });
  $("#editBtn").addEventListener("click", function () {
    if (!STATE.selectedUid) return toast("Open a material first");
    window.Workflows.openEdit(STATE.selectedUid);
  });
  $("#sheetFav").addEventListener("click", function () {
    if (!STATE.selectedUid) return;
    STATE.favorites[STATE.selectedUid] = !STATE.favorites[STATE.selectedUid];
    $("#sheetFav").textContent = STATE.favorites[STATE.selectedUid] ? "♥ Saved" : "♡ Save";
    renderGallery(getFiltered());
  });
  $("#sheetRequest").addEventListener("click", function () {
    if (!STATE.selectedUid) return toast("Open a material first");
    window.Workflows.openEdit(STATE.selectedUid);
  });

  $$(".topnav a").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      if (a.dataset.nav === "Review") { window.Workflows.openReview(); return; }
      if (a.dataset.nav === "Suppliers") { switchMode("suppliers"); return; }
      if (a.dataset.nav === "Materials") { switchMode("admin"); return; }
      $$(".topnav a").forEach(function (x) { x.classList.remove("active"); });
      a.classList.add("active");
      toast(a.dataset.nav + " view");
    });
  });

  // keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); $("#globalSearch").focus();
    }
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") {
      e.preventDefault(); $("#adminSearch").focus();
    }
    if (e.key === "Escape") {
      closeSheet();
      if ($("#supModal").classList.contains("open")) closeSupplierForm();
    }
  });
  // init workflows (edit form + approval) with shared context
  window.Workflows.init({
    ALL: ALL,
    DB: DB,
    byUid: byUid,
    renderAll: renderAll,
    toast: toast,
    esc: esc,
    swatchClass: swatchClass,
    onStage: function (uid) { STATE.selectedRows[uid] = true; }
  });
  // now wfOverlay exists — close supplier form when the shared dim layer is clicked
  $("#wfOverlay").addEventListener("click", function () {
    if ($("#supModal").classList.contains("open")) closeSupplierForm();
  });

  // init
  $("#bTotal").textContent = ALL.length;
  STATE.selectedUid = ALL[0] ? ALL[0].uid : null;
  renderAll();
  // allow deep-linking to a mode: index.html?mode=browse
  var params = new URLSearchParams(location.search);
  if (params.get("mode") === "browse") switchMode("browse");
})();
