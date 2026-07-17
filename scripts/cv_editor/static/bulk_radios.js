/*
 * Bulk-radio button helpers — Tier B / B6 (2026-05-27).
 *
 * Extracted from pubmed_sync.html + qc_triage.html. Both pages had
 * 2-4 nearly-identical bulk-set-radios + Stage-E two-step apply
 * handlers. Consolidating here keeps a11y + safety contracts in ONE
 * place — see scripts/CLAUDE.md gotchas #51 + #57 for the contracts
 * this helper hard-codes (the critic explicitly rejected making them
 * caller-overridable opts).
 *
 * Public API: window.BulkRadios = {wireBulkDecision, wireBulkApplyTwoStep}
 *
 * wireBulkDecision({button, getRadios, value, statusEl?, onAfter?, disarmSiblings?})
 *   Single-click handler. On click: each radio returned by getRadios()
 *   has its `.checked` set to (radio.value === value); radio groups
 *   handle their own exclusivity. Dispatches a synthetic `change`
 *   event on each touched radio so FormDirtyGuard observes the
 *   mutation (gotcha #51 critical).
 *   - getRadios: () => NodeList | Array of radio input nodes
 *   - value: string the helper checks; non-matching radios in same group uncheck
 *   - statusEl: optional element to write status into. Wire-time warns
 *     to console if statusEl lacks aria-live="polite".
 *   - onAfter(touched, total): optional callback after the click runs;
 *     replaces the default status-text write.
 *   - disarmSiblings: optional array of buttons to call ._disarm() on
 *     before the bulk-set fires (cross-disarm for nearby two-step apply
 *     buttons; see pubmed_sync.html bulk-vs-apply contract gotcha #51).
 *
 * wireBulkApplyTwoStep({button, getRadios, statusEl?, predicate?,
 *                       armLabel, cancelText?, timeoutMs?})
 *   Two-step destructive bulk-apply (Stage E / I10 pattern). First click
 *   arms; second click within `timeoutMs` commits. Auto-resets on
 *   timeout. Hard-codes ARIA + a11y contracts that the critic flagged
 *   as MUST-NOT-be-opt: `aria-pressed`, the "! " non-color glyph prefix
 *   on the armed label (gotcha #57 U-H1), and the 200ms min-interval
 *   between arm and commit (gotcha #57 U-H2 — held Enter / fast double-
 *   click guard).
 *   - getRadios: () => NodeList | Array of radio nodes the commit checks
 *   - predicate(radio): optional filter applied before commit; skipped
 *     radios are also subtracted from the armed-state count.
 *   - armLabel(applicableCount, totalCount, skippedCount): builds the
 *     armed-state label text (WITHOUT the leading "! " glyph — helper
 *     prepends).
 *   - cancelText: optional text written to statusEl on timeout reset.
 *   - timeoutMs: default 5000.
 *   Side-effect: sets button._disarm = disarm so siblings can pull
 *   the button back out of armed state (DOM-attribute pattern is the
 *   established convention per gotcha #51; do not refactor to a
 *   module-level Set without re-thinking the page-navigation case).
 */
(function (global) {
  'use strict';

  var MIN_CONFIRM_MS = 200;  // gotcha #57 U-H2

  function dispatchChange(input) {
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function assertAriaLive(statusEl, fname) {
    if (statusEl && statusEl.getAttribute('aria-live') !== 'polite') {
      console.warn(
        fname + ': statusEl is missing aria-live="polite" — ' +
        'bulk-confirm messages will not announce to screen readers.'
      );
    }
  }

  function wireBulkDecision(opts) {
    var button = opts.button;
    var getRadios = opts.getRadios;
    var value = opts.value;
    var statusEl = opts.statusEl;
    var onAfter = opts.onAfter;
    var disarmSiblings = opts.disarmSiblings;
    assertAriaLive(statusEl, 'wireBulkDecision');

    button.addEventListener('click', function () {
      if (disarmSiblings) {
        for (var i = 0; i < disarmSiblings.length; i++) {
          var sib = disarmSiblings[i];
          if (sib && typeof sib._disarm === 'function') sib._disarm();
        }
      }
      var radios = Array.prototype.slice.call(getRadios());
      var touched = 0;
      radios.forEach(function (r) {
        var want = (r.value === value);
        if (r.checked !== want) {
          r.checked = want;
          if (want) {
            dispatchChange(r);
            touched++;
          }
        }
      });
      if (onAfter) {
        onAfter(touched, radios.length);
      } else if (statusEl) {
        statusEl.textContent = touched
          ? ('Set ' + touched + ' row(s).')
          : 'No changes (already set).';
      }
    });
  }

  function wireBulkApplyTwoStep(opts) {
    var button = opts.button;
    var getRadios = opts.getRadios;
    var predicate = opts.predicate || function () { return true; };
    var statusEl = opts.statusEl;
    var armLabel = opts.armLabel;
    var cancelText = opts.cancelText;
    var TIMEOUT_MS = opts.timeoutMs || 5000;
    assertAriaLive(statusEl, 'wireBulkApplyTwoStep');

    var origLabel = button.textContent;
    var armed = false;
    var armedAt = 0;
    var armTimer = null;

    button.setAttribute('aria-pressed', 'false');

    function disarm() {
      armed = false;
      armedAt = 0;
      button.textContent = origLabel;
      button.classList.remove('btn-destructive-armed');
      button.setAttribute('aria-pressed', 'false');
      if (armTimer) {
        clearTimeout(armTimer);
        armTimer = null;
      }
    }

    function partitionRadios() {
      var all = Array.prototype.slice.call(getRadios());
      var applicable = all.filter(predicate);
      return {all: all, applicable: applicable};
    }

    function arm() {
      var parts = partitionRadios();
      // "Applicable" = not predicate-filtered AND not already checked.
      var changeable = parts.applicable.filter(function (r) {
        return !r.checked;
      });
      if (changeable.length === 0) {
        if (statusEl) {
          statusEl.textContent = parts.applicable.length === 0
            ? 'Nothing to apply.'
            : 'All applicable rows are already set.';
        }
        return;
      }
      armed = true;
      armedAt = Date.now();
      button.classList.add('btn-destructive-armed');
      button.setAttribute('aria-pressed', 'true');
      var skipped = parts.all.length - parts.applicable.length;
      // Hard-coded "! " glyph (gotcha #57 U-H1) — NOT opt-overridable.
      button.textContent = '! ' + armLabel(changeable.length, parts.all.length, skipped);
      if (statusEl) {
        statusEl.textContent =
          'Armed: click again within ' + Math.round(TIMEOUT_MS / 1000) +
          ' seconds.';
      }
      armTimer = setTimeout(function () {
        disarm();
        if (statusEl && cancelText) statusEl.textContent = cancelText;
      }, TIMEOUT_MS);
    }

    function commit() {
      // Gotcha #57 U-H2: 200ms min-interval — guard against held Enter
      // or fast double-click firing both events in one burst.
      if (Date.now() - armedAt < MIN_CONFIRM_MS) return;
      var parts = partitionRadios();
      var touched = 0;
      parts.applicable.forEach(function (r) {
        if (!r.checked) {
          r.checked = true;
          dispatchChange(r);
          touched++;
        }
      });
      var skipped = parts.all.length - parts.applicable.length;
      if (statusEl) {
        statusEl.textContent =
          'Set ' + touched + ' row(s) to apply.' +
          (skipped ? ' Skipped ' + skipped + ' row(s).' : '');
      }
      disarm();
    }

    button.addEventListener('click', function () {
      if (!armed) arm();
      else commit();
    });

    // Cross-disarm hook: callers wire siblings via opts.disarmSiblings.
    button._disarm = disarm;
  }

  global.BulkRadios = {
    wireBulkDecision: wireBulkDecision,
    wireBulkApplyTwoStep: wireBulkApplyTwoStep,
  };
})(window);
