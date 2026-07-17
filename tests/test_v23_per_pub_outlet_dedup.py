"""V23-A (2026-05-25): per-publication outlet URL dedup.

Stage C / I7 added save-time per-note URL dedup. V23-A extends the scope:
within a single publication, the same URL appearing in two different
media notes is also deduped — first occurrence wins, later duplicate is
dropped. If a media note loses every outlet to cross-note dedup, the
empty note is dropped entirely (matches the existing empty-note filter
in notes_form_to_yaml).

Cross-publication dedup is OUT OF SCOPE: two papers legitimately covered
in the same press piece is normal.

User direction (2026-05-25): 'In publications, I want an option to
deduplicate URLs of the media per publication so if two different
publications are cited in the same media, it should appear in each but
if one publication has two links to the same URL, it should be
de-duplicated.'
"""

from __future__ import annotations

from cv_editor import notes_helpers


def test_same_url_in_two_notes_keeps_only_first():
    """Cross-note dedup: same URL in note 1 and note 2 → kept only in
    note 1; note 2 still exists if it has OTHER outlets."""
    notes_form = [
        {
            "type": "media",
            "outlets": [
                {"name": "CNN", "url": "https://cnn.com/x"},
                {"name": "BBC", "url": "https://bbc.com/y"},
            ],
        },
        {
            "type": "media",
            "outlets": [
                {"name": "CNN-Other", "url": "https://cnn.com/x"},
                {"name": "Reuters", "url": "https://reuters.com/z"},
            ],
        },
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 2
    n1_names = [o.get("name") if isinstance(o, dict) else o for o in out[0].get("outlets") or []]
    n2_names = [o.get("name") if isinstance(o, dict) else o for o in out[1].get("outlets") or []]
    assert n1_names == ["CNN", "BBC"]
    assert n2_names == ["Reuters"]  # CNN-Other dropped (cnn.com/x already seen in note 1)


def test_same_url_in_three_notes_keeps_first():
    """A URL appearing in 3 different media notes is kept in note 1 only."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://cnn.com/x"}]},
        {
            "type": "media",
            "outlets": [
                {"name": "BBC", "url": "https://bbc.com/y"},
                {"name": "CNN-Dup-2", "url": "https://cnn.com/x"},
            ],
        },
        {
            "type": "media",
            "outlets": [
                {"name": "Reuters", "url": "https://reuters.com/z"},
                {"name": "CNN-Dup-3", "url": "https://cnn.com/x"},
            ],
        },
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 3
    assert [o.get("name") for o in out[0].get("outlets")] == ["CNN"]
    assert [o.get("name") for o in out[1].get("outlets")] == ["BBC"]
    assert [o.get("name") for o in out[2].get("outlets")] == ["Reuters"]


def test_empty_note_after_cross_note_dedup_is_dropped():
    """If a media note's only outlet is a cross-note duplicate, the whole
    note is dropped from the result (matches the existing empty-note
    filter behavior)."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://cnn.com/x"}]},
        {"type": "media", "outlets": [{"name": "Dup", "url": "https://cnn.com/x"}]},
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 1  # second note had no surviving outlets
    assert [o.get("name") if isinstance(o, dict) else o for o in out[0].get("outlets")] == ["CNN"]


def test_url_less_outlets_preserved_across_all_notes():
    """Outlets without URLs (name-only) are NEVER deduped across notes —
    they aren't duplicates of anything. The string-shape outlet (the
    common case for URL-less rows from form_outlet_to_yaml) survives."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "CNN"}]},
        {"type": "media", "outlets": [{"name": "CNN"}]},  # same name, no URL
        {"type": "media", "outlets": [{"name": "BBC"}]},
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 3
    names = [
        (o.get("name") if isinstance(o, dict) else o)
        for note in out
        for o in note.get("outlets") or []
    ]
    assert names == ["CNN", "CNN", "BBC"]


def test_url_normalization_matches_per_note_rules():
    """Cross-note dedup uses the SAME normalization as per-note dedup
    (case-insensitive, trailing-slash-ignoring; query strings preserved).
    Reuses _outlet_url_key so the two layers stay in lockstep."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://CNN.com/x/"}]},
        {
            "type": "media",
            "outlets": [
                {"name": "Dup-case-slash", "url": "https://cnn.com/x"},
                {"name": "Reuters", "url": "https://reuters.com/z"},
            ],
        },
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 2
    n2_names = [o.get("name") for o in out[1].get("outlets")]
    assert n2_names == ["Reuters"]


def test_different_query_strings_preserved_across_notes():
    """Query strings are part of the dedup key (per Stage C / I7 — same
    rule applies across notes). Different `?utm=...` URLs survive."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "Source", "url": "https://x.com/a?utm=fb"}]},
        {"type": "media", "outlets": [{"name": "Source-Other", "url": "https://x.com/a?utm=tw"}]},
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 2
    assert (out[0].get("outlets")[0]).get("url") == "https://x.com/a?utm=fb"
    assert (out[1].get("outlets")[0]).get("url") == "https://x.com/a?utm=tw"


def test_non_media_notes_unaffected_by_cross_note_dedup():
    """Cross-note dedup ONLY applies to media notes. Commentary/letter/
    etc. are pass-through."""
    notes_form = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://cnn.com/x"}]},
        {"type": "commentary", "citation": "Author A et al. 2026. Some commentary."},
        {
            "type": "media",
            "outlets": [
                {"name": "Dup", "url": "https://cnn.com/x"},
                {"name": "BBC", "url": "https://bbc.com/y"},
            ],
        },
        {"type": "letter", "citation": "Author B et al. 2026. Some letter."},
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 4
    assert out[0]["type"] == "media"
    assert [o.get("name") for o in out[0]["outlets"]] == ["CNN"]
    assert out[1]["type"] == "commentary"
    assert "Author A" in out[1]["citation"]
    assert out[2]["type"] == "media"
    assert [o.get("name") for o in out[2]["outlets"]] == ["BBC"]
    assert out[3]["type"] == "letter"
    assert "Author B" in out[3]["citation"]


def test_cross_publication_isolation_via_separate_calls():
    """The seen_urls set is local to a single notes_form_to_yaml call
    (one publication). Two separate calls with the same URL both see
    that URL fresh — cross-publication dedup MUST NOT happen."""
    notes_form_pub1 = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://cnn.com/x"}]},
    ]
    notes_form_pub2 = [
        {"type": "media", "outlets": [{"name": "CNN", "url": "https://cnn.com/x"}]},
    ]
    out1 = notes_helpers.notes_form_to_yaml(notes_form_pub1)
    out2 = notes_helpers.notes_form_to_yaml(notes_form_pub2)
    # Both publications keep their CNN outlet.
    assert len(out1) == 1 and len(out1[0].get("outlets") or []) == 1
    assert len(out2) == 1 and len(out2[0].get("outlets") or []) == 1


def test_per_note_and_cross_note_compose_correctly():
    """Per-note dedup runs first (inside form_note_to_yaml), then cross-
    note dedup. Verify both layers fire when a duplicate exists BOTH
    within note 1 AND between note 1 and note 2."""
    notes_form = [
        {
            "type": "media",
            "outlets": [
                {"name": "CNN", "url": "https://cnn.com/x"},
                {"name": "CNN-DupWithin1", "url": "https://cnn.com/x"},  # within-note dup
            ],
        },
        {
            "type": "media",
            "outlets": [
                {"name": "CNN-DupCrossNote", "url": "https://cnn.com/x"},  # cross-note dup
                {"name": "BBC", "url": "https://bbc.com/y"},
            ],
        },
    ]
    out = notes_helpers.notes_form_to_yaml(notes_form)
    assert len(out) == 2
    assert [o.get("name") for o in out[0]["outlets"]] == ["CNN"]
    assert [o.get("name") for o in out[1]["outlets"]] == ["BBC"]
