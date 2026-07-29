"""
Flask app for the CV editor.

Generic CRUD routes (work for every section's structure):

    GET  /<section>                       list view
    GET  /<section>/new                   manual-add form
    GET  /<section>/<int:idx>             read-only detail
    GET  /<section>/<int:idx>/edit        edit form
    POST /<section>/save                  create-or-update
    POST /<section>/<int:idx>/delete      delete
    POST /<section>/undo                  restore most recent backup
    GET  /<section>/backups               list backups for this section
    POST /<section>/restore               restore a specific backup

Publications-specific (V1b):

    GET|POST /publications/import                    citation import (4 tabs)
    GET|POST /publications/<int:idx>/promote         preprint -> published

Meta (single_record):

    GET  /meta                            read-only header / footer / sections
    GET  /meta/edit                       edit
    POST /meta/save                       save

Cross-cutting:

    GET  /                                section index
    GET  /search?q=...                    cross-section grep (V2-E)
    POST /rebuild                         trigger ./build.sh
    GET  /healthz                         liveness
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from filelock import Timeout
from flask import (
    Flask,
    Response,
    abort,
    flash,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from ruamel.yaml.comments import CommentedMap

from cv_editor import (
    altmetric_tracker_cache,
    capabilities,
    nav,
    notes_helpers,
    paths,
    schemas,
    sections,
    sort_keys,
    tracker_walk,
    url_helpers,
    validate,
    yaml_io,
)
from cv_editor.author_names import (
    author_to_form,
    first_author_display,
    normalize_authors_for_render,
)

# V20-cleanup T2 (2026-05-18): derived from FIELD_HANDLERS, not
# hand-maintained. See `cv_editor/field_handlers.py:FieldHandler.is_json`.
from cv_editor.field_handlers import JSON_FIELD_TYPES as _JSON_FIELD_TYPES
from cv_editor.field_handlers import empty_json_value as _empty_json_value_handler

# Module-scope ROOT tracks the active workspace root via the seam hook (an
# external reader may still `app.ROOT`); create_app() shadows it locally
# for its whole body so every route closure resolves the configured root.
ROOT = paths.data_root()


@paths.on_configure
def _refresh_root() -> None:
    global ROOT
    ROOT = paths.data_root()


def create_app(data_dir=None, project_root=None) -> Flask:
    # P1 seam: allow the caller to point the editor at an external
    # workspace/engine root before anything resolves a path. configure()
    # also writes CV_EDITOR_* into os.environ so spawned subprocesses
    # inherit the redirect. Omitted args leave the active config untouched.
    if data_dir is not None or project_root is not None:
        paths.configure(data_dir=data_dir, project_root=project_root)
    # Shadow ROOT (workspace: data/, qc/, .cache/) for the whole body; use
    # _ENGINE (project root: scripts/, build.sh, cwd) for engine assets.
    ROOT = paths.data_root()
    _ENGINE = paths.project_root()

    # CP4/H6: fail loud rather than write YAML into the Python install tree.
    # If a bare `cv-editor` runs from an installed wheel with CV_EDITOR_* unset,
    # data_root() falls back to _LEGACY_ROOT (= <venv>/lib/pythonX.Y) and the
    # "Initialize" scaffold / any save would land in site-packages. Refuse to
    # boot with an actionable pointer. Keyed on the RESOLVED write root, so a
    # configured/env-set external workspace (e.g. the wheel route-smoke's
    # CV_EDITOR_DATA_ROOT) is external -> this never fires there; example_dir()'s
    # read-only importlib.resources fallback is unaffected (it's not data_root()).
    if paths.is_inside_install_tree(ROOT):
        raise RuntimeError(
            f"CV editor workspace resolved inside the Python install tree ({ROOT}).\n"
            "Set CV_EDITOR_DATA_ROOT (and CV_EDITOR_PROJECT_ROOT) to your CV "
            "workspace, or launch via ./launch_editor.sh. Refusing to run so "
            "writes don't land in site-packages."
        )

    # P5 (paper_trail inversion): resolve the active template's capabilities
    # ONCE per app instance. Gates whether the freeze/typography/altmetric
    # feature ROUTES register (threaded into StyleDeps/PublicationsDeps below)
    # and whether their nav links + in-page UI show (injected into the template
    # context). Private repo -> active template `bespoke` -> all-True -> the
    # editor is unchanged.
    caps = capabilities.current()

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # M1 (2026-05-29): per-process secret key instead of a hardcoded
    # constant. The launcher (cv_editor/cli.py) mints CV_EDITOR_SECRET_KEY
    # so one stable key spans the running editor; absent the env var
    # (e.g. tests) a fresh random key is used per app instance.
    import os as _os_sk
    import secrets as _secrets_sk

    app.secret_key = _os_sk.environ.get("CV_EDITOR_SECRET_KEY") or _secrets_sk.token_hex(32)

    # Tier-2 refactor (2026-05-27): unified file-path knob registry.
    # Every sidecar/snapshot/log path the editor reads is funneled
    # through `_cfg_path(key)`. Tests redirect via
    # `app.config[key] = tmp_path / ...` per the established sidecar
    # idiom (gotchas #57 + #63). Adding a new knob is a 1-line tuple
    # entry below + `_cfg_path("YOUR_KEY")` at the read site.
    #
    # Lazy import for the PubMed-sync default (its module-level constant
    # lives outside cv_editor).
    from cv_editor import pubmed_sync as _pubmed_sync_mod

    _DEFAULT_PATHS = {
        "TRACKER_CACHE_PATH": ROOT / ".cache" / "altmetric" / "trackers.json",
        "PUBMED_SYNC_SIDECAR_PATH": _pubmed_sync_mod.SIDECAR_PATH,
        "CITATION_CACHE_PATH": ROOT / ".cache" / "citation_counts.json",
        "CITATION_SNAPSHOT_PATH": ROOT / "data" / "citation_counts.json",
        "PMSYNC_DECISIONS_GEN_PATH": ROOT / "qc" / "pubmed_sync_decisions.gen.yml",
        "QC_DECISIONS_PATH": ROOT / "qc" / "qc_decisions.json",
        "LOG_PATH": ROOT / ".cache" / "cv_editor.log",
    }
    for _k, _v in _DEFAULT_PATHS.items():
        app.config.setdefault(_k, str(_v) if isinstance(_v, Path) else _v)

    def _cfg_path(key: str) -> Path:
        """Read a file-path knob from app.config as a Path. The default
        was wired in by the _DEFAULT_PATHS loop above; tests override
        by setting app.config[key] directly. Centralises the 7
        previously-scattered `Path(app.config[key])` / `app.config.get(
        key, default)` patterns."""
        return Path(app.config[key])

    # /quit token (2026-05-28): the launcher (scripts/cv_editor.py)
    # mints CV_EDITOR_QUIT_TOKEN and exports it via env. Tests skip
    # the launcher and leave QUIT_TOKEN empty — the /quit route then
    # short-circuits the gate, preserving the existing test surface.
    import os as _os_env

    app.config.setdefault("QUIT_TOKEN", _os_env.environ.get("CV_EDITOR_QUIT_TOKEN", ""))

    @app.context_processor
    def _inject_quit_token():
        return {"quit_token": app.config.get("QUIT_TOKEN", "")}

    # Tier B / B9 (2026-05-27): file logging for background-daemon
    # exceptions + flash-level diagnostics. Gated on `not app.testing`
    # so the pytest suite (hundreds of create_app() calls) doesn't
    # write to the user's real log file. Attaches a FileHandler
    # directly to app.logger (NOT root via logging.basicConfig, which
    # would propagate and re-introduce the test-pollution problem).
    if not app.config.get("TESTING"):
        import logging as _logging

        log_path = _cfg_path("LOG_PATH")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _handler = _logging.FileHandler(str(log_path), encoding="utf-8")
        _handler.setFormatter(
            _logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        )
        # Only add if no FileHandler is already attached (a second
        # create_app() in the same process — rare but possible in tests
        # that flip TESTING off — must not double-write).
        if not any(isinstance(h, _logging.FileHandler) for h in app.logger.handlers):
            app.logger.addHandler(_handler)
            app.logger.setLevel(_logging.INFO)

    # V20-cleanup M6 (2026-05-18): CSRF defense via Origin/Referer
    # check on state-changing requests. Threat model: another browser
    # tab on a malicious origin POSTs to localhost:<port> while the
    # editor is running. The launcher binds to 127.0.0.1; the check
    # below rejects cross-origin POSTs by exact netloc match (NOT
    # `startswith`, which the V20-cleanup pre-impl review flagged as
    # exploitable via `localhost:5000.attacker.com`-style suffixes).
    # Permissive on missing Origin/Referer: old browsers don't always
    # send them on same-origin form POSTs. TESTING bypass keeps the
    # 860+ existing test suite working without per-test Origin headers.
    @app.before_request
    def _csrf_origin_check():
        from urllib.parse import urlparse

        from flask import request as _req

        if _req.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return None
        if app.config.get("TESTING"):
            return None
        origin = _req.headers.get("Origin") or _req.headers.get("Referer", "")
        if not origin:
            return None  # legitimate missing header on some browsers
        try:
            parsed = urlparse(origin)
        except ValueError:
            return abort(403)
        if parsed.scheme not in ("http", "https"):
            return abort(403)
        # `request.host` is "127.0.0.1:53231" or similar. Build an
        # explicit allowlist covering the three localhost spellings;
        # exact-netloc match (NOT startswith) is the security boundary.
        host_no_port = _req.host.split(":")[0]
        port = ""
        if ":" in _req.host:
            port = ":" + _req.host.split(":")[1]
        allowed_netlocs = {
            _req.host,
            "127.0.0.1" + port,
            "localhost" + port,
            "[::1]" + port,
        }
        if parsed.netloc in allowed_netlocs:
            return None
        # Also allow IPv6 loopback variants without port (some browsers
        # canonicalize).
        if host_no_port == "127.0.0.1" and parsed.netloc in {
            "localhost" + port,
            "127.0.0.1" + port,
        }:
            return None
        abort(403)

    # ----- helpers -----

    def _load_section(section_key: str):
        sch = schemas.get(section_key)
        path = ROOT / sch["file"]
        header, data = yaml_io.load(path)
        return sch, path, header, data

    # V17-D: 11 routes wrapped write_with_backup in identical
    # try/StaleFileError/Timeout blocks with slightly drifted flash copy.
    # Centralize the catch + flash + 409 redirect so the user-facing
    # message stays consistent and V13/V14/V15 routes don't re-introduce
    # divergence.
    #
    # Returns None on success; on failure returns a Flask response tuple
    # the route should `return` directly. The mutation is the caller's
    # responsibility (some routes mutate `data` in place before calling).
    def write_or_409(path, header, data, *, expected_mtime_ns, redirect_to):
        try:
            yaml_io.write_with_backup(
                path,
                header,
                data,
                expected_mtime_ns=expected_mtime_ns,
            )
            return None
        except yaml_io.StaleFileError as e:
            flash(f"Stale form: {e}. Reload to see the current state.", "warn")
            return redirect(redirect_to), 409
        except Timeout:
            flash("Another writer holds the file lock; try again in a moment.", "warn")
            return redirect(redirect_to), 409

    # V17-D: collapse the 13+ routes that each open with the same section
    # validation + meta-handling ceremony. on_meta picks how to handle a
    # request that hits a /<section>/... URL with section='meta':
    #   redirect_view  - send to /meta (read-only views, lists)
    #   redirect_edit  - send to /meta/edit (edit forms)
    #   redirect_save  - send to /meta/save (POST save handler; 307 preserves POST)
    #   abort_405      - meta has no equivalent for this op (delete, duplicate, csv)
    #   allow          - meta passes through (file-level ops: backups, undo, restore)
    _META_HANDLERS = {
        "redirect_view": lambda: redirect(url_for("meta_view")),
        "redirect_edit": lambda: redirect(url_for("meta_edit")),
        "redirect_save": lambda: redirect(url_for("meta_save"), code=307),
        "abort_405": lambda: abort(405),
    }

    def require_section(*, on_meta="redirect_view"):
        if on_meta != "allow" and on_meta not in _META_HANDLERS:
            raise ValueError(f"unknown on_meta: {on_meta!r}")

        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(section, *args, **kwargs):
                if section not in schemas.SCHEMAS:
                    abort(404)
                if section == "meta" and on_meta != "allow":
                    return _META_HANDLERS[on_meta]()
                return fn(section, *args, **kwargs)

            return wrapper

        return decorator

    def _publication_qc(entry, self_nm: str) -> dict:
        """Compute the publications-only QC flags. Centralized so the
        list view and the form-view-state derivation can't drift apart
        (V8-V11-D R2 MEDIUM dedup)."""
        return {
            "needs_contribution": notes_helpers.needs_contribution_note(entry, self_nm),
        }

    # V14: cached snapshot reader. Reloads the snapshot from disk every
    # call so the editor reflects fetcher updates without a server restart;
    # the file is small (~5 KB for 100 DOIs) so this is cheap.
    # Editor-side: consult snapshot first; fall back to sidecar so the
    # 0-vs-— distinction (R2-H1) survives. Snapshot omits zero-count
    # entries (renderer would suppress them anyway), but the editor must
    # show "0" for fetched-but-zero and "—" for never-attempted.
    # R1-M2 fix: load both files once per request via flask.g.
    def _citation_lookup_for_request():
        import flask

        g = flask.g
        if getattr(g, "_citation_lookup", None) is None:
            from cv_editor.citation_counts import CitationCache, CountStatus, load_snapshot

            snap_path = _cfg_path("CITATION_SNAPSHOT_PATH")
            cache_path = _cfg_path("CITATION_CACHE_PATH")
            snap_counts = load_snapshot(snap_path).get("counts") or {}
            sidecar_counts: dict = {}
            try:
                cache = CitationCache.load(cache_path)
                for k, e in cache.all().items():
                    if e.status == CountStatus.FETCHED and e.count is not None:
                        sidecar_counts[k] = e.count
            except Exception:
                pass
            g._citation_lookup = (snap_counts, sidecar_counts)
        return g._citation_lookup

    def _citation_count_for(doi):
        if not doi:
            return None
        from cv_editor.citation_counts import _doi_key

        k = _doi_key(str(doi))
        snap, sidecar = _citation_lookup_for_request()
        rec = snap.get(k)
        if rec is not None:
            return rec.get("count") if isinstance(rec, dict) else rec
        # Fall back to sidecar so fetched-zero shows `0`, not `—`.
        if k in sidecar:
            return sidecar[k]
        return None

    def _get_expected_mtime_ns(req) -> int:
        """Parse the hidden mtime_ns form field. Returns 0 if absent or
        non-integer so write_with_backup can raise StaleFileError. Used by
        every save/bulk/restore route (V8-V11-D R2 MEDIUM dedup)."""
        try:
            return int(req.form.get("mtime_ns") or 0)
        except (TypeError, ValueError):
            return 0

    def _self_name() -> str:
        try:
            _, meta = yaml_io.load(ROOT / "data" / "meta.yml")
            return str(meta.get("self_bold", "")).strip() if meta else ""
        except Exception:
            return ""

    def _form_payload(req, sch: dict) -> tuple[dict, dict]:
        """Pull form fields into (form_data, parse_errors). A JSON field
        whose hidden input is malformed yields a parse_errors entry so the
        save handler can refuse rather than silently wipe the field."""
        out: dict = {}
        parse_errors: dict = {}
        for f in sch["fields"]:
            name, ftype = f["name"], f["type"]
            if ftype in _JSON_FIELD_TYPES:
                raw = req.form.get(f"{name}_json", "")
                if not raw:
                    out[name] = _empty_json_value(ftype)
                    continue
                try:
                    out[name] = json.loads(raw)
                except json.JSONDecodeError:
                    out[name] = _empty_json_value(ftype)
                    parse_errors[name] = (
                        "form data corrupted (malformed JSON in hidden field) — "
                        "reload the page and re-enter your changes"
                    )
            elif ftype == "bool":
                out[name] = bool(req.form.get(name))
            else:
                out[name] = req.form.get(name, "").strip() or None
        return out, parse_errors

    def _empty_json_value(ftype: str):
        # V20-cleanup T2 (2026-05-18): delegates to the FIELD_HANDLERS
        # registry; no longer hand-maintained here.
        return _empty_json_value_handler(ftype)

    def _form_to_entry(form: dict, sch: dict, existing=None):
        """Apply form values to a CommentedMap (existing, preserving order +
        comments) or build a fresh one. Schema-driven across all field types.

        V20 (2026-05-18): the 13-elif chain over `ftype` collapsed into a
        single dispatch through `cv_editor.field_handlers.FIELD_HANDLERS`.
        Each handler's `apply(form, field, entry)` mutates the in-progress
        entry directly, so a handler that needs to write auxiliary keys
        beyond its own field name stays co-located with the field it
        belongs to.
        """
        from cv_editor.field_handlers import FIELD_HANDLERS

        entry = existing if isinstance(existing, CommentedMap) else CommentedMap()
        for f in sch["fields"]:
            FIELD_HANDLERS[f["type"]].apply(form, f, entry)
        return entry

    def _target_from_form(req, sch: dict) -> dict:
        """Build a `target` dict for sections.insert_entry from form fields."""
        structure = sch["structure"]
        if structure == "list_of_subsections":
            return {"subsection": req.form.get("subsection") or sch.get("default_subsection")}
        if structure == "clusters":
            return {
                "institution": (req.form.get("cluster_institution") or "").strip(),
                "city": (req.form.get("cluster_city") or "").strip(),
            }
        if structure == "subsections_of_clusters":
            return {
                "subsection": req.form.get("subsection") or sch.get("default_subsection"),
                "institution": (req.form.get("cluster_institution") or "").strip(),
                "city": (req.form.get("cluster_city") or "").strip(),
            }
        return {}

    def _row_for_listing(section_key: str, sch: dict, rec: dict, self_nm: str) -> dict:
        """Translate a flat record into a list-view row. Section-specific
        derivations happen here so the template stays simple."""
        entry = rec["entry"]
        ctx = rec["ctx"]
        row = {
            "global_idx": rec["global_idx"],
            "highlighted": bool(entry.get("highlighted")),
            "view_url": url_for("entry_view", section=section_key, idx=rec["global_idx"]),
        }
        list_cols = sch.get("list_columns", []) or []
        for col in list_cols:
            if col == "first_author":
                row["first_author"] = first_author_display(
                    normalize_authors_for_render(entry.get("authors"))
                )
            elif col == "subsection":
                row["subsection"] = ctx.get("subsection", "")
            elif col == "institution":
                row["institution"] = ctx.get("institution", "")
            else:
                v = entry.get(col)
                row[col] = "" if v is None else str(v)
        # Always include subsection/institution for filter dropdowns.
        row["subsection"] = ctx.get("subsection", "")
        row["institution"] = ctx.get("institution", "")
        # Date-aware sort keys. Computed for any section that lists 'date'
        # or 'year' so client- and server-side sort agree on chronological
        # order across years (string compare on 'MM/YYYY' is wrong: 03/2026
        # would sort before 12/2025).
        sort_values = {}
        if "date" in list_cols:
            sort_values["date"] = sort_keys.date_sort_norm(entry.get("date"))
        if "year" in list_cols:
            sort_values["year"] = sort_keys.year_month_sort_norm(
                entry.get("year"), entry.get("month"), entry.get("day")
            )
        row["sort_values"] = sort_values
        # Date-conditional render feature: flag an entry that is hidden until a
        # future start, or that renders open-ended until a future end. Surfaced
        # as an always-visible list badge (NOT coupled to the `highlighted`
        # hidden attribute) so deferred entries are spottable without opening
        # each one. Only meaningful for date-gated sections.
        if section_key in validate.DATE_GATED_SECTIONS:
            row["future_status"] = validate.date_conditional_status(entry.get("date") or "")
            row["future_note"] = validate.date_gate_note(entry, section_key)
        else:
            row["future_status"] = None
            row["future_note"] = None
        if section_key == "publications":
            row.update(_publication_qc(entry, self_nm))
            row["title"] = str(entry.get("title", "(no title)"))
            # V14: citation count from the committed snapshot (data/citation_counts.json).
            # Renderer uses lowercase keys; mirror here. Editor distinguishes
            # `0` (fetched, no citations yet) from `—` (not in snapshot).
            row["citation_count"] = _citation_count_for(entry.get("doi"))
            # 2026-05-26: per-row media counts. `shown` = outlets without
            # highlighted: true (rendered when show_media=true); `total` =
            # every outlet across every media note. Sort key uses total
            # (high-coverage papers float to the top when sorted desc).
            shown, total = 0, 0
            for n in entry.get("notes") or []:
                if isinstance(n, dict) and n.get("type") == "media":
                    for o in n.get("outlets") or []:
                        total += 1
                        is_hl = isinstance(o, dict) and o.get("highlighted", False)
                        if not is_hl:
                            shown += 1
            row["media_shown_count"] = shown
            row["media_total_count"] = total
        else:
            row["needs_contribution"] = False
        return row

    def _derive_form_view_state(sch, entry, *, want_self_author=False, self_name=""):
        """Derive the same form-shaped state both edit and view templates
        need: author_forms, notes_form, simple_notes_form, open_access_form,
        list_field_data, amount_display_for, self_author_status, needs_contribution.

        Extracted in V5-D dedup pass (R2-H3). Computes the field-type set
        once so callers don't re-walk sch["fields"] eight times."""
        types = {f["type"] for f in sch["fields"]}
        state = {
            "author_forms": (
                [author_to_form(a) for a in normalize_authors_for_render(entry.get("authors"))]
                if "author_list" in types
                else []
            ),
            "notes_form": (
                notes_helpers.notes_yaml_to_form(entry.get("notes"))
                if "typed_notes" in types
                else []
            ),
            "simple_notes_form": (
                notes_helpers.simple_notes_yaml_to_form(entry.get("notes"))
                if "simple_notes" in types
                else []
            ),
            "open_access_form": (
                notes_helpers.open_access_yaml_to_form(entry.get("open_access"))
                if "open_access_dict" in types
                else None
            ),
            "list_field_data": {},
            "amount_display_for": {},
            "self_author_status": "absent",
            "needs_contribution": False,
        }
        for f in sch["fields"]:
            if f["type"] in ("string_list", "audiences_set"):
                v = entry.get(f["name"])
                state["list_field_data"][f["name"]] = [str(s) for s in (v or [])]
            elif f["type"] == "grant_amount":
                v = entry.get(f["name"])
                if v is None:
                    state["amount_display_for"][f["name"]] = ""
                else:
                    sv = str(v)
                    state["amount_display_for"][f["name"]] = sv[2:] if sv.startswith(r"\$") else sv
        if want_self_author and "author_list" in types and self_name:
            state["self_author_status"] = notes_helpers.self_author_position(
                entry.get("authors") or [], self_name
            )
            state.update(_publication_qc(entry, self_name))
        return state

    def _render_edit_form(
        section_key: str,
        mode: str,
        sch,
        path,
        *,
        entry,
        ctx,
        global_idx,
        errors=None,
        form_data=None,
        target_override=None,
        disagreements=None,
        staged_source=None,
        extra_warning=None,
    ):
        """One renderer for new + edit + import-staged. All sections."""
        structure = sch["structure"]
        state = _derive_form_view_state(
            sch,
            entry,
            want_self_author=(section_key == "publications"),
            self_name=_self_name() if section_key == "publications" else "",
        )
        author_forms = state["author_forms"]
        notes_form = state["notes_form"]
        simple_notes_form = state["simple_notes_form"]
        open_access_form = state["open_access_form"]
        list_field_data = state["list_field_data"]
        amount_display_for = state["amount_display_for"]
        self_author_status = state["self_author_status"]
        needs_contribution = state["needs_contribution"]

        # Active-grant past-end-date warning (research_support only).
        warning = extra_warning
        if section_key == "research_support":
            check_form = form_data or {f["name"]: entry.get(f["name"]) for f in sch["fields"]}
            w = validate.grant_end_date_warning(check_form)
            if w and not warning:
                warning = w

        # Picker defaults (subsection / institution / city).
        target = target_override or {}
        if not target:
            if structure == "list_of_subsections":
                target = {"subsection": ctx.get("subsection") or sch.get("default_subsection")}
            elif structure == "clusters":
                target = {"institution": ctx.get("institution"), "city": ctx.get("city")}
            elif structure == "subsections_of_clusters":
                target = {
                    "subsection": ctx.get("subsection") or sch.get("default_subsection"),
                    "institution": ctx.get("institution"),
                    "city": ctx.get("city"),
                }

        existing_targets = sections.list_targets(_load_section(section_key)[3], structure)

        return render_template(
            "entry_edit.html",
            section_key=section_key,
            mode=mode,
            section_label=sch["label"],
            structure=structure,
            fields=sch["fields"],
            subsections=sch.get("subsections", []),
            existing_targets=existing_targets,
            target=target,
            entry=entry,
            author_forms=author_forms,
            notes_form=notes_form,
            simple_notes_form=simple_notes_form,
            open_access_form=open_access_form,
            list_field_data=list_field_data,
            amount_display_for=amount_display_for,
            global_idx=global_idx,
            mtime_ns=yaml_io.mtime_ns(path),
            errors=errors or {},
            form_data=form_data or {},
            self_author_status=self_author_status,
            needs_contribution=needs_contribution,
            primary_note_types=notes_helpers.PRIMARY_NOTE_TYPES,
            note_type_label=notes_helpers.NOTE_TYPE_LABEL,
            all_note_types=notes_helpers.NOTE_TYPES,
            disagreements=disagreements or {},
            staged_source=staged_source,
            extra_warning=warning,
        )

    # ----- V17-D: background-job kicker factory -----
    # Both V3's QC kicker and V7's URL-verify kicker followed the same
    # daemon-thread + single-in-flight + start-failure-resets-flag shape.
    # V14 (citation counts) will need a third. Centralize once.
    import threading as _threading

    def _make_kicker(*, name: str, build_argv, timeout: int):
        """Return a (kick, state, lock) triple for a single-in-flight
        background subprocess runner. `build_argv(**kwargs)` is called
        inside the kick to construct the subprocess argv (so caller-provided
        flags like force=True can flow through). On any subprocess error
        the running flag is reset so subsequent kicks don't dead-lock.

        V13-V19-D R1-M4-fix-followup (2026-05-18): the lock is an RLock
        because callers may legitimately hold it ACROSS a `kick()` call
        when they need to serialize state-check + side-effect + kick
        atomically (V19's apply route is the canonical example — see
        scripts/CLAUDE.md gotcha #20). A plain non-reentrant Lock would
        deadlock the same-thread re-acquisition inside `kick()`; the
        test_pubmed_sync_apply_writes_decisions_file regression was
        the trigger for this fix.
        """
        lock = _threading.RLock()
        state = {"running": False, "last_started": 0.0}

        def kick(**kwargs):
            with lock:
                if state["running"]:
                    return
                state["running"] = True
                state["last_started"] = time.time()
            argv = build_argv(**kwargs)

            def _run():
                try:
                    subprocess.run(
                        argv,
                        cwd=str(_ENGINE),
                        capture_output=True,
                        timeout=timeout,
                    )
                except Exception:
                    # T4.10 + Tier B / B9 (2026-05-27): surface background
                    # subprocess failures to both the terminal AND the
                    # cv_editor.log file. logger.exception() captures the
                    # full traceback (previous logger.warning() only
                    # logged the exception string — silent traceback
                    # loss on daemon threads was the R4 BLOCKER).
                    app.logger.exception(
                        "background subprocess %r failed (argv=%r)",
                        name,
                        argv,
                    )
                finally:
                    with lock:
                        state["running"] = False

            try:
                _threading.Thread(target=_run, daemon=True).start()
            except Exception:
                with lock:
                    state["running"] = False
                raise

        return kick, state, lock

    # ----- Altmetric tracker resolution cache (V13 finish) -----
    # TRACKER_CACHE_PATH default + setdefault are in the _DEFAULT_PATHS
    # block at create_app() top. Read sites use _cfg_path("TRACKER_CACHE_PATH").

    # PubMed-sync sidecar path: PUBMED_SYNC_SIDECAR_PATH in _DEFAULT_PATHS.
    # Reading callers go through _pubmed_sync_sidecar_path() — the thin
    # wrapper preserves the existing call sites without churn.
    def _pubmed_sync_sidecar_path() -> Path:
        return _cfg_path("PUBMED_SYNC_SIDECAR_PATH")

    # V20-cleanup M3 (2026-05-18): mtime-keyed cache for the PubMed
    # sync sidecar. `entry_view` used to re-parse the full sidecar
    # JSON on every page view (~5-15ms for 93 PMIDs); now it reads
    # via this helper, which keeps a single in-memory state and
    # only re-loads when the file mtime changes. Cache is stashed on
    # app.config (consistent with TRACKER_CACHE_PATH idiom). The
    # `flash()` call is guarded by `has_request_context()` so the
    # helper is safe to reuse from non-request callers (background
    # threads etc.).
    app.config.setdefault("_PMSYNC_SIDECAR_CACHE", {"mtime_ns": -1, "state": None})
    # V20-cleanup M1 (2026-05-18): mtime-keyed memo for the triage-page
    # compute_decisions() result. Key is (sidecar.mtime_ns, pubs.mtime_ns);
    # force=True is implicit (the editor never calls with force=False).
    app.config.setdefault("_PMSYNC_DECISIONS_CACHE", {"key": None, "result": None})

    # V20-cleanup M2 (2026-05-18): UUID-keyed pending-form snapshots.
    # On apply-route rejection, the user's per-row radio choices +
    # reason inputs are snapshotted under a fresh UUID. The redirect
    # back to /pubmed_sync includes ?pending=<uuid>; the view pops the
    # snapshot and re-populates the form so a 30+-row triage doesn't
    # lose progress on one missing-reason mistake. Flask's signed-cookie
    # session can't hold 30 rows × form data (~4KB cap); a process-local
    # dict bypasses that constraint. Bounded to MAX entries (FIFO).
    # Pending-form stores (post-batch refactor, 2026-05-25). Two
    # callers today, factory expects a 3rd (style_save 409 has the
    # same browser-doesn't-follow-Location-on-4xx bug class as
    # entry_save). The factory owns lifecycle (UUID minting, FIFO
    # eviction, pop semantics); callers own payload shaping.
    def _make_pending_store(config_key: str, max_n: int):
        """Factory for UUID-keyed, FIFO-bounded in-memory pending-form
        stores. Returns (stash_raw, pop) callables.

        `stash_raw(snapshot: dict) -> str`: returns UUID hex (or ""
        if snapshot empty). Caller is responsible for shaping the
        snapshot — each store has its own payload contract.

        `pop(token: str) -> dict`: pops + returns snapshot, or {} if
        the token is absent (FIFO-evicted, never-stashed, or stale).

        Pattern history:
        - V20-cleanup M2: _PMSYNC_PENDING for /pubmed_sync/apply 409.
        - Stage B / I8 (2026-05-25): _ENTRY_PENDING for entry_save 409.
        - Cross-cutting reviewer flagged a 3rd lander (style_save 409
          still uses `write_or_409` with the same UX bug); when that
          lands, just call `_make_pending_store("_STYLE_PENDING", 20)`.
        """
        import uuid as _uuid

        app.config.setdefault(config_key, {})

        def stash_raw(snapshot: dict) -> str:
            if not snapshot:
                return ""
            token = _uuid.uuid4().hex
            pending = app.config[config_key]
            pending[token] = snapshot
            while len(pending) > max_n:
                oldest = next(iter(pending))
                pending.pop(oldest, None)
            return token

        def pop(token: str) -> dict:
            if not token:
                return {}
            return app.config[config_key].pop(token, {}) or {}

        return stash_raw, pop

    _pmsync_stash_raw, _pmsync_pop_pending = _make_pending_store("_PMSYNC_PENDING", 20)
    _entry_stash_raw, _entry_pop_pending = _make_pending_store("_ENTRY_PENDING", 20)
    _style_stash_raw, _style_pop_pending = _make_pending_store("_STYLE_PENDING", 20)

    def _pubmed_sync_state_cached():
        """Return the PubMed-sync `SidecarState` (or None on failure)
        via the existing mtime-keyed cache. Single source for both
        per-PMID flagged-fields lookup and the V23-B Phase 1.5 cross-
        system silencing index. Failure-safe: returns None and logs
        on any load error (gotcha #40a)."""
        from flask import has_request_context

        from cv_editor.pubmed_sync import load_sidecar

        cache_entry = app.config["_PMSYNC_SIDECAR_CACHE"]
        path = _pubmed_sync_sidecar_path()
        mtime = yaml_io.mtime_ns(path) if path.exists() else 0
        if cache_entry["mtime_ns"] != mtime:
            try:
                cache_entry["state"] = load_sidecar(path)
                cache_entry["mtime_ns"] = mtime
            except Exception as exc:
                app.logger.warning("pmsync sidecar load failed: %s", exc)
                if has_request_context():
                    flash(
                        "PubMed sidecar load failed — banner counts may be stale.",
                        "warn",
                    )
                cache_entry["mtime_ns"] = -1
        return cache_entry["state"]

    def _qc_state_for_cross_check():
        """Load (qc_sidecar, qc_decisions) for the cross-check. Returns
        (None, None) on failure. Used by PubMed-sync read paths to
        compute the cross-system silencing index. V23-B Phase 1.5."""
        try:
            from cv_editor import qc_decisions, qc_sync
            from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

            sc = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
            dec = qc_decisions.load(_qc_decisions_path(), silent=True)
            return sc, dec
        except Exception as exc:
            app.logger.warning("qc state for cross-check load failed: %s", exc)
            return None, None

    def _qc_apply_clear_matching_pmsync_overrides(applies: list) -> int:
        """V23-B Phase 1.5 (2026-05-26): after /qc/apply writes new
        canonical to YAML, remove any PubMed-sync overrides whose
        snapshot now mismatches reality. Called from /qc/apply post-
        write. Returns the count of cleared overrides. Held under
        `_cross_system_apply_lock` to serialize with /pubmed_sync/apply's
        symmetric cross-clear.

        `applies` is the list of {"finding": ..., ...} dicts the apply
        route built. Only entries on CROSS_FIELDS with a pmid get
        considered.
        """
        from cv_editor.decision_cross_check import CROSS_FIELDS
        from cv_editor.pubmed_sync import save_sidecar

        try:
            with _cross_system_apply_lock:
                state = _pubmed_sync_state_cached()
                if state is None or not state.accepted_yaml_overrides:
                    return 0
                cleared = 0
                for d in applies:
                    f = d["finding"]
                    ftype = f.get("_finding_type", "MISMATCH")
                    if ftype not in ("MISMATCH", "VARIANT"):
                        continue
                    field_name = f.get("field")
                    if field_name not in CROSS_FIELDS:
                        continue
                    pmid_s = str(f.get("pmid") or "").strip()
                    if not pmid_s:
                        continue
                    overrides = state.accepted_yaml_overrides.get(pmid_s)
                    if not overrides or field_name not in overrides:
                        continue
                    del overrides[field_name]
                    if not overrides:
                        del state.accepted_yaml_overrides[pmid_s]
                    cleared += 1
                if cleared:
                    save_sidecar(_pubmed_sync_sidecar_path(), state)
                    # Invalidate cache so next read sees the change.
                    app.config["_PMSYNC_SIDECAR_CACHE"]["mtime_ns"] = -1
                return cleared
        except Exception as exc:
            app.logger.warning("qc apply cross-clear failed: %s", exc)
            return 0

    def _pmsync_apply_clear_matching_qc_decisions(decisions: list) -> int:
        """V23-B Phase 1.5 (2026-05-26): symmetric of the QC side.
        After /pubmed_sync/apply will write PubMed values to YAML,
        tombstone any matching QC decisions on (pmid, field) where
        field is in CROSS_FIELDS. Called from /pubmed_sync/apply
        BEFORE kicking the subprocess (the subprocess is fire-and-
        forget, but the QC tombstone is local-only and synchronous).
        Returns the count of tombstoned decisions. Held under
        `_cross_system_apply_lock`.

        `decisions` is the list of {"pmid", "field", "decision",
        "reason"} dicts the route built. Only apply_pubmed on
        CROSS_FIELDS get considered.
        """
        from cv_editor.decision_cross_check import CROSS_FIELDS, build_qc_decisions_index

        try:
            with _cross_system_apply_lock:
                qc_sc, qc_dec = _qc_state_for_cross_check()
                if qc_dec is None:
                    return 0
                idx = build_qc_decisions_index(qc_sc, qc_dec)
                if not idx:
                    return 0
                tombstoned = 0
                for d in decisions:
                    if d.get("decision") != "apply_pubmed":
                        continue
                    field_name = d.get("field")
                    if field_name not in CROSS_FIELDS:
                        continue
                    pmid_s = str(d.get("pmid") or "").strip()
                    if not pmid_s:
                        continue
                    entry_in_idx = idx.get((pmid_s, field_name))
                    if entry_in_idx is None:
                        continue
                    fid, _dec = entry_in_idx
                    qc_dec.remove(fid)  # tombstones with 30-day TTL
                    tombstoned += 1
                if tombstoned:
                    qc_dec.save_atomic(_qc_decisions_path())
                return tombstoned
        except Exception as exc:
            app.logger.warning("pmsync apply cross-clear failed: %s", exc)
            return 0

    def _pubmed_flagged_for_pmid(pmid: str, entry) -> list[str]:
        """Effective flagged fields for one PMID, using the cached
        sidecar state + cross-system silencing. Returns an empty list
        when the sidecar is missing, corrupt, or doesn't know the PMID.
        """
        from cv_editor.decision_cross_check import build_qc_decisions_index
        from cv_editor.pubmed_sync import effective_flagged_fields

        pmid_s = str(pmid or "").strip()
        if not pmid_s:
            return []
        state = _pubmed_sync_state_cached()
        if state is None:
            return []
        rec = state.entries.get(pmid_s)
        if rec is None:
            return []
        # V23-B Phase 1.5 (2026-05-26): cross-check via QC decisions.
        qc_sc, qc_dec = _qc_state_for_cross_check()
        qc_idx = build_qc_decisions_index(qc_sc, qc_dec) if qc_dec else {}
        return list(
            effective_flagged_fields(
                entry,
                rec,
                state.accepted_yaml_overrides.get(pmid_s, {}),
                pmid=pmid_s,
                qc_decisions_index=qc_idx,
            )
        )

    def _pubmed_cross_silenced_for_pmid(pmid: str, entry) -> list[tuple[str, dict]]:
        """V23-B Phase 1.5 (2026-05-26): list of (field, badge) pairs
        for fields silenced by a matching QC keep_yaml decision. Used
        by entry_view banner sub-line."""
        from cv_editor.decision_cross_check import build_qc_decisions_index
        from cv_editor.pubmed_sync import cross_silenced_flagged_fields

        pmid_s = str(pmid or "").strip()
        if not pmid_s:
            return []
        state = _pubmed_sync_state_cached()
        if state is None:
            return []
        rec = state.entries.get(pmid_s)
        if rec is None:
            return []
        qc_sc, qc_dec = _qc_state_for_cross_check()
        if not qc_dec:
            return []
        qc_idx = build_qc_decisions_index(qc_sc, qc_dec)
        return cross_silenced_flagged_fields(
            entry,
            rec,
            state.accepted_yaml_overrides.get(pmid_s, {}),
            qc_idx,
            pmid=pmid_s,
        )

    def _tracker_cache() -> altmetric_tracker_cache.TrackerCache:
        """Return a fresh TrackerCache view.

        The cache reads the sidecar JSON on construction; callers should
        be short-lived (single request) so re-reading on every call
        avoids stale-in-memory state across concurrent requests.
        """
        return altmetric_tracker_cache.TrackerCache(_cfg_path("TRACKER_CACHE_PATH"))

    # R8-H3: tracker walking + substitution use tracker_walk module
    # functions directly at call sites. Only the two closures that bind
    # the cache or load YAML stay — they earn their closure status by
    # avoiding repeated `_load_section` / `_tracker_cache()` arguments
    # at every call site.

    def _iter_publication_trackers(data):
        """Yield {pub_idx, ...} dict rows for templates that want the
        legacy as_row() shape. Direct callers prefer
        tracker_walk.iter_tracker_outlets()."""
        for ref in tracker_walk.iter_tracker_outlets(data):
            yield ref.as_row()

    def _count_unresolved_trackers() -> dict:
        try:
            _, _, _, data = _load_section("publications")
        except Exception:
            return {
                "total_trackers": 0,
                "pubs_with_trackers": 0,
                "by_status": {
                    "failed_network": 0,
                    "failed_rate_limit": 0,
                    "failed_no_redirect": 0,
                    "unknown": 0,
                },
            }
        return tracker_walk.count_unresolved_trackers(data, _tracker_cache())

    def _entry_unresolved_tracker_count(entry) -> int:
        return tracker_walk.entry_unresolved_tracker_count(entry, _tracker_cache())

    _kick_qc_if_idle, _qc_state, _qc_lock = _make_kicker(
        name="qc",
        build_argv=lambda: [sys.executable, "-m", "cv_editor.qc_publications"],
        timeout=300,
    )
    _kick_url_verify_if_idle, _url_state, _url_lock = _make_kicker(
        name="url_verify",
        build_argv=lambda *, force=False: (
            [sys.executable, "-m", "cv_editor.verify_urls", "--quiet"]
            + (["--force"] if force else [])
        ),
        timeout=600,
    )

    # V19: PubMed sync dry-run + apply kickers.
    _kick_pubmed_dryrun_if_idle, _pmsync_state, _pmsync_lock = _make_kicker(
        name="pubmed_sync_dryrun",
        build_argv=lambda *, force=False, only_epub=False: (
            [sys.executable, "-m", "cv_editor.pubmed_sync", "--dry-run", "--quiet"]
            + (["--force"] if force else [])
            + (["--only-epub"] if only_epub else [])
        ),
        timeout=600,
    )
    _kick_pubmed_apply_if_idle, _pmsync_apply_state, _pmsync_apply_lock = _make_kicker(
        name="pubmed_sync_apply",
        # Task #33 fix (2026-05-25): --force ensures every PMID gets
        # re-fetched, not just those past the 90d/14d TTL. Without it,
        # `record_keep_yaml_overrides` silently skips any keep_yaml
        # decision whose PMID is in-TTL ("PMID not fetched this run"),
        # so the override never persists and the flag re-surfaces on
        # the next dry-run. Apply is user-triggered (not on every page
        # load) so the re-fetch cost is bounded.
        build_argv=lambda *, decisions_path: [
            sys.executable,
            "-m",
            "cv_editor.pubmed_sync",
            "--apply",
            "--decisions",
            str(decisions_path),
            "--force",
            "--quiet",
        ],
        timeout=600,
    )

    # V23-B Phase 1 (2026-05-25): the QC-apply state + RLock. Constructed
    # HERE and handed BY REFERENCE into QCTriageDeps (the /qc/apply route +
    # sweep gating live in qc_triage_routes) AND used by the cross-system
    # clear helpers (_qc_apply_clear_matching_pmsync_overrides etc., still
    # in this file). All consumers MUST share the SAME objects (gotcha #69);
    # QCTriageDeps requires these fields (no default) so a dropped kwarg
    # fails loudly rather than handing the route a private lock (M2 review
    # HIGH-1). RLock per V13-V19-D R1-H2 (a non-reentrant Lock has
    # deadlocked the kicker before; same risk class here).
    import threading as _threading_qc

    _qc_apply_lock = _threading_qc.RLock()
    _qc_apply_state = {"running": False}
    # V23-B Phase 1.5 (2026-05-26): cross-system apply lock. Held briefly
    # when either /qc/apply or /pubmed_sync/apply needs to clear the
    # OTHER system's matching decision after an apply. Shared by reference
    # into QCTriageDeps + the pubmed-sync apply path.
    _cross_system_apply_lock = _threading_qc.RLock()

    # V14: citation-count fetcher (mirrors verify_urls pattern).
    _kick_citation_fetch_if_idle, _cit_state, _cit_lock = _make_kicker(
        name="citation_fetch",
        build_argv=lambda *, force=False: (
            [sys.executable, "-m", "cv_editor.fetch_citation_counts", "--quiet"]
            + (["--force"] if force else [])
        ),
        timeout=600,
    )

    def _qc_status():
        """Return a dict describing whether qc/report.md has fresher findings
        than the publications YAML. Used to surface a non-blocking banner."""
        pubs = ROOT / "data" / "publications.yml"
        report = ROOT / "qc" / "report.md"
        if not (pubs.exists() and report.exists()):
            return {"running": _qc_state["running"], "fresh": False, "url": None}
        fresh = report.stat().st_mtime >= pubs.stat().st_mtime
        return {
            "running": _qc_state["running"],
            "fresh": fresh,
            "url": "/qc/report",
            "mtime": datetime.fromtimestamp(report.stat().st_mtime).isoformat(timespec="seconds"),
        }

    def _resolve_idx(section_key: str, idx: int):
        """Common pattern: load section, locate by idx, abort 404 if missing."""
        sch, path, header, data = _load_section(section_key)
        rec = sections.locate(data, sch["structure"], idx)
        if rec is None:
            abort(404)
        return sch, path, header, data, rec

    def _sse_frames(tuples):
        """Translate (kind, payload) tuples from build_runner.stream_subprocess
        into SSE wire frames. V5-D dedup extraction."""
        for kind, payload in tuples:
            if kind == "line":
                safe = payload.replace("\r", "")
                yield f"event: line\ndata: {json.dumps(safe)}\n\n"
            elif kind == "done":
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
            elif kind == "error":
                yield f"event: error\ndata: {json.dumps(payload)}\n\n"

    def _sse_response(frames):
        """T3.3 / R8-H1: wrap an SSE frames iterable in a Flask Response.
        `frames` may be a generator or a plain iterable of (kind, payload)
        tuples — the second form lets error-only callers build a one-shot
        response without a separate generator. Always appends a terminal
        `close` event."""

        def _gen():
            yield from _sse_frames(frames)
            yield "event: close\ndata: \"\"\n\n"

        return Response(
            stream_with_context(_gen()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Basic-QC + URL-verify routes live in cv_editor/qc_basic_routes.py
    # (2026-05-29 M2 extraction). _qc_status stays a create_app() closure
    # (entry_view shares it); both background kickers stay in create_app()
    # (the basic-QC kicker is shared with entry_save/trackers/promote) and
    # are passed BY REFERENCE.
    from cv_editor.qc_basic_routes import QCBasicDeps, register_qc_basic_routes

    register_qc_basic_routes(
        app,
        QCBasicDeps(
            root=ROOT,
            qc_status=_qc_status,
            qc_kick=_kick_qc_if_idle,
            url_state=_url_state,
            url_kick=_kick_url_verify_if_idle,
        ),
    )

    # ----- V14: citation counts -----

    # V14 citation-counts routes live in cv_editor/citations_routes.py
    # (2026-05-29 M2 extraction). The shared helpers (_cfg_path,
    # _load_section) stay create_app() closures and are handed in via
    # deps so their behaviour is provably unchanged. The citation
    # kicker (_kick_citation_fetch_if_idle / _cit_state) is defined
    # above and passed by reference.
    from cv_editor.citations_routes import (
        CitationsDeps,
        register_citations_routes,
    )

    register_citations_routes(
        app,
        CitationsDeps(
            root=ROOT,
            cfg_path=_cfg_path,
            load_section=_load_section,
            logger=app.logger,
            cit_kick=_kick_citation_fetch_if_idle,
            cit_state=_cit_state,
        ),
    )

    def _style_stash_pending(form: dict, mode: str, idx: int | None) -> str:
        """Tier B / B5 (2026-05-27): stash a /style/save form snapshot.

        Contract: `form` is the already-normalized dict built in
        `style_save` (filename, audience, every `bv.DEFAULT_TRUE_INPUTS`
        + `bv.BOOLEAN_INPUTS` key). Any future key added to those
        tuples round-trips automatically — pinned by
        `test_style_save_409_round_trips_all_default_true_inputs`.

        `existing_filenames` is recomputed server-side on re-render;
        do NOT stash it (would freeze a snapshot of the variants list
        and miss any concurrent write the user must reconcile).
        """
        if not form:
            return ""
        return _style_stash_raw(
            {
                "form": dict(form),
                "mode": mode,
                "idx": idx,
            }
        )

    def _entry_stash_pending(form_data: dict, target: dict) -> str:
        """Stage B / I8 (2026-05-25): stash a parsed entry-form
        snapshot. We stash the OUTPUT of _form_payload (parsed dict
        with native Python types — lists for authors, dicts for
        typed_notes/OA, etc.) NOT raw request.form, because complex
        sub-editors drive their UI from _derive_form_view_state(sch,
        entry, ...) which reads entry.get(...) — NOT raw form. The
        parsed dict is what _form_to_entry consumes, so we round-trip
        through the V20 B2 FIELD_HANDLERS dispatch.
        """
        if not form_data:
            return ""
        return _entry_stash_raw(
            {
                "form_data": dict(form_data),
                "target": dict(target or {}),
            }
        )

    def _pending_save_warning(cause: str | None) -> str:
        """Build the banner-warn text shown when a pending-form
        snapshot is consumed. The `cause` is the exception class name
        from the original 409 (StaleFileError or Timeout) so the user
        sees the underlying reason, not just a generic "conflict".
        Future-proofing (cross-cutting review R2): an unknown cause
        falls through to the raw class name so a future exception
        rename is visible rather than silently dropped."""
        suffix = ""
        if cause == "StaleFileError":
            suffix = " (the file was modified by another save — likely a different tab or the background QC job)."
        elif cause == "Timeout":
            suffix = " (another writer was holding the file lock when your save fired)."
        elif cause:
            suffix = f" (cause: {cause})."
        return (
            "Your changes were preserved from a prior save attempt "
            "that conflicted with another write — review and save again." + suffix
        )

    # V19 PubMed-sync routes live in cv_editor/pubmed_sync_routes.py
    # (2026-05-29 M2b extraction). The cross-system cluster + both
    # kickers + the apply lock/state + the _PMSYNC_PENDING store stay
    # create_app() closures and are passed BY REFERENCE so lock/state
    # identity is preserved (gotchas #56/#69).
    from cv_editor.pubmed_sync_routes import (
        PubmedSyncDeps,
        register_pubmed_sync_routes,
    )

    register_pubmed_sync_routes(
        app,
        PubmedSyncDeps(
            root=ROOT,
            cfg_path=_cfg_path,
            config=app.config,
            logger=app.logger,
            load_section=_load_section,
            pubmed_sync_sidecar_path=_pubmed_sync_sidecar_path,
            qc_state_for_cross_check=_qc_state_for_cross_check,
            pmsync_apply_clear_matching_qc_decisions=_pmsync_apply_clear_matching_qc_decisions,
            pmsync_dryrun_kick=_kick_pubmed_dryrun_if_idle,
            pmsync_state=_pmsync_state,
            pmsync_apply_kick=_kick_pubmed_apply_if_idle,
            pmsync_apply_state=_pmsync_apply_state,
            pmsync_apply_lock=_pmsync_apply_lock,
            pmsync_stash_raw=_pmsync_stash_raw,
            pmsync_pop_pending=_pmsync_pop_pending,
        ),
    )

    # ----- V23-B Phase 3: QC triage UI -----

    # QC_DECISIONS_PATH default is in _DEFAULT_PATHS at create_app() top.
    def _qc_decisions_path() -> Path:
        return _cfg_path("QC_DECISIONS_PATH")

    # V23-B Phase 1 + Phase 3 routes (and their state / kicker /
    # pending-store) live in `cv_editor/qc_triage_routes.py` (2026-05-28
    # extraction). Wire them here now that all the cross-cutting helpers
    # they depend on are defined. The deps object IS the seam: every
    # field used to be a name `create_app()` closed over implicitly.
    from cv_editor.qc_triage_routes import (
        QCTriageDeps,
        register_qc_triage_routes,
    )

    register_qc_triage_routes(
        app,
        QCTriageDeps(
            ROOT=ROOT,
            yaml_io=yaml_io,
            qc_decisions_path=_qc_decisions_path,
            load_section=_load_section,
            pending_save_warning=_pending_save_warning,
            qc_apply_clear_matching_pmsync_overrides=_qc_apply_clear_matching_pmsync_overrides,
            pubmed_sync_state_cached=_pubmed_sync_state_cached,
            make_pending_store=_make_pending_store,
            make_kicker=_make_kicker,
            qc_apply_state=_qc_apply_state,
            qc_apply_lock=_qc_apply_lock,
        ),
    )

    # ----- routes: generic per-section CRUD + meta -----
    # Live in cv_editor/sections_routes.py (2026-05-29 M2b extraction).
    # The hot path. Every helper it calls (incl. require_section, the
    # entry pending store, and the cross-system banner helpers) stays a
    # create_app() closure, passed BY REFERENCE.
    from cv_editor.sections_routes import SectionsDeps, register_sections_routes

    register_sections_routes(
        app,
        SectionsDeps(
            root=ROOT,
            load_section=_load_section,
            require_section=require_section,
            self_name=_self_name,
            resolve_idx=_resolve_idx,
            render_edit_form=_render_edit_form,
            derive_form_view_state=_derive_form_view_state,
            row_for_listing=_row_for_listing,
            form_to_entry=_form_to_entry,
            form_payload=_form_payload,
            target_from_form=_target_from_form,
            get_expected_mtime_ns=_get_expected_mtime_ns,
            write_or_409=write_or_409,
            qc_status=_qc_status,
            entry_unresolved_tracker_count=_entry_unresolved_tracker_count,
            pubmed_flagged_for_pmid=_pubmed_flagged_for_pmid,
            pubmed_sync_state_cached=_pubmed_sync_state_cached,
            pubmed_cross_silenced_for_pmid=_pubmed_cross_silenced_for_pmid,
            qc_decisions_path=_qc_decisions_path,
            entry_stash_pending=_entry_stash_pending,
            entry_pop_pending=_entry_pop_pending,
            pending_save_warning=_pending_save_warning,
            qc_kick=_kick_qc_if_idle,
        ),
    )

    # ----- V5-B: freeze workspace -----

    # Style / typography / freeze routes live in cv_editor/style_routes.py
    # (2026-05-29 M2 extraction). Shared closures (load_section,
    # write_or_409, get_expected_mtime_ns, the _STYLE_PENDING pair,
    # _pending_save_warning, _sse_response) stay in create_app() and are
    # passed via deps; the 4 style/freeze-internal helpers moved into the
    # module.
    from cv_editor.style_routes import StyleDeps, register_style_routes

    register_style_routes(
        app,
        StyleDeps(
            load_section=_load_section,
            write_or_409=write_or_409,
            get_expected_mtime_ns=_get_expected_mtime_ns,
            style_stash_pending=_style_stash_pending,
            style_pop_pending=_style_pop_pending,
            pending_save_warning=_pending_save_warning,
            sse_response=_sse_response,
            capabilities=caps,
        ),
    )

    # Publications-specific routes live in cv_editor/publications_routes.py
    # (2026-05-29 M2b extraction). The tracker-cache trio + the basic-QC
    # kicker stay create_app() closures (shared with index/entry_view) and
    # are passed BY REFERENCE. The _VERIFY_HEAD_PROBE setdefault moved into
    # the module's register fn (only the verify_resolved route reads it).
    from cv_editor.publications_routes import (
        PublicationsDeps,
        register_publications_routes,
    )

    register_publications_routes(
        app,
        PublicationsDeps(
            root=ROOT,
            load_section=_load_section,
            write_or_409=write_or_409,
            get_expected_mtime_ns=_get_expected_mtime_ns,
            resolve_idx=_resolve_idx,
            render_edit_form=_render_edit_form,
            form_to_entry=_form_to_entry,
            tracker_cache=_tracker_cache,
            count_unresolved_trackers=_count_unresolved_trackers,
            iter_publication_trackers=_iter_publication_trackers,
            sse_response=_sse_response,
            qc_kick=_kick_qc_if_idle,
            capabilities=caps,
        ),
    )

    # Cross-cutting shell routes (index/search/rebuild/healthz/quit) live
    # in cv_editor/core_routes.py (2026-05-29 M2b — last module). The SSE
    # responder + tracker/cross-system banner helpers stay create_app()
    # closures, passed BY REFERENCE.
    from cv_editor.core_routes import CoreDeps, register_core_routes

    register_core_routes(
        app,
        CoreDeps(
            root=ROOT,
            cfg_path=_cfg_path,
            load_section=_load_section,
            logger=app.logger,
            count_unresolved_trackers=_count_unresolved_trackers,
            tracker_cache=_tracker_cache,
            pubmed_sync_state_cached=_pubmed_sync_state_cached,
            qc_state_for_cross_check=_qc_state_for_cross_check,
            qc_decisions_path=_qc_decisions_path,
            sse_response=_sse_response,
        ),
    )

    # M5 5b: whole-CV exports (Markdown now; HTML follows in CP4).
    from cv_editor.export_routes import ExportDeps, register_export_routes

    register_export_routes(app, ExportDeps(root=ROOT, logger=app.logger))

    # ----- template helpers -----
    @app.template_filter("safe_url")
    def _safe_url(value):
        """Render a URL only if it uses an http(s)/mailto scheme. Defends
        against `javascript:` and `data:` URIs that could leak into a YAML
        field. Returns '#' for anything that doesn't pass."""
        if not value:
            return "#"
        s = str(value).strip()
        low = s.lower()
        if low.startswith(("http://", "https://", "mailto:")):
            return s
        return "#"

    @app.template_filter("id_url")
    def _id_url(value, kind):
        """Delegate to cv_editor.url_helpers.id_url so the script CLI can
        reuse the exact same logic without spinning up Flask."""
        return url_helpers.id_url(value, kind)

    @app.template_filter("altmetric_url")
    def _altmetric_url(title):
        """Deep link into Altmetric Explorer searching for the article
        title (quoted phrase). Lets the user (signed into Explorer) scan
        press mentions there, then manually copy them into the entry's
        notes.media.outlets list. The DOI-based search the V12 version
        used stopped returning hits after the 10 Nov 2025 API pivot."""
        return url_helpers.altmetric_url(title)

    @app.template_filter("pluralize")
    def _pluralize(count, singular, plural):
        """Tier B / B7 (2026-05-27): replace the 12 inline
        `{{ '' if N == 1 else 's' }}` snippets in _macros.html. Takes
        BOTH forms explicitly because naive `+s` corrupts "match" →
        "matchs" (correct: "matches") and "decision" → "decisions"
        (correct). Pre-impl critic R-B flagged this with a real
        example in qc_findings_banner_entry.
        Usage: {{ count|pluralize("finding", "findings") }}
        Also handles verb agreement: {{ count|pluralize("has", "have") }}.
        """
        try:
            n = int(count) if count is not None else 0
        except (TypeError, ValueError):
            n = 0
        return singular if n == 1 else plural

    @app.context_processor
    def inject_helpers():
        # current_section: best-effort guess of which nav item is active.
        # Routes use either `section` (URL converter) or a literal key
        # we infer from request.path.
        section = None
        path = request.path if request else ""
        for key in schemas.SCHEMAS:
            if path == f"/{key}" or path.startswith(f"/{key}/"):
                section = key
                break
        if section is None:
            for nav_key in nav._PATH_DERIVED_NAV_KEYS:
                if path == f"/{nav_key}" or path.startswith(f"/{nav_key}/"):
                    section = nav_key
                    break
        if section is None and path.startswith("/publications/trackers"):
            section = "trackers"

        # 1.2.0 nav seam: host-contributed entries, resolved to URLs. `_resolve` is
        # documented not to raise, and this belt-and-braces catch is where the
        # damage WOULD land — a raise here 500s all 25 templates that extend
        # base.html, `/` included, which is the only recovery surface.
        #
        # The MATCHING below sits inside the same try as defence in depth, NOT as
        # the fix for anything demonstrated. N1's checkpoint found a real escape
        # here — `url_for` can return a non-str via a host's
        # `url_build_error_handler`, and the matcher's attribute access then raised
        # AttributeError out of this processor (reproduced) — but what closes that
        # is `nav._resolve`'s `isinstance(url, str)` guard. Given the guard,
        # `match_path` is always a str on a frozen dataclass, so this block cannot
        # raise: mutating it back out of the try leaves the suite green (measured).
        # Kept anyway, because a future field or matcher change would not be so safe.
        try:
            extra_nav = nav._resolve(app)
            if section is None:
                # Host entries, matched on their OWN resolved path — the engine
                # never matches a literal host path. `extra_nav` is longest-first,
                # so a host registering both /reports and /reports/monthly resolves
                # the sub-page to the sub-page. `match_path` is decoded and
                # slash-normalised; `script_root` is added back because `url_for`
                # includes it while `request.path` does not. A host route passing
                # `current_section` explicitly never reaches this fallback.
                mounted = ((request.script_root if request else "") + path).rstrip("/") or "/"
                for r in extra_nav:
                    if mounted == r.match_path or mounted.startswith(r.match_path + "/"):
                        section = r.key
                        break
        except Exception:  # a host bug must not take down every page
            _nav_failure_logged = app.extensions.setdefault("cv_editor_nav_failed", [])
            if not _nav_failure_logged:
                # Log the traceback ONCE. A persistent failure would otherwise
                # append a full traceback to the real log file on every request.
                _nav_failure_logged.append(True)
                app.logger.exception("nav: resolving host-contributed entries failed")
            else:
                app.logger.warning("nav: resolving host-contributed entries failed again")
            extra_nav = []
        # T3.6: centralize TRACKER_HOSTS — JS reads from this single source.
        return {
            "messages": get_flashed_messages(with_categories=True),
            "current_section": section,
            "extra_nav": extra_nav,
            "tracker_hosts": sorted(url_helpers.TRACKER_HOSTS),
            # P5: templates branch on capabilities to hide nav links + in-page
            # UI for features whose routes aren't registered (so url_for can't
            # BuildError on an unregistered endpoint). All-True in the private
            # repo -> every link/banner still shows -> UI unchanged.
            "capabilities": caps,
        }

    # Record which url rules are the ENGINE's, before any host attaches its own.
    # `nav`'s host/engine path-overlap warning needs the distinction, and the live
    # url map cannot provide it after the fact.
    nav._snapshot_engine_rules(app)
    return app
