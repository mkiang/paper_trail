"""Cross-cutting "shell" routes (extracted from app.py 2026-05-29, M2b — last).

The app's dashboard + utility routes: the index page (which aggregates
tracker / PubMed-sync / citations / QC banner counts — the most cross-
cutting read in the app), cross-section search, the rebuild triggers, the
health check, and the token-gated quit.

The SSE primitives (`_sse_frames` / `_sse_response`) STAY in create_app()
(shared with style + publications) and are handed to `rebuild_stream` via
deps.sse_response. All the banner helpers (tracker trio, cross-system
state) stay create_app() closures, passed BY REFERENCE.

Routes (endpoint names unchanged — register-on-app, gotcha #69):
  GET  /                  index
  GET  /search            search
  POST /rebuild/stream    rebuild_stream  (SSE)
  POST /rebuild           rebuild
  GET  /healthz           healthz
  POST /quit              quit_app

Behaviour-identical (M2 fingerprint guard).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from cv_editor import (
    build_runner,
    bulk_replace,
    data_check,
    paths,
    scaffold,
    schemas,
    sections,
    validate,
    yaml_io,
    yaml_to_bibtex,
)
from cv_editor.yaml_io import CorruptedShapeError, StaleFileError

# ----- "Commit pending edits" support (A6) -----------------------------------
#
# The commit button stages the files the editor MANAGES and makes one local git
# commit in the workspace repo. The add-set is a POSITIVE allowlist: the safe
# failure mode is UNDER-inclusion (a real sidecar left unstaged would keep the
# tree dirty), so every editor-written data file lives here. QC/URL reports
# (qc/report.*, qc/urls_report.md, qc/pubmed_sync_report.md,
# qc/citations_report.md) are regenerated churn and are deliberately EXCLUDED —
# they are never staged. data/*.yml is globbed at run time (top-level only, so
# the data/example/ corpus — an engine asset, not user data — stays out).
_COMMIT_STATIC_PATHS = (
    "data/citation_counts.json",
    "data/publications_pubmed_sync.json",
    "qc/qc_decisions.json",
    "qc/pubmed_sync_decisions.gen.yml",
    "qc/pubmed_sync_decisions.template.yml",
    "publications.bib",
)


def _git(root: Path, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _commit_add_set(root: Path) -> list[str]:
    """Repo-relative paths the editor manages, restricted to those that EXIST
    (``git add`` errors on a missing path). Never ``git add -A``."""
    rels: list[str] = []
    data_dir = root / "data"
    if data_dir.is_dir():
        rels += sorted(f"data/{p.name}" for p in data_dir.glob("*.yml"))
    for rel in _COMMIT_STATIC_PATHS:
        if (root / rel).exists():
            rels.append(rel)
    return rels


def _git_worktree_info(root: Path) -> dict:
    """Cheap read for the index button: is ``root`` inside a git worktree, how
    many editor-managed files are dirty, the current branch (None if detached).
    Every failure degrades to a non-repo result so the button simply hides."""
    blank = {"is_repo": False, "pending": 0, "branch": None, "detached": False}
    try:
        r = _git(root, "rev-parse", "--is-inside-work-tree")
        if r.returncode != 0 or r.stdout.strip() != "true":
            return blank
        info = {"is_repo": True, "pending": 0, "branch": None, "detached": False}
        add_set = _commit_add_set(root)
        if add_set:
            st = _git(root, "status", "--porcelain", "--", *add_set)
            info["pending"] = sum(1 for line in st.stdout.splitlines() if line.strip())
        b = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if b.returncode == 0:
            info["branch"] = b.stdout.strip()
        else:
            info["detached"] = True
        return info
    except (OSError, subprocess.SubprocessError):
        return blank


@dataclass
class CoreDeps:
    """DI surface for the shell routes. The tracker trio + cross-system
    state helpers + the SSE responder are create_app() closures shared by
    reference (index reads them for its banners; rebuild_stream needs the
    one SSE responder used app-wide)."""

    root: Path
    cfg_path: Callable[[str], Path]
    load_section: Callable
    logger: object
    count_unresolved_trackers: Callable[[], dict]
    tracker_cache: Callable
    pubmed_sync_state_cached: Callable
    qc_state_for_cross_check: Callable
    qc_decisions_path: Callable
    sse_response: Callable


def register_core_routes(app: Flask, deps: CoreDeps) -> None:
    ROOT = deps.root
    _cfg_path = deps.cfg_path
    _load_section = deps.load_section
    _count_unresolved_trackers = deps.count_unresolved_trackers
    _tracker_cache = deps.tracker_cache
    _pubmed_sync_state_cached = deps.pubmed_sync_state_cached
    _qc_state_for_cross_check = deps.qc_state_for_cross_check
    _qc_decisions_path = deps.qc_decisions_path
    _sse_response = deps.sse_response

    def _data_check_counts():
        """(error_count, warning_count) for the index banner, MEMOIZED on
        app.config keyed by the set of data-file mtimes. The index is the hottest
        page and check_data re-parses the ~400KB publications.yml, so recompute
        ONLY when a data file changes (gotcha #40 mtime-keyed-cache idiom). Bypassed
        under TESTING so a monkeypatched data_check.check_data is always honored."""

        def _compute():
            s = data_check.summarize(data_check.check_data())
            return (s.get(data_check.ERROR, 0), s.get(data_check.WARNING, 0))

        if app.config.get("TESTING"):
            return _compute()
        try:
            key = tuple(
                (ROOT / schemas.get(k)["file"]).stat().st_mtime_ns
                if (ROOT / schemas.get(k)["file"]).exists()
                else 0
                for k in schemas.all_sections()
            )
        except Exception:
            return _compute()
        cached = app.config.get("_DATA_CHECK_CACHE")
        if cached is not None and cached[0] == key:
            return cached[1]
        result = _compute()
        app.config["_DATA_CHECK_CACHE"] = (key, result)
        return result

    @app.route("/")
    def index():
        section_cards = []
        for key in schemas.all_sections():
            sch = schemas.get(key)
            count = "?"
            try:
                _, _, _, data = _load_section(key)
                if sch["structure"] == "single_record":
                    count = 1 if data is not None else 0
                else:
                    count = sum(1 for _ in sections.flatten(data, sch["structure"]))
            except Exception:
                pass
            section_cards.append(
                {
                    "key": key,
                    "label": sch["label"],
                    "count": count,
                    "list_url": (
                        url_for("meta_view")
                        if key == "meta"
                        else url_for("section_list", section=key)
                    ),
                    # V15: quick-add `+` button on the index card. Meta is a
                    # single_record (no "new entry" concept) so it gets no
                    # quick-add URL.
                    "new_url": (None if key == "meta" else url_for("entry_new", section=key)),
                }
            )

        # M5-5d CP8: first-run onboarding card. The card counts above are a
        # cheap pre-filter (a populated corpus — the normal case — skips the
        # authoritative call entirely); scaffold.corpus_is_empty is the
        # single source of truth and is FAIL-CLOSED (a "?" count means a
        # load failure, which the predicate treats as NOT empty — never
        # invite a reset over a corpus that merely failed to parse).
        cards_all_zero = all(c["count"] == 0 for c in section_cards if c["key"] != "meta")
        corpus_empty = bool(cards_all_zero and scaffold.corpus_is_empty())

        # The default variant's PDF is what the quick Rebuild produces;
        # gauge staleness against it. Name resolved live (M5-5d) to the
        # corpus's FIRST build variant — so the gauge keeps working after a
        # reset-to-blank/example.
        default_variant = build_runner.default_variant_name()
        cv_pdf = ROOT / "output" / f"{default_variant}.pdf"
        rebuild_needed = False
        if cv_pdf.exists():
            pdf_mtime = cv_pdf.stat().st_mtime
            for sch_key in schemas.all_sections():
                p = ROOT / schemas.get(sch_key)["file"]
                if p.exists() and p.stat().st_mtime > pdf_mtime:
                    rebuild_needed = True
                    break
        tracker_counts = _count_unresolved_trackers()
        # V20 post-impl (2026-05-18): stale-resolved discoverability —
        # surface the count on the index too so users with 0 unresolved
        # but resolved-URLs past TTL can find the Trackers page.
        try:
            stale_resolved_count = sum(1 for _ in _tracker_cache().stale_resolved())
        except Exception:
            stale_resolved_count = 0
        # V13-V19-D R3-M6 (2026-05-18): surface PubMed-sync + citations
        # queues on the index too. Discovery via Tools menu was easy to
        # miss across sessions. Both helpers tolerate cold state (empty
        # sidecar / missing snapshot) and return zeros rather than
        # raising — the macros render nothing when count == 0.
        # V13-V19-D tail R1-H1 fix (2026-05-18): load publications.yml
        # once and reuse for both banners. The PubMed banner MUST go
        # through `effective_flagged_fields` (the same predicate the
        # triage page uses, per gotcha #35 (d)) so banner truth matches
        # triage page truth. The earlier "cheap approximation" missed
        # the re-surfaced case (override snapshot diverged from current
        # YAML → triage page surfaces it, banner silenced it).
        try:
            _, _, _, pub_data = _load_section("publications")
        except Exception as exc:
            deps.logger.warning("index: publications load failed: %s", exc)
            pub_data = None
        pubmed_flagged_count = 0
        pubmed_cross_silenced_count = 0
        if pub_data is not None:
            try:
                from cv_editor.decision_cross_check import build_qc_decisions_index
                from cv_editor.pubmed_sync import effective_flagged_fields

                state = _pubmed_sync_state_cached()
                if state is not None:
                    # V23-B Phase 1.5 (2026-05-26): cross-system silencing
                    # via QC keep_yaml decisions on the 5-field overlap.
                    qc_sc, qc_dec = _qc_state_for_cross_check()
                    qc_idx = build_qc_decisions_index(qc_sc, qc_dec) if qc_dec else {}
                    pubs_by_pmid: dict[str, dict] = {}
                    for sub in pub_data:
                        for e in sub.get("entries") or []:
                            pmid_s = str(e.get("pmid") or "").strip()
                            if pmid_s:
                                pubs_by_pmid[pmid_s] = e
                    for pmid, rec in state.entries.items():
                        entry = pubs_by_pmid.get(pmid)
                        if entry is None:
                            # Orphan PMID in sidecar; can't evaluate
                            # override yaml_value without the entry.
                            # Count fields with no override (matches old
                            # behavior for this case).
                            overrides = state.accepted_yaml_overrides.get(pmid, {})
                            pubmed_flagged_count += sum(
                                1 for f in (rec.fields_flagged or []) if f not in overrides
                            )
                            continue
                        active = effective_flagged_fields(
                            entry,
                            rec,
                            state.accepted_yaml_overrides.get(pmid, {}),
                            pmid=pmid,
                            qc_decisions_index=qc_idx,
                        )
                        pubmed_flagged_count += len(active)
                        # Count cross-silenced separately for the
                        # banner sub-line.
                        if qc_idx:
                            from cv_editor.pubmed_sync import cross_silenced_flagged_fields

                            cs = cross_silenced_flagged_fields(
                                entry,
                                rec,
                                state.accepted_yaml_overrides.get(pmid, {}),
                                qc_idx,
                                pmid=pmid,
                            )
                            pubmed_cross_silenced_count += len(cs)
            except Exception as exc:
                deps.logger.warning("index: pubmed sidecar read failed: %s", exc)
        citations_never_attempted = 0
        if pub_data is not None:
            try:
                from cv_editor.citation_counts import CitationCache

                cache = CitationCache.load(_cfg_path("CITATION_CACHE_PATH"))
                seen = set(cache.all().keys())
                for sub in pub_data:
                    for e in sub.get("entries") or []:
                        doi = (e.get("doi") or "").strip().lower()
                        if doi and doi not in seen:
                            citations_never_attempted += 1
            except Exception as exc:
                deps.logger.warning("index: citation cache read failed: %s", exc)
        # V23-B Phase 3 + Phase 1 (2026-05-25): QC findings banner.
        # Phase 1 upgrade: banner truth = triage page truth (V13-V19-D
        # R2-H1 invariant). Must subtract decisions sidecar from raw
        # totals via effective_findings, not just read summary.total_findings.
        # All three surfaces (index banner, entry_view banner, /qc/triage
        # page) share the same effective_findings call.
        try:
            from cv_editor import qc_decisions, qc_sync
            from cv_editor.qc_publications import SIDECAR_PATH as _QC_SIDECAR_PATH

            _qc_sidecar = qc_sync.load_sidecar(_QC_SIDECAR_PATH, silent=True)
            _qc_decisions = qc_decisions.load(_qc_decisions_path(), silent=True)
            _qc_by_idx = {}
            if pub_data is not None:
                for _s, _sub in enumerate(pub_data):
                    for _e, _entry in enumerate(_sub.get("entries") or []):
                        _qc_by_idx[_s * 10000 + _e] = _entry
            _qc_eff = qc_sync.effective_findings(
                _qc_sidecar,
                _qc_decisions,
                current_yaml_by_global_idx=_qc_by_idx,
                pubmed_sync_state=_pubmed_sync_state_cached(),
            )
            # Banner sums only Phase 1 + Phase 3 active types (the ones
            # the user can act on today). Phase 2 (renames) + Phase 4
            # (title search) are deferred — surfacing them in the banner
            # would invite clicks on a page that can't act on them yet.
            qc_findings_count = (
                len(_qc_eff.get("mismatches") or [])
                + len(_qc_eff.get("variants") or [])
                + len(_qc_eff.get("id_enrichments") or [])
                + len(_qc_eff.get("pmid_mismatches") or [])
                + len(_qc_eff.get("self_absent") or [])
            )
            qc_cross_silenced_count = qc_sync.cross_silenced_total(_qc_eff)
        except Exception as exc:
            deps.logger.warning("index: qc sidecar read failed: %s", exc)
            qc_findings_count = 0
            qc_cross_silenced_count = 0
        # M5 5c-i: whole-corpus validation banner (offline, fast — recompute,
        # no sidecar). Errors break the build; warnings render but are likely
        # wrong. Both 0 -> the banner macro renders nothing.
        try:
            data_error_count, data_warning_count = _data_check_counts()
        except Exception as exc:
            deps.logger.warning("index: data check failed: %s", exc)
            data_error_count = 0
            data_warning_count = 0
        # Date-conditional feature: count entries currently hidden (future
        # start) or rendering open-ended (future end) across the date-gated
        # sections, so a deferred item entered "whenever convenient" can't be
        # silently forgotten. Cheap — these seven files are all tiny.
        future_pending_count = 0
        for key in validate.DATE_GATED_SECTIONS:
            try:
                _, _, _, fdata = _load_section(key)
                fsch = schemas.get(key)
                for rec in sections.flatten(fdata, fsch["structure"]):
                    if validate.date_conditional_status(rec["entry"].get("date") or ""):
                        future_pending_count += 1
            except Exception as exc:
                deps.logger.warning("index: future-date scan failed for %s: %s", key, exc)
        git_status = _git_worktree_info(paths.data_root())
        return render_template(
            "index.html",
            sections=section_cards,
            git_status=git_status,
            future_pending_count=future_pending_count,
            rebuild_needed=rebuild_needed,
            cv_pdf_exists=cv_pdf.exists(),
            default_variant=default_variant,
            corpus_empty=corpus_empty,
            tracker_counts=tracker_counts,
            stale_resolved_count=stale_resolved_count,
            pubmed_flagged_count=pubmed_flagged_count,
            pubmed_cross_silenced_count=pubmed_cross_silenced_count,
            citations_never_attempted=citations_never_attempted,
            qc_findings_count=qc_findings_count,
            qc_cross_silenced_count=qc_cross_silenced_count,
            data_error_count=data_error_count,
            data_warning_count=data_warning_count,
        )

    @app.route("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        results = []
        if q:
            ql = q.lower()
            for key in schemas.all_sections():
                if key == "meta":
                    continue
                sch = schemas.get(key)
                try:
                    _, _, _, data = _load_section(key)
                except Exception:
                    continue
                for rec in sections.flatten(data, sch["structure"]):
                    snippet = _search_match(rec["entry"], ql)
                    if snippet:
                        results.append(
                            {
                                "section_key": key,
                                "section_label": sch["label"],
                                "ctx": rec["ctx"],
                                "snippet": snippet,
                                "view_url": url_for(
                                    "entry_view", section=key, idx=rec["global_idx"]
                                ),
                            }
                        )
        return render_template("search.html", q=q, results=results)

    def _search_match(entry, ql: str) -> str:
        """Return a context snippet if entry matches `ql`; else empty string.
        Walks scalar fields and string-list fields recursively."""
        for v in _walk_scalars(entry):
            if ql in v.lower():
                # Truncate snippet around the match.
                i = v.lower().find(ql)
                start = max(0, i - 30)
                end = min(len(v), i + len(ql) + 30)
                pre = "…" if start > 0 else ""
                post = "…" if end < len(v) else ""
                return f"{pre}{v[start:end]}{post}"
        return ""

    def _walk_scalars(node):
        if node is None:
            return
        if isinstance(node, dict):
            for v in node.values():
                yield from _walk_scalars(v)
        elif isinstance(node, list):
            for v in node:
                yield from _walk_scalars(v)
        elif isinstance(node, bool):
            return
        else:
            yield str(node)

    @app.route("/validate")
    def validate_data():
        """M5 5c-i: whole-corpus validation report. Read-only, offline. Lists
        located issues (errors first) with a jump-to-edit link per entry."""
        issues = data_check.check_data()
        rows = []
        for i in issues:
            if i.section == "meta":
                edit_url = url_for("meta_view")
            elif i.global_idx is not None:
                edit_url = url_for("entry_view", section=i.section, idx=i.global_idx)
            else:
                edit_url = None
            rows.append({"issue": i, "edit_url": edit_url})
        rows.sort(
            key=lambda r: (
                r["issue"].severity != data_check.ERROR,
                r["issue"].file,
                r["issue"].line or 0,
            )
        )
        counts = data_check.summarize(issues)
        return render_template(
            "validate.html",
            rows=rows,
            error_count=counts.get(data_check.ERROR, 0),
            warning_count=counts.get(data_check.WARNING, 0),
            current_section="validate",
        )

    @app.route("/replace", methods=["GET", "POST"])
    def search_replace():
        """M5 5c-ii: global literal search/replace across text fields of every
        section. ask -> preview -> apply. v1 = text/textarea fields only (the
        bulk_replace allow-list hard-refuses IDs/dates/numbers). The apply does an
        all-or-nothing per-file mtime PREFLIGHT (write nothing if any file drifted
        since preview), then best-effort writes each touched file via
        write_with_backup, rendering a manifest (wrote / failed / not-attempted)."""
        if request.method == "GET":
            return render_template(
                "replace.html",
                stage="ask",
                needle="",
                replacement="",
                case_sensitive=False,
                current_section="replace",
            )

        action = request.form.get("action", "preview")
        needle = request.form.get("needle", "")
        replacement = request.form.get("replacement", "")
        case_sensitive = bool(request.form.get("case_sensitive"))
        if not needle:
            flash("Enter text to find.", "warn")
            return redirect(url_for("search_replace"))

        if action == "preview":
            all_hits, section_mtimes, breakdown = [], {}, {}
            for key in bulk_replace.searchable_sections():
                try:
                    _sch, path, _header, data = _load_section(key)
                except Exception:
                    continue
                section_mtimes[key] = yaml_io.mtime_ns(path)
                hits = bulk_replace.collect_in_section(
                    data, key, needle, replacement, case_sensitive
                )
                if hits:
                    breakdown[key] = (schemas.get(key)["label"], len(hits))
                    all_hits.extend(hits)
            if not all_hits:
                flash(f"No matches for {needle!r} in editable text fields.", "info")
                return redirect(url_for("search_replace"))
            return render_template(
                "replace.html",
                stage="preview",
                needle=needle,
                replacement=replacement,
                case_sensitive=case_sensitive,
                hits=all_hits,
                breakdown=breakdown,
                total_count=sum(h.count for h in all_hits),
                section_mtimes=section_mtimes,
                current_section="replace",
            )

        # action == apply
        selected = set(request.form.getlist("hit"))  # hit.key strings
        if not selected:
            flash("No replacements selected — nothing to do.", "warn")
            return redirect(url_for("search_replace"))
        per_section: dict[str, set] = {}
        for k in selected:
            try:
                sec, gidx, field = k.split("|", 2)
                per_section.setdefault(sec, set()).add((int(gidx), field))
            except ValueError:
                continue
        touched = sorted(per_section)

        # Pass A — load all touched files + re-check each carried mtime. ANY drift
        # aborts the whole apply with NOTHING written (avoids a partial write in
        # the common stale-tab case; write_with_backup re-checks under its lock too).
        loaded = {}
        for sec in touched:
            carried = request.form.get(f"mtime_{sec}")
            try:
                _sch, path, header, data = _load_section(sec)
            except Exception as exc:
                flash(f"Could not load {sec}: {exc}", "warn")
                return redirect(url_for("search_replace"))
            if carried is None or str(yaml_io.mtime_ns(path)) != str(carried):
                flash(
                    f"{schemas.get(sec)['label']} changed since preview — "
                    "nothing was written. Re-run the search.",
                    "warn",
                )
                return redirect(url_for("search_replace"))
            loaded[sec] = (path, header, data, int(carried))

        # Pass B — best-effort writes with a manifest.
        wrote, failed, not_attempted = [], None, []
        for i, sec in enumerate(touched):
            path, header, data, carried = loaded[sec]
            n = bulk_replace.apply_in_section(
                data, sec, per_section[sec], needle, replacement, case_sensitive
            )
            if n == 0:
                continue
            try:
                backup = yaml_io.write_with_backup(path, header, data, expected_mtime_ns=carried)
            except (StaleFileError, CorruptedShapeError, Timeout) as exc:
                failed = {"section": sec, "label": schemas.get(sec)["label"], "reason": str(exc)}
                not_attempted = list(touched[i + 1 :])
                break
            except Exception as exc:  # any other write failure -> honest manifest
                deps.logger.warning("search_replace: write failed for %s: %s", sec, exc)
                failed = {"section": sec, "label": schemas.get(sec)["label"], "reason": str(exc)}
                not_attempted = list(touched[i + 1 :])
                break
            wrote.append(
                {
                    "section": sec,
                    "label": schemas.get(sec)["label"],
                    "count": n,
                    "backup": backup.name,
                }
            )
        return render_template(
            "replace.html",
            stage="manifest",
            needle=needle,
            replacement=replacement,
            wrote=wrote,
            failed=failed,
            not_attempted=not_attempted,
            not_attempted_labels=[schemas.get(s)["label"] for s in not_attempted],
            current_section="replace",
        )

    def _latest_reset_manifest():
        """Best-effort read of the newest reset snapshot's manifest — used by
        the failure page so a mid-run crash still shows what happened (the
        manifest is rewritten after every phase)."""
        import json as _json

        try:
            snaps = sorted(yaml_io.BACKUP_DIR.glob("reset-*"), key=lambda p: p.name)
            if snaps:
                m = _json.loads((snaps[-1] / "manifest.json").read_text(encoding="utf-8"))
                # A crash mid-sections-phase leaves the on-disk manifest
                # without snapshot_dir (reset() adds it on the next phase
                # write) — we know the dir we just read from.
                m.setdefault("snapshot_dir", str(snaps[-1]))
                return m
        except Exception:
            pass
        return None

    @app.route("/reset", methods=["GET", "POST"])
    def reset_cv():
        """M5-5d: guarded whole-corpus reset (blank scaffold / example corpus).

        POST recomputes corpus emptiness SERVER-SIDE (never trusts the form)
        and requires the mode-matched typed phrase whenever there is anything
        to lose (a corrupt corpus counts as something to lose — the predicate
        is fail-closed). scaffold.reset snapshots the whole tree first.
        Success renders a manifest page (the /replace precedent — flashes
        escape HTML so they can't carry the Backups links); phrase mismatch
        re-renders the form directly with 400 (gotcha #46: browsers don't
        follow 4xx redirects). scaffold is called via module attribute so
        route tests can monkeypatch it."""

        def _confirm(mode, status=200):
            return render_template(
                "reset.html",
                stage="confirm",
                mode=mode,
                corpus_empty=scaffold.corpus_is_empty(),
                current_section="reset",
            ), status

        if request.method == "GET":
            mode = request.args.get("mode", "example")
            if mode not in scaffold.MODES:
                mode = "example"
            return _confirm(mode)

        mode = (request.form.get("mode") or "").strip()
        if mode not in scaffold.MODES:
            flash("Unknown reset mode — nothing was changed.", "warn")
            return _confirm("example", 400)

        if not scaffold.corpus_is_empty():
            expected = f"reset to {mode}"
            phrase = (request.form.get("confirm_phrase") or "").strip().lower()
            if phrase != expected:
                flash(
                    f'Confirmation phrase mismatch — type exactly "{expected}" '
                    "to proceed. Nothing was changed.",
                    "warn",
                )
                return _confirm(mode, 400)

        try:
            manifest = scaffold.reset(mode)
        except Exception as exc:
            deps.logger.exception("reset (%s) failed mid-run", mode)
            return render_template(
                "reset.html",
                stage="failed",
                mode=mode,
                error=str(exc),
                manifest=_latest_reset_manifest(),
                current_section="reset",
            ), 500

        written = manifest.get("phases", {}).get("sections", {})
        section_keys = set(schemas.all_sections())
        backup_links = [
            (schemas.get(f[:-4])["label"], url_for("entry_backups", section=f[:-4]))
            for f in sorted(written)
            if f.endswith(".yml") and f[:-4] in section_keys
        ]
        # A host corpus file has no schema entry and so no Backups URL, but it
        # WAS rewritten — listing only the schema sections would under-report
        # the reset on the page that exists to say what it did.
        other_written = [
            f for f in sorted(written) if f.endswith(".yml") and f[:-4] not in section_keys
        ]
        return render_template(
            "reset.html",
            stage="manifest",
            mode=mode,
            manifest=manifest,
            backup_links=backup_links,
            other_written=other_written,
            current_section="reset",
        )

    @app.route("/rebuild/stream", methods=["POST"])
    def rebuild_stream():
        """Live-tail rebuild via Server-Sent Events. Yields one SSE event per
        build-output line plus a terminal `done` or `error` event."""
        mode = request.form.get("mode", "cv_only")
        return _sse_response(build_runner.stream_rebuild(mode))

    @app.route("/rebuild", methods=["POST"])
    def rebuild():
        mode = request.form.get("mode", "cv_only")
        try:
            if mode == "full":
                result = build_runner.rebuild_full()
            else:
                result = build_runner.rebuild_cv_only()
        except Timeout:
            flash("Another build is already running; wait for it to finish.", "warn")
            return redirect(url_for("index")), 409

        if result.ok:
            flash(f"Build OK ({result.cmd}, {result.duration_s:.1f}s).", "ok")
        else:
            tail = result.stderr_tail or result.stdout_tail or "(no output)"
            flash(f"Build FAILED ({result.cmd}, exit {result.returncode}).\n{tail}", "warn")
        return redirect(url_for("index"))

    @app.route("/commit", methods=["POST"])
    def commit_pending():
        """Stage the editor-managed data files + a freshly-regenerated
        publications.bib and make ONE local git commit in the workspace repo.

        LOCAL only — never pushes (the private repo syncs its .git via Dropbox;
        an installed-wheel consumer pushes on their own schedule). Every git
        failure flashes and redirects rather than 500ing, covering: not a repo,
        detached HEAD (a commit there is orphaned — refused), a running build
        (lock busy), nothing staged, and an unset git identity."""
        root = paths.data_root()
        info = _git_worktree_info(root)
        if not info["is_repo"]:
            flash("Commit unavailable — the workspace is not a git repository.", "warn")
            return redirect(url_for("index"))
        if info["detached"]:
            flash(
                "Refusing to commit on a detached HEAD — a commit here would not be "
                "on any branch and is easily lost. Check out a branch first.",
                "warn",
            )
            return redirect(url_for("index"))

        # Regenerate publications.bib in-process, cooperating with a running
        # build via the shared lock (the generator's write is non-atomic). The
        # lockfile's parent (output/) may not exist in a fresh workspace —
        # create it first (mirrors build_lock_check.py) so acquire() can only
        # ever raise Timeout, never FileNotFoundError.
        try:
            paths.output_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        lock = FileLock(str(build_runner.LOCK), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            flash("A build is running; wait for it to finish, then commit.", "warn")
            return redirect(url_for("index")), 409
        try:
            yaml_to_bibtex.main()
        except Exception as exc:
            deps.logger.exception("commit: publications.bib regen failed")
            flash(f"Commit aborted — could not regenerate publications.bib: {exc}", "warn")
            return redirect(url_for("index"))
        finally:
            lock.release()

        add_set = _commit_add_set(root)
        if not add_set:
            flash("Nothing to commit — no editor-managed files found in the workspace.", "warn")
            return redirect(url_for("index"))
        try:
            add = _git(root, "add", "--", *add_set, timeout=30)
            if add.returncode != 0:
                flash(f"Commit failed at `git add`: {(add.stderr or add.stdout).strip()}", "warn")
                return redirect(url_for("index"))
        except (OSError, subprocess.SubprocessError) as exc:
            flash(f"Commit failed — could not run git add: {exc}", "warn")
            return redirect(url_for("index"))

        # Nothing staged (everything already committed) -> `git commit` would
        # exit non-zero; report cleanly instead of surfacing a scary error.
        if _git(root, "diff", "--cached", "--quiet").returncode == 0:
            flash("Nothing to commit — your editor changes are already committed.", "ok")
            return redirect(url_for("index"))

        message = (request.form.get("message") or "").strip() or "Update CV data via editor"
        c = _git(root, "commit", "-m", message, timeout=30)
        if c.returncode != 0:
            detail = (c.stderr or c.stdout).strip() or f"git exited {c.returncode}"
            deps.logger.warning("commit: git commit failed: %s", detail)
            flash(f"Commit failed: {detail}", "warn")
            return redirect(url_for("index"))

        head = _git(root, "rev-parse", "--short", "HEAD")
        short = head.stdout.strip() if head.returncode == 0 else "?"
        n = len(add_set)
        flash(
            f"Committed {short} on {info['branch']} — staged {n} editor "
            f"file{'' if n == 1 else 's'}. Not pushed (sync/push separately).",
            "ok",
        )
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz():
        return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

    @app.route("/quit", methods=["POST"])
    def quit_app():
        """Clean shutdown. Local-only; the launcher binds to 127.0.0.1.

        Token-gated: the launcher mints `CV_EDITOR_QUIT_TOKEN` and the
        template embeds it as a hidden input. A stray `curl localhost/quit`
        from another shell doesn't know the token and gets 403. Tests
        run without a launcher; QUIT_TOKEN is empty there and the gate
        falls through.
        """
        expected = app.config.get("QUIT_TOKEN") or ""
        if expected:
            import secrets as _secrets_cmp

            provided = (request.form.get("quit_token") or "").strip()
            # Constant-time compare (M1) so the token can't be timing-probed.
            if not _secrets_cmp.compare_digest(provided, expected):
                abort(403)
        import os
        import signal
        import threading

        def _shutdown():
            import time

            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_shutdown, daemon=True).start()
        return "Goodbye. You can close this tab.", 200
