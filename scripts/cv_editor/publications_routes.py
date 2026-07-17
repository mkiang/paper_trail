"""Publications-specific routes (extracted from app.py 2026-05-29, M2b).

The largest extraction: bulk ops, press-URL title fetch, Altmetric tracker
resolution (+ the global Trackers page + SSE sweeps), cross-entry author
rename, citation import (4 tabs), and preprint->published promotion. Plus
the import/promote staging helpers (_stage_from_id, _enrich_parsed,
_merge_with_disagreements, _render_staged), which are publications-only and
move here.

Shared helpers (load_section, write_or_409, get_expected_mtime_ns,
resolve_idx, render_edit_form, form_to_entry, the tracker-cache trio,
sse_response, the basic-QC kicker) stay as create_app() closures and are
handed in BY REFERENCE via deps — the tracker trio + qc kicker are shared
with index/entry_view, so moving them would fork shared state.

Routes (endpoint names unchanged — register-on-app, gotcha #69):
  POST /publications/bulk
  POST /publications/fetch_title
  POST /publications/altmetric/resolve
  GET  /publications/trackers
  POST /publications/trackers/verify_resolved   (SSE)
  POST /publications/trackers/resolve_all        (SSE)
  POST /publications/<int:idx>/trackers/resolve
  GET|POST /publications/rename-author
  GET|POST /publications/import
  GET|POST /publications/<int:idx>/promote

Behaviour-identical (M2 fingerprint + test_v1b/v9/v13/v20 tracker tests).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from filelock import Timeout
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from ruamel.yaml import YAMLError
from ruamel.yaml.comments import CommentedMap

from cv_editor import (
    altmetric_client,
    author_rename,
    citation_parse,
    enrichment,
    notes_helpers,
    orcid_client,
    preprint,
    schemas,
    sections,
    tracker_walk,
    url_helpers,
    url_title_fetcher,
    yaml_io,
)
from cv_editor.author_names import author_to_form, normalize_authors_for_render
from cv_editor.capabilities import Capabilities


@dataclass
class PublicationsDeps:
    """DI surface for the publications routes. The tracker-cache trio +
    the basic-QC kicker are SHARED with index/entry_view — passed by
    reference so the same cache/kicker objects are used everywhere."""

    root: Path
    load_section: Callable[[str], tuple]
    write_or_409: Callable
    get_expected_mtime_ns: Callable
    resolve_idx: Callable
    render_edit_form: Callable
    form_to_entry: Callable
    tracker_cache: Callable
    count_unresolved_trackers: Callable[[], dict]
    iter_publication_trackers: Callable
    sse_response: Callable
    qc_kick: Callable[..., None]  # _kick_qc_if_idle (shared)
    capabilities: Capabilities


def register_publications_routes(app: Flask, deps: PublicationsDeps) -> None:
    ROOT = deps.root
    _load_section = deps.load_section
    write_or_409 = deps.write_or_409
    _get_expected_mtime_ns = deps.get_expected_mtime_ns
    _resolve_idx = deps.resolve_idx
    _render_edit_form = deps.render_edit_form
    _form_to_entry = deps.form_to_entry
    _tracker_cache = deps.tracker_cache
    _count_unresolved_trackers = deps.count_unresolved_trackers
    _iter_publication_trackers = deps.iter_publication_trackers
    _sse_response = deps.sse_response
    _kick_qc_if_idle = deps.qc_kick

    # P5 (paper_trail inversion): the Altmetric trackers/resolve routes are a
    # per-template CAPABILITY. Gate their registration (intra-module — the base
    # publication routes: bulk, fetch_title, rename-author, import, promote stay
    # PUBLIC/unconditional). `_skip_route` defines the view but never binds it to
    # a URL, so `modern` (caps false) serves no tracker endpoint while `bespoke`
    # (caps true) registers every route exactly as before -> byte-identical
    # behaviour in the private repo.
    def _skip_route(*_a, **_k):
        return lambda f: f

    _altmetric_route = app.route if deps.capabilities.altmetric else _skip_route
    _altmetric_post = app.post if deps.capabilities.altmetric else _skip_route

    _BULK_ACTIONS = (
        "set_hidden",
        "unset_hidden",
        "move_subsection",
    )

    @app.route("/publications/bulk", methods=["POST"])
    def publication_bulk():
        action = request.form.get("bulk_action") or ""
        if action not in _BULK_ACTIONS:
            abort(400)
        raw_ids = request.form.getlist("selected")
        # Defensive parse: only accept integer-shaped ids.
        try:
            idxs = sorted({int(x) for x in raw_ids if x.strip().isdigit()})
        except (TypeError, ValueError):
            abort(400)
        if not idxs:
            flash("No publications selected; pick at least one row.", "warn")
            return redirect(url_for("section_list", section="publications"))

        sch, path, header, data = _load_section("publications")
        structure = sch["structure"]
        expected_mtime_ns = _get_expected_mtime_ns(request)

        target_subsection = request.form.get("target_subsection") or ""
        if action == "move_subsection":
            if sections.find_subsection_idx(data, target_subsection) < 0:
                flash(f"Unknown target subsection: {target_subsection!r}.", "warn")
                return redirect(url_for("section_list", section="publications")), 400

        n_changed = 0
        if action == "move_subsection":
            # Process in reverse so earlier idxs remain valid as we delete.
            # Pre-resolve the entries first (copy out), then delete + insert.
            to_move = []
            for idx in idxs:
                rec = sections.locate(data, structure, idx)
                if rec is None:
                    continue
                to_move.append(rec)
            # Delete from highest idx first so the loc tuples we cached
            # are not invalidated.
            for rec in sorted(to_move, key=lambda r: r["global_idx"], reverse=True):
                sections.delete_entry(data, structure, rec["loc"])
            # Re-insert at the target subsection.
            for rec in to_move:
                sections.insert_entry(
                    data,
                    structure,
                    {"subsection": target_subsection},
                    rec["entry"],
                )
                n_changed += 1
        else:
            # In-place mutations; order doesn't matter for indices.
            for idx in idxs:
                rec = sections.locate(data, structure, idx)
                if rec is None:
                    continue
                e = rec["entry"]
                if action == "set_hidden":
                    e["highlighted"] = True
                elif action == "unset_hidden":
                    e.pop("highlighted", None)
                n_changed += 1

        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("section_list", section="publications"),
        )
        if err:
            return err

        if action == "move_subsection":
            flash(
                f"Moved {n_changed} entr{'y' if n_changed == 1 else 'ies'} → "
                f"'{target_subsection}'.",
                "ok",
            )
        else:
            label = {
                "set_hidden": "marked hidden",
                "unset_hidden": "un-hidden",
            }[action]
            msg = f"{n_changed} entr{'y' if n_changed == 1 else 'ies'} {label}."
            if action == "set_hidden":
                msg += " (Dropped from cv.pdf; still visible in everything.pdf.)"
            flash(msg, "ok")
        return redirect(url_for("section_list", section="publications"))

    # ----- publications-specific (V13): paste press URL -> media outlet -----
    #
    # Synchronous proxy fetcher for the typed-notes editor's media-note row.
    # No cache (one-shot), no kicker (interactive), JSON response so the
    # client-side JS can append to the in-memory notes[] array without a
    # page reload. Scheme allow-list guards against the user pasting a
    # javascript:/file:/etc. URI by mistake; fetch failures return 200
    # with a populated `error` field so the JS shows an inline flash
    # instead of a generic XHR error.
    @app.post("/publications/fetch_title")
    def publications_fetch_title():
        url = (request.form.get("url") or "").strip()
        if not url_helpers.is_safe_fetch_url(url):
            return jsonify({"title": None, "url": url, "error": "Invalid URL"}), 400
        title = url_title_fetcher.fetch_title(url)
        if title is None:
            return jsonify({"title": None, "url": url, "error": "Could not fetch title"}), 200
        return jsonify({"title": title, "url": url, "error": None}), 200

    # ----- publications-specific: Altmetric tracker URL resolution -----
    #
    # The V13-A paste-API-URL ingest flow (Altmetric Explorer JSON:API)
    # was removed 2026-05-28. The V12 Altmetric Explorer DEEP-LINK button
    # in entry_view.html (via url_helpers.altmetric_url) is unchanged —
    # the user follows it to Explorer in a browser (institutional SSO), then
    # manually copies relevant outlets into notes.media.outlets. Tracker
    # URL resolution stays — it operates on URLs the user pastes into
    # outlet rows after the manual Explorer round-trip.

    @_altmetric_post("/publications/altmetric/resolve")
    def publications_altmetric_resolve():
        """Resolve a single tracker URL via the persistent cache.

        Two modes (V20 D2, 2026-05-18 — R2-H2 fix):

        * **Cache-only** (no `idx` form field). Used by the inline
          outlets editor in `entry_edit.html` where the entry is
          mid-edit and committing YAML would conflict with the user's
          unsaved form. Returns the resolution; YAML is updated when
          the user clicks Save like any other field change.

        * **Atomic** (`idx=<int>`). Used by the Trackers page per-row
          Resolve button on a saved entry. Resolves, then writes YAML
          via `write_or_409` with the substituted URL applied to entry
          `idx`. Cache invariant holds: every resolved entry in the
          cache has the resolved URL also written to YAML. On a
          stale-mtime conflict, returns `{conflict: true, view_url:
          ...}` with a 409 status so the JS can redirect.

        Form params:
            url          tracker URL to resolve (required)
            force        truthy → bypass cache (default false)
            idx          optional int → enable atomic-write mode
            mtime_ns     required when idx is set; the publications.yml
                         mtime the form was rendered at (stale-write
                         guard)
        """
        url = (request.form.get("url") or "").strip()
        force = (request.form.get("force") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        idx_raw = (request.form.get("idx") or "").strip()
        atomic_mode = idx_raw != ""

        if not url_helpers.is_safe_fetch_url(url):
            return jsonify({"final_url": None, "url": url, "error": "Invalid URL"}), 400

        cache = _tracker_cache()
        result = altmetric_client.resolve_tracker_url_with_cache(
            url,
            cache,
            force=force,
        )

        base_response = {
            "final_url": result.final_url,
            "url": url,
            "strategy": result.strategy,
            "status": result.status,
            "error": result.error if not result.is_resolved else None,
        }

        # V20 post-impl R1-HIGH-1 fix (2026-05-18): in cache-only mode,
        # commit the cache write now (the caller is responsible for any
        # YAML changes via entry_save). In atomic mode, defer cache.save()
        # until AFTER write_or_409 succeeds — otherwise a 409 strands the
        # cache in "resolved" while YAML keeps the tracker URL, recreating
        # the very R2-H2 invariant violation D2 was meant to fix.
        if not atomic_mode:
            cache.save()
            return jsonify(base_response), 200

        # Atomic mode: validate idx, optionally rewrite YAML.
        try:
            idx = int(idx_raw)
        except ValueError:
            # Cache had its in-memory state updated by the resolver but
            # not persisted. Persist anyway — the resolution itself is
            # valid; the caller just gave us a bad idx.
            cache.save()
            return jsonify({**base_response, "error": "invalid idx"}), 400

        if not result.is_resolved or not result.final_url:
            # Resolution failed — no YAML write needed; persist the
            # cache miss/failure entry per the existing contract.
            cache.save()
            return jsonify(base_response), 200

        sch, path, header, data = _load_section("publications")
        rec = sections.locate(data, sch["structure"], idx)
        if rec is None:
            # Idx was wrong but resolution succeeded — persist the
            # cache hit (a future Trackers-page sweep can apply it).
            cache.save()
            return jsonify({**base_response, "error": "entry not found"}), 404

        # Substitute on this entry, then atomically write via the
        # existing write_or_409 machinery so the stale-form guard fires
        # cleanly on conflict.
        expected_mtime_ns = _get_expected_mtime_ns(request)
        from cv_editor import tracker_walk as _tw

        _tw.substitute_tracker_urls_on_entry(
            rec["entry"],
            {url: result.final_url},
        )
        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("entry_view", section="publications", idx=idx),
        )
        if err:
            # Stale form — YAML wasn't written. Back out the in-memory
            # cache promise so a future retry re-resolves and re-writes
            # YAML atomically. (cache._entries is the source of truth
            # because cache.save() hasn't fired yet.)
            cache._entries.pop(url, None)
            # write_or_409 returned a (Response, 409) tuple; convert to
            # the JSON contract this route promises so the JS can
            # branch without parsing HTML.
            return jsonify(
                {
                    **base_response,
                    "conflict": True,
                    "view_url": url_for(
                        "entry_view",
                        section="publications",
                        idx=idx,
                    ),
                    "error": "stale form — entry was modified since this page loaded",
                }
            ), 409

        # Success: YAML committed. Now persist the cache — both sides
        # agree on the resolution.
        cache.save()
        return jsonify(
            {
                **base_response,
                "atomic_written": True,
                "view_url": url_for(
                    "entry_view",
                    section="publications",
                    idx=idx,
                ),
            }
        ), 200

    # ----- V13 finish: global Trackers page + per-entry resolve sweep -----

    @_altmetric_route("/publications/trackers", methods=["GET"])
    def publications_trackers():
        """Global listing of every unresolved tracker URL, grouped by
        publication. Joins data/publications.yml against the sidecar
        cache to surface per-tracker status."""
        _, _, _, data = _load_section("publications")
        cache = _tracker_cache()
        groups: list[dict] = []
        last_pub = None
        for tr in _iter_publication_trackers(data):
            cached = cache.get(tr["url"])
            if cached and cached.status == "resolved":
                continue
            row = dict(tr)
            row["status"] = cached.status if cached else "unknown"
            row["last_attempt_ts"] = cached.last_attempt_ts if cached else ""
            row["attempt_count"] = cached.attempt_count if cached else 0
            row["error"] = cached.error if cached else None
            if last_pub is None or last_pub["pub_idx"] != tr["pub_idx"]:
                last_pub = {
                    "pub_idx": tr["pub_idx"],
                    "pub_title": tr["pub_title"],
                    "pub_date": tr["pub_date"],
                    "rows": [],
                    "view_url": url_for(
                        "entry_view",
                        section="publications",
                        idx=tr["pub_idx"],
                    ),
                }
                groups.append(last_pub)
            last_pub["rows"].append(row)
        counts = _count_unresolved_trackers()
        # V20 D3 (2026-05-18): how many `resolved` entries are past the
        # 30-day TTL? The number drives the optional "Verify resolved"
        # banner; the actual TTL check + HEAD probes run only when the
        # button is clicked (never on page render).
        stale_resolved_count = sum(1 for _ in cache.stale_resolved())
        # V20 D2 (2026-05-18): publications.yml mtime_ns at page-render
        # time. Per-row Resolve buttons pass this back so a concurrent
        # editor save surfaces as a 409 instead of being silently
        # overwritten.
        _path = ROOT / schemas.get("publications")["file"]
        pub_mtime_ns = yaml_io.mtime_ns(_path)
        return render_template(
            "trackers.html",
            groups=groups,
            counts=counts,
            stale_resolved_count=stale_resolved_count,
            pub_mtime_ns=pub_mtime_ns,
        )

    # V20 D3 (2026-05-18 — R2-H4 fix): the SSE "Verify resolved" sweep.
    # Walks `cache.stale_resolved()` (resolved entries past TTL),
    # HEAD-probes the final URL, refreshes the TTL clock on success,
    # logs dead URLs for manual fix on failure. Never downgrades the
    # cache entry to failed_network — the cache key is the TRACKER URL,
    # which still works; only the final URL has rotted, and the user
    # needs to edit YAML to remove/replace the outlet. The
    # `_VERIFY_HEAD_PROBE` indirection is for hermetic tests.
    # V20-cleanup T4 (2026-05-18): probe lives in `cv_editor.url_helpers`
    # now — see gotcha #38. UA stays `cv-editor/1.0` (no PII).
    app.config.setdefault("_VERIFY_HEAD_PROBE", url_helpers.head_probe)

    @_altmetric_route("/publications/trackers/verify_resolved", methods=["POST"])
    def publications_trackers_verify_resolved():
        """SSE-streamed sweep: HEAD-probe every resolved entry past
        RESOLVED_TTL_DAYS. Refresh TTL on success; log on failure. No
        YAML writes — this route only updates the cache.

        Politeness: 100ms gap between probes. The sweep is bounded by
        the number of stale-resolved entries; typical run is a handful.
        """
        cache = _tracker_cache()
        stale = list(cache.stale_resolved())

        def _frames():
            yield ("line", f"verifying {len(stale)} resolved URL(s) past TTL…")
            probe = app.config["_VERIFY_HEAD_PROBE"]
            ok = broken = 0
            try:
                import time as _time

                for tracker_url, entry in stale:
                    if not entry.final_url:
                        continue
                    _time.sleep(0.1)
                    alive = probe(entry.final_url)
                    if alive:
                        cache.touch_resolved(tracker_url)
                        ok += 1
                        yield ("line", f"OK   {entry.final_url[:80]}")
                    else:
                        broken += 1
                        yield (
                            "line",
                            f"DEAD {entry.final_url[:80]} "
                            f"(manual fix needed — edit YAML to remove/replace this outlet)",
                        )
            finally:
                cache.save()
            yield ("line", f"done — {ok} verified, {broken} dead")
            yield ("done", "")

        return _sse_response(_frames())

    @_altmetric_route("/publications/trackers/resolve_all", methods=["POST"])
    def publications_trackers_resolve_all():
        """SSE-streamed sweep: walk every unresolved tracker in the CV,
        resolve through the cache, rewrite resolved URLs back to YAML.

        T1.3 invariants:
          (a) mtime_ns captured at LOAD time, not at write time — so a
              concurrent save during the (potentially minutes-long)
              sweep surfaces as a StaleFileError instead of being
              silently overwritten.
          (b) YAML write happens BEFORE cache.save() — a YAML failure
              leaves the cache un-promised so next sweep retries.
          (c) try/finally around the resolve loop guarantees cache.save()
              runs on client disconnect (GeneratorExit) so in-memory
              cache state isn't lost mid-stream.
        """
        sch, path, header, data = _load_section("publications")
        load_mtime_ns = yaml_io.mtime_ns(path)
        cache = _tracker_cache()
        # R7-H1: include ALL trackers in the sweep, not just cache-misses.
        # Per-row Resolve clicks cache `resolved` results without touching
        # YAML — the SSE sweep is the only place that writes back. If we
        # filtered cache-resolved entries out here, those URLs would be
        # silently stranded forever (cache says resolved; YAML still has
        # tracker; both UIs hide them). resolve_tracker_url_with_cache
        # short-circuits on cache-resolved so this costs ~0 extra time.
        targets = list(_iter_publication_trackers(data))

        def _frames():
            yield ("line", f"sweeping {len(targets)} tracker URL(s)…")
            substitutions: dict[str, str] = {}
            resolved = failed = 0
            seen_urls: set[str] = set()
            wrote_yaml = False
            try:
                for tr in targets:
                    u = tr["url"]
                    if u in seen_urls:
                        continue  # dedup; same URL may appear under multiple outlets
                    seen_urls.add(u)
                    result = altmetric_client.resolve_tracker_url_with_cache(
                        u,
                        cache,
                    )
                    if result.is_resolved and result.final_url:
                        substitutions[u] = result.final_url
                        resolved += 1
                        # Stage B / I9 (2026-05-25): three mutually
                        # exclusive verbs — `resolved` (fresh network
                        # success), `cached` (cache hit, no network
                        # call), `network error`/`attempted, still
                        # failed` (fresh failure). Post-impl reviewer
                        # flagged the old "kept" wording as ambiguous;
                        # `cached` ties the line to the cache behavior.
                        if result.from_cache:
                            yield (
                                "line",
                                f"cached   [{result.strategy}] {tr['outlet_name']} ({u[:50]}…)",
                            )
                        else:
                            yield (
                                "line",
                                f"resolved [{result.strategy}] {tr['outlet_name']} ({u[:50]}…)",
                            )
                    else:
                        failed += 1
                        # Stage B / I9: failure is always a fresh attempt
                        # post-I9 (no cached failures). Surface the error
                        # reason if present so home-network blocks and
                        # rate-limits are diagnosable from the console.
                        if result.error:
                            yield (
                                "line",
                                f"network error: {result.error} "
                                f"[{result.status}] "
                                f"{tr['outlet_name']} ({u[:50]}…)",
                            )
                        else:
                            yield (
                                "line",
                                f"attempted, still failed "
                                f"[{result.status}] "
                                f"{tr['outlet_name']} ({u[:50]}…)",
                            )
                # Apply substitutions to YAML FIRST, then save cache.
                # Invariant: cache never promises resolutions that aren't on disk.
                if substitutions:
                    yield ("line", f"writing {len(substitutions)} URL substitution(s) to YAML…")
                    tracker_walk.substitute_tracker_urls_in_publications(data, substitutions)
                    try:
                        yaml_io.write_with_backup(
                            path,
                            header,
                            data,
                            expected_mtime_ns=load_mtime_ns,
                        )
                        wrote_yaml = True
                        yield ("line", "YAML write complete.")
                    # R7-H2: widen the except tuple so PermissionError,
                    # OSError (disk full), and ruamel parse errors ALSO
                    # short-circuit via the `return` below, leaving
                    # wrote_yaml False so the finally skips cache.save()
                    # (otherwise stale resolution promises would persist
                    # to the cache).
                    #
                    # V13-V19-D R1-M5 fix (2026-05-18): narrowed from
                    # bare `Exception` to specific exception types so
                    # programmer errors (AttributeError, KeyError) bubble
                    # rather than masquerading as YAML write failures.
                    # ruamel.yaml.YAMLError covers parse errors; OSError
                    # covers the disk-full path; StaleFileError + Timeout
                    # are write_with_backup's own contract errors.
                    except (yaml_io.StaleFileError, Timeout, OSError, YAMLError) as e:
                        app.logger.exception("trackers SSE: YAML write failed")
                        yield ("error", f"YAML write failed: {e}")
                        return
                yield (
                    "done",
                    {
                        "resolved": resolved,
                        "failed": failed,
                        "total": len(seen_urls),
                        "substituted": len(substitutions) if wrote_yaml else 0,
                    },
                )
            except GeneratorExit:
                # R7-H2: client disconnect mid-resolve loop. We may hold
                # unconfirmed substitutions in memory, but wrote_yaml is
                # still False, so the finally below skips cache.save()
                # whenever substitutions are pending (its
                # `wrote_yaml or not substitutions` gate is False) —
                # otherwise the next sweep would treat the in-memory
                # resolutions as on-disk and the URLs would be stranded
                # forever.
                raise
            finally:
                # R5-H1 + R7-H2 invariant: only persist cache when its
                # state matches disk. Save when (a) YAML write succeeded
                # so substitutions are real, OR (b) no substitutions
                # were attempted (pure resolution-failure case —
                # cache.record() recorded only failed_* statuses, which
                # are safe to persist). Skip when (c) YAML write FAILED
                # OR client disconnected — those substitutions promise
                # resolutions that didn't land on disk.
                if wrote_yaml or not substitutions:
                    try:
                        cache.save()
                    except (OSError, ValueError) as e:
                        app.logger.warning(
                            "tracker cache save failed: %s",
                            e,
                        )

        return _sse_response(_frames())

    @_altmetric_route("/publications/<int:idx>/trackers/resolve", methods=["POST"])
    def publication_trackers_resolve_entry(idx):
        """Synchronous per-entry sweep. Resolves every tracker URL on
        one publication and atomically rewrites the YAML on success.
        Redirects to entry_view with a flash."""
        sch, path, header, data, rec = _resolve_idx("publications", idx)
        entry = rec["entry"]
        cache = _tracker_cache()
        # R6-M2: accept force=1 so the entry-view banner can unstick
        # cached failed_no_redirect entries without a Trackers-page detour.
        force = (request.form.get("force") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        substitutions: dict[str, str] = {}
        resolved = failed = 0
        for _ni, _oi, _name, u in tracker_walk.iter_entry_tracker_outlets(entry):
            cached = cache.get(u)
            # R5-M3: also require final_url is truthy — guards against
            # a corrupt cache holding status=resolved with final_url=null.
            if cached and cached.status == "resolved" and cached.final_url:
                substitutions[u] = cached.final_url
                continue
            result = altmetric_client.resolve_tracker_url_with_cache(
                u,
                cache,
                force=force,
            )
            if result.is_resolved and result.final_url:
                substitutions[u] = result.final_url
                resolved += 1
            else:
                failed += 1
        if substitutions:
            tracker_walk.substitute_tracker_urls_on_entry(entry, substitutions)
            err = write_or_409(
                path,
                header,
                data,
                expected_mtime_ns=_get_expected_mtime_ns(request),
                redirect_to=url_for("entry_view", section="publications", idx=idx),
            )
            if err is not None:
                # YAML write failed; don't claim the cache state is good either.
                return err
        # Save cache AFTER YAML write succeeds (T1.3 order invariant).
        cache.save()
        if resolved or failed:
            flash(
                f"Resolved {resolved} / {resolved + failed} tracker URLs on this entry.",
                "ok" if failed == 0 else "warn",
            )
        else:
            flash("No tracker URLs to resolve on this entry.", "info")
        return redirect(url_for("entry_view", section="publications", idx=idx))

    # ----- publications-specific (V3): cross-entry author rename -----

    @app.route("/publications/rename-author", methods=["GET", "POST"])
    def publication_rename_author():
        sch, path, header, data = _load_section("publications")
        all_names = author_rename.collect_unique_author_names(data)

        if request.method == "GET":
            return render_template(
                "rename_author.html",
                stage="ask",
                all_names=all_names,
                old_name="",
                new_name="",
                affected=[],
                mtime_ns=yaml_io.mtime_ns(path),
            )

        action = request.form.get("action", "preview")
        old_name = (request.form.get("old_name") or "").strip()
        new_name = (request.form.get("new_name") or "").strip()
        if not old_name:
            flash("Pick a source author name.", "warn")
            return redirect(url_for("publication_rename_author"))
        affected = author_rename.find_affected(data, old_name)

        if action == "preview":
            return render_template(
                "rename_author.html",
                stage="preview",
                all_names=all_names,
                old_name=old_name,
                new_name=new_name,
                affected=affected,
                mtime_ns=yaml_io.mtime_ns(path),
            )

        # apply
        if not new_name:
            flash("Target name is empty; nothing to do.", "warn")
            return redirect(url_for("publication_rename_author"))
        if new_name == old_name:
            flash("Target name equals source — no change to make.", "warn")
            return redirect(url_for("publication_rename_author"))
        n = author_rename.apply_rename(data, old_name, new_name)
        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=_get_expected_mtime_ns(request),
            redirect_to=url_for("publication_rename_author"),
        )
        if err:
            return err
        _kick_qc_if_idle()
        flash(f"Renamed {n} occurrence{'s' if n != 1 else ''}: {old_name!r} → {new_name!r}.", "ok")
        return redirect(url_for("section_list", section="publications"))

    # ----- publications-specific (V1b) -----

    @app.route("/publications/import", methods=["GET", "POST"])
    def publication_import():
        sch = schemas.get("publications")
        path = ROOT / sch["file"]

        if request.method == "GET":
            return render_template(
                "add_publication.html",
                section_label=sch["label"],
                subsections=sch["subsections"],
                default_subsection=sch.get("default_subsection"),
            )

        source = request.form.get("source", "paste")
        text = ""
        if source == "file":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("No file selected.", "warn")
                return redirect(url_for("publication_import"))
            try:
                text = f.read().decode("utf-8")
            except UnicodeDecodeError:
                f.seek(0)
                text = f.read().decode("latin-1", errors="replace")
        elif source == "doi_pmid":
            ident = (request.form.get("ident") or "").strip()
            doi, pmid = citation_parse.detect_id_from_paste(ident)
            if not (doi or pmid):
                flash("Could not detect a DOI or PMID in that input.", "warn")
                return redirect(url_for("publication_import"))
            staged = _stage_from_id(doi=doi, pmid=pmid)
            return _render_staged(staged, sch, path)
        elif source == "orcid":
            # CP5b: DISCOVERY ONLY. Fetch the ORCID public works list, extract
            # DOI/PMID refs, partition against the CV, and render a table whose
            # "new" rows each fire the EXISTING single-ID `doi_pmid` import above
            # (which re-enriches from PubMed/Crossref + stages for human review).
            # ORCID metadata never becomes YAML directly — this route never
            # writes. Outbound is one no-PII GET inside orcid_client (gotcha #14).
            orcid_id = (request.form.get("orcid_id") or "").strip()
            if not orcid_client.is_valid_orcid_id(orcid_id):
                flash(
                    "Not a well-formed ORCID iD (expected 0000-0000-0000-0000; "
                    "the last character may be X).",
                    "warn",
                )
                return redirect(url_for("publication_import"))
            works = orcid_client.fetch_works(orcid_id)
            if works is None:
                flash(
                    f"Could not fetch works for {orcid_id} — unknown iD, no "
                    "public works, or a network error. Check the iD and retry.",
                    "warn",
                )
                return redirect(url_for("publication_import"))
            refs = orcid_client.extract_external_ids(works)
            _, _, _, data = _load_section("publications")
            existing = [rec["entry"] for rec in sections.flatten(data, sch["structure"])]
            part = orcid_client.partition_against_cv(refs, existing)
            return render_template(
                "orcid_partition.html",
                section_label=sch["label"],
                orcid_id=orcid_id,
                n_discovered=len(refs),
                new=part.new,
                in_cv=part.in_cv,
                no_id=part.no_id,
            )
        else:
            text = (request.form.get("text") or "").strip()

        if not text:
            flash("Empty input.", "warn")
            return redirect(url_for("publication_import"))

        parsed = citation_parse.parse_citation_block(text)
        if not parsed:
            flash("Could not parse the input as BibTeX, NLM citation, or DOI text.", "warn")
            return redirect(url_for("publication_import"))

        if len(parsed) > 1:
            return render_template(
                "staged_list.html",
                section_label=sch["label"],
                staged=parsed,
            )
        return _render_staged(_enrich_parsed(parsed[0]), sch, path)

    def _stage_from_id(doi: str | None = None, pmid: str | None = None) -> dict:
        if pmid:
            res = enrichment.enrich_via_pmid(pmid)
            pm = (res.get("pubmed") or {}).get("data") or {}
            rec = pm.get(pmid)
            form = enrichment.to_form_entry(rec) if rec else {}
            form.setdefault("pmid", pmid)
            conv = (res.get("idconverter") or {}).get("data") or {}
            for k in ("doi", "pmcid"):
                if not form.get(k) and conv.get(k):
                    form[k] = conv[k]
            return {"merged": form, "disagreements": {}, "_source": "pmid"}
        if doi:
            res = enrichment.enrich_via_doi(doi)
            merged, disagree = enrichment.merge_canonical_into_form({"doi": doi}, res)
            return {"merged": merged, "disagreements": disagree, "_source": "doi"}
        return {"merged": {}, "disagreements": {}}

    def _enrich_parsed(parsed: dict) -> dict:
        if parsed.get("pmid"):
            res = enrichment.enrich_via_pmid(parsed["pmid"])
            pm = (res.get("pubmed") or {}).get("data") or {}
            rec = pm.get(parsed["pmid"])
            canonical = enrichment.to_form_entry(rec) if rec else {}
            return _merge_with_disagreements(parsed, canonical, res)
        if parsed.get("doi"):
            res = enrichment.enrich_via_doi(parsed["doi"])
            merged, disagree = enrichment.merge_canonical_into_form(parsed, res)
            return {
                "merged": merged,
                "disagreements": disagree,
                "_source": parsed.get("_source", "?"),
            }
        return {"merged": parsed, "disagreements": {}, "_source": parsed.get("_source", "?")}

    def _merge_with_disagreements(parsed: dict, canonical: dict, enrichment_result: dict) -> dict:
        non_author = [
            "title",
            "journal",
            "year",
            "month",
            "day",
            "volume",
            "issue",
            "pages",
            "doi",
            "pmcid",
        ]
        merged = dict(parsed)
        disagree = {}
        for f in non_author:
            cv = canonical.get(f)
            pv = merged.get(f)
            if cv in (None, "", []):
                continue
            if pv != cv and pv not in (None, "", []):
                disagree[f] = {"parsed": pv, "canonical": cv, "source": "pubmed"}
            merged[f] = cv
        if canonical.get("authors"):
            old_names = [
                a.get("name") if isinstance(a, dict) else a for a in (parsed.get("authors") or [])
            ]
            new_names = [a.get("name") for a in canonical["authors"]]
            if old_names != new_names:
                disagree["authors"] = {
                    "parsed": parsed.get("authors") or [],
                    "canonical": canonical["authors"],
                    "source": "pubmed",
                }
            merged.setdefault("authors", parsed.get("authors") or [])
        conv = (enrichment_result.get("idconverter") or {}).get("data") or {}
        for k in ("pmid", "doi", "pmcid"):
            if not merged.get(k) and conv.get(k):
                merged[k] = conv[k]
        return {"merged": merged, "disagreements": disagree, "_source": parsed.get("_source", "?")}

    def _render_staged(staged: dict, sch: dict, path) -> "Response":
        merged = staged.get("merged") or {}
        disagree = staged.get("disagreements") or {}
        # Build a CommentedMap entry from the merged form so _render_edit_form's
        # author_forms / notes_form derivation works uniformly.
        entry = CommentedMap()
        for k, v in merged.items():
            if v is not None:
                entry[k] = v
        return _render_edit_form(
            "publications",
            "new",
            sch,
            path,
            entry=entry,
            ctx={},
            global_idx=None,
            target_override={"subsection": sch.get("default_subsection")},
            disagreements=disagree,
            staged_source=staged.get("_source"),
        )

    @app.route("/publications/<int:idx>/promote", methods=["GET", "POST"])
    def publication_promote(idx: int):
        sch, path, header, data, rec = _resolve_idx("publications", idx)
        existing = rec["entry"]

        if request.method == "GET":
            return render_template(
                "promote_preprint.html",
                row={"global_idx": idx, "subsection": rec["ctx"].get("subsection", "")},
                entry=existing,
                stage="ask",
                diff=None,
                canonical=None,
                mtime_ns=yaml_io.mtime_ns(path),
                error=None,
            )

        action = request.form.get("action", "preview")
        ident = (request.form.get("ident") or "").strip()
        doi, pmid = citation_parse.detect_id_from_paste(ident)
        if not (doi or pmid):
            return render_template(
                "promote_preprint.html",
                row={"global_idx": idx, "subsection": rec["ctx"].get("subsection", "")},
                entry=existing,
                stage="ask",
                diff=None,
                canonical=None,
                mtime_ns=yaml_io.mtime_ns(path),
                error="Could not detect a DOI or PMID in that input.",
            )

        if pmid:
            res = enrichment.enrich_via_pmid(pmid)
            pm = (res.get("pubmed") or {}).get("data") or {}
            canonical = enrichment.to_form_entry(pm.get(pmid)) if pm.get(pmid) else {}
            canonical.setdefault("pmid", pmid)
        else:
            res = enrichment.enrich_via_doi(doi)
            pm = (res.get("pubmed") or {}).get("data") or {}
            cr = (res.get("crossref") or {}).get("data") or {}
            canonical = enrichment.to_form_entry(pm or cr)
            if doi and not canonical.get("doi"):
                canonical["doi"] = doi
        conv = (res.get("idconverter") or {}).get("data") or {}
        for k in ("pmid", "pmcid", "doi"):
            if not canonical.get(k) and conv.get(k):
                canonical[k] = conv[k]
        if not canonical:
            return render_template(
                "promote_preprint.html",
                row={"global_idx": idx, "subsection": rec["ctx"].get("subsection", "")},
                entry=existing,
                stage="ask",
                diff=None,
                canonical=None,
                mtime_ns=yaml_io.mtime_ns(path),
                error="No canonical record found for that ID.",
            )

        # V13-V19-D R3-H1 fix (2026-05-17): preserve EVERY field from
        # the existing entry, not just the bibliographic subset. Previously
        # the hardcoded list silently dropped `open_access`, `epub_date`,
        # `open_access_decided`, and any future schema additions on
        # promote — even though `build_promotion_diff` advertised them
        # as `preserves`. Mirrors the V18-A `_port_flags` fix in
        # `preprint.apply_promotion`.
        # Fields with structured form representations (authors, notes,
        # open_access) get their dedicated yaml→form converter; everything
        # else passes through as-is.
        _STRUCTURED_KEYS = {"authors", "notes", "open_access"}
        existing_form = {
            k: v
            for k, v in (existing.items() if isinstance(existing, dict) else [])
            if k not in _STRUCTURED_KEYS and v not in (None, "", [], {})
        }
        existing_form["authors"] = [
            author_to_form(a) for a in normalize_authors_for_render(existing.get("authors"))
        ]
        existing_form["notes"] = notes_helpers.notes_yaml_to_form(existing.get("notes"))
        if existing.get("open_access"):
            existing_form["open_access"] = notes_helpers.open_access_yaml_to_form(
                existing.get("open_access")
            )
        diff = preprint.build_promotion_diff(existing_form, canonical)

        if action == "preview":
            return render_template(
                "promote_preprint.html",
                row={"global_idx": idx, "subsection": rec["ctx"].get("subsection", "")},
                entry=existing,
                stage="preview",
                diff=diff,
                canonical=canonical,
                existing_form=existing_form,
                target_subsection=sch.get("default_subsection"),
                subsections=sch["subsections"],
                mtime_ns=yaml_io.mtime_ns(path),
                error=None,
            )

        target_subsection = request.form.get("target_subsection") or sch.get("default_subsection")
        keep_notes = request.form.get("keep_notes") == "on"
        author_choice = request.form.get("author_choice", "canonical")
        manual_authors_raw = request.form.get("manual_authors_json", "[]")
        try:
            chosen_authors = json.loads(manual_authors_raw) if manual_authors_raw else []
        except json.JSONDecodeError:
            chosen_authors = []
        if author_choice == "preprint":
            chosen = existing_form.get("authors") or []
        elif author_choice == "manual":
            chosen = chosen_authors
        else:
            chosen = canonical.get("authors") or []

        merged = preprint.apply_promotion(
            existing_form,
            canonical,
            chosen_authors=chosen,
            drop_notes=None if keep_notes else list(range(len(existing_form.get("notes") or []))),
        )

        sections.delete_entry(data, sch["structure"], rec["loc"])
        new_entry = _form_to_entry(merged, sch)
        sections.insert_entry(data, sch["structure"], {"subsection": target_subsection}, new_entry)
        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=_get_expected_mtime_ns(request),
            redirect_to=url_for("section_list", section="publications"),
        )
        if err:
            return err

        _kick_qc_if_idle()
        flash(f"Promoted preprint to published; entry moved to '{target_subsection}'.", "ok")
        return redirect(url_for("section_list", section="publications"))
