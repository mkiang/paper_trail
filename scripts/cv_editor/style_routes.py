"""Style / build-variants + typography + freeze routes (extracted from
app.py 2026-05-29, M2 — last leaf).

V4 build-variants editor, the advanced typography editor, and the V5
freeze-workspace tool. Follows the `register_qc_triage_routes(app, deps)`
pattern (gotcha #69): endpoint names stay flat.

Routes registered here:
  - GET  /freeze                       — freeze_list
  - POST /freeze                       — freeze_create
  - POST /freeze/<name>/delete         — freeze_delete
  - POST /freeze/prune                 — freeze_prune
  - GET  /style                        — style_list
  - GET  /style/typography             — style_typography
  - POST /style/typography/save        — style_typography_save
  - GET  /style/new                    — style_new
  - GET  /style/<int:idx>/edit         — style_edit
  - POST /style/save                   — style_save
  - POST /style/<int:idx>/delete       — style_delete
  - POST /style/<int:idx>/duplicate    — style_duplicate
  - POST /style/<int:idx>/build/stream — style_build_stream

The 4 internal helpers (_load_meta_and_variants, _freeze_variant_names,
_typography_groups, _preview_load_data) are local to this feature, so they
move here as nested functions. Shared closures (load_section, write_or_409,
get_expected_mtime_ns, the style pending-store pair, pending_save_warning,
sse_response) stay in create_app() and are handed in via deps.
Behaviour-identical (M2 fingerprint guard + test_v4/test_v5/test_advanced_typography).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from filelock import Timeout
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from cv_editor import build_runner, freezer, yaml_io
from cv_editor import build_variants as bv
from cv_editor import typography_knobs as tk
from cv_editor.capabilities import Capabilities


@dataclass
class StyleDeps:
    """DI surface for the style/typography/freeze routes. All read-only
    closures from create_app(); shared by reference."""

    load_section: Callable[[str], tuple]
    write_or_409: Callable
    get_expected_mtime_ns: Callable
    style_stash_pending: Callable[[dict, str, int | None], str]
    style_pop_pending: Callable[[str], dict]
    pending_save_warning: Callable[[str | None], str]
    sse_response: Callable
    capabilities: Capabilities


def register_style_routes(app: Flask, deps: StyleDeps) -> None:
    _load_section = deps.load_section
    write_or_409 = deps.write_or_409
    _get_expected_mtime_ns = deps.get_expected_mtime_ns
    _style_stash_pending = deps.style_stash_pending
    _style_pop_pending = deps.style_pop_pending
    _pending_save_warning = deps.pending_save_warning
    _sse_response = deps.sse_response

    # P5 (paper_trail inversion): the freeze/flatten tool + the advanced
    # typography editor are per-template CAPABILITIES. Gate their route
    # registrations (intra-module — the build-variants /style routes stay
    # PUBLIC/unconditional). `_skip_route` defines the view but never binds
    # it to a URL, so `modern` (caps false) serves no freeze/typography
    # endpoint while `bespoke` (caps true) registers every route exactly as
    # before -> byte-identical behaviour in the private repo.
    def _skip_route(*_a, **_k):
        return lambda f: f

    _freeze_route = app.route if deps.capabilities.freeze else _skip_route
    _typography_route = app.route if deps.capabilities.typography else _skip_route

    def _freeze_variant_names():
        _, _, _, _, variants = _load_meta_and_variants()
        names = [
            str(v.get("filename")).strip() for v in variants if (v.get("filename") or "").strip()
        ]
        # Offer the default variant first so freezing "the default CV" always
        # works even if its meta row is missing. The default is resolved from
        # meta (the first build variant), so this holds for any corpus.
        default = build_runner.default_variant_name()
        if default not in names:
            names = [default] + names
        return names

    @_freeze_route("/freeze", methods=["GET"])
    def freeze_list():
        existing = freezer.list_frozen()
        return render_template(
            "freeze.html",
            variants=_freeze_variant_names(),
            default_variant=build_runner.default_variant_name(),
            frozen=[
                {
                    "name": r.path.name,
                    "relpath": r.relpath,
                    "files": r.files_copied,
                    "bytes": r.bytes_copied,
                    "variant": r.variant or "—",
                }
                for r in existing
            ],
        )

    @_freeze_route("/freeze", methods=["POST"])
    def freeze_create():
        chosen = (request.form.get("variant") or build_runner.default_variant_name()).strip()
        names = _freeze_variant_names()
        if chosen not in names:
            flash(f"Unknown build variant: {chosen!r}.", "warn")
            return redirect(url_for("freeze_list")), 400
        # Resolve the variant's --input flags.
        _, _, _, _, variants = _load_meta_and_variants()
        variant = next((v for v in variants if (v.get("filename") or "").strip() == chosen), None)
        inputs = bv.variant_inputs_map(variant) if variant is not None else {}
        try:
            result = freezer.freeze_workspace(variant_inputs=inputs, variant_name=chosen)
        except Exception as e:
            flash(f"Freeze failed: {type(e).__name__}: {e}", "warn")
            return redirect(url_for("freeze_list")), 500
        flash(
            f"Flattened {chosen} → {result.relpath}/ "
            f"({result.files_copied} files, {result.bytes_copied // 1024} KB). "
            f"Run `cd {result.relpath} && ./render.sh` to render.",
            "ok",
        )
        return redirect(url_for("freeze_list"))

    @_freeze_route("/freeze/<name>/delete", methods=["POST"])
    def freeze_delete(name: str):
        try:
            freezer.delete_frozen(name)
            flash(f"Deleted frozen workspace {name}.", "ok")
        except (ValueError, FileNotFoundError) as e:
            flash(f"Delete failed: {e}", "warn")
            return redirect(url_for("freeze_list")), 400
        return redirect(url_for("freeze_list"))

    @_freeze_route("/freeze/prune", methods=["POST"])
    def freeze_prune():
        raw = request.form.get("days_old", "30")
        try:
            days = int(raw)
        except ValueError:
            flash(f"Bad days_old value: {raw!r}.", "warn")
            return redirect(url_for("freeze_list")), 400
        if days <= 0:
            flash("days_old must be positive (got {0}).".format(days), "warn")
            return redirect(url_for("freeze_list")), 400
        try:
            deleted = freezer.prune_frozen(days_old=days)
        except Exception as e:
            flash(f"Prune failed: {type(e).__name__}: {e}", "warn")
            return redirect(url_for("freeze_list")), 500
        if deleted:
            flash(
                f"Pruned {len(deleted)} frozen workspace"
                f"{'s' if len(deleted) != 1 else ''} older than {days} days.",
                "ok",
            )
        else:
            flash(f"No frozen workspaces older than {days} days; nothing pruned.", "ok")
        return redirect(url_for("freeze_list"))

    @_freeze_route("/freeze/flatten/stream", methods=["POST"])
    def freeze_flatten_stream():
        """Build the primary (or every) variant PDF + its flattened .typ into
        output/flattened_typs/, streamed via SSE. Reuses the shared build lock
        (stream_subprocess) so it cooperates with rebuilds + CLI invocations;
        the child script's own lock probe is skipped via the
        CV_EDITOR_INTERNAL_BUILD env stream_subprocess sets. CSRF is covered by
        the app-wide _csrf_origin_check before_request."""
        mode = request.form.get("mode", "primary")
        argv = [sys.executable, "-m", "cv_editor.build_flattened"]
        if mode == "all":
            argv.append("--all")
        return _sse_response(build_runner.stream_subprocess(argv))

    # ----- V4: Style / build-variants editor -----

    def _load_meta_and_variants():
        sch, path, header, meta = _load_section("meta")
        if meta is None:
            meta = CommentedMap()
        variants = meta.get("build_variants")
        if variants is None:
            variants = CommentedSeq()
            meta["build_variants"] = variants
        return sch, path, header, meta, variants

    @app.route("/style")
    def style_list():
        sch, path, _, _, variants = _load_meta_and_variants()
        rows = []
        for i, v in enumerate(variants):
            rows.append(
                {
                    "idx": i,
                    "filename": str(v.get("filename", "")),
                    "chips": bv.variant_chips(v),
                    "edit_url": url_for("style_edit", idx=i),
                }
            )
        return render_template(
            "style_list.html",
            variants=rows,
            mtime_ns=yaml_io.mtime_ns(path),
        )

    def _typography_groups(rows):
        """Group resolved knob rows by category, preserving discovery order."""
        groups: dict = {}
        for r in rows:
            groups.setdefault(r["group"], []).append(r)
        return groups

    @_typography_route("/style/typography")
    def style_typography():
        knobs = tk.discover_knobs()
        _, path, _, meta, _ = _load_meta_and_variants()
        rows = tk.resolve_current(knobs, meta.get("typography"))
        return render_template(
            "style_typography.html",
            groups=_typography_groups(rows),
            errors={},
            mtime_ns=yaml_io.mtime_ns(path),
        )

    @_typography_route("/style/typography/save", methods=["POST"])
    def style_typography_save():
        knobs = tk.discover_knobs()
        _, path, header, meta, _ = _load_meta_and_variants()
        expected_mtime_ns = _get_expected_mtime_ns(request)
        errors: dict = {}
        overrides = CommentedMap()
        submitted: dict = {}
        for k in knobs:
            ok, norm, err = tk.validate_value(k.kind, request.form.get(k.key, ""))
            submitted[k.meta_key] = norm
            if not ok:
                errors[k.key] = err
                continue
            # Persist only non-default, non-empty values. Quote every value so a
            # colour like "#000000" can't be read back as a YAML comment.
            if norm != "" and norm != k.default:
                overrides[k.meta_key] = DoubleQuotedScalarString(norm)
        if errors:
            rows = tk.resolve_current(knobs, submitted)
            return render_template(
                "style_typography.html",
                groups=_typography_groups(rows),
                errors=errors,
                mtime_ns=yaml_io.mtime_ns(path),
            ), 400
        # Mutate only the typography key on the loaded meta; write the whole tree.
        if len(overrides) > 0:
            meta["typography"] = overrides
        elif "typography" in meta:
            del meta["typography"]
        err = write_or_409(
            path,
            header,
            meta,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("style_typography"),
        )
        if err:
            return err
        flash("Typography saved. Rebuild to see the changes.", "ok")
        return redirect(url_for("style_typography"))

    @app.route("/style/new", methods=["GET"])
    def style_new():
        _, path, _, meta, variants = _load_meta_and_variants()
        audience_options = bv.audience_choices(meta, _preview_load_data)
        # Tier B / B5: re-populate the form if returning from a stale-mtime
        # / Timeout 409. existing_filenames is recomputed against current
        # variants (NEVER stashed) so a duplicate-name conflict that arose
        # between the failed save and this re-render surfaces immediately.
        pending = _style_pop_pending(request.args.get("pending") or "")
        if pending and pending.get("mode") == "new":
            form = pending["form"]
            existing_filenames = [v.get("filename") for v in variants]
            errors = bv.validate_form(
                form, existing_filenames=existing_filenames, audiences=audience_options
            )
            extra_warning = _pending_save_warning(request.args.get("pending_cause"))
        else:
            form = bv.default_form()
            existing_filenames = [v.get("filename") for v in variants]
            errors = {}
            extra_warning = None
        return render_template(
            "style_edit.html",
            mode="new",
            idx=None,
            form=form,
            audiences=audience_options,
            boolean_inputs=bv.BOOLEAN_INPUTS,
            existing_filenames=existing_filenames,
            errors=errors,
            mtime_ns=yaml_io.mtime_ns(path),
            preview=None,
            extra_warning=extra_warning,
        )

    def _preview_load_data(key):
        """impact_preview callback: returns the data tree for the section.
        Schema is looked up by build_variants.impact_preview via schemas.get."""
        return _load_section(key)[3]

    @app.route("/style/<int:idx>/edit", methods=["GET"])
    def style_edit(idx: int):
        _, path, _, meta, variants = _load_meta_and_variants()
        audience_options = bv.audience_choices(meta, _preview_load_data)
        if idx < 0 or idx >= len(variants):
            abort(404)
        v = variants[idx]
        # Tier B / B5: re-populate from a pending 409 snapshot if present.
        # existing_filenames + errors are recomputed against the current
        # post-reload variants list, so a duplicate-name conflict that
        # arose between the failed save and this re-render surfaces
        # before the user clicks Save a second time.
        pending = _style_pop_pending(request.args.get("pending") or "")
        if pending and pending.get("mode") == "edit" and pending.get("idx") == idx:
            form = pending["form"]
            existing_filenames = [vv.get("filename") for vv in variants if vv is not v]
            errors = bv.validate_form(
                form, existing_filenames=existing_filenames, audiences=audience_options
            )
            extra_warning = _pending_save_warning(request.args.get("pending_cause"))
        else:
            form = bv.variant_to_form(v)
            existing_filenames = [vv.get("filename") for vv in variants if vv is not v]
            errors = {}
            extra_warning = None
        preview = bv.impact_preview(
            _preview_load_data,
            audience=form["audience"] or bv.DEFAULT_AUDIENCE,
            show_highlighted=form.get("show_highlighted", False),
        )
        return render_template(
            "style_edit.html",
            mode="edit",
            idx=idx,
            form=form,
            audiences=audience_options,
            boolean_inputs=bv.BOOLEAN_INPUTS,
            existing_filenames=existing_filenames,
            errors=errors,
            mtime_ns=yaml_io.mtime_ns(path),
            preview=preview,
            extra_warning=extra_warning,
        )

    @app.route("/style/save", methods=["POST"])
    def style_save():
        sch, path, header, meta, variants = _load_meta_and_variants()
        audience_options = bv.audience_choices(meta, _preview_load_data)
        mode = request.form.get("mode", "new")
        if mode not in ("new", "edit"):
            abort(400)
        idx_raw = request.form.get("idx") or ""
        expected_mtime_ns = _get_expected_mtime_ns(request)

        # Form payload (V4 is simple HTML inputs; no JSON-hidden fields).
        # Post-batch refactor (2026-05-25): default-true flags loop via
        # bv.DEFAULT_TRUE_INPUTS so a new default-true flag never gets
        # silently dropped here (the failure mode Stage D's post-impl
        # review caught: show_media_urls was missing from this dict,
        # leaving the checkbox inert despite all helper plumbing).
        form = {
            "filename": request.form.get("filename", "").strip(),
            "audience": request.form.get("audience", "").strip(),
        }
        for key in bv.DEFAULT_TRUE_INPUTS:
            form[key] = bool(request.form.get(key))
        for key in bv.BOOLEAN_INPUTS:
            form[key] = bool(request.form.get(key))

        # Filename uniqueness check excludes the current row in edit mode.
        existing = [v.get("filename") for v in variants]
        if mode == "edit" and idx_raw.isdigit():
            existing = [f for i, f in enumerate(existing) if i != int(idx_raw)]
        errors = bv.validate_form(form, existing_filenames=existing, audiences=audience_options)

        if errors:
            return render_template(
                "style_edit.html",
                mode=mode,
                idx=int(idx_raw) if idx_raw.isdigit() else None,
                form=form,
                audiences=audience_options,
                boolean_inputs=bv.BOOLEAN_INPUTS,
                existing_filenames=existing,
                errors=errors,
                mtime_ns=expected_mtime_ns,
                preview=None,
            ), 400

        if mode == "edit":
            if not idx_raw.isdigit():
                abort(400)
            i = int(idx_raw)
            if i < 0 or i >= len(variants):
                abort(404)
            # Preserve unknown YAML keys on the existing variant.
            new_v = bv.form_to_variant(form, existing=variants[i], audiences=audience_options)
            variants[i] = new_v
        else:
            new_v = bv.form_to_variant(form, audiences=audience_options)
            variants.append(new_v)

        # Tier B / B5 (2026-05-27): inline write_with_backup + stash-and-
        # 302 on stale-mtime / Timeout. Mirrors entry_save's pattern;
        # replaces write_or_409 which returned (redirect, 409) — browsers
        # don't auto-follow 4xx Location headers, leaving the user on a
        # "Redirecting" stub with their style edits lost. The pending-
        # store reuses _pending_save_warning(cause) so the recovery
        # banner is byte-identical to entry_save's.
        try:
            yaml_io.write_with_backup(
                path,
                header,
                meta,
                expected_mtime_ns=expected_mtime_ns,
            )
        except (yaml_io.StaleFileError, Timeout) as e:
            idx_for_stash = int(idx_raw) if (mode == "edit" and idx_raw.isdigit()) else None
            token = _style_stash_pending(form, mode, idx_for_stash)
            if mode == "edit" and idx_for_stash is not None:
                dest = url_for("style_edit", idx=idx_for_stash)
            else:
                dest = url_for("style_new")
            if token:
                dest = f"{dest}?pending={token}&pending_cause={type(e).__name__}"
            return redirect(dest)

        flash(f"Saved variant '{form['filename']}'.", "ok")
        return redirect(url_for("style_list"))

    @app.route("/style/<int:idx>/delete", methods=["POST"])
    def style_delete(idx: int):
        _, path, header, meta, variants = _load_meta_and_variants()
        if idx < 0 or idx >= len(variants):
            abort(404)
        if len(variants) <= 1:
            flash(
                "Refusing to delete the last variant — ./build.sh would have nothing to compile.",
                "warn",
            )
            return redirect(url_for("style_list")), 400
        expected_mtime_ns = _get_expected_mtime_ns(request)
        deleted = variants[idx]
        variants.pop(idx)
        err = write_or_409(
            path,
            header,
            meta,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("style_list"),
        )
        if err:
            return err
        flash(f"Deleted variant '{deleted.get('filename')}'.", "ok")
        return redirect(url_for("style_list"))

    @app.route("/style/<int:idx>/duplicate", methods=["POST"])
    def style_duplicate(idx: int):
        _, path, header, meta, variants = _load_meta_and_variants()
        if idx < 0 or idx >= len(variants):
            abort(404)
        expected_mtime_ns = _get_expected_mtime_ns(request)
        src = variants[idx]
        existing = {v.get("filename") for v in variants}
        raw_base = str(src.get("filename") or "variant").strip()
        # If the source's filename is malformed (hand-edited YAML), fall
        # back to "variant" so the new name passes the validator.
        base = raw_base if bv.FILENAME_RE.match(raw_base) else "variant"
        new_name = f"{base}-copy"
        # Bounded loop — collision-resolve up to len(variants)+10 tries,
        # then bail. Pathological data shouldn't be able to spin.
        for n in range(2, len(variants) + 12):
            if new_name not in existing:
                break
            new_name = f"{base}-copy{n}"
        else:
            flash(
                "Could not find an available duplicate name; rename existing copies first.", "warn"
            )
            return redirect(url_for("style_list")), 409
        if not bv.FILENAME_RE.match(new_name):
            flash(f"Generated duplicate name {new_name!r} failed validation.", "warn")
            return redirect(url_for("style_list")), 400
        new_v = CommentedMap()
        new_v["filename"] = new_name
        new_v["inputs"] = CommentedMap(src.get("inputs") or {})
        variants.append(new_v)
        err = write_or_409(
            path,
            header,
            meta,
            expected_mtime_ns=expected_mtime_ns,
            redirect_to=url_for("style_list"),
        )
        if err:
            return err
        flash(f"Duplicated '{base}' → '{new_name}'.", "ok")
        return redirect(url_for("style_list"))

    @app.route("/style/<int:idx>/build/stream", methods=["POST"])
    def style_build_stream(idx: int):
        """Build just one variant via SSE. Reuses the existing build lock so
        it cooperates with the global rebuild and CLI invocations."""
        _, _, _, _, variants = _load_meta_and_variants()
        if idx < 0 or idx >= len(variants):
            abort(404)
        try:
            argv = bv.variant_typst_argv(variants[idx])
        except bv.InvalidVariantError as e:
            return _sse_response([("error", str(e))])

        return _sse_response(build_runner.stream_subprocess(argv))
