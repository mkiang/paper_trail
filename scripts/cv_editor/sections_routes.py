"""Section-generic CRUD + meta routes (extracted from app.py 2026-05-29, M2b).

The hot path: every YAML section is edited through these generic
`/<section>/*` routes. `entry_view` is the most banner-entangled route in
the app (it reads QC + PubMed-sync + tracker + citation state for its
banners), so it gets the largest deps surface. Plus the meta single-record
routes.

Everything these routes call stays as create_app() closures and is handed
in BY REFERENCE via deps — including the `require_section` decorator
factory, the entry pending-store pair, and the cross-system banner helpers
(`pubmed_flagged_for_pmid`, `pubmed_sync_state_cached`,
`pubmed_cross_silenced_for_pmid`). Zero logic moved; behaviour-identical.

Routes (endpoint names unchanged — register-on-app, gotcha #69):
  GET  /<section>                      section_list
  GET  /<section>/new                  entry_new
  GET  /<section>/<int:idx>            entry_view
  GET  /<section>/<int:idx>/edit       entry_edit
  POST /<section>/save                 entry_save
  POST /<section>/<int:idx>/duplicate  entry_duplicate
  POST /<section>/<int:idx>/delete     entry_delete
  POST /<section>/undo                 entry_undo
  GET  /<section>/backups              entry_backups
  GET  /<section>/backups/<name>/diff  backup_diff
  POST /<section>/restore              entry_restore
  GET  /meta                           meta_view
  GET  /meta/edit                      meta_edit
  POST /meta/save                      meta_save

Validated by M2 fingerprint + test_v2_routes/test_v2_sections + the V17/V20
restore/duplicate/save suites.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from filelock import Timeout
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from cv_editor import (
    notes_helpers,
    preprint,
    schemas,
    sections,
    validate,
    yaml_io,
)


@dataclass
class SectionsDeps:
    """DI surface for the section-generic CRUD + meta routes. All fields
    are create_app() closures/objects shared by reference (the cross-system
    banner helpers + entry pending store + require_section decorator)."""

    root: Path
    load_section: Callable
    require_section: Callable
    self_name: Callable
    resolve_idx: Callable
    render_edit_form: Callable
    derive_form_view_state: Callable
    row_for_listing: Callable
    form_to_entry: Callable
    form_payload: Callable
    target_from_form: Callable
    get_expected_mtime_ns: Callable
    write_or_409: Callable
    qc_status: Callable
    entry_unresolved_tracker_count: Callable
    pubmed_flagged_for_pmid: Callable
    pubmed_sync_state_cached: Callable
    pubmed_cross_silenced_for_pmid: Callable
    qc_decisions_path: Callable
    entry_stash_pending: Callable
    entry_pop_pending: Callable
    pending_save_warning: Callable
    qc_kick: Callable[..., None]


def _js_unmounted_rejection():
    """M4 data-safety guard. The entry-edit form ships a `js_mounted` hidden
    field (value="") that entry_edit.js flips to "1" ONLY after every editor
    mounts. If it is PRESENT but != "1", the page rendered but its JavaScript
    did not finish (noscript / a JS error / a partial mount), so the JS-driven
    hidden fields (authors / notes / open_access / audiences / sections) would
    submit EMPTY and silently WIPE the existing values. Refuse the save then.

    A MISSING field (a non-form or test POST) is allowed: the production
    entry-edit template always renders the field, so the only POSTs without it
    are scripted/test ones, which are not the JS-mount-failure threat. Returns
    a redirect Response to abort the save, or None to let it proceed.
    """
    mounted = request.form.get("js_mounted")
    if mounted is not None and mounted != "1":
        flash(
            "The editor didn't finish loading (JavaScript), so your changes "
            "were NOT saved — this protects your existing data from being "
            "cleared. Reload the page with JavaScript enabled and try again.",
            "warn",
        )
        return redirect(request.referrer or url_for("index"))
    return None


def register_sections_routes(app: Flask, deps: SectionsDeps) -> None:
    ROOT = deps.root
    _load_section = deps.load_section
    _self_name = deps.self_name
    _resolve_idx = deps.resolve_idx
    _render_edit_form = deps.render_edit_form
    _derive_form_view_state = deps.derive_form_view_state
    _row_for_listing = deps.row_for_listing
    _form_to_entry = deps.form_to_entry
    _form_payload = deps.form_payload
    _target_from_form = deps.target_from_form
    _get_expected_mtime_ns = deps.get_expected_mtime_ns
    write_or_409 = deps.write_or_409
    _qc_status = deps.qc_status
    _entry_unresolved_tracker_count = deps.entry_unresolved_tracker_count
    _pubmed_flagged_for_pmid = deps.pubmed_flagged_for_pmid
    _pubmed_sync_state_cached = deps.pubmed_sync_state_cached
    _pubmed_cross_silenced_for_pmid = deps.pubmed_cross_silenced_for_pmid
    _qc_decisions_path = deps.qc_decisions_path
    _entry_stash_pending = deps.entry_stash_pending
    _entry_pop_pending = deps.entry_pop_pending
    _pending_save_warning = deps.pending_save_warning
    _kick_qc_if_idle = deps.qc_kick
    require_section = deps.require_section

    @app.route("/<section>")
    @require_section(on_meta="redirect_view")
    def section_list(section: str):
        sch, _, _, data = _load_section(section)
        self_nm = _self_name()
        rows = [
            _row_for_listing(section, sch, rec, self_nm)
            for rec in sections.flatten(data, sch["structure"])
        ]

        # Default sort: reverse-chronological by the normalized sort key.
        # Uses the year+month+day key for publications and the
        # end-date-dominant key for everything with a 'date' column.
        # Open-ended ranges ('MM/YYYY -') sort to the top of desc.
        def _sortkey(r):
            sv = r.get("sort_values") or {}
            return sv.get("year") or sv.get("date") or ""

        rows.sort(key=_sortkey, reverse=True)
        n_needs = sum(1 for r in rows if r.get("needs_contribution"))
        # Filter dropdowns.
        subsections = sch.get("subsections") or sorted(
            {r["subsection"] for r in rows if r["subsection"]}
        )
        institutions = sorted({r["institution"] for r in rows if r["institution"]})
        return render_template(
            "section_list.html",
            section_key=section,
            section_label=sch["label"],
            structure=sch["structure"],
            list_columns=sch.get("list_columns") or [],
            rows=rows,
            subsections=subsections,
            institutions=institutions,
            n_needs_contribution=n_needs,
            qc_status=_qc_status() if section == "publications" else None,
            mtime_ns=yaml_io.mtime_ns(ROOT / sch["file"]),
            show_hidden_default=bool(sch.get("show_hidden_default", False)),
        )

    @app.route("/<section>/new", methods=["GET"])
    @require_section(on_meta="redirect_edit")
    def entry_new(section: str):
        sch, path, _, _ = _load_section(section)
        # Stage B / I8 (2026-05-25): consume a pending-form snapshot from
        # entry_save's 409 path. Synthetic entry comes from re-applying
        # _form_to_entry on the parsed payload so every sub-editor
        # (authors, typed_notes, audiences, OA, outlets) round-trips
        # through the same dispatch the live edit path uses.
        pending = _entry_pop_pending(request.args.get("pending") or "")
        if pending and pending.get("form_data"):
            entry = _form_to_entry(pending["form_data"], sch)
            target_override = pending.get("target") or None
            warn = _pending_save_warning(request.args.get("pending_cause"))
            return _render_edit_form(
                section,
                "new",
                sch,
                path,
                entry=entry,
                ctx={},
                global_idx=None,
                form_data=pending["form_data"],
                target_override=target_override,
                extra_warning=warn,
            )
        return _render_edit_form(
            section,
            "new",
            sch,
            path,
            entry=CommentedMap(),
            ctx={},
            global_idx=None,
        )

    @app.route("/<section>/<int:idx>")
    @require_section(on_meta="redirect_view")
    def entry_view(section: str, idx: int):
        sch, path, _, _, rec = _resolve_idx(section, idx)
        entry = rec["entry"]
        ctx = rec["ctx"]
        state = _derive_form_view_state(
            sch,
            entry,
            want_self_author=(section == "publications"),
            self_name=_self_name() if section == "publications" else "",
        )
        author_forms = state["author_forms"]
        notes_form = state["notes_form"]
        simple_notes_form = state["simple_notes_form"]
        open_access_form = state["open_access_form"]
        self_author_status = state["self_author_status"]
        needs_contribution = state["needs_contribution"]
        is_pp = section == "publications" and preprint.is_preprint(entry)
        tracker_count = _entry_unresolved_tracker_count(entry) if section == "publications" else 0
        pubmed_flagged_fields: list[str] = []
        if section == "publications":
            pmid = str(entry.get("pmid") or "").strip()
            if pmid:
                # V20-cleanup M3 (2026-05-18): delegated to the
                # mtime-keyed cache helper. Use shared effective_flagged
                # helper via the helper (banner truth = triage truth).
                pubmed_flagged_fields = _pubmed_flagged_for_pmid(pmid, entry)
        # V23-B Phase 1 (2026-05-25): per-entry QC findings banner.
        # Same effective_findings predicate as index + triage page
        # (V13-V19-D R2-H1 invariant).
        # Post-impl C-H1 fix (2026-05-25): the QC sidecar keys findings
        # by `s_idx*10000+e_idx` (qc_publications.py:233,248), NOT by
        # the route's sequential flatten idx. Translate via rec["loc"]
        # so the banner counts non-first-subsection publications too.
        qc_entry_findings: list = []
        qc_entry_cross_silenced: list = []
        pubmed_cross_silenced_entry: list = []
        if section == "publications":
            try:
                from cv_editor import qc_decisions, qc_sync
                from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

                _qc_sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
                _qc_decisions = qc_decisions.load(_qc_decisions_path(), silent=True)
                loc = rec.get("loc") or ()
                qc_global_idx = (loc[0] * 10000 + loc[1]) if len(loc) >= 2 else idx
                _qc_by_idx = {qc_global_idx: entry}
                # V23-B Phase 1.5 (2026-05-26): pass pubmed_sync_state for
                # cross-system silencing.
                _pmsync_state = _pubmed_sync_state_cached()
                qc_entry_findings = qc_sync.effective_for_entry(
                    _qc_sidecar,
                    _qc_decisions,
                    global_idx=qc_global_idx,
                    current_yaml_by_global_idx=_qc_by_idx,
                    pubmed_sync_state=_pmsync_state,
                )
                _qc_eff_full = qc_sync.effective_findings(
                    _qc_sidecar,
                    _qc_decisions,
                    current_yaml_by_global_idx=_qc_by_idx,
                    pubmed_sync_state=_pmsync_state,
                )
                qc_entry_cross_silenced = qc_sync.cross_silenced_for_entry(
                    _qc_eff_full,
                    qc_global_idx,
                )
            except Exception as exc:
                app.logger.warning("entry_view: qc lookup failed: %s", exc)
            # V23-B Phase 1.5: PubMed sync cross-silenced fields for this PMID.
            pmid_s = str(entry.get("pmid") or "").strip()
            if pmid_s:
                try:
                    pubmed_cross_silenced_entry = _pubmed_cross_silenced_for_pmid(
                        pmid_s,
                        entry,
                    )
                except Exception as exc:
                    app.logger.warning("entry_view: pubmed cross-silence lookup failed: %s", exc)
        return render_template(
            "entry_view.html",
            section_key=section,
            section_label=sch["label"],
            structure=sch["structure"],
            fields=sch["fields"],
            row={
                "global_idx": idx,
                "subsection": ctx.get("subsection", ""),
                "institution": ctx.get("institution", ""),
                "city": ctx.get("city", ""),
            },
            entry=entry,
            author_forms=author_forms,
            notes_form=notes_form,
            simple_notes_form=simple_notes_form,
            open_access_form=open_access_form,
            mtime_ns=yaml_io.mtime_ns(path),
            self_author_status=self_author_status,
            needs_contribution=needs_contribution,
            is_preprint=is_pp,
            tracker_count=tracker_count,
            pubmed_flagged_fields=pubmed_flagged_fields,
            pubmed_cross_silenced_entry=pubmed_cross_silenced_entry,
            qc_entry_findings=qc_entry_findings,
            qc_entry_cross_silenced=qc_entry_cross_silenced,
            group_outlets=notes_helpers.group_outlets_for_display,
            future_note=validate.date_gate_note(entry, section),
        )

    @app.route("/<section>/<int:idx>/edit", methods=["GET"])
    @require_section(on_meta="redirect_edit")
    def entry_edit(section: str, idx: int):
        sch, path, _, _, rec = _resolve_idx(section, idx)
        # Stage B / I8: consume pending-form snapshot from entry_save 409.
        # If found, build synthetic entry via _form_to_entry; else fall
        # through to the canonical on-disk entry. Stale UUID → falls
        # through cleanly (no error, no warning — the dict evicted; the
        # user gets the current YAML state, which is also valid).
        pending = _entry_pop_pending(request.args.get("pending") or "")
        if pending and pending.get("form_data"):
            entry = _form_to_entry(pending["form_data"], sch)
            target_override = pending.get("target") or None
            warn = _pending_save_warning(request.args.get("pending_cause"))
            return _render_edit_form(
                section,
                "edit",
                sch,
                path,
                entry=entry,
                ctx=rec["ctx"],
                global_idx=idx,
                form_data=pending["form_data"],
                target_override=target_override,
                extra_warning=warn,
            )
        return _render_edit_form(
            section,
            "edit",
            sch,
            path,
            entry=rec["entry"],
            ctx=rec["ctx"],
            global_idx=idx,
        )

    @app.route("/<section>/save", methods=["POST"])
    @require_section(on_meta="redirect_save")
    def entry_save(section: str):
        rejection = _js_unmounted_rejection()
        if rejection is not None:
            return rejection
        sch, path, header, data = _load_section(section)
        structure = sch["structure"]
        if data is None and structure != "single_record":
            # M5-5d CP8: a comments-only section file loads as data=None
            # (yaml_io.load) and insert_entry would call None.insert — a 500
            # on the FIRST entry. Blank scaffolds write [] bodies precisely
            # to avoid this, but a hand-created header-only file lands here.
            data = CommentedSeq()
        mode = request.form.get("mode")
        if mode not in ("new", "edit"):
            # Reviewer-1 HIGH (V5-D): a malformed/replayed request with no
            # `mode` field used to silently fall through to the new-entry
            # branch, duplicating the entry. Refuse instead.
            abort(400)
        global_idx_raw = request.form.get("global_idx") or ""
        expected_mtime_ns = _get_expected_mtime_ns(request)
        target = _target_from_form(request, sch)

        form_data, parse_errors = _form_payload(request, sch)
        errors = validate.validate_entry(form_data, sch["fields"])
        errors.update(parse_errors)

        # Validate the target subsection against the schema's `subsections`
        # list — the single source of truth that also feeds the dropdown. A
        # schema subsection with NO entries yet is a valid target (insert_entry
        # creates the group on first use). Was: checked data presence, which
        # both rejected empty-but-valid subsections AND silently accepted a
        # data/schema mismatch — the presentations drift bug where the dropdown
        # offered names the data didn't have, so saves 400'd and the edit form
        # couldn't pre-select the entry's real subsection (2026-05-30).
        if structure in ("list_of_subsections", "subsections_of_clusters"):
            allowed = sch.get("subsections") or []
            if target.get("subsection") not in allowed:
                errors["subsection"] = (
                    f"unknown subsection: {target.get('subsection')!r} — not in "
                    f"this section's schema 'subsections' list"
                )
        if structure == "subsections_of_clusters":
            if not (target.get("institution") or "").strip():
                errors["cluster_institution"] = "required"
        if structure == "clusters":
            if not (target.get("institution") or "").strip():
                errors["cluster_institution"] = "required"

        if errors:
            return _render_edit_form(
                section,
                mode,
                sch,
                path,
                entry=_form_to_entry(form_data, sch),
                ctx={},
                global_idx=int(global_idx_raw) if global_idx_raw.isdigit() else None,
                errors=errors,
                form_data=form_data,
                target_override=target,
            ), 400

        # Track the saved entry's final loc so we can redirect to its
        # detail page on success (mirrors `entry_duplicate`). For an
        # in-place edit, the loc is unchanged; for a moved or new entry,
        # it's whatever `insert_entry` returns.
        final_loc = None
        try:
            if mode == "edit":
                idx = int(global_idx_raw)
                rec = sections.locate(data, structure, idx)
                if rec is None:
                    abort(404)
                existing = rec["entry"]
                target_changed = False
                if structure == "list_of_subsections":
                    target_changed = rec["ctx"].get("subsection") != target.get("subsection")
                elif structure == "clusters":
                    target_changed = rec["ctx"].get("institution") != target.get("institution")
                elif structure == "subsections_of_clusters":
                    target_changed = rec["ctx"].get("subsection") != target.get(
                        "subsection"
                    ) or rec["ctx"].get("institution") != target.get("institution")
                if target_changed:
                    sections.delete_entry(data, structure, rec["loc"])
                    new_entry = _form_to_entry(form_data, sch)
                    final_loc = sections.insert_entry(data, structure, target, new_entry)
                else:
                    _form_to_entry(form_data, sch, existing=existing)
                    final_loc = rec["loc"]
            else:  # new
                new_entry = _form_to_entry(form_data, sch)
                final_loc = sections.insert_entry(data, structure, target, new_entry)

        except notes_helpers.UnknownNoteTypeError as e:
            # Bad note type in the hidden JSON — surface as a field-level
            # error and re-render the form so the user can pick a valid type.
            errors["notes"] = str(e)
            return _render_edit_form(
                section,
                mode,
                sch,
                path,
                entry=_form_to_entry({k: v for k, v in form_data.items() if k != "notes"}, sch),
                ctx={},
                global_idx=int(global_idx_raw) if global_idx_raw.isdigit() else None,
                errors=errors,
                form_data=form_data,
                target_override=target,
            ), 400
        except ValueError as e:
            flash(f"Save failed: {e}", "warn")
            return redirect(url_for("section_list", section=section)), 400

        # Stage B / I8 (2026-05-25): instead of returning a 409 with a
        # redirect Location header (which browsers don't auto-follow,
        # stranding the user on a "Redirecting" stub page with their
        # unsaved form values lost), stash the parsed form payload under
        # a UUID and 302 to entry_edit/entry_new with ?pending=<uuid>.
        # The target GET route re-populates via _form_to_entry(form_data)
        # and renders a banner-warn explaining the conflict.
        try:
            yaml_io.write_with_backup(
                path,
                header,
                data,
                expected_mtime_ns=expected_mtime_ns,
            )
        except (yaml_io.StaleFileError, Timeout) as e:
            # Stash + redirect 302 (browsers don't auto-follow 4xx
            # Location headers, which left the user stuck on the
            # Redirecting stub page with their changes lost). The
            # `extra_warning` in the target route carries the user-
            # facing explanation — no separate flash() so the user
            # doesn't see two near-identical amber banners stacked.
            token = _entry_stash_pending(form_data, target)
            if mode == "edit" and global_idx_raw.isdigit():
                dest = url_for(
                    "entry_edit",
                    section=section,
                    idx=int(global_idx_raw),
                )
            else:
                dest = url_for("entry_new", section=section)
            if token:
                dest = f"{dest}?pending={token}&pending_cause={type(e).__name__}"
            return redirect(dest)
        if section == "publications":
            _kick_qc_if_idle()
        flash("Saved.", "ok")
        # Redirect back to the saved entry's detail page so the user can
        # immediately verify what landed. Falls back to the section list
        # if the loc lookup fails (e.g. structure-level oddity).
        final_idx = None
        if final_loc is not None:
            final_idx = next(
                (
                    r["global_idx"]
                    for r in sections.flatten(data, structure)
                    if r["loc"] == final_loc
                ),
                None,
            )
        if final_idx is None:
            return redirect(url_for("section_list", section=section))
        return redirect(url_for("entry_view", section=section, idx=final_idx))

    @app.route("/<section>/<int:idx>/duplicate", methods=["POST"])
    @require_section(on_meta="abort_405")
    def entry_duplicate(section: str, idx: int):
        """Deep-copy an entry into the same target group; redirect to the
        new entry's edit form. Useful for repeat guest lectures, recurring
        service roles, near-identical talks at different venues."""
        sch, path, header, data, rec = _resolve_idx(section, idx)
        structure = sch["structure"]
        expected_mtime_ns = _get_expected_mtime_ns(request)
        import copy as _copy

        new_entry = _copy.deepcopy(rec["entry"])
        ctx = rec["ctx"]
        if structure == "list_of_subsections":
            target = {"subsection": ctx.get("subsection")}
        elif structure == "clusters":
            target = {"institution": ctx.get("institution"), "city": ctx.get("city")}
        elif structure == "subsections_of_clusters":
            target = {
                "subsection": ctx.get("subsection"),
                "institution": ctx.get("institution"),
                "city": ctx.get("city"),
            }
        else:
            target = {}
        new_loc = sections.insert_entry(data, structure, target, new_entry)
        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("section_list", section=section),
        )
        if err:
            return err
        # Find the new entry's global_idx so we can land the user on its
        # edit form. insert_entry returns the loc tuple; flatten() yields
        # global_idx + loc, so we look the loc up.
        new_idx = next(
            (r["global_idx"] for r in sections.flatten(data, structure) if r["loc"] == new_loc),
            None,
        )
        if new_idx is None:
            flash("Duplicated, but couldn't locate the new entry. Open it from the list.", "warn")
            return redirect(url_for("section_list", section=section))
        flash("Duplicated. Edit the date / details and save.", "ok")
        return redirect(url_for("entry_edit", section=section, idx=new_idx))

    @app.route("/<section>/<int:idx>/delete", methods=["POST"])
    @require_section(on_meta="abort_405")
    def entry_delete(section: str, idx: int):
        sch, path, header, data, rec = _resolve_idx(section, idx)
        expected_mtime_ns = _get_expected_mtime_ns(request)
        sections.delete_entry(data, sch["structure"], rec["loc"])
        err = write_or_409(
            path,
            header,
            data,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("section_list", section=section),
        )
        if err:
            return err
        title = (
            rec["entry"].get("title")
            or rec["entry"].get("role")
            or rec["entry"].get("award")
            or rec["entry"].get("name")
            or "(entry)"
        )
        flash(f"Deleted: {str(title)[:60]}", "ok")
        return redirect(url_for("section_list", section=section))

    @app.route("/<section>/undo", methods=["POST"])
    @require_section(on_meta="allow")
    def entry_undo(section: str):
        sch = schemas.get(section)
        path = ROOT / sch["file"]
        backups = yaml_io.list_backups(path.name)
        if not backups:
            flash("No backups available for undo.", "warn")
            return redirect(url_for("section_list", section=section))
        # V17-D fix (C-H1): require mtime_ns from the form. Without it,
        # a save in another tab between page-render and click-Undo gets
        # silently clobbered by the older backup.
        expected_mtime_ns = _get_expected_mtime_ns(request)
        try:
            yaml_io.restore_from_backup(
                backups[0],
                path,
                expected_mtime_ns=expected_mtime_ns,
            )
            flash(f"Restored from {backups[0].name}", "ok")
        except yaml_io.StaleFileError as e:
            flash(f"Stale form: {e}. Reload to see the current state.", "warn")
            return redirect(url_for("section_list", section=section)), 409
        except Timeout:
            flash("Another writer holds the file lock; try again in a moment.", "warn")
            return redirect(url_for("section_list", section=section)), 409
        except Exception as e:
            flash(f"Restore failed: {e}", "warn")
        return redirect(url_for("section_list", section=section))

    @app.route("/<section>/backups")
    @require_section(on_meta="allow")
    def entry_backups(section: str):
        sch = schemas.get(section)
        path = ROOT / sch["file"]
        backups = yaml_io.list_backups(path.name)
        rows = []
        current_size = path.stat().st_size if path.exists() else 0
        for bk in backups:
            try:
                ts_ns = int(bk.name.rsplit(".", 2)[-2])
                ts = datetime.fromtimestamp(ts_ns / 1e9)
            except Exception:
                ts_ns = 0
                ts = None
            rows.append(
                {
                    "name": bk.name,
                    "size": bk.stat().st_size,
                    "ts": ts.isoformat(timespec="seconds") if ts else "?",
                    "ts_ns": ts_ns,
                }
            )
        return render_template(
            "backups.html",
            section_key=section,
            section_label=sch["label"],
            backups=rows,
            current_size=current_size,
            mtime_ns=yaml_io.mtime_ns(path) if path.exists() else 0,
        )

    @app.route("/<section>/backups/<name>/diff")
    @require_section(on_meta="allow")
    def backup_diff(section: str, name: str):
        sch = schemas.get(section)
        path = ROOT / sch["file"]
        if not re.match(rf"^{re.escape(path.name)}\.\d+\.bak$", name):
            abort(400)
        bk = yaml_io.BACKUP_DIR / name
        if not bk.exists():
            abort(404)
        cur_lines = path.read_text().splitlines(keepends=True) if path.exists() else []
        bk_lines = bk.read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                cur_lines,
                bk_lines,
                fromfile=f"current/{path.name}",
                tofile=f"backup/{name}",
                n=3,
            )
        )
        return render_template(
            "backup_diff.html",
            section_key=section,
            section_label=sch["label"],
            backup_name=name,
            diff=diff or "(no differences)",
            mtime_ns=yaml_io.mtime_ns(path) if path.exists() else 0,
        )

    @app.route("/<section>/restore", methods=["POST"])
    @require_section(on_meta="allow")
    def entry_restore(section: str):
        sch = schemas.get(section)
        path = ROOT / sch["file"]
        name = request.form.get("backup_name", "")
        if not re.match(rf"^{re.escape(path.name)}\.\d+\.bak$", name):
            abort(400)
        bk = yaml_io.BACKUP_DIR / name
        if not bk.exists():
            abort(404)
        # V17-D fix (C-H2): require mtime_ns. Same race as undo — pick a
        # backup, click Restore, while another tab saves in between → the
        # newer save gets clobbered silently. Mtime guard catches it.
        expected_mtime_ns = _get_expected_mtime_ns(request)
        try:
            yaml_io.restore_from_backup(
                bk,
                path,
                expected_mtime_ns=expected_mtime_ns,
            )
            flash(f"Restored {path.name} from {name}.", "ok")
        except yaml_io.StaleFileError as e:
            flash(f"Stale form: {e}. Reload to see the current state.", "warn")
            return redirect(url_for("entry_backups", section=section)), 409
        except Timeout:
            flash("Another writer holds the file lock; try again.", "warn")
            return redirect(url_for("entry_backups", section=section)), 409
        except Exception as e:
            flash(f"Restore failed: {e}", "warn")
            return redirect(url_for("entry_backups", section=section)), 400
        return redirect(url_for("section_list", section=section))

    # ----- meta-specific routes (single_record) -----

    @app.route("/meta")
    def meta_view():
        sch, path, _, data = _load_section("meta")
        return render_template(
            "meta_view.html",
            section_label=sch["label"],
            fields=sch["fields"],
            entry=data or CommentedMap(),
            mtime_ns=yaml_io.mtime_ns(path),
        )

    @app.route("/meta/edit", methods=["GET"])
    def meta_edit():
        sch, path, _, data = _load_section("meta")
        return _render_edit_form(
            "meta",
            "edit",
            sch,
            path,
            entry=data or CommentedMap(),
            ctx={},
            global_idx=None,
        )

    @app.route("/meta/save", methods=["POST"])
    def meta_save():
        rejection = _js_unmounted_rejection()
        if rejection is not None:
            return rejection
        sch, path, header, data = _load_section("meta")
        expected_mtime_ns = _get_expected_mtime_ns(request)
        form_data, parse_errors = _form_payload(request, sch)
        errors = validate.validate_entry(form_data, sch["fields"])
        errors.update(parse_errors)
        if errors:
            return _render_edit_form(
                "meta",
                "edit",
                sch,
                path,
                entry=_form_to_entry(form_data, sch, existing=data),
                ctx={},
                global_idx=None,
                errors=errors,
                form_data=form_data,
            ), 400
        new_data = _form_to_entry(form_data, sch, existing=data)
        err = write_or_409(
            path,
            header,
            new_data,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("meta_view"),
        )
        if err:
            return err
        flash("Meta saved.", "ok")
        return redirect(url_for("meta_view"))
