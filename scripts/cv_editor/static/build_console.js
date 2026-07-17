// Shared SSE build-console client.
//
// Renders a streaming subprocess log into a console pane. Used by the
// index page (rebuild cv.pdf / rebuild all) and the style page
// (per-variant build). Extracted from inline <script> blocks in
// index.html and style_list.html (R2-H2 dedup pass).
//
// Usage:
//
//   BuildConsole.attach({
//     trigger:    '[data-rebuild]',     // selector for buttons
//     urlFromBtn: btn => '/rebuild/stream',
//     bodyFromBtn: btn => new URLSearchParams({mode: btn.dataset.rebuild}),
//     consoleEl:  document.getElementById('build-console'),
//     bodyEl:     document.getElementById('build-console-body'),
//     footerEl:   document.getElementById('build-console-footer'),
//   });
//
//   // For per-row buttons whose dataset is the variant index:
//   BuildConsole.attach({
//     trigger:    '[data-build-variant]',
//     urlFromBtn: btn => `/style/${btn.dataset.buildVariant}/build/stream`,
//     bodyFromBtn: () => null,
//     ...
//   });
//
// SSE frame format (matches the server-side generator):
//   event: line\ndata: "stdout/stderr line"\n\n
//   event: done\ndata: {"ok": true, "returncode": 0, "duration_s": 1.2, "cmd": "..."}\n\n
//   event: error\ndata: "Another build is already running."\n\n
//   event: close\ndata: ""\n\n
window.BuildConsole = (function () {
  function attach(opts) {
    opts = opts || {};
    // M1 (2026-05-29): fail loudly on mis-wired calls. A prior caller
    // passed consoleId/bodyId/footerId (strings) instead of the
    // consoleEl/bodyEl/footerEl DOM elements this expects, silently
    // breaking the console. Warn + bail instead of throwing on first use.
    const _required = ['trigger', 'urlFromBtn', 'consoleEl', 'bodyEl', 'footerEl'];
    const _missing = _required.filter(k => !opts[k]);
    if (_missing.length) {
      console.warn(
        'BuildConsole.attach: missing/invalid option(s): ' + _missing.join(', ') +
        ' — console not wired. (Pass DOM elements consoleEl/bodyEl/footerEl, ' +
        'a trigger selector, and urlFromBtn.)'
      );
      return;
    }
    const {trigger, urlFromBtn, bodyFromBtn, consoleEl, bodyEl, footerEl, closeBtn, labelFromBtn, onDone} = opts;
    function open(url, body, label, btn) {
      bodyEl.textContent = '';
      footerEl.textContent = `Starting (${label || 'build'})…`;
      consoleEl.hidden = false;
      // T4.5: disable the trigger button while the stream is in flight
      // so the user can't fire a second sweep that steps on the first.
      if (btn) btn.disabled = true;
      const reenable = () => { if (btn) btn.disabled = false; };
      const init = {method: 'POST', headers: {'Accept': 'text/event-stream'}};
      if (body) init.body = body;
      fetch(url, init).then(async resp => {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += decoder.decode(value, {stream: true});
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            handleFrame(buf.slice(0, i));
            buf = buf.slice(i + 2);
          }
        }
        reenable();
      }).catch(e => {
        footerEl.textContent = `Stream error: ${e}`;
        reenable();
      });
    }

    function handleFrame(frame) {
      let event = 'message', data = '';
      frame.split('\n').forEach(line => {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      });
      if (!data) return;
      let parsed;
      try { parsed = JSON.parse(data); } catch { parsed = data; }
      if (event === 'line') {
        bodyEl.textContent += parsed + '\n';
        bodyEl.scrollTo({top: bodyEl.scrollHeight, behavior: 'smooth'});
      } else if (event === 'done') {
        // Branch on payload shape: build-style has ok+cmd+duration_s+returncode;
        // resolve-all sweep has resolved+total+substituted; otherwise fall back
        // to JSON for forward-compat. Discriminate on multiple keys to defend
        // against future payload-shape additions accidentally colliding.
        if (parsed && typeof parsed === 'object' && 'ok' in parsed && 'cmd' in parsed) {
          footerEl.textContent =
            `Build ${parsed.ok ? 'OK' : 'FAILED'} (${parsed.cmd}, ${parsed.duration_s.toFixed(1)}s, exit ${parsed.returncode}).`;
        } else if (parsed && typeof parsed === 'object' && 'resolved' in parsed && 'total' in parsed && 'substituted' in parsed) {
          // T2.2c: friendly summary for tracker resolve-all sweep.
          const bits = [];
          bits.push(`Resolved ${parsed.resolved} of ${parsed.total} tracker URL${parsed.total === 1 ? '' : 's'}`);
          if (parsed.substituted) bits.push(`${parsed.substituted} URL${parsed.substituted === 1 ? '' : 's'} written to YAML`);
          if (parsed.failed) bits.push(`${parsed.failed} could not be resolved (try a different network)`);
          footerEl.textContent = bits.join('; ') + '.';
        } else {
          footerEl.textContent = `Done: ${JSON.stringify(parsed)}`;
        }
        if (typeof onDone === 'function') onDone(parsed);
      } else if (event === 'error') {
        footerEl.textContent = `Error: ${parsed}`;
      }
    }

    document.querySelectorAll(trigger).forEach(btn => btn.addEventListener('click', () => {
      const url = urlFromBtn(btn);
      const body = bodyFromBtn(btn);
      const label = labelFromBtn ? labelFromBtn(btn) : (btn.dataset.rebuild || btn.dataset.buildVariant);
      open(url, body, label, btn);
    }));

    if (closeBtn) closeBtn.addEventListener('click', () => { consoleEl.hidden = true; });
  }

  return {attach};
})();
