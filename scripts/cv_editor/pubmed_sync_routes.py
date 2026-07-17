"""V19 PubMed-sync UI routes (extracted from app.py 2026-05-29, M2b).

The most entangled extraction: the apply route participates in the
cross-system (QC <-> PubMed-sync) decision-silencing dance (gotcha #59).
To keep that sound, the shared/cross-system pieces are NOT moved — they
stay as create_app() closures and are handed in BY REFERENCE via deps:
  - both PubMed kickers (dry-run + apply) + their state dicts + the apply
    RLock (the kicker lock the apply route holds across check->write->kick)
  - `pubmed_sync_sidecar_path` (also read by _pubmed_sync_state_cached)
  - `qc_state_for_cross_check` + `pmsync_apply_clear_matching_qc_decisions`
    (the cross-system cluster, also touched by /qc/apply + entry_view)
  - the _PMSYNC_PENDING store (stash_raw + pop)
Only the pubmed_sync-SPECIFIC helpers + the 5 routes move here.

Routes registered here (endpoint names unchanged — register-on-app, gotcha #69):
  - GET  /pubmed_sync                  — pubmed_sync_view
  - POST /pubmed_sync/run              — pubmed_sync_run
  - GET  /pubmed_sync/status           — pubmed_sync_status_json
  - POST /pubmed_sync/apply            — pubmed_sync_apply
  - POST /pubmed_sync/apply_autofills  — pubmed_sync_apply_autofills (gotcha #81)
  - GET  /qc/pubmed_sync_report        — pubmed_sync_report_text

Behaviour-identical (M2 fingerprint + test_v19_pubmed_sync_ui +
test_v23b_phase15_decision_cross_check + test_tier_b_concurrent_apply).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from cv_editor import schemas, yaml_io


@dataclass
class PubmedSyncDeps:
    """DI surface for the PubMed-sync routes. The locks/state/kickers and
    the cross-system helpers are SHARED objects passed by reference — they
    must be the SAME objects create_app() uses elsewhere (gotchas #56/#69).
    This module only READS pmsync_state/pmsync_apply_state and HOLDS
    pmsync_apply_lock; it never reassigns them."""

    root: Path
    cfg_path: Callable[[str], Path]
    config: object  # app.config (for _PMSYNC_DECISIONS_CACHE)
    logger: object  # app.logger
    load_section: Callable[[str], tuple]
    pubmed_sync_sidecar_path: Callable[[], Path]
    qc_state_for_cross_check: Callable[[], tuple]
    pmsync_apply_clear_matching_qc_decisions: Callable[[list], int]
    pmsync_dryrun_kick: Callable[..., None]  # _kick_pubmed_dryrun_if_idle
    pmsync_state: dict  # _pmsync_state (read .["running"])
    pmsync_apply_kick: Callable[..., None]  # _kick_pubmed_apply_if_idle
    pmsync_apply_state: dict  # _pmsync_apply_state
    pmsync_apply_lock: object  # _pmsync_apply_lock (RLock)
    pmsync_stash_raw: Callable[[dict], str]  # _pmsync_stash_raw
    pmsync_pop_pending: Callable[[str], dict]  # _pmsync_pop_pending


def register_pubmed_sync_routes(app: Flask, deps: PubmedSyncDeps) -> None:
    ROOT = deps.root
    _cfg_path = deps.cfg_path
    _load_section = deps.load_section
    _pubmed_sync_sidecar_path = deps.pubmed_sync_sidecar_path
    _qc_state_for_cross_check = deps.qc_state_for_cross_check
    _pmsync_state = deps.pmsync_state
    _pmsync_apply_state = deps.pmsync_apply_state
    _pmsync_apply_lock = deps.pmsync_apply_lock

    _PMSYNC_REPORT_PATH = ROOT / "qc" / "pubmed_sync_report.md"

    def _pmsync_decisions_gen_path() -> Path:
        return _cfg_path("PMSYNC_DECISIONS_GEN_PATH")

    def _pubmed_sync_status():
        """Status payload for /pubmed_sync. Reads the sidecar + report
        mtimes; never touches the network."""
        from cv_editor.pubmed_sync import load_sidecar

        state = load_sidecar(_pubmed_sync_sidecar_path())
        sidecar_entries = len(state.entries)
        accepted_overrides = sum(len(v) for v in state.accepted_yaml_overrides.values())
        report_mtime = (
            datetime.fromtimestamp(_PMSYNC_REPORT_PATH.stat().st_mtime).isoformat(
                timespec="seconds"
            )
            if _PMSYNC_REPORT_PATH.exists()
            else None
        )
        return {
            "running_dryrun": _pmsync_state["running"],
            "running_apply": _pmsync_apply_state["running"],
            "sidecar_entries": sidecar_entries,
            "accepted_overrides": accepted_overrides,
            "report_mtime": report_mtime,
            "report_url": "/qc/pubmed_sync_report" if _PMSYNC_REPORT_PATH.exists() else None,
        }

    def _pubmed_sync_triage_rows():
        """Re-run the dry-run diff in-process to materialize the flagged
        decisions. Reads from the PubMed cache (`.cache/pubmed/`); only
        falls back to the network for PMIDs the dry-run didn't reach.

        Returns a list of plain dicts (one per entry) shaped for the
        template — each `flags` is a list of {field, yaml_value,
        pubmed_value} so Jinja can iterate without dataclass access.

        R-H3 fix (2026-05-17): bail when the sidecar is empty (i.e. no
        dry-run has ever run). compute_decisions would otherwise treat
        every PMID as needing refresh and synchronously fetch ~100
        PMIDs from PubMed on the request thread — first-visit cliff.

        Live-test fix (2026-05-17): pass `force=True` so the triage page
        re-diffs EVERY sidecar entry, not just those past TTL. Without
        this, an entry whose previous dry-run flagged a field (recorded
        in `sidecar.entries[pmid].fields_flagged`) wouldn't appear on
        the triage page while its sidecar TTL was fresh — the banner on
        entry_view would say "1 flag pending" but the triage page would
        show "No pending flags." With force=True every PMID goes through
        diff_one again; with a warm `.cache/pubmed/` the fetch is local
        and cheap."""
        from cv_editor.pubmed_sync import _author_list_preview, compute_decisions, load_sidecar

        sidecar_path = _pubmed_sync_sidecar_path()
        state = load_sidecar(sidecar_path)
        if not state.entries:
            return (
                [],
                (
                    "No dry-run has been run yet. Click 'Run dry-run' above to "
                    "populate the sidecar; the triage list will appear here on "
                    "the next page load (after ~30–60s)."
                ),
                [],
                [],
            )
        # V20-cleanup M1 (2026-05-18, post-impl-review HIGH-4 fix):
        # compute_decisions(force=True) previously used the module-level
        # SIDECAR_PATH constant — ignoring the app.config override. Tests
        # that redirected via PUBMED_SYNC_SIDECAR_PATH ended up reading
        # the real sidecar (mostly benign, occasionally surprising). Pass
        # the resolved path explicitly. Also caches the result via
        # _PMSYNC_DECISIONS_CACHE keyed on (sidecar.mtime_ns,
        # pubs.mtime_ns) — the editor ALWAYS calls force=True, so the
        # force flag is intentionally NOT in the key.
        #
        # FUTURE-PROOFING (V20-cleanup post-impl-review M_b): if a 2nd
        # caller in app.py ever passes a non-default `only_epub`,
        # `ttl_days`, `no_cache`, `cache_dir`, or `resolve_dois` to
        # compute_decisions below, the cache key MUST include those —
        # otherwise the cache would return the wrong result for the new
        # caller. Today there is only this one editor caller with fixed
        # kwargs (resolve_dois is fixed-False here — see the call below);
        # the assertion just below makes the constraint explicit.
        pubs_path = ROOT / schemas.get("publications")["file"]
        cache_state = deps.config["_PMSYNC_DECISIONS_CACHE"]
        sidecar_mtime = yaml_io.mtime_ns(sidecar_path) if sidecar_path.exists() else 0
        pubs_mtime = yaml_io.mtime_ns(pubs_path) if pubs_path.exists() else 0
        cache_key = (sidecar_mtime, pubs_mtime)
        if cache_state["key"] == cache_key and cache_state["result"] is not None:
            result = cache_state["result"]
        else:
            try:
                result = compute_decisions(
                    force=True,
                    # resolve_dois=False (the default) is LOAD-BEARING here:
                    # this runs on the request thread, and DOI resolution
                    # fires a live, cache-bypassed esearch. Keeping it off
                    # means the editor only ever SHOWS/APPLIES DOI
                    # resolutions a background dry-run already recorded in
                    # the sidecar — no uncached network on the hot path.
                    # See gotcha #81. (Fixed-False, like `force` above, so
                    # it stays out of the _PMSYNC_DECISIONS_CACHE key.)
                    resolve_dois=False,
                    sidecar_path=sidecar_path,
                )
            except Exception as e:
                deps.logger.warning("pubmed_sync compute_decisions failed: %s", e)
                return [], str(e), [], []
            cache_state["key"] = cache_key
            cache_state["result"] = result
        # V23-B Phase 1.5 (2026-05-26): build QC decisions index once
        # for cross-system silencing.
        from cv_editor.decision_cross_check import build_qc_decisions_index

        qc_sc, qc_dec = _qc_state_for_cross_check()
        qc_idx = build_qc_decisions_index(qc_sc, qc_dec) if qc_dec else {}
        # Map pmid -> live yaml entry so the cross-check can compute
        # canonical YAML values for normalization.
        _, _, _, pub_data = _load_section("publications")
        pubs_by_pmid: dict[str, dict] = {}
        for sub in pub_data:
            for e in sub.get("entries") or []:
                p = str(e.get("pmid") or "").strip()
                if p:
                    pubs_by_pmid[p] = e

        rows = []
        cross_silenced_rows = []  # (pmid, title_preview, global_idx, [(field, badge)])
        for d in result.decisions:
            # Apply cross-check (moves flags into d.cross_silenced).
            # NOTE: cached result is mutated; a sidecar mtime bump
            # invalidates the cache so stale cross-silenced state is
            # bounded to one render.
            if qc_idx and d.flags:
                from cv_editor.pubmed_sync import apply_overrides_to_decision

                apply_overrides_to_decision(
                    d,
                    None,  # no PubMed overrides — already applied
                    entry=pubs_by_pmid.get(d.pmid),
                    qc_decisions_index=qc_idx,
                )
            if d.cross_silenced:
                cs_field_rows = []
                for fname, badge in d.cross_silenced.items():
                    cs_field_rows.append(
                        {
                            "field": fname,
                            "badge": badge,
                        }
                    )
                cross_silenced_rows.append(
                    {
                        "pmid": d.pmid,
                        "global_idx": d.global_idx,
                        "title_preview": d.title_preview,
                        "fields": cs_field_rows,
                    }
                )
            if not d.flags:
                continue
            flag_rows = []
            for fname, (yv, pv) in d.flags.items():
                if fname == "authors":
                    yv_show = _author_list_preview(str(yv or ""))
                    pv_show = _author_list_preview(str(pv or ""))
                else:
                    yv_show = str(yv or "").replace("\n", " ")[:200]
                    pv_show = str(pv or "").replace("\n", " ")[:200]
                flag_rows.append(
                    {
                        "field": fname,
                        "yaml_value": yv_show,
                        "pubmed_value": pv_show,
                    }
                )
            rows.append(
                {
                    "pmid": d.pmid,
                    "global_idx": d.global_idx,
                    "title_preview": d.title_preview,
                    "publication_status": d.publication_status,
                    "flags": flag_rows,
                }
            )
        # Pending auto-fills (pmid/pmcid/volume/...). `apply_fills` writes
        # these on the next --apply regardless of any flag decision; the
        # "Apply auto-fills" button commits them without a keep/apply
        # choice (the fix for "resolved a PMID but no flag → no way to
        # apply from the UI"). resolved-from-DOI rows first. See gotcha #81.
        autofill_rows = []
        for d in result.decisions:
            if not d.fills:
                continue
            autofill_rows.append(
                {
                    "pmid": d.pmid,
                    "global_idx": d.global_idx,
                    "title_preview": d.title_preview,
                    "resolved_from_doi": getattr(d, "resolved_from_doi", False),
                    "fills": [{"field": k, "value": str(v)} for k, v in d.fills.items()],
                }
            )
        autofill_rows.sort(key=lambda r: (not r["resolved_from_doi"], r["global_idx"]))
        return rows, None, cross_silenced_rows, autofill_rows

    def _pmsync_stash_pending(form_dict: dict) -> str:
        """V20-cleanup M2: save the /pubmed_sync/apply form snapshot.
        Keeps only decision-/reason- fields (small payload). Returns
        a UUID hex (or "" if no decision fields). Persistence is
        handled by the factory; this just shapes the snapshot."""
        snapshot = {
            k: v
            for k, v in form_dict.items()
            if k.startswith("decision-") or k.startswith("reason-")
        }
        return deps.pmsync_stash_raw(snapshot)

    def _pmsync_write_and_kick(decisions: list[dict]) -> tuple[bool, int]:
        """Atomically write the editor gen-decisions file and kick
        `pubmed_sync.py --apply` in the background, holding the apply lock
        across the running-check + write + kick (R-H1 / V13-V19-D R1-H2).

        `decisions` MAY be empty: `--apply` still writes every pending
        auto-fill (pmid/pmcid/volume/...) via the CLI's unconditional
        `apply_fills`. That is exactly how the "Apply auto-fills" button
        commits a DOI-resolved PMID without needing a flag decision.

        Returns (started, qc_tombstoned). started=False → an apply was
        already running (caller flashes + redirects)."""
        import os as _os

        import yaml as pyyaml

        with _pmsync_apply_lock:
            if _pmsync_apply_state["running"]:
                return (False, 0)
            gen = _pmsync_decisions_gen_path()
            gen.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: tmp + fsync + os.replace (V13-V19-D R1-H2).
            tmp_path = gen.with_suffix(gen.suffix + ".tmp")
            with open(tmp_path, "w") as f:
                f.write("# V19 editor-generated decisions. Do not edit by hand.\n")
                pyyaml.safe_dump({"decisions": decisions}, f, sort_keys=False, allow_unicode=True)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp_path, gen)
            # V23-B Phase 1.5: tombstone matching QC decisions BEFORE the
            # subprocess kicks (local + sync; keeps /qc/triage consistent).
            qc_tombstoned = deps.pmsync_apply_clear_matching_qc_decisions(decisions)
            deps.pmsync_apply_kick(decisions_path=gen)
        return (True, qc_tombstoned)

    @app.route("/pubmed_sync", methods=["GET"])
    def pubmed_sync_view():
        status = _pubmed_sync_status()
        triage_rows, triage_error, cross_silenced_rows, autofill_rows = _pubmed_sync_triage_rows()
        # V20-cleanup M2: pop the snapshot once (consumed on first read).
        pending_form = deps.pmsync_pop_pending(request.args.get("pending") or "")
        return render_template(
            "pubmed_sync.html",
            status=status,
            triage_rows=triage_rows,
            triage_error=triage_error,
            cross_silenced_rows=cross_silenced_rows,
            autofill_rows=autofill_rows,
            pending_form=pending_form,
        )

    @app.route("/pubmed_sync/run", methods=["POST"])
    def pubmed_sync_run():
        force = bool(request.form.get("force"))
        only_epub = bool(request.form.get("only_epub"))
        deps.pmsync_dryrun_kick(force=force, only_epub=only_epub)
        msg = "PubMed dry-run kicked off in the background. Refresh in ~30–60s to see flagged entries."
        if force:
            msg += " (--force: ignoring TTL, re-fetching every PMID.)"
        elif only_epub:
            msg += " (--only-epub: refreshing only epub/ahead-of-print entries.)"
        flash(msg, "ok")
        return redirect(url_for("pubmed_sync_view"))

    @app.route("/pubmed_sync/status")
    def pubmed_sync_status_json():
        return _pubmed_sync_status()

    # Known field names the CLI's diff loop flags. Used for R-H2 validation:
    # any decision-<pmid>-<field> POST whose <field> isn't in this set is
    # rejected as malformed (catches future template bugs and hand-crafted
    # POSTs).
    _PMSYNC_FLAG_FIELDS = frozenset(("title", "journal", "doi", "authors", "year", "month", "day"))

    @app.route("/pubmed_sync/apply", methods=["POST"])
    def pubmed_sync_apply():
        """Collect per-flag triage decisions from the form, write a
        decisions YAML to qc/pubmed_sync_decisions.gen.yml, then kick
        `pubmed_sync.py --apply --decisions` in the background.

        Validation policy (post-review hardening 2026-05-17):
          R-H1: refuse the request if an apply is already running. Avoids
                the race where two near-simultaneous POSTs write the same
                gen file and only one kicker fires.
          R-H2: pmid must be all-digits and ≤12 chars; field must be in
                the known FLAG_FIELDS set. Anything else is dropped with
                a warn flash (would otherwise no-op silently in the CLI).
          R-H5: if any keep_yaml is missing a reason, REJECT the whole
                submission — don't write the file, don't kick the apply.
                Forces the user to fix before any partial commit lands.
        """
        # R-H1 (V19) + V13-V19-D R1-H2 (2026-05-17): the claim-check +
        # atomic gen-file write + kick all happen UNDER _pmsync_apply_lock
        # (in _pmsync_write_and_kick) so two concurrent POSTs can't write
        # torn YAML. `_pmsync_apply_state["running"]` stays True until the
        # daemon thread finishes.

        # Form shape: each flagged item posts decision-<pmid>-<field>=<choice>
        # and reason-<pmid>-<field>=<text>.
        decisions: list[dict] = []
        missing_reasons: list[tuple[str, str]] = []
        for key, value in request.form.items():
            if not key.startswith("decision-"):
                continue
            rest = key[len("decision-") :]
            if "-" not in rest:
                continue
            pmid, fname = rest.split("-", 1)
            # R-H2: validate pmid + field.
            if not pmid.isdigit() or len(pmid) > 12:
                flash(f"Malformed pmid {pmid!r} in form; decision dropped.", "warn")
                continue
            if fname not in _PMSYNC_FLAG_FIELDS:
                flash(f"Unknown field {fname!r} in form; decision dropped.", "warn")
                continue
            choice = (value or "").strip()
            if choice not in ("keep_yaml", "apply_pubmed"):
                continue  # skip "defer" / "edit_manually"
            reason = (request.form.get(f"reason-{pmid}-{fname}") or "").strip()
            if choice == "keep_yaml" and not reason:
                missing_reasons.append((pmid, fname))
                continue
            decisions.append(
                {
                    "pmid": pmid,
                    "field": fname,
                    "decision": choice,
                    "reason": reason,
                }
            )

        # R-H5: reject the whole submission on any missing-reason error.
        # Forces the user to fix every keep_yaml before ANY decision lands.
        # V20-cleanup M2: snapshot the form before redirecting so the
        # user doesn't lose 30+ rows of triage progress.
        if missing_reasons:
            detail = ", ".join(f"PMID {p}/{f}" for p, f in missing_reasons[:3])
            more = "" if len(missing_reasons) <= 3 else f" (+{len(missing_reasons) - 3} more)"
            flash(
                f"'Keep YAML' requires a reason. Missing on {detail}{more}. "
                "No decisions applied — fix the missing reasons and resubmit.",
                "warn",
            )
            token = _pmsync_stash_pending(dict(request.form.items()))
            target = url_for("pubmed_sync_view")
            if token:
                target += f"?pending={token}"
            return redirect(target)

        if not decisions:
            flash("No decisions to apply (no items had a non-blank choice).", "warn")
            return redirect(url_for("pubmed_sync_view"))

        started, qc_tombstoned = _pmsync_write_and_kick(decisions)
        if not started:
            flash(
                "An apply is already running — wait for it to finish "
                "(refresh in ~30s) before submitting again.",
                "warn",
            )
            return redirect(url_for("pubmed_sync_view"))
        extra = f" Tombstoned {qc_tombstoned} matching QC decision(s)." if qc_tombstoned else ""
        flash(
            f"Apply step kicked off for {len(decisions)} decision"
            f"{'' if len(decisions) == 1 else 's'}."
            + extra
            + " Refresh in ~30s to see the updated sidecar and any YAML rewrites.",
            "ok",
        )
        return redirect(url_for("pubmed_sync_view"))

    @app.route("/pubmed_sync/apply_autofills", methods=["POST"])
    def pubmed_sync_apply_autofills():
        """Commit pending auto-fills (pmid / pmcid / volume / ...) WITHOUT
        needing any flag decision. Kicks `--apply` with an EMPTY decisions
        list: the CLI's `apply_fills` writes every missing field it found
        (including a DOI-resolved PMID), while flagged disagreements are
        left untouched (they re-surface on the next dry-run). This is the
        fix for "sync resolved a PMID but the entry has no flag, so the
        triage form's Apply button never appears." See gotcha #81."""
        started, _qc = _pmsync_write_and_kick([])
        if not started:
            flash(
                "An apply is already running — wait for it to finish "
                "(refresh in ~30s) before submitting again.",
                "warn",
            )
            return redirect(url_for("pubmed_sync_view"))
        flash(
            "Applying auto-fills (pmid / pmcid / volume / ...) in the background. "
            "Flagged disagreements are left as-is. Refresh in ~30s.",
            "ok",
        )
        return redirect(url_for("pubmed_sync_view"))

    @app.route("/qc/pubmed_sync_report")
    def pubmed_sync_report_text():
        if not _PMSYNC_REPORT_PATH.exists():
            return (
                "qc/pubmed_sync_report.md not present yet — run a dry-run from /pubmed_sync first."
            ), 404
        return Response(_PMSYNC_REPORT_PATH.read_text(), mimetype="text/plain")
