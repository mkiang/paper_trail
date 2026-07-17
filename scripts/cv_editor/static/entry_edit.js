/* Entry-edit form behavior (V20 B3, 2026-05-18 — extracted from
 * templates/entry_edit.html). All per-entry state arrives via
 * <script id="entry-edit-data" type="application/json">. The Jinja
 * template ships only data; this file owns logic.
 *
 * Shape of the JSON-block:
 *   {
 *     section_key:       "publications" | ...,
 *     primary_note_types: [...],
 *     all_note_types:     [...],
 *     note_type_label:    {type: label, ...},
 *     author_forms:       [...],      // optional (publications only)
 *     open_access_form:   {...},      // optional (publications only)
 *     notes_form:         [...],      // optional (publications only)
 *     simple_notes_form:  [...],      // optional (presentations / service)
 *     list_field_data:    {field: [...], ...},
 *     tracker_hosts:      [host, ...],
 *     routes: {
 *       altmetric_resolve: "/publications/altmetric/resolve",
 *       fetch_title:       "/publications/fetch_title",
 *     }
 *   }
 *
 * Editors mounted (conditional on the relevant root element being
 * present in the DOM):
 *   - Authors editor       (#authors-editor)
 *   - Open-access editor   (#oa-editor)
 *   - Typed notes editor   (#notes-editor)
 *   - Simple notes editor  (.simple-notes-editor)
 *   - String list editor   (.string-list-editor)
 *   - Audiences set        (.audiences-set)
 */
(function () {
  const DATA_EL = document.getElementById("entry-edit-data");
  if (!DATA_EL) return;  // no-op when not on an entry-edit page
  const DATA = JSON.parse(DATA_EL.textContent || "{}");

  const SECTION_KEY = DATA.section_key;
  const PRIMARY_TYPES = DATA.primary_note_types || [];
  const TYPE_LABEL = DATA.note_type_label || {};
  const ROUTES = DATA.routes || {};

  const CONTENT_FIELD = {
    commentary: "citation", letter: "citation", response: "citation",
    editorial: "text", contributions: "text", note: "text",
  };
  const TYPE_HINT = {
    commentary: "Citation: full bibliographic string of the commentary on this work. (legacy type)",
    letter: "Citation: full bibliographic string of the letter. (legacy type)",
    response: "Citation: full bibliographic string of the author response. (legacy type)",
    media: "Add each outlet by name. URL and 'hide outlet' are optional per outlet.",
    editorial: "Free text describing the editorial-board pick or similar honor. (legacy type)",
    contributions: "Free text describing this paper's authorship contributions. Default template (CRediT taxonomy) auto-fills on type select — delete what doesn't apply.",
    note: "Free text — generic note rendered as a sub-bullet.",
  };
  // Default CRediT-taxonomy template prefilled when a note becomes
  // type=contributions with an empty body. User deletes the items
  // that don't apply.
  const CONTRIBUTIONS_DEFAULT = (
    "Conceptualization; methodology; data curation; formal analysis; "
    + "investigation; software; validation; visualization; "
    + "writing --- original draft; writing --- review and editing; "
    + "supervision; project administration; funding acquisition."
  );

  function dropdownTypes(currentType) {
    const opts = PRIMARY_TYPES.slice();
    if (currentType && !opts.includes(currentType)) opts.unshift(currentType);
    return opts;
  }

  function escapeAttr(s) { return String(s == null ? "" : s).replace(/"/g, "&quot;"); }
  function escapeHtml(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  // ---------------- Authors editor (publications only) ----------------
  if (document.getElementById("authors-editor")) {
    let authors = (DATA.author_forms || []).slice();
    if (authors.length === 0) authors.push({name: "", co_first: false, co_senior: false, group_authorship: false});
    const authorsEditor = document.getElementById("authors-editor");
    const authorsHidden = document.getElementById("authors-json");

    function renderAuthors() {
      authorsEditor.innerHTML = "";
      authors.forEach((a, i) => {
        const row = document.createElement("div");
        row.className = "author-row";
        row.draggable = true;
        row.dataset.i = i;
        row.innerHTML = `
          <span class="drag-handle" title="Drag to reorder">⋮⋮</span>
          <input type="text" class="author-name" data-i="${i}" value="${escapeAttr(a.name)}" placeholder="Surname I (or compound surname)" tabindex="0">
          <label title="Marked as a co-first author (renders † superscript and footnote sentence)."><input type="checkbox" class="author-cofirst" data-i="${i}" ${a.co_first ? "checked" : ""}> co-first (†)</label>
          <label title="Marked as a co-senior author (renders ‡ superscript and footnote sentence)."><input type="checkbox" class="author-cosenior" data-i="${i}" ${a.co_senior ? "checked" : ""}> co-senior (‡)</label>
          <label title="Corporate / consortium / working-group author (e.g. Example Consortium, X Working Group, Y Collaborative). Renders with ◊ superscript and footnote sentence."><input type="checkbox" class="author-grpauth" data-i="${i}" ${a.group_authorship ? "checked" : ""}> group author (◊)</label>
          <button type="button" class="row-btn move-up" data-i="${i}" title="Move up">&uarr;</button>
          <button type="button" class="row-btn move-down" data-i="${i}" title="Move down">&darr;</button>
          <button type="button" class="row-btn remove" data-i="${i}" title="Remove">&times;</button>`;
        authorsEditor.appendChild(row);
      });
      bindAuthorEvents();
      bindAuthorDragEvents();
      authorsHidden.value = JSON.stringify(authors);
    }

    let dragSrcIdx = null;
    function bindAuthorDragEvents() {
      authorsEditor.querySelectorAll(".author-row").forEach(row => {
        row.addEventListener("dragstart", e => {
          dragSrcIdx = +row.dataset.i;
          row.classList.add("dragging");
          if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
        });
        row.addEventListener("dragend", () => {
          row.classList.remove("dragging");
          authorsEditor.querySelectorAll(".author-row").forEach(r => r.classList.remove("drop-target"));
          dragSrcIdx = null;
        });
        row.addEventListener("dragover", e => {
          e.preventDefault();
          if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
          authorsEditor.querySelectorAll(".author-row").forEach(r => r.classList.remove("drop-target"));
          row.classList.add("drop-target");
        });
        row.addEventListener("drop", e => {
          e.preventDefault();
          const dst = +row.dataset.i;
          if (dragSrcIdx === null || dst === dragSrcIdx) return;
          const [moved] = authors.splice(dragSrcIdx, 1);
          authors.splice(dst, 0, moved);
          renderAuthors();
        });
      });
    }
    function bindAuthorEvents() {
      authorsEditor.querySelectorAll(".author-name").forEach(el => {
        el.addEventListener("input", e => { authors[+e.target.dataset.i].name = e.target.value; authorsHidden.value = JSON.stringify(authors); });
        el.addEventListener("keydown", e => {
          if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
            e.preventDefault();
            const i = +e.target.dataset.i;
            if (e.key === "ArrowUp" && i > 0) authorSwap(i, i - 1);
            else if (e.key === "ArrowDown" && i < authors.length - 1) authorSwap(i, i + 1);
          }
        });
      });
      authorsEditor.querySelectorAll(".author-cofirst").forEach(el => {
        el.addEventListener("change", e => { authors[+e.target.dataset.i].co_first = e.target.checked; authorsHidden.value = JSON.stringify(authors); });
      });
      authorsEditor.querySelectorAll(".author-cosenior").forEach(el => {
        el.addEventListener("change", e => { authors[+e.target.dataset.i].co_senior = e.target.checked; authorsHidden.value = JSON.stringify(authors); });
      });
      authorsEditor.querySelectorAll(".author-grpauth").forEach(el => {
        el.addEventListener("change", e => { authors[+e.target.dataset.i].group_authorship = e.target.checked; authorsHidden.value = JSON.stringify(authors); });
      });
      authorsEditor.querySelectorAll(".move-up").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i; if (i > 0) authorSwap(i, i - 1);
      }));
      authorsEditor.querySelectorAll(".move-down").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i; if (i < authors.length - 1) authorSwap(i, i + 1);
      }));
      authorsEditor.querySelectorAll(".remove").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i;
        authors.splice(i, 1);
        if (authors.length === 0) authors.push({name: "", co_first: false, co_senior: false, group_authorship: false});
        renderAuthors();
      }));
    }
    function authorSwap(i, j) { [authors[i], authors[j]] = [authors[j], authors[i]]; renderAuthors(); }
    document.getElementById("author-add").addEventListener("click", () => {
      authors.push({name: "", co_first: false, co_senior: false, group_authorship: false}); renderAuthors();
    });
    renderAuthors();
  }

  // ---------------- Open access editor (publications only) ----------------
  if (document.getElementById("oa-editor")) {
    let oa = (DATA.open_access_form || {});
    const oaEditor = document.getElementById("oa-editor");
    const oaHidden = document.getElementById("open_access-json");

    function renderOA() {
      oaEditor.innerHTML = "";
      ["paper", "code", "data"].forEach(key => {
        const v = oa[key] || {enabled: false, url: ""};
        const row = document.createElement("div");
        row.className = "oa-row";
        row.innerHTML = `
          <label class="oa-key"><input type="checkbox" class="oa-enabled" data-key="${key}" ${v.enabled ? "checked" : ""}> ${key} is open</label>
          <input type="text" class="oa-url" data-key="${key}" value="${escapeAttr(v.url)}" placeholder="Optional URL — leave blank to store as 'true'" ${v.enabled ? "" : "disabled"}>`;
        oaEditor.appendChild(row);
      });
      oaEditor.querySelectorAll(".oa-enabled").forEach(el => el.addEventListener("change", e => {
        const k = e.target.dataset.key; oa[k] = oa[k] || {enabled: false, url: ""}; oa[k].enabled = e.target.checked; renderOA();
      }));
      oaEditor.querySelectorAll(".oa-url").forEach(el => el.addEventListener("input", e => {
        const k = e.target.dataset.key; oa[k] = oa[k] || {enabled: false, url: ""}; oa[k].url = e.target.value; oaHidden.value = JSON.stringify(oa);
      }));
      oaHidden.value = JSON.stringify(oa);
    }
    renderOA();
  }

  // ---------------- Typed notes editor (publications only) ----------------
  if (document.getElementById("notes-editor")) {
    let notes = (DATA.notes_form || []).slice();
    const notesEditor = document.getElementById("notes-editor");
    const notesHidden = document.getElementById("notes-json");

    // Stage C / I4 (2026-05-25): the "View on Altmetric" link now lives
    // inside the FIRST media note (one button per entry, not per note;
    // it's an entry-level concern keyed on title). Stay in sync with
    // url_helpers.altmetric_url — colon-space subtitle strip, embedded-
    // quote scrub, title-only query (no author keyword). Canonical contract
    // pinned by test_altmetric_url_parity_baseline_for_js_port — if you edit
    // this function, also update the Python source AND the parity test cases.
    function altmetricExplorerUrl(title) {
      if (!title) return null;
      let cleaned = String(title).trim()
        .replace(/\*/g, '').replace(/_/g, '')
        .replace(/“/g, '').replace(/”/g, '')
        .replace(/"/g, '')
        .trim();
      if (!cleaned) return null;
      if (cleaned.includes(': ')) {
        const main = cleaned.split(': ', 1)[0].trim();
        if (main) cleaned = main;
      }
      const q = `"${cleaned}"`;
      return `https://www.altmetric.com/explorer/highlights?q=${encodeURIComponent(q)}`;
    }

    function renderAltmetricExplorerBar() {
      // P5: gated on the active template's `altmetric` capability. Under a
      // template without it (e.g. modern), ROUTES.altmetric_resolve is
      // omitted, so this link + the tracker Resolve UI would be dead.
      if (!DATA.altmetric_enabled) return "";
      if (SECTION_KEY !== "publications") return "";
      const titleInput = document.querySelector('input[name="title"], textarea[name="title"]');
      const title = titleInput ? titleInput.value : "";
      const url = altmetricExplorerUrl(title);
      if (!url) {
        return `<div class="outlet-altmetric-explorer hint">
                  <span class="muted">Set a title above to enable Altmetric Explorer search for press mentions.</span>
                </div>`;
      }
      return `<div class="outlet-altmetric-explorer">
                <a class="altmetric-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer"
                   title="Open Altmetric Explorer for this article (searches by main title — text before any ': ' subtitle; sign-in may be required). Scan press mentions there, then paste each URL into the outlets above.">
                  View on Altmetric &nearr;
                </a>
                <span class="hint">— scan press mentions, then paste each URL into the outlets above.</span>
              </div>`;
    }

    function renderNotes() {
      notesEditor.innerHTML = "";
      const firstMediaIdx = notes.findIndex(n => (n.type || "note") === "media");
      notes.forEach((n, i) => {
        const t = n.type || "note";
        const wrap = document.createElement("div");
        wrap.className = "note-row";
        let bodyHtml = "";
        if (t === "media") {
          bodyHtml = renderOutletsHtml(n, i, i === firstMediaIdx);
        } else {
          const field = CONTENT_FIELD[t] || "text";
          const v = escapeHtml(n[field] || "");
          bodyHtml = `<textarea class="note-content" data-i="${i}" data-field="${field}" rows="3" placeholder="${TYPE_HINT[t] || ""}">${v}</textarea>`;
        }
        const typeOpts = dropdownTypes(t)
          .map(tt => `<option value="${tt}" ${tt === t ? "selected" : ""}>${TYPE_LABEL[tt] || tt}${!PRIMARY_TYPES.includes(tt) ? " (legacy)" : ""}</option>`)
          .join("");
        wrap.innerHTML = `
          <div class="note-head">
            <select class="note-type" data-i="${i}">${typeOpts}</select>
            <label><input type="checkbox" class="note-hidden" data-i="${i}" ${n.highlighted ? "checked" : ""}> hide by default</label>
            <span class="grow"></span>
            <button type="button" class="row-btn note-up" data-i="${i}" title="Move up">&uarr;</button>
            <button type="button" class="row-btn note-down" data-i="${i}" title="Move down">&darr;</button>
            <button type="button" class="row-btn remove note-remove" data-i="${i}" title="Remove note">&times;</button>
          </div>
          <div class="note-body">${bodyHtml}</div>
          <p class="hint">${TYPE_HINT[t] || ""}</p>`;
        notesEditor.appendChild(wrap);
      });
      bindNoteEvents();
      notesHidden.value = JSON.stringify(notes);
    }

    // T3.6: TRACKER_HOSTS sourced from server-side url_helpers.TRACKER_HOSTS
    // via context-processor injection.
    const TRACKER_HOSTS = new Set(DATA.tracker_hosts || []);
    function isTrackerUrl(u) {
      if (!u) return false;
      try { return TRACKER_HOSTS.has(new URL(u).hostname); }
      catch (e) { return false; }
    }

    function groupOutletsPreview(outlets) {
      const kept = (outlets || []).filter(o => (o.name || "").trim() && !o.highlighted);
      if (kept.length === 0) return "<span class='muted'>(no visible outlets yet)</span>";
      const groups = [];
      const byKey = new Map();
      for (const o of kept) {
        const name = (o.name || "").trim();
        const key = name.toLowerCase();
        const url = (o.url || "").trim();
        if (byKey.has(key)) {
          byKey.get(key).items.push({url});
        } else {
          const g = {name, items: [{url}]};
          groups.push(g);
          byKey.set(key, g);
        }
      }
      const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
      const stripItalic = s => String(s).replace(/_/g, "");
      return "<strong>Preview:</strong> " + groups.map(g => {
        const name = stripItalic(g.name);
        if (g.items.length === 1) {
          return esc(name);
        }
        const digits = g.items.slice(1).map((_, i) => i + 2).join(", ");
        return `${esc(name)} (${digits})`;
      }).join(", ");
    }

    function renderOutletsHtml(n, ni, isFirstMedia) {
      const outlets = n.outlets || [];
      outlets.sort((a, b) =>
        (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase())
      );
      const trackerCount = outlets.filter(o => isTrackerUrl(o.url)).length;
      // P5: the Resolve UI depends on ROUTES.altmetric_resolve, which is only
      // present when the active template has the `altmetric` capability.
      const AM = !!DATA.altmetric_enabled;
      const rows = outlets.map((o, oi) => {
        const tracker = isTrackerUrl(o.url);
        const resolveBtn = (tracker && AM)
          ? `<button type="button" class="row-btn outlet-resolve" title="Resolve this tracker URL (works on networks that allow ct.moreover.com)">Resolve</button>`
          : "";
        const trackerBadge = tracker
          ? `<span class="outlet-tracker-badge" title="Tracker URL; click Resolve when on a network that allows ct.moreover.com">tracker</span>`
          : "";
        return `
        <div class="outlet-row ${tracker ? "outlet-row-tracker" : ""}" data-ni="${ni}" data-oi="${oi}">
          <input type="text" class="outlet-name" value="${escapeAttr(o.name)}" placeholder="Outlet name (plain text — italics are stripped at render time)">
          <input type="text" class="outlet-url" value="${escapeAttr(o.url)}" placeholder="Optional URL">
          ${trackerBadge}
          ${resolveBtn}
          <label><input type="checkbox" class="outlet-hidden" ${o.highlighted ? "checked" : ""}> hide this outlet</label>
          <button type="button" class="row-btn outlet-remove" title="Remove outlet">&times;</button>
        </div>`;
      }).join("");
      const resolveAllBar = (trackerCount > 0 && AM)
        ? `<div class="outlet-resolve-all-bar">
             <button type="button" class="btn-secondary outlet-resolve-all" data-ni="${ni}" title="Resolve every tracker URL in this note in one pass">Resolve all ${trackerCount} tracker URL${trackerCount === 1 ? "" : "s"}</button>
             <span class="outlet-resolve-all-status hint"></span>
           </div>`
        : "";
      const previewHtml = `<div class="outlet-preview-hint" data-ni="${ni}">${groupOutletsPreview(outlets)}</div>`;
      const sortHint = `<p class="hint">Outlets are sorted alphabetically on save. Duplicate URLs are dropped automatically (first occurrence wins).</p>`;
      // Stage C / I4 (2026-05-25): "View on Altmetric" link rendered
      // ONLY on the first media note (entry-level concern keyed on
      // title; N copies adds clutter without value).
      const altmetricExplorerBar = isFirstMedia ? renderAltmetricExplorerBar() : "";
      return `<div class="outlets">${rows}</div>
              ${sortHint}
              ${previewHtml}
              ${resolveAllBar}
              <div class="outlet-paste-url" data-ni="${ni}">
                <input type="url" class="paste-url-input" placeholder="Paste press URL to auto-fetch title">
                <button type="button" class="btn-secondary paste-url-fetch">Fetch title</button>
                <span class="paste-url-status hint"></span>
              </div>
              ${altmetricExplorerBar}
              <button type="button" class="btn-secondary outlet-add" data-ni="${ni}">+ Add outlet</button>`;
    }

    function bindNoteEvents() {
      notesEditor.querySelectorAll(".note-type").forEach(el => el.addEventListener("change", e => {
        const i = +e.target.dataset.i;
        notes[i].type = e.target.value;
        if (notes[i].type === "contributions" && !(notes[i].text || "").trim()) {
          notes[i].text = CONTRIBUTIONS_DEFAULT;
        }
        renderNotes();
      }));
      notesEditor.querySelectorAll(".note-hidden").forEach(el => el.addEventListener("change", e => {
        notes[+e.target.dataset.i].highlighted = e.target.checked; notesHidden.value = JSON.stringify(notes);
      }));
      notesEditor.querySelectorAll(".note-content").forEach(el => el.addEventListener("input", e => {
        const i = +e.target.dataset.i; const field = e.target.dataset.field; notes[i][field] = e.target.value; notesHidden.value = JSON.stringify(notes);
      }));
      notesEditor.querySelectorAll(".note-up").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i; if (i > 0) { [notes[i-1], notes[i]] = [notes[i], notes[i-1]]; renderNotes(); }
      }));
      notesEditor.querySelectorAll(".note-down").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i; if (i < notes.length - 1) { [notes[i+1], notes[i]] = [notes[i], notes[i+1]]; renderNotes(); }
      }));
      notesEditor.querySelectorAll(".note-remove").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i; notes.splice(i, 1); renderNotes();
      }));
      notesEditor.querySelectorAll(".outlet-add").forEach(el => el.addEventListener("click", e => {
        const ni = +e.target.dataset.ni;
        notes[ni].outlets = notes[ni].outlets || [];
        notes[ni].outlets.push({name: "", url: "", highlighted: false});
        renderNotes();
      }));
      notesEditor.querySelectorAll(".outlet-row").forEach(row => {
        const ni = +row.dataset.ni, oi = +row.dataset.oi;
        const o = notes[ni].outlets[oi];
        row.querySelector(".outlet-name").addEventListener("input", e => { o.name = e.target.value; notesHidden.value = JSON.stringify(notes); });
        row.querySelector(".outlet-url").addEventListener("input", e => {
          o.url = e.target.value;
          notesHidden.value = JSON.stringify(notes);
          if (isTrackerUrl(o.url) !== row.classList.contains("outlet-row-tracker")) renderNotes();
        });
        row.querySelector(".outlet-hidden").addEventListener("change", e => { o.highlighted = e.target.checked; notesHidden.value = JSON.stringify(notes); });
        row.querySelector(".outlet-remove").addEventListener("click", () => { notes[ni].outlets.splice(oi, 1); renderNotes(); });
        const resolveBtn = row.querySelector(".outlet-resolve");
        if (resolveBtn) {
          resolveBtn.addEventListener("click", async () => {
            resolveBtn.disabled = true;
            const prevText = resolveBtn.textContent;
            resolveBtn.textContent = "Resolving…";
            try {
              const fd = new FormData(); fd.append("url", o.url);
              const resp = await fetch(ROUTES.altmetric_resolve, {method: "POST", body: fd});
              if (!resp.ok) { resolveBtn.textContent = `HTTP ${resp.status}`; return; }
              const ctype = resp.headers.get("content-type") || "";
              if (!ctype.includes("application/json")) { resolveBtn.textContent = "non-JSON"; return; }
              const data = await resp.json();
              if (data && data.final_url && data.final_url !== o.url) {
                o.url = data.final_url;
                notesHidden.value = JSON.stringify(notes);
                renderNotes();
              } else {
                resolveBtn.textContent = "no change";
              }
            } catch (e) {
              resolveBtn.textContent = `failed`;
            } finally {
              setTimeout(() => {
                if (resolveBtn.isConnected) {
                  resolveBtn.disabled = false;
                  resolveBtn.textContent = prevText;
                }
              }, 2500);
            }
          });
        }
      });
      notesEditor.querySelectorAll(".outlet-resolve-all").forEach(btn => {
        const ni = +btn.dataset.ni;
        const statusEl = btn.parentElement.querySelector(".outlet-resolve-all-status");
        btn.addEventListener("click", async () => {
          const outlets = notes[ni].outlets || [];
          const indices = outlets.map((o, i) => isTrackerUrl(o.url) ? i : -1).filter(i => i >= 0);
          if (indices.length === 0) { statusEl.textContent = "Nothing to resolve."; return; }
          btn.disabled = true;
          let resolved = 0, kept = 0, errors = 0;
          for (let k = 0; k < indices.length; k++) {
            const oi = indices[k];
            const o = outlets[oi];
            statusEl.textContent = `Resolving ${k + 1}/${indices.length} (${o.name || "?"})…`;
            try {
              const fd = new FormData(); fd.append("url", o.url);
              const resp = await fetch(ROUTES.altmetric_resolve, {method: "POST", body: fd});
              if (!resp.ok) { errors += 1; continue; }
              const ctype = resp.headers.get("content-type") || "";
              if (!ctype.includes("application/json")) { errors += 1; continue; }
              const data = await resp.json();
              if (data && data.final_url && data.final_url !== o.url) {
                o.url = data.final_url;
                resolved += 1;
              } else { kept += 1; }
            } catch (e) { errors += 1; }
          }
          notesHidden.value = JSON.stringify(notes);
          const bits = [];
          if (resolved) bits.push(`${resolved} resolved`);
          if (kept) bits.push(`${kept} unchanged (network may still be blocking)`);
          if (errors) bits.push(`${errors} error${errors === 1 ? "" : "s"}`);
          statusEl.textContent = bits.join("; ") || "Done.";
          renderNotes();
        });
      });
      notesEditor.querySelectorAll(".outlet-paste-url").forEach(row => {
        const ni = +row.dataset.ni;
        const input = row.querySelector(".paste-url-input");
        const btn = row.querySelector(".paste-url-fetch");
        const status = row.querySelector(".paste-url-status");
        const doFetch = async () => {
          const url = input.value.trim();
          if (!url) return;
          btn.disabled = true; status.textContent = "Fetching…";
          try {
            const fd = new FormData(); fd.append("url", url);
            const resp = await fetch(ROUTES.fetch_title, {method: "POST", body: fd});
            const data = await resp.json();
            if (data && data.title) {
              notes[ni].outlets = notes[ni].outlets || [];
              notes[ni].outlets.push({name: data.title, url: data.url, highlighted: false});
              input.value = ""; status.textContent = "";
              renderNotes();
            } else {
              status.textContent = (data && data.error) || "No title found — add by hand";
            }
          } catch (e) {
            status.textContent = "Fetch failed — add by hand";
          } finally {
            btn.disabled = false;
          }
        };
        btn.addEventListener("click", doFetch);
        input.addEventListener("keydown", e => {
          if (e.key === "Enter") { e.preventDefault(); doFetch(); }
        });
      });
    }

    document.getElementById("note-add").addEventListener("click", () => {
      notes.push({type: "note", text: "", citation: "", outlets: [], highlighted: false});
      renderNotes();
    });
    // Stage C / I4 (2026-05-25): when the title changes, re-render so
    // the Altmetric Explorer link inside the first media note picks up
    // the new search query. Without this, entry_new users would see
    // "Set a title above to enable..." even after typing a title.
    const titleInput = document.querySelector('input[name="title"], textarea[name="title"]');
    if (titleInput) {
      titleInput.addEventListener("input", () => {
        // Only re-render if at least one media note exists (otherwise
        // the title change has no visible effect on the notes editor).
        if (notes.some(n => (n.type || "note") === "media")) renderNotes();
      });
    }
    renderNotes();
  }

  // ---------------- Simple notes editors (presentations + service) ----------------
  // V20 B3: rewired through ListEditor factory.
  document.querySelectorAll(".simple-notes-editor").forEach(editor => {
    const fieldName = editor.dataset.field;
    const hidden = document.getElementById(`${fieldName}-json`);
    const initial = (DATA.simple_notes_form || []).map(
      n => ({text: n.text || "", highlighted: !!n.highlighted}),
    );
    const addBtn = document.querySelector(`[data-add-simple-note="${fieldName}"]`);
    window.ListEditor.attach({
      editor,
      hidden,
      initial,
      addBtn,
      newItem: () => ({text: "", highlighted: false}),
      rowHtml: (n, i) => `
        <div class="simple-note-row">
          <textarea class="simple-note-text" data-i="${i}" rows="2" placeholder="Sub-bullet text">${escapeHtml(n.text)}</textarea>
          <label><input type="checkbox" class="simple-note-hidden" data-i="${i}" ${n.highlighted ? "checked" : ""}> hide by default</label>
          <button type="button" class="row-btn list-up" data-i="${i}" title="Move up">&uarr;</button>
          <button type="button" class="row-btn list-down" data-i="${i}" title="Move down">&darr;</button>
          <button type="button" class="row-btn remove list-remove" data-i="${i}" title="Remove">&times;</button>
        </div>`,
      bindRow: (items, hidden, render) => {
        editor.querySelectorAll(".simple-note-text").forEach(el => el.addEventListener("input", e => {
          items[+e.target.dataset.i].text = e.target.value;
          hidden.value = JSON.stringify(items);
        }));
        editor.querySelectorAll(".simple-note-hidden").forEach(el => el.addEventListener("change", e => {
          items[+e.target.dataset.i].highlighted = e.target.checked;
          hidden.value = JSON.stringify(items);
        }));
      },
    });
  });

  // ---------------- String list editors (extras, sections list, etc) ----------------
  // V20 B3: rewired through ListEditor factory.
  const STRING_LIST_DATA = DATA.list_field_data || {};
  document.querySelectorAll(".string-list-editor").forEach(editor => {
    const fieldName = editor.dataset.field;
    const hidden = document.getElementById(`${fieldName}-json`);
    const initial = (STRING_LIST_DATA[fieldName] || []).slice();
    const addBtn = document.querySelector(`[data-add-string="${fieldName}"]`);
    window.ListEditor.attach({
      editor,
      hidden,
      initial,
      addBtn,
      newItem: () => "",
      rowHtml: (s, i) => `
        <div class="string-list-row">
          <input type="text" class="string-list-input" data-i="${i}" value="${escapeAttr(s)}">
          <button type="button" class="row-btn list-up" data-i="${i}" title="Move up">&uarr;</button>
          <button type="button" class="row-btn list-down" data-i="${i}" title="Move down">&darr;</button>
          <button type="button" class="row-btn remove list-remove" data-i="${i}" title="Remove">&times;</button>
        </div>`,
      bindRow: (items, hidden, render) => {
        editor.querySelectorAll(".string-list-input").forEach(el => el.addEventListener("input", e => {
          items[+e.target.dataset.i] = e.target.value;
          hidden.value = JSON.stringify(items);
        }));
      },
    });
  });

  // ---------------- Audiences sets (checkbox groups) ----------------
  document.querySelectorAll(".audiences-set").forEach(group => {
    const fieldName = group.dataset.field;
    const hidden = document.getElementById(`${fieldName}-json`);
    function update() {
      const selected = Array.from(group.querySelectorAll("input[type=checkbox]:checked")).map(el => el.dataset.audience);
      hidden.value = JSON.stringify(selected);
    }
    group.querySelectorAll("input[type=checkbox]").forEach(el => el.addEventListener("change", update));
    update();
  });

  // Flip the save-guard sentinel LAST — after every editor above has mounted.
  // If any mount threw, the IIFE aborts before this line, leaving js_mounted
  // empty so the save route rejects the save (an empty JS-driven hidden field
  // can't silently wipe existing data). See entry_edit.html + the save routes.
  const _jsMounted = document.getElementById("js-mounted");
  if (_jsMounted) _jsMounted.value = "1";
})();
