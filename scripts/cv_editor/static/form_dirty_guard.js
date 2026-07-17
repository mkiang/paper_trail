// T3.4: shared form-dirty guard + Cmd+S submit.
//
// Use:
//   <form id="my-form">...</form>
//   <script src="{{ url_for('static', filename='form_dirty_guard.js') }}"></script>
//   <script>FormDirtyGuard.attach('my-form');</script>
//
// `attach(formId)` wires:
//   1. `beforeunload` prompt if the form has been modified since load.
//   2. Cmd+S / Ctrl+S submits the form (preventing the browser's
//      default "save page" handler).
//
// Originally inline in entry_edit.html (V5-D); extracted so style_edit,
// rename_author, promote_preprint, and any future form template can
// reuse the same protection.
window.FormDirtyGuard = (function () {
  function attach(formId) {
    const form = document.getElementById(formId);
    if (!form) return;
    let dirty = false;
    form.addEventListener("input", () => { dirty = true; });
    form.addEventListener("change", () => { dirty = true; });
    form.addEventListener("submit", () => { dirty = false; });
    window.addEventListener("beforeunload", e => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";  // Chrome
      }
    });
    // Intentional Cancel/back links opt out of the prompt: mark a link
    // `data-skip-dirty` and clicking it clears the dirty flag so beforeunload
    // stays silent. Scoped to MARKED links only — a blanket clear-on-any-link
    // would silently regress the guard (accidental navigation would lose edits).
    document.querySelectorAll("[data-skip-dirty]").forEach(el => {
      el.addEventListener("click", () => { dirty = false; });
    });
    window.addEventListener("keydown", e => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        form.requestSubmit();
      }
    });
  }
  return {attach};
})();
