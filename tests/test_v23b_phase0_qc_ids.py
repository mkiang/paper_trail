"""V23-B Phase 0: stable-ID property tests (2026-05-25).

Per staging reviewer's mitigation: write the ID-stability test BEFORE
the emit code. Reject any ID scheme that fails the "insert an entry,
idx shifts" property test.

The contract every ID function must obey:
  1. Same finding emitted twice -> same ID (no spurious churn).
  2. Re-emitting after an unrelated YAML edit (insert/delete/reorder
     of a DIFFERENT entry) -> same ID.
  3. Canonical-value drift (PubMed updates a journal name) -> DIFFERENT ID
     so a stale `keep_yaml` decision can't silence the re-surfaced finding.
  4. Severity flip (MISMATCH <-> VARIANT) -> DIFFERENT ID so the
     re-classified finding re-surfaces explicitly.
  5. Different entities, same finding shape -> DIFFERENT IDs.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from cv_editor import qc_ids  # noqa: E402

# ---- hash8 sanity ----


def test_hash8_is_8_chars():
    assert len(qc_ids.hash8("anything")) == 8


def test_hash8_is_deterministic():
    assert qc_ids.hash8("x") == qc_ids.hash8("x")


def test_hash8_distinguishes_inputs():
    assert qc_ids.hash8("a") != qc_ids.hash8("b")


def test_hash8_handles_empty():
    # SHA-256 of empty string is a known value; just check it returns 8 chars.
    assert len(qc_ids.hash8("")) == 8
    assert len(qc_ids.hash8(None or "")) == 8


# ---- _entity_key preference order ----


def test_entity_key_prefers_pmid():
    key = qc_ids._entity_key(pmid="123", doi="10.x/y", title="Some title")
    assert key == "123"


def test_entity_key_falls_back_to_doi_hash():
    key = qc_ids._entity_key(pmid=None, doi="10.x/y", title="Some title")
    assert key.startswith("doi:")
    assert len(key) == len("doi:") + 8


def test_entity_key_falls_back_to_title_hash():
    key = qc_ids._entity_key(pmid=None, doi=None, title="Some Title")
    assert key.startswith("title:")
    assert len(key) == len("title:") + 8


def test_entity_key_raises_when_no_identifier():
    import pytest

    with pytest.raises(ValueError, match="no PMID, DOI, or title"):
        qc_ids._entity_key(pmid=None, doi=None, title=None)
    with pytest.raises(ValueError):
        qc_ids._entity_key(pmid="", doi="", title="")


def test_entity_key_doi_case_insensitive():
    a = qc_ids._entity_key(doi="10.ABC/XYZ")
    b = qc_ids._entity_key(doi="10.abc/xyz")
    assert a == b


def test_entity_key_title_strips_whitespace():
    a = qc_ids._entity_key(title="Some Title")
    b = qc_ids._entity_key(title="  Some Title  ")
    assert a == b


# ---- MISMATCH / VARIANT IDs ----


def test_mismatch_id_stable_across_re_emit():
    """Same finding emitted twice in successive sweeps -> same ID."""
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    b = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    assert a == b


def test_mismatch_id_changes_when_canonical_drifts():
    """If PubMed updates the journal name, the ID MUST change so a stale
    keep_yaml decision doesn't silence the re-surfaced finding."""
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    b = qc_ids.mismatch_id(
        source="pubmed", pmid="123", field="journal", canonical="JAMA: The Journal of..."
    )
    assert a != b


def test_mismatch_id_changes_when_field_changes():
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    b = qc_ids.mismatch_id(source="pubmed", pmid="123", field="title", canonical="JAMA")
    assert a != b


def test_mismatch_id_changes_when_entity_changes():
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    b = qc_ids.mismatch_id(source="pubmed", pmid="456", field="journal", canonical="JAMA")
    assert a != b


def test_mismatch_id_changes_when_source_changes():
    """Two different sources (PubMed + Crossref) flagging the same
    field+entity get distinct IDs."""
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="journal", canonical="JAMA")
    b = qc_ids.mismatch_id(source="crossref", pmid="123", field="journal", canonical="JAMA")
    assert a != b


def test_variant_id_distinct_from_mismatch_id():
    """Severity flip (MISMATCH <-> VARIANT) re-surfaces as a different ID."""
    a = qc_ids.mismatch_id(source="pubmed", pmid="123", field="authors", canonical="A; B; C")
    b = qc_ids.variant_id(source="pubmed", pmid="123", field="authors", canonical="A; B; C")
    assert a != b
    assert a.startswith("MM:")
    assert b.startswith("VA:")


def test_variant_id_canonical_drift_changes_id():
    a = qc_ids.variant_id(source="crossref", doi="10.x/y", field="pages", canonical="1-10")
    b = qc_ids.variant_id(source="crossref", doi="10.x/y", field="pages", canonical="1-11")
    assert a != b


# ---- ID_ENRICHMENT IDs ----


def test_id_enrichment_id_uses_pmid_entity():
    out = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    assert out == "ID:123:doi"


def test_id_enrichment_id_falls_back_to_doi():
    out = qc_ids.id_enrichment_id(doi="10.x/y", suggested_field="pmid")
    assert out.startswith("ID:doi:")
    assert out.endswith(":pmid")


def test_id_enrichment_id_distinct_for_different_suggested_fields():
    a = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    b = qc_ids.id_enrichment_id(pmid="123", suggested_field="pmcid")
    assert a != b


def test_id_enrichment_id_stable_across_re_emit():
    a = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    b = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    assert a == b


# ---- PMID_MISMATCH IDs ----


def test_pmid_mismatch_id_stable():
    a = qc_ids.pmid_mismatch_id(pmid="90000011")
    b = qc_ids.pmid_mismatch_id(pmid="90000011")
    assert a == b
    assert a == "PM:pubmed:90000011:no_record"


def test_pmid_mismatch_id_distinct_pmids():
    a = qc_ids.pmid_mismatch_id(pmid="123")
    b = qc_ids.pmid_mismatch_id(pmid="456")
    assert a != b


def test_pmid_mismatch_id_strips_whitespace():
    a = qc_ids.pmid_mismatch_id(pmid=" 123 ")
    b = qc_ids.pmid_mismatch_id(pmid="123")
    assert a == b


# ---- AUTHOR_NAME_VARIANT / JOURNAL_NAME_VARIANT IDs ----


def test_author_name_variant_id_normalizes_case():
    a = qc_ids.author_name_variant_id(normalized_key="Cohen AK")
    b = qc_ids.author_name_variant_id(normalized_key="cohen ak")
    assert a == b
    assert a == "AN:cohen ak"


def test_journal_name_variant_id_normalizes_case():
    a = qc_ids.journal_name_variant_id(normalized_key="JAMA")
    b = qc_ids.journal_name_variant_id(normalized_key="jama")
    assert a == b
    assert a == "JN:jama"


def test_author_and_journal_variant_ids_have_distinct_prefixes():
    a = qc_ids.author_name_variant_id(normalized_key="same")
    b = qc_ids.journal_name_variant_id(normalized_key="same")
    assert a != b
    assert a.startswith("AN:")
    assert b.startswith("JN:")


# ---- SELF_ABSENT IDs ----


def test_self_absent_id_stable_across_re_emit():
    a = qc_ids.self_absent_id(pmid="123")
    b = qc_ids.self_absent_id(pmid="123")
    assert a == b


def test_self_absent_id_uses_entity_key():
    a = qc_ids.self_absent_id(pmid="123")
    assert a == "SA:123"
    b = qc_ids.self_absent_id(doi="10.x/y")
    assert b.startswith("SA:doi:")


# ---- MISSING_IDS IDs (the hardest case: no seed ID) ----


def test_missing_ids_id_stable_for_same_title_year():
    a = qc_ids.missing_ids_id(title="Some Paper Title", year=2025)
    b = qc_ids.missing_ids_id(title="Some Paper Title", year=2025)
    assert a == b


def test_missing_ids_id_changes_when_title_changes():
    a = qc_ids.missing_ids_id(title="Title A", year=2025)
    b = qc_ids.missing_ids_id(title="Title B", year=2025)
    assert a != b


def test_missing_ids_id_changes_when_year_changes():
    a = qc_ids.missing_ids_id(title="Same Title", year=2024)
    b = qc_ids.missing_ids_id(title="Same Title", year=2025)
    assert a != b


def test_missing_ids_id_handles_no_year():
    a = qc_ids.missing_ids_id(title="Some Title", year=None)
    b = qc_ids.missing_ids_id(title="Some Title")
    assert a == b


def test_missing_ids_id_case_insensitive():
    a = qc_ids.missing_ids_id(title="Some Title", year=2025)
    b = qc_ids.missing_ids_id(title="SOME TITLE", year=2025)
    assert a == b


# ---- The headline property test: idx shifts don't break IDs ----


def test_ids_survive_yaml_idx_shifts():
    """The whole point of the stable-ID scheme. Simulate two sweeps:
    sweep A flags finding F in publications.yml. Between sweeps, the
    user inserts an unrelated entry above F (which shifts F's idx).
    Sweep B must produce the SAME id for F.

    Concretely: the ID is built from (source, pmid, field, canonical) —
    NONE of which depend on the entry's position in the YAML list. So
    inserting/deleting/reordering OTHER entries cannot change F's ID
    by construction.
    """
    sweep_a = qc_ids.mismatch_id(
        source="pubmed",
        pmid="90000011",
        field="journal",
        canonical="JAMA: The Journal of the American Medical Association",
    )
    # Between sweeps, the user inserted 50 entries above the publication
    # at publications.yml. The publication's `global_idx` shifted from 5
    # to 55. The PMID is unchanged. The canonical is unchanged. Sweep B:
    sweep_b = qc_ids.mismatch_id(
        source="pubmed",
        pmid="90000011",
        field="journal",
        canonical="JAMA: The Journal of the American Medical Association",
    )
    assert sweep_a == sweep_b


def test_ids_survive_yaml_idx_shifts_for_missing_ids():
    """Same property, harder case: no seed ID. The (title, year) tuple
    is the only stable handle, but it IS stable across idx shifts."""
    a = qc_ids.missing_ids_id(title="Some unique paper title", year=2025)
    # The user inserted entries around this one; idx shifted; title +
    # year still identifies the paper:
    b = qc_ids.missing_ids_id(title="Some unique paper title", year=2025)
    assert a == b


def test_ids_survive_yaml_idx_shifts_for_id_enrichment():
    """ID_ENRICHMENT discriminator is (entity, suggested_field). Idx
    shifts don't touch either."""
    a = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    b = qc_ids.id_enrichment_id(pmid="123", suggested_field="doi")
    assert a == b
