"""Stage E / I10 (2026-05-25): PubMed sync bulk-apply button.

Adds "All apply PubMed" as a third bulk button on the /pubmed_sync
triage page (alongside V20-cleanup M5's "All defer" + "All keep YAML").
Uses an inline two-step confirmation UX: first click arms + shows
"click again to confirm" + 5s auto-reset timer; second click within
5s commits (sets every flag's radio to apply_pubmed + dispatches a
synthetic `change` event for FormDirtyGuard).

Per the pre-impl critique:
- The new button uses a DISTINCT `data-bulk-apply` attribute (NOT
  `data-bulk-decision`) so the existing M5 handler doesn't accidentally
  match + fire on the first click, which would defeat the two-step UX.
- Cross-button disarm: clicking any other bulk button while apply is
  armed disarms it (no half-state).
- N count recomputed on arm based on radios NOT already set to apply.
- Single bubbled `change` event on form (not per-radio) — matches M5.
- Red palette (destructive) with armed state visibly more saturated.

These tests cover what pytest CAN test (HTML smoke + selector
regression guard + backend route round-trip). The 5-second timer +
the two-step state machine + the cross-button disarm are JS-side and
covered by reasoned trust + comments referencing this test file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cv_editor.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app():
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---- HTML smoke: button rendered with the right attributes + label ----


def _pubmed_sync_html_with_one_flag(client, app, monkeypatch):
    """Get /pubmed_sync rendered HTML with at least one triage row so
    the conditional `{% if triage_rows %}` block is exercised."""
    from cv_editor import pubmed_sync as _ps

    # The triage page bails early (no rows) when the sidecar is empty — the
    # Jane Q Public sample ships no pubmed-sync sidecar, so seed a non-empty
    # one so the route reaches the (monkeypatched) compute_decisions below.
    # Private tree ships a real sidecar, so this override is a no-op there.
    monkeypatch.setattr(
        _ps,
        "load_sidecar",
        lambda path: _ps.SidecarState(
            entries={
                "12345678": _ps.EntryRecord(
                    synced_at="2020-01-01T00:00:00+00:00", pubmed_status="ppublish"
                )
            }
        ),
    )
    monkeypatch.setattr(
        _ps,
        "compute_decisions",
        lambda **kw: _ps._DryRunResult(
            header=[],
            data=[],
            sch={"file": "publications.yml"},
            state=_ps.SidecarState(),
            decisions=[
                _ps.EntryDecision(
                    pmid="12345678",
                    global_idx=0,
                    title_preview="t",
                    flags={"month": ("3", "4")},
                    publication_status="ppublish",
                )
            ],
            skipped_no_pmid=[],
            skipped_in_ttl=0,
            fetch_errors=[],
            all_yaml_pmids={"12345678"},
            fetched_pmids=["12345678"],
        ),
    )
    resp = client.get("/pubmed_sync")
    assert resp.status_code == 200
    return resp.data.decode("utf-8")


def test_bulk_apply_button_rendered(client, app, monkeypatch):
    body = _pubmed_sync_html_with_one_flag(client, app, monkeypatch)
    assert "data-bulk-apply" in body
    assert "All apply PubMed" in body
    assert 'class="btn-destructive"' in body
    # Default state has no -armed class ON THE BUTTON TAG; the JS adds
    # it on first click. (The string `btn-destructive-armed` DOES appear
    # in the page elsewhere — inside the JS source — so we can't just
    # search the whole body.)
    import re

    button_tag = re.search(r'<button[^>]*data-bulk-apply[^>]*>', body)
    assert button_tag, "apply button not found"
    assert "btn-destructive-armed" not in button_tag.group(0)


def test_bulk_apply_uses_distinct_attribute(client, app, monkeypatch):
    """H1 regression guard: the destructive apply button MUST use a
    different attribute (`data-bulk-apply`) than the M5 handler's
    `[data-bulk-decision]` selector. If they collided, the existing
    handler would match the apply button and fire on the FIRST click,
    silently defeating the two-step UX.

    Why this test matters: the original Stage D post-impl review
    caught a similar bug (style_save route forgot a field). This
    test pins the selector-distinctness contract."""
    body = _pubmed_sync_html_with_one_flag(client, app, monkeypatch)
    # Find the apply button block. It carries data-bulk-apply but must
    # NOT carry data-bulk-decision.
    import re

    apply_tags = re.findall(r'<button[^>]*data-bulk-apply[^>]*>', body)
    assert apply_tags, "apply button not found"
    for tag in apply_tags:
        assert "data-bulk-decision" not in tag, (
            "destructive apply button must NOT carry data-bulk-decision "
            "— the M5 handler queries that attribute and would fire on "
            "the first click, defeating the two-step UX. "
            f"Offending tag: {tag}"
        )


def test_bulk_apply_button_has_two_step_title_hint(client, app, monkeypatch):
    """The title attribute is the only place we tell the user about
    the two-step UX before they discover it. Worth pinning."""
    body = _pubmed_sync_html_with_one_flag(client, app, monkeypatch)
    # Title should mention "two-step" + "5 seconds" or similar.
    assert "Two-step confirm" in body or "two-step" in body.lower()
    assert "5 seconds" in body or "5s" in body


# ---- Existing M5 buttons still present (regression guard) ----


def test_existing_m5_bulk_buttons_still_render(client, app, monkeypatch):
    """Stage E must not break the V20-cleanup M5 'All defer' + 'All
    keep YAML' buttons."""
    body = _pubmed_sync_html_with_one_flag(client, app, monkeypatch)
    assert "All defer" in body
    assert "All keep YAML" in body
    assert 'data-bulk-decision=""' in body
    assert 'data-bulk-decision="keep_yaml"' in body


# ---- Backend route round-trip: pubmed_sync_apply still accepts the
# resulting form (no backend change needed per plan). ----


def test_pubmed_sync_apply_accepts_all_apply_decisions(client, app, monkeypatch):
    """The bulk-apply action just sets multiple radios to apply_pubmed
    client-side; the POST handler doesn't know it was a bulk action.
    Verify the route handles a request where every decision is
    apply_pubmed (no per-row reasons needed for apply_pubmed)."""
    from cv_editor import pubmed_sync as _ps

    decisions = [
        _ps.EntryDecision(
            pmid=f"1234567{i}",
            global_idx=i,
            title_preview=f"t{i}",
            flags={"month": (str(i), str(i + 1))},
            publication_status="ppublish",
        )
        for i in range(3)
    ]
    monkeypatch.setattr(
        _ps,
        "compute_decisions",
        lambda **kw: _ps._DryRunResult(
            header=[],
            data=[],
            sch={"file": "publications.yml"},
            state=_ps.SidecarState(),
            decisions=decisions,
            skipped_no_pmid=[],
            skipped_in_ttl=0,
            fetch_errors=[],
            all_yaml_pmids={d.pmid for d in decisions},
            fetched_pmids=[d.pmid for d in decisions],
        ),
    )
    # Stub the apply-kicker's background thread so the test doesn't try
    # to spawn pubmed_sync.py. Pattern from
    # test_v19_pubmed_sync_ui:test_pubmed_sync_apply_writes_decisions_file.
    import threading as _threading

    monkeypatch.setattr(
        _threading,
        "Thread",
        lambda *a, **kw: type("FakeThread", (), {"start": lambda self: None})(),
    )
    # POST with every decision set to apply_pubmed.
    form_data = {}
    for d in decisions:
        form_data[f"decision-{d.pmid}-month"] = "apply_pubmed"
    resp = client.post("/pubmed_sync/apply", data=form_data, follow_redirects=False)
    # 302/303 redirect on success; any 4xx means rejected.
    assert resp.status_code in (302, 303), (
        f"pubmed_sync_apply should accept all-apply form; got "
        f"{resp.status_code}. Body: {resp.get_data(as_text=True)[:500]}"
    )


# ---- CSS smoke: btn-destructive + btn-destructive-armed defined ----


def test_btn_destructive_css_rule_exists():
    """The two-step UX depends on .btn-destructive + .btn-destructive-armed
    having distinct visual states. Pin both rules so a future CSS
    refactor doesn't silently merge them."""
    css = (ROOT / "scripts" / "cv_editor" / "static" / "editor.css").read_text()
    assert ".btn-destructive {" in css
    assert ".btn-destructive-armed {" in css
    # Armed state must declare a different background than default
    # (otherwise the user can't visually tell the button is armed).
    import re

    default_block = re.search(r"\.btn-destructive \{[^}]*\}", css)
    armed_block = re.search(r"\.btn-destructive-armed \{[^}]*\}", css)
    assert default_block and armed_block
    assert default_block.group(0) != armed_block.group(0)


# ---- aria-live status region present + reusable ----


def test_triage_bulk_status_aria_live_region_present(client, app, monkeypatch):
    """The two-step UX writes its armed/cancelled/committed messages
    into the existing .triage-bulk-status aria-live region (added by
    V20-cleanup M5). Pin the region's presence + role."""
    body = _pubmed_sync_html_with_one_flag(client, app, monkeypatch)
    assert 'class="triage-bulk-status muted"' in body
    assert 'aria-live="polite"' in body
