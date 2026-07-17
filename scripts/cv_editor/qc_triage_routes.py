"""V23-B QC triage routes (Phase 3 + Phase 1).

Extracted from app.py 2026-05-28. The routes still need extensive
closure-scoped state (`_qc_apply_lock`, `_qc_apply_state`, the QC
sweep kicker, pending-store) so this module exposes a single
`register_qc_triage_routes(app, deps)` entry point that wires them
inside `create_app()`. `deps` is a SimpleNamespace carrying every
helper / state object that crosses the QC↔app.py boundary; see the
docstring on `QCTriageDeps` below for the full contract.

Routes registered here:
  - GET  /qc/triage                — `qc_triage_view`
  - POST /qc/triage/run            — `qc_triage_run`
  - GET  /qc/triage/status         — `qc_triage_status_json`
  - POST /qc/apply                 — `qc_apply`

Routes NOT moved (stay in app.py):
  - GET  /qc/report, GET /qc/status, POST /qc/run  — basic QC sweep
    (predates V23-B; uses a different kicker, no triage UI)
  - GET  /qc/urls_report, /qc/citations_report,
         /qc/pubmed_sync_report                    — other features

Helpers + state ALSO kept in app.py (cross-cutting):
  - `_qc_state_for_cross_check`, `_qc_apply_clear_matching_pmsync_overrides`
    (both called from /pubmed_sync/apply too)
  - `_cross_system_apply_lock` — held INSIDE
    `_qc_apply_clear_matching_pmsync_overrides`; the triage apply route
    reaches it transitively via that helper (no deps field needed)
  - `_qc_decisions_path` (called from the index route's banner block)
"""

from __future__ import annotations

import sys
import threading as _threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from filelock import Timeout
from flask import Flask, flash, redirect, render_template, request, url_for

# Allowed finding-id prefixes that the apply route accepts.
# Phase 1 covers MISMATCH (MM:), VARIANT (VA:), ID_ENRICHMENT (ID:).
# PMID_MISMATCH (PM:), SELF_ABSENT (SA:), AUTHOR_NAME_VARIANT (AN:),
# JOURNAL_NAME_VARIANT (JN:), MISSING_IDS (MI:) are not Phase 1.
_QC_APPLY_PREFIXES = ("MM:", "VA:", "ID:")
_QC_VALID_DECISIONS = frozenset(("apply", "keep_yaml", "defer"))


@dataclass
class QCTriageDeps:
    """The dependency-injection surface for the QC triage routes.

    Kept as a dataclass for typing + explicit naming; create_app()
    populates it after building all its closure-scoped helpers and
    then calls `register_qc_triage_routes(app, deps)`.

    The deps object IS the seam — every field here used to be a
    name in `create_app()` that the route handlers closed over
    implicitly. Now they close over `deps.*`.
    """

    ROOT: Path
    yaml_io: object  # cv_editor.yaml_io module
    qc_decisions_path: Callable[[], Path]
    load_section: Callable[[str], tuple]
    pending_save_warning: Callable[[str | None], str]
    qc_apply_clear_matching_pmsync_overrides: Callable[[list], int]
    pubmed_sync_state_cached: Callable[[], object]
    make_pending_store: Callable[[str, int], tuple]
    make_kicker: Callable[..., tuple]
    # Shared-by-reference apply state + RLock (gotcha #69). REQUIRED — no
    # default. create_app() MUST hand in the SAME objects it uses for the
    # cross-system clears + sweep gating, so a dropped kwarg fails loudly
    # at construction instead of silently giving this module a PRIVATE
    # lock/state (which would diverge with green tests — M2 review HIGH-1).
    qc_apply_state: dict
    qc_apply_lock: _threading.RLock


def register_qc_triage_routes(app: Flask, deps: QCTriageDeps) -> None:
    """Define the V23-B Phase 3 + Phase 1 routes on `app`.

    Returns None. The QC sweep kicker, pending-store, and sweep-status
    helpers built here are used ONLY by the routes in this module (the
    apply route calls `maybe_kick_qc_sweep` directly). An earlier
    version returned a 6-key handle dict, but `create_app()` always
    discarded it — no caller ever consumed it (M3.0 cleanup).

    Mutates `deps.qc_apply_state` (the running-flag) under
    `deps.qc_apply_lock`; every other field of deps is read-only.
    """
    ROOT = deps.ROOT
    yaml_io = deps.yaml_io

    # ----- QC sweep kicker (subprocess: qc_publications.py) -----
    kick_qc_sweep_if_idle, qc_sweep_state, _qc_sweep_lock = deps.make_kicker(
        name="qc_sweep",
        build_argv=lambda: [
            sys.executable,
            "-m",
            "cv_editor.qc_publications",
        ],
        timeout=600,
    )

    # ----- apply lock + state + pending store -----
    # Held in deps so the index route + post-save hooks can introspect.
    qc_stash_pending, qc_pop_pending = deps.make_pending_store(
        "_QC_PENDING",
        20,
    )

    def maybe_kick_qc_sweep():
        """Sweep kicker that refuses to fire during apply.

        Post-impl C-M4 fix (2026-05-25): the check + kick must be
        atomic to close a TOCTOU window where apply flips `running`
        between the check and the kick. RLock so the apply route
        (which holds qc_apply_lock) can still call this from inside
        the lock without deadlocking."""
        with deps.qc_apply_lock:
            if deps.qc_apply_state.get("running"):
                return  # apply has the lock; sweep would race
            kick_qc_sweep_if_idle()

    # ----- inline helpers used by the routes -----

    def qc_triage_status_payload():
        """Status payload for /qc/triage. Reads the sidecar + scratches
        running-state from the kicker; tolerates missing sidecar."""
        from cv_editor import qc_sync
        from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

        sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
        totals = qc_sync.summary_totals(sidecar)
        return {
            "sidecar_loaded": sidecar is not None,
            "total_findings": totals["total_findings"],
            "totals": totals["totals"],
            "generated_at": (sidecar or {}).get("generated_at"),
            "running": bool(qc_sweep_state.get("running")),
            "applying": bool(deps.qc_apply_state.get("running")),
            "report_url": "/qc/report" if (ROOT / "qc" / "report.md").exists() else None,
        }

    def qc_load_publications_by_idx():
        """Load publications.yml and return (data, by_idx) where by_idx
        maps global_idx -> entry dict, matching the global_idx scheme
        in qc_publications.py:208 (s_idx * 10000 + e_idx)."""
        _, _, _, data = deps.load_section("publications")
        by_idx = {}
        for s_idx, sub in enumerate(data):
            for e_idx, entry in enumerate(sub.get("entries") or []):
                by_idx[s_idx * 10000 + e_idx] = entry
        return data, by_idx

    def qc_seq_idx_map():
        """Map qc_global_idx (s_idx*10000+e_idx) -> sequential flatten
        idx that entry_edit / entry_view route on. Post-impl C-H2 fix
        (2026-05-25): triage 'Jump to edit' links must translate from
        the sidecar's QC idx to the route's sequential idx."""
        try:
            _, _, _, data = deps.load_section("publications")
        except Exception:
            return {}
        out = {}
        seq = 0
        for s_idx, sub in enumerate(data or []):
            for e_idx, _entry in enumerate(sub.get("entries") or []):
                out[s_idx * 10000 + e_idx] = seq
                seq += 1
        return out

    def qc_parse_apply_form(form, sidecar_findings_by_id):
        """Parse the triage form into a list of (finding_id, decision,
        reason) tuples. Validate finding_id is in sidecar AND a Phase 1
        prefix AND decision is in VALID_DECISIONS. Returns
        (decisions_list, errors_list)."""
        decisions = []
        errors = []
        for key in form.keys():
            if not key.startswith("decision-"):
                continue
            fid = key[len("decision-") :]
            if not any(fid.startswith(p) for p in _QC_APPLY_PREFIXES):
                errors.append(f"finding {fid}: not a Phase 1 finding type")
                continue
            dec_val = form.get(key, "").strip()
            if not dec_val:
                continue  # unset radio == no decision
            if dec_val not in _QC_VALID_DECISIONS:
                errors.append(f"finding {fid}: invalid decision {dec_val!r}")
                continue
            if fid not in sidecar_findings_by_id:
                errors.append(f"finding {fid}: not in current sidecar")
                continue
            reason = (form.get(f"reason-{fid}") or "").strip()
            decisions.append(
                {
                    "finding_id": fid,
                    "decision": dec_val,
                    "reason": reason or None,
                    "finding": sidecar_findings_by_id[fid],
                }
            )
        return decisions, errors

    def qc_validate_length_changed_authors(decisions):
        """Reject `apply` decisions on authors-field rows with
        length_changed=True UNLESS the row was also marked as confirmed
        via a per-row `confirm-<fid>=1` form field. Strict reject (no
        silent downgrade) per spec §6 + UX H4.

        Returns list of rejected finding_ids (empty if all OK)."""
        rejected = []
        for d in decisions:
            if d["decision"] != "apply":
                continue
            f = d["finding"]
            if f.get("field") != "authors":
                continue
            if not f.get("length_changed"):
                continue
            if not d.get("_confirmed_length_changed"):
                rejected.append(d["finding_id"])
        return rejected

    # ----- routes -----

    @app.route("/qc/triage", methods=["GET"])
    def qc_triage_view():
        from cv_editor import qc_decisions, qc_sync
        from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

        sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
        decisions = qc_decisions.load(deps.qc_decisions_path(), silent=True)
        try:
            _, by_idx = qc_load_publications_by_idx()
        except Exception as exc:
            app.logger.warning("qc_triage: publications load failed: %s", exc)
            by_idx = {}
        effective = qc_sync.effective_findings(
            sidecar,
            decisions,
            current_yaml_by_global_idx=by_idx,
            pubmed_sync_state=deps.pubmed_sync_state_cached(),
        )
        # Build per-section rendering: sections from iter_finding_sections
        # use the RAW sidecar (so phase placeholders work); for Phase 1
        # active types we substitute effective rows.
        sections = list(qc_sync.iter_finding_sections(sidecar))
        for sec in sections:
            key = sec["key"]
            # self_absent (2026-06-08): acknowledged rows are filtered out
            # of `effective`, so substitute the effective rows here too.
            if key in ("mismatches", "variants", "id_enrichments", "self_absent"):
                sec["rows"] = effective.get(key, [])
                sec["count"] = len(sec["rows"])
        # Acknowledged self_absent rows (suppressed above) for the
        # reversible "Acknowledged (N)" collapsible. Raw sidecar rows
        # whose id carries a non-defer decision.
        acknowledged_self_absent = []
        for f in (sidecar or {}).get("findings", {}).get("self_absent", []) or []:
            dec = decisions.get(f.get("id", ""))
            if dec is not None and dec.decision != "defer":
                acknowledged_self_absent.append(f)
        # V23-B Phase 1.5 (2026-05-26): cross-silenced rows per section
        # for the "Silenced by PubMed sync" collapsible below each table.
        cross_silenced_by_section = effective.get("cross_silenced") or {}
        status = qc_triage_status_payload()
        # Pending-form snapshot (post-409 re-population).
        pending_form = qc_pop_pending(request.args.get("pending") or "")
        # Diff banner for stashed decisions whose IDs vanished /
        # appeared since the user started (UX H3).
        pending_diff = {"vanished": [], "appeared": []}
        if pending_form:
            current_ids = set()
            for key in ("mismatches", "variants", "id_enrichments"):
                for f in (sidecar or {}).get("findings", {}).get(key, []) or []:
                    fid = f.get("id")
                    if fid:
                        current_ids.add(fid)
            stashed_ids = set(pending_form.get("decision_ids") or [])
            # Post-impl U-M3 fix (2026-05-25): don't pre-slice — the
            # template clamps with `[:5]` and shows "(... and N more)"
            # when the true count exceeds the displayed sample.
            pending_diff["vanished"] = sorted(stashed_ids - current_ids)
            pending_diff["appeared"] = sorted(current_ids - stashed_ids)
        return render_template(
            "qc_triage.html",
            current_section="qc_triage",
            sections=sections,
            status=status,
            anchor_for=qc_sync.entry_edit_anchor,
            decisions=decisions,
            pending_form=pending_form,
            pending_diff=pending_diff,
            qc_to_seq=qc_seq_idx_map(),
            cross_silenced_by_section=cross_silenced_by_section,
            cross_silenced_total=qc_sync.cross_silenced_total(effective),
            acknowledged_self_absent=acknowledged_self_absent,
        )

    @app.route("/qc/triage/run", methods=["POST"])
    def qc_triage_run():
        # Refuse if apply is in flight (UX M2): flash banner-info so the
        # user knows the post-apply sweep will catch them up automatically.
        if deps.qc_apply_state.get("running"):
            flash(
                "Apply in progress; QC sweep will run automatically when it finishes.",
                "info",
            )
            return redirect(url_for("qc_triage_view"))
        maybe_kick_qc_sweep()
        flash(
            "QC sweep kicked off in the background. Refresh in ~30–90s to see updated findings.",
            "ok",
        )
        return redirect(url_for("qc_triage_view"))

    @app.route("/qc/triage/status")
    def qc_triage_status_json():
        """JSON status endpoint for polling (parallels pubmed_sync_status)."""
        return qc_triage_status_payload()

    @app.route("/qc/apply", methods=["POST"])
    def qc_apply():
        """V23-B Phase 1 apply route. Writes decisions sidecar + applies
        per-row apply decisions to publications.yml as ONE atomic batch
        (one read -> N in-memory mutations -> one write_with_backup).

        Correctness contract (reviewer-locked):
          - try/finally clears deps.qc_apply_state['running'] (C-H1)
          - Catches Timeout from write_with_backup (C-H2)
          - mtime check INSIDE write_with_backup via expected_mtime_ns (C-H3)
          - Re-validates sidecar IDs + canonical_value_at_decision INSIDE
            the apply lock to catch CLI-bypass sidecar drift (C-H4)
          - length_changed authors validated BEFORE the lock (M5)
        """
        from cv_editor import qc_decisions, qc_sync
        from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

        # Step 1: load sidecar (outer; the inner re-load happens under
        # the apply lock for the CLI-bypass guard).
        sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
        if sidecar is None:
            flash("No QC sidecar present; run a sweep first.", "warn")
            return redirect(url_for("qc_triage_view"))

        # Build {finding_id -> finding} index for fast validation.
        all_findings_by_id = {}
        for key in ("mismatches", "variants", "id_enrichments"):
            for f in sidecar.get("findings", {}).get(key, []):
                fid = f.get("id")
                if fid:
                    all_findings_by_id[fid] = dict(f)
                    all_findings_by_id[fid]["_finding_type"] = (
                        "MISMATCH"
                        if key == "mismatches"
                        else ("VARIANT" if key == "variants" else "ID_ENRICHMENT")
                    )

        # Step 2: parse + validate form.
        decisions, form_errors = qc_parse_apply_form(
            request.form,
            all_findings_by_id,
        )
        # Post-impl C-M1 fix (2026-05-25): flash a single warn line
        # listing the count + first 3 IDs of silently-dropped rows so
        # the user notices malformed submissions instead of a quiet
        # "Recorded 0 decisions".
        if form_errors:
            preview = ", ".join(form_errors[:3])
            more = f" (+{len(form_errors) - 3} more)" if len(form_errors) > 3 else ""
            flash(
                f"Dropped {len(form_errors)} malformed decision row(s): {preview}{more}",
                "warn",
            )

        # Annotate length_changed confirmations from the form.
        for d in decisions:
            confirm_key = f"confirm-{d['finding_id']}"
            d["_confirmed_length_changed"] = bool(request.form.get(confirm_key))

        # Step 3: length_changed authors guard (fail-fast pre-lock).
        rejected_lc = qc_validate_length_changed_authors(decisions)
        if rejected_lc:
            token = qc_stash_pending(
                {
                    "decision_ids": [d["finding_id"] for d in decisions],
                    "form": dict(request.form),
                    "rejected_length_changed": rejected_lc,
                }
            )
            flash(
                f"Rejected {len(rejected_lc)} length-changed author apply(s); "
                "confirm individually per row. Your other decisions are preserved.",
                "warn",
            )
            return redirect(url_for("qc_triage_view", pending=token))

        # Step 4: required-reason guard for MISMATCH + keep_yaml.
        missing_reasons = []
        for d in decisions:
            if d["decision"] != "keep_yaml":
                continue
            ftype = d["finding"].get("_finding_type")
            if ftype != "MISMATCH":
                continue  # reason optional for VARIANT + ID_ENRICHMENT
            if not d.get("reason"):
                missing_reasons.append(d["finding_id"])
        if missing_reasons:
            token = qc_stash_pending(
                {
                    "decision_ids": [d["finding_id"] for d in decisions],
                    "form": dict(request.form),
                    "missing_reasons": missing_reasons,
                }
            )
            flash(
                f"{len(missing_reasons)} keep_yaml MISMATCH decision(s) "
                "missing a reason. Your other decisions are preserved.",
                "warn",
            )
            return redirect(url_for("qc_triage_view", pending=token))

        # Step 5+: take apply lock + atomic batch.
        applies: list = []
        with deps.qc_apply_lock:
            deps.qc_apply_state["running"] = True
            try:
                # Re-load sidecar INSIDE the lock to catch CLI-bypass
                # sidecar drift (C-H4).
                live_sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
                live_ids = set()
                for key in ("mismatches", "variants", "id_enrichments"):
                    for f in (live_sidecar or {}).get("findings", {}).get(key, []):
                        fid = f.get("id")
                        if fid:
                            live_ids.add(fid)
                drifted = [d["finding_id"] for d in decisions if d["finding_id"] not in live_ids]
                if drifted:
                    token = qc_stash_pending(
                        {
                            "decision_ids": [d["finding_id"] for d in decisions],
                            "form": dict(request.form),
                            "drifted": drifted,
                        }
                    )
                    flash(
                        f"{len(drifted)} finding(s) no longer in the QC sidecar "
                        "(CLI sweep ran between page load and submit). "
                        "Re-review and try again.",
                        "warn",
                    )
                    return redirect(
                        url_for(
                            "qc_triage_view",
                            pending=token,
                        )
                    )

                # Load existing decisions sidecar; tombstone IDs that
                # no longer exist; prune expired tombstones.
                existing = qc_decisions.load(deps.qc_decisions_path(), silent=True)
                for fid in list(existing.decisions.keys()):
                    if fid not in live_ids:
                        existing.remove(fid)
                existing.prune_expired_tombstones()

                # Update / replace decisions from this batch.
                for d in decisions:
                    f = d["finding"]
                    ftype = f.get("_finding_type", "MISMATCH")
                    set_kwargs = dict(
                        decision=d["decision"],
                        finding_type=ftype,
                    )
                    if d.get("reason"):
                        set_kwargs["reason"] = d["reason"]
                    if ftype in ("MISMATCH", "VARIANT"):
                        set_kwargs["yaml_value_at_decision"] = str(f.get("yaml_value") or "")
                        set_kwargs["canonical_value_at_decision"] = str(
                            f.get("canonical_value") or ""
                        )
                    elif ftype == "ID_ENRICHMENT":
                        set_kwargs["suggested_value_at_decision"] = str(
                            f.get("suggested_value") or ""
                        )
                    existing.set(d["finding_id"], **set_kwargs)

                # Build the apply set (decisions whose value is "apply"
                # and whose finding is in Phase 1's coverage).
                applies = [d for d in decisions if d["decision"] == "apply"]

                applied_count = 0
                skipped_already_filled = 0
                if applies:
                    # ONE read of publications.yml.
                    sch, pubs_path, header, pub_data = deps.load_section("publications")
                    by_idx = {}
                    for s_idx, sub in enumerate(pub_data):
                        for e_idx, entry in enumerate(sub.get("entries") or []):
                            by_idx[s_idx * 10000 + e_idx] = entry
                    # N in-memory mutations.
                    from ruamel.yaml.comments import CommentedSeq as _CSeq

                    for d in applies:
                        f = d["finding"]
                        gidx = f.get("global_idx")
                        entry = by_idx.get(gidx)
                        if entry is None:
                            continue  # entry shifted/deleted; skip
                        ftype = f.get("_finding_type", "MISMATCH")
                        if ftype in ("MISMATCH", "VARIANT"):
                            field_name = f.get("field")
                            new_val = f.get("canonical_value")
                            if field_name and new_val is not None:
                                # Task #30 root-cause fix (2026-05-25):
                                # qc_publications.py joins canonical
                                # authors as "; "-separated string for
                                # display, but the YAML field MUST be a
                                # list. Without this conversion, the
                                # apply writes `authors: a; b; c; d`
                                # (bare scalar) — which corrupts the
                                # YAML and breaks `./build.sh`.
                                if field_name == "authors" and isinstance(new_val, str):
                                    names = [n.strip() for n in new_val.split(";") if n.strip()]
                                    new_val = _CSeq(names)
                                entry[field_name] = new_val
                                applied_count += 1
                        elif ftype == "ID_ENRICHMENT":
                            field_name = f.get("suggested_field")
                            new_val = f.get("suggested_value")
                            # Post-impl C-M2 fix (2026-05-25): if the user
                            # added the ID manually between sweep and apply,
                            # don't clobber it.
                            if field_name and new_val is not None:
                                if entry.get(field_name):
                                    skipped_already_filled += 1
                                else:
                                    entry[field_name] = new_val
                                    applied_count += 1
                    # ONE write_with_backup. mtime guard inside via
                    # expected_mtime_ns (C-H3).
                    expected_ns = live_sidecar.get("publications_yml_mtime_ns")
                    # M1 (2026-05-29): FAIL CLOSED when the sidecar lacks
                    # the publications mtime. Previously expected_ns=None
                    # silently skipped the stale-file guard in
                    # write_with_backup — the task #42 corruption vector
                    # (a fixture/sidecar without the mtime could overwrite
                    # live data unguarded). Refuse and tell the user to
                    # re-sweep rather than write without protection.
                    if expected_ns is None:
                        token = qc_stash_pending(
                            {
                                "decision_ids": [d["finding_id"] for d in decisions],
                                "form": dict(request.form),
                                "missing_mtime": True,
                            }
                        )
                        flash(
                            "The QC sidecar is missing publications.yml's "
                            "modification time, so the safe-write guard "
                            "cannot run. Re-run the QC sweep, then apply "
                            "again. Your decisions are preserved.",
                            "warn",
                        )
                        return redirect(url_for("qc_triage_view", pending=token))
                    try:
                        yaml_io.write_with_backup(
                            pubs_path,
                            header,
                            pub_data,
                            expected_mtime_ns=expected_ns,
                        )
                    except yaml_io.StaleFileError:
                        token = qc_stash_pending(
                            {
                                "decision_ids": [d["finding_id"] for d in decisions],
                                "form": dict(request.form),
                                "stale_mtime": True,
                            }
                        )
                        flash(
                            "publications.yml was modified between the QC "
                            "sweep and your apply. Your decisions are "
                            "preserved; re-run the sweep + try again.",
                            "warn",
                        )
                        return redirect(
                            url_for(
                                "qc_triage_view",
                                pending=token,
                            )
                        )
                    except Timeout:
                        token = qc_stash_pending(
                            {
                                "decision_ids": [d["finding_id"] for d in decisions],
                                "form": dict(request.form),
                                "timeout": True,
                            }
                        )
                        flash(
                            "Another writer holds publications.yml; try again "
                            "in a moment. Your decisions are preserved.",
                            "warn",
                        )
                        return redirect(
                            url_for(
                                "qc_triage_view",
                                pending=token,
                            )
                        )
                    except yaml_io.CorruptedShapeError as e:
                        # Task #42 (2026-05-26): the apply attempted to
                        # write a known-bad shape. Refuse and surface to
                        # the user rather than silently mutating YAML.
                        token = qc_stash_pending(
                            {
                                "decision_ids": [d["finding_id"] for d in decisions],
                                "form": dict(request.form),
                                "corrupted_shape": True,
                            }
                        )
                        flash(
                            f"Refused to apply — write guard caught a "
                            f"corruption pattern: {e}. Your decisions "
                            f"are preserved; investigate the canonical "
                            f"value in the QC sidecar before retrying.",
                            "warn",
                        )
                        return redirect(
                            url_for(
                                "qc_triage_view",
                                pending=token,
                            )
                        )

                # Persist decisions sidecar atomically.
                existing.save_atomic(deps.qc_decisions_path())
            finally:
                deps.qc_apply_state["running"] = False

        # V23-B Phase 1.5 (2026-05-26): cross-system auto-clear.
        # For each `apply` decision on a CROSS_FIELDS field, if a
        # matching PubMed-sync override exists for the same (pmid,
        # field), remove it. Pre-emptively clear to avoid the
        # one-render flap.
        pmsync_cleared = deps.qc_apply_clear_matching_pmsync_overrides(applies)

        # Post-apply: kick a fresh sweep so the sidecar reflects the
        # new YAML state.
        maybe_kick_qc_sweep()

        decided = len(decisions)
        extra = (
            f" Skipped {skipped_already_filled} ID enrichment(s) where the "
            "field was already populated."
            if skipped_already_filled
            else ""
        )
        if pmsync_cleared:
            extra += f" Cleared {pmsync_cleared} matching PubMed sync override(s)."
        flash(
            f"Recorded {decided} decision(s); applied {applied_count} "
            "field overwrite(s)."
            + extra
            + " QC sweep kicked off — refresh in ~30s for the updated sidecar.",
            "ok",
        )
        return redirect(url_for("qc_triage_view"))

    @app.route("/qc/self_absent/acknowledge", methods=["POST"])
    def qc_self_absent_acknowledge():
        """Acknowledge (dismiss) or un-acknowledge a SELF_ABSENT finding
        (2026-06-08).

        SELF_ABSENT has no apply semantics — there's nothing to auto-fix;
        the user is asserting the paper legitimately has no self-author.
        We record a `keep_yaml` decision keyed by the `SA:` finding id;
        `qc_sync.effective_findings` then suppresses the row (and the
        banner counts, since they sum `effective`). Form `undo=1` removes
        the decision so the finding reappears.

        Held under `deps.qc_apply_lock` (the shared RLock) so a concurrent
        `/qc/apply` decisions write can't clobber this one (lost update).
        """
        from cv_editor import qc_decisions

        fid = (request.form.get("finding_id") or "").strip()
        undo = (request.form.get("undo") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not fid.startswith("SA:"):
            flash(f"Not a self-absent finding: {fid!r}.", "warn")
            return redirect(url_for("qc_triage_view"))
        with deps.qc_apply_lock:
            existing = qc_decisions.load(deps.qc_decisions_path(), silent=True)
            if undo:
                existing.remove(fid)
                msg = (
                    "Un-acknowledged — the entry will reappear in the "
                    "self-absent list on the next render."
                )
            else:
                existing.set(
                    fid,
                    decision="keep_yaml",
                    finding_type="SELF_ABSENT",
                )
                msg = (
                    "Acknowledged — dismissed from the self-absent list. "
                    "Use the 'Acknowledged' list to undo."
                )
            existing.save_atomic(deps.qc_decisions_path())
        flash(msg, "ok")
        return redirect(url_for("qc_triage_view"))

    # No return value (M3.0): the kicker / pending-store / sweep-status
    # helpers built above are referenced only by the routes registered
    # here; create_app() needs nothing back.
