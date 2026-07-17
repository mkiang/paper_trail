/* Generic list-editor factory (V20 B3, 2026-05-18).
 *
 * Mounts a "list of N similar rows" widget against a hidden JSON
 * input + per-row up/down/remove buttons. Used today by:
 *   - simple-notes (presentations / service)
 *   - string-list (extras, section order, etc.)
 *
 * Authors editor and outlets editor have richer interactions
 * (drag-and-drop, alt-arrow swap, conditional row rendering) and
 * remain hand-coded in entry_edit.js. Threshold to migrate them
 * into this factory is "two more list widgets need the same
 * features" — three reorder/remove widgets are not yet enough.
 *
 * API:
 *   ListEditor.attach({
 *     editor,      // root DOM element (querySelectorAll target)
 *     hidden,      // hidden input element where JSON state is written
 *     initial,     // initial array of items (defaults to [])
 *     addBtn,      // optional button element; click → push empty + render
 *     newItem,     // factory for an empty row: () => ({...})
 *     rowHtml,     // (item, i) => html string
 *     bindRow,     // optional: (items, hidden, render) => void — wires
 *                  // per-row inputs to mutate items[i] in place
 *   });
 *
 * Per-row template MUST include buttons with classes:
 *   .list-up      → move row up
 *   .list-down    → move row down
 *   .list-remove  → splice this row
 * Each carrying data-i="<index>" so the handler knows which row.
 */
window.ListEditor = (function () {
  function attach({editor, hidden, initial, addBtn, newItem, rowHtml, bindRow}) {
    let items = (initial || []).slice();

    function render() {
      editor.innerHTML = "";
      items.forEach((item, i) => {
        const r = document.createElement("div");
        r.innerHTML = rowHtml(item, i);
        // Avoid an extra wrapping div by unwrapping single-child html.
        if (r.children.length === 1) {
          editor.appendChild(r.firstElementChild);
        } else {
          editor.appendChild(r);
        }
      });
      hidden.value = JSON.stringify(items);
      editor.querySelectorAll(".list-up").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i;
        if (i > 0) { [items[i - 1], items[i]] = [items[i], items[i - 1]]; render(); }
      }));
      editor.querySelectorAll(".list-down").forEach(el => el.addEventListener("click", e => {
        const i = +e.target.dataset.i;
        if (i < items.length - 1) { [items[i + 1], items[i]] = [items[i], items[i + 1]]; render(); }
      }));
      editor.querySelectorAll(".list-remove").forEach(el => el.addEventListener("click", e => {
        items.splice(+e.target.dataset.i, 1); render();
      }));
      if (typeof bindRow === "function") bindRow(items, hidden, render);
    }

    if (addBtn) {
      addBtn.addEventListener("click", () => {
        items.push(typeof newItem === "function" ? newItem() : (newItem || {}));
        render();
      });
    }
    render();
  }
  return {attach};
})();
