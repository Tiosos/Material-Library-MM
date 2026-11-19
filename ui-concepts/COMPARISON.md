# Material Library — UI design concepts (3 versions)

> A redesign of the **material master-data** interface for the cabinetmaking and woodworking industry
> Deliverables: three English static visual design drafts (PNG, 1600×1000) plus this comparison
> Scope: material master data only (classification, attributes, variants, brands, suppliers, prices, approvals) — **no live inventory quantities** (handled by a separate system)
> Goal: work well on both desktop and touch screens; attractive and familiar to users

---

## Research basis before design

- **Comparable products**: InvenTree (open-source inventory / master data with strong facets and tables), TimberCloud / CabinetShop Maestro (woodworking software focused on material selection and visualisation), modern data tools (the restrained typography and information hierarchy of Linear / Notion).
- **Current system**: a single blue theme, Edit/Add/Review tabs, a 210px sidebar plus a table; 322 materials, 9 categories, 26 facets, 20 units. The data itself is solid — what is missing is **information hierarchy, visual guidance and touch readability**.
- **User habits**: a material administrator's core loop is "find material → view detail → change data → go through approval"; a selector's core loop is "browse by appearance/brand → compare → confirm". The three concepts each strengthen a different part of that chain.

---

## Concept A — Flagship Workbench

**Positioning**: a modern SaaS data workbench. Facet filtering on the left, a dense table in the centre and a detail panel on the right — a four-column structure including a narrow icon rail.

### Design details
- **Layout**: icon rail (64px) → facet sidebar (248px) → main table (flexible) → detail panel (380px). Global search and primary navigation at the top; a dark "staged changes" action bar at the bottom.
- **Visuals**: cool white base + indigo accent (#4f46e5), restrained and professional; every material carries a **material colour swatch** so woodgrain / metal / board are recognisable at a glance.
- **Information design**: table columns Material / Category / Variants / Unit cost / Confidence / Status / Updated; data confidence is quantified with a progress bar; status uses colour badges (Active / Needs review / Duplicate?).
- **Workflow**: multi-select reveals the staging bar — "edits are not written directly; they take effect only after approval" makes the existing approval semantics visible. The right detail panel has four tabs: Details / Variants / History / Relations.
- **Trust cues**: top metrics "Valid records 98.4%" and "Open reviews 3" make data health visible.

### Strengths
- High information density — many fields at once, so **bulk tidying and reconciliation are fastest**.
- Facets + table follow the existing mental model, giving the **lowest learning cost**.
- Approval flow, confidence and provenance are all visible — the closest fit for "material master-data governance".
- Desktop-first efficiency: the right detail panel does not require leaving the list.

### Weaknesses
- Table-first, so it is **less intuitive for material selection** (swatches, not real appearance).
- On small or touch screens the four columns compress into drawers; responsive behaviour is included, but touch is not as good as the card version.

### Best for
Administrator day-to-day governance, bulk editing, change approvals and data-quality clean-up — **the safest choice as the main system screen**.

---

## Concept B — Material Gallery

**Positioning**: a visual-first card gallery. Large swatches / material previews drive "select by appearance", with warm tones, a card grid and a right-hand detail drawer.

### Design details
- **Layout**: top bar + hero title + category pill strip + card grid (3 columns) + right slide-out detail panel (430px).
- **Visuals**: warm beige base (#f6f3ee) + terracotta accent (#b1552e), wood/warm material feel; large card swatches recreate woodgrain, brushed metal and black edging in CSS.
- **Card information**: brand (small label) / name / variant tags / price / Verified / Review status; favourite heart at the top right; a selected card gets an accent outline.
- **Detail drawer**: large preview + option variants + supply information + "Where it's used" relations, emphasising **material context** rather than a pile of fields.
- **Touch-friendly**: large card hit areas and horizontally scrollable category pills suit finger operation on tablets.

### Strengths
- **Most intuitive for selecting and viewing materials** — non-specialists get started quickly, matching the woodworking habit of "choose from a sample".
- Warm, branded visuals suit showing clients and designers.
- Best mobile / touch experience; the card grid adapts its column count automatically.
- Light interactions such as favourites and category pills reduce search effort.

### Weaknesses
- **Low information density** — fewer materials visible at once, so bulk editing and reconciliation are slow.
- Material previews are CSS swatches; real photographs would need extra image assets to maintain.
- Long names and many fields can be truncated on cards, and exact field comparison is weaker than in a table.

### Best for
Selection, sample review, catalogue browsing and use by non-technical colleagues or clients — **suited to a "browse end"**.

---

## Concept C — Command Bar

**Positioning**: a keyboard-driven, minimal, high-efficiency interface. Dark surface, a large centred search box, shortcuts, a compact result list and a right-hand shortcut / recent-access panel.

### Design details
- **Layout**: top bar with three mode tabs (Search ⌘1 / Add ⌘2 / Review ⌘3) + a focused central search area + result list + right shortcut/status panel + bottom status bar.
- **Visuals**: deep ink base (#0d1016) + blue-violet accent (#7c9cff) + mint/amber status colours; a monospace font reinforces the "command line" feel, and matched keywords are highlighted in results.
- **Interaction**: result rows carry number shortcuts (1–8), ↑↓ to move, ↵ to open, ⌘E to edit and ⌘D to mark duplicate; filters appear as removable `key:value` chips (brand:Blum, cat:hardware.hinge).
- **Efficiency cues**: "8 results · 8 ms" search timing, recent visits and pending items (3 to review / 4 suspected duplicates / 166 missing prices).
- **Mode switching**: search / add / review are one shortcut away, covering the administrator's frequent actions.

### Strengths
- **Maximum keyboard efficiency** — fastest lookup and entry for power users.
- Dark, low-distraction surface is comfortable over long sessions; keyword highlighting and timing feedback feel like a "professional tool".
- Three modes converge find / add / review into one surface, keeping the mental model unified.
- Most distinctive visually — the strongest "professional master-data system" character.

### Weaknesses
- **Highest learning cost** — shortcuts must be memorised; unfriendly to new or occasional users.
- Dark theme plus dense information reduces readability on small screens or in brightly lit workshops.
- No material appearance preview, so it is also unsuitable for "select by look".
- Shortcuts do not work on touch screens; it would degrade to a plain search box.

### Best for
Senior administrators, high-frequency entry/lookup and efficiency-first scenarios — **suited to an "expert mode / power-user interface"**.

---

## Side-by-side comparison

| Dimension | A Flagship Workbench | B Material Gallery | C Command Bar |
|---|:---:|:---:|:---:|
| Information density | ★★★★★ | ★★☆☆☆ | ★★★★☆ |
| Visual appeal | ★★★★☆ | ★★★★★ | ★★★★☆ |
| Ease of adoption | ★★★★★ | ★★★★★ | ★★☆☆☆ |
| Selection intuitiveness | ★★★☆☆ | ★★★★★ | ★☆☆☆☆ |
| Bulk edit / approval | ★★★★★ | ★★☆☆☆ | ★★★★☆ |
| Touch / mobile fit | ★★★☆☆ | ★★★★★ | ★★☆☆☆ |
| Desktop efficiency | ★★★★★ | ★★★☆☆ | ★★★★★ |
| Master-data governance | ★★★★★ | ★★★☆☆ | ★★★★★ |

### Decision guidance
1. **Only one version** → choose **A**: closest to the core goal of organising material master data, low learning cost, complete governance and the best desktop efficiency; it can replace the existing screen smoothly.
2. **Want both an admin end and a selection end** → combine **A + B**: A as the administrator workbench, B as the browse/selection end, sharing one data source.
3. **Want professional differentiation** → **A + C**: A as the default screen and C as a switchable "expert mode" for power users.

> All three concepts share the same data fields and approval semantics (staging → change request → approval), so they can be combined freely without splitting the data. When a live inventory system is connected later, a read-only "on hand" slot can be added to the detail panel / cards without changing the overall structure.

---

## File list

| File | Description |
|---|---|
| `concept-a-workbench.png` | Concept A design draft (Flagship Workbench) |
| `concept-b-gallery.png` | Concept B design draft (Material Gallery) |
| `concept-c-command.png` | Concept C design draft (Command Bar) |
| `concept-*.html` | Editable HTML source for each concept; re-render as needed |
