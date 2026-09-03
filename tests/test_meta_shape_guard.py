"""The meta.yml shape guard: the write-path half, and its import hygiene.

Companion to `test_field_type_coupling.py` (the registry's coupled sites) and
the round-trip tests in `test_v2_routes.py` (the form path). Gotcha #96.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from cv_editor import paths, yaml_io
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def _meta():
    _, data = yaml_io.load(paths.data_dir() / "meta.yml")
    return data


def test_the_real_corpus_passes():
    """Self-guard: if this fails, every refusal test below is untrustworthy."""
    yaml_io._validate_meta_data(_meta())


@pytest.mark.parametrize(
    "label,mutate",
    [
        ("address stringified", lambda d: d.__setitem__("address", str(list(d["address"])))),
        ("footer stringified", lambda d: d.__setitem__("footer", str(dict(d["footer"])))),
        ("address absent", lambda d: d.pop("address")),
        ("footer absent", lambda d: d.pop("footer")),
        ("sections absent", lambda d: d.pop("sections")),
        ("footer missing template", lambda d: d["footer"].pop("template")),
        ("footer missing date_format", lambda d: d["footer"].pop("date_format")),
        ("name is a list", lambda d: d.__setitem__("name", ["A", "B"])),
        ("address is a mapping", lambda d: d.__setitem__("address", {"a": 1})),
    ],
)
def test_bad_shapes_are_refused(label, mutate):
    data = _meta()
    mutate(data)
    with pytest.raises(yaml_io.CorruptedShapeError):
        yaml_io._validate_meta_data(data)


@pytest.mark.parametrize(
    "label,mutate",
    [
        ("address empty", lambda d: d.__setitem__("address", CommentedSeq())),
        ("contacts empty", lambda d: d.__setitem__("contacts", CommentedSeq())),
        ("sections empty", lambda d: d.__setitem__("sections", CommentedSeq())),
        ("show_on_first_page absent", lambda d: d["footer"].pop("show_on_first_page", None)),
    ],
)
def test_empty_but_present_is_allowed(label, mutate):
    """PRESENCE, not non-emptiness.

    `address: []` and `contacts: []` are exactly what
    `scaffold._blank_meta_body()` and the example corpora write. A non-empty
    check here would refuse to write a blank tree, which is what
    `test_m5_scaffold.py`'s blank-tree assertion would have caught -- and is
    why `required: True` was the wrong tool for this.
    """
    data = _meta()
    mutate(data)
    yaml_io._validate_meta_data(data)


def test_a_non_mapping_is_ignored_rather_than_crashing():
    """Defensive: an empty or list-shaped meta.yml must not raise TypeError."""
    yaml_io._validate_meta_data(None)
    yaml_io._validate_meta_data([])
    # An empty MAPPING is a different case: it is a dict, so every required key
    # is genuinely missing and refusing is correct. What matters is that it is a
    # CorruptedShapeError -- an intended, catchable refusal -- and never a
    # TypeError or AttributeError leaking out of the guard.
    with pytest.raises(yaml_io.CorruptedShapeError):
        yaml_io._validate_meta_data(CommentedMap())


def test_write_with_backup_refuses_a_bad_shape(tmp_path):
    """The guard is wired into the WRITE, not merely importable."""
    target = tmp_path / "meta.yml"
    good = _meta()
    yaml_io.write_new(target, "# header\n", good)

    bad = _meta()
    bad["address"] = "['a', 'b']"
    before = target.read_bytes()
    with pytest.raises(yaml_io.CorruptedShapeError):
        yaml_io.write_with_backup(target, "# header\n", bad)
    assert target.read_bytes() == before, "a refused write still touched the file"


def test_importing_yaml_io_first_does_not_empty_the_tag_vocabulary():
    """`yaml_io` must stay a LEAF module.

    `schemas.py` runs data-reading widen hooks at import time, and those hooks
    do a function-local `from cv_editor import paths, yaml_io`. If `yaml_io`
    ever gains a module-level `from cv_editor import schemas`, importing
    `yaml_io` FIRST yields a partially-initialized module, the widen hook
    swallows the failure, and the topic-tag vocabulary ends up silently EMPTY --
    gotcha #92's exact state, with no test failing.

    Run in a FRESH interpreter, because import order within one process is
    whatever the first importer happened to establish.
    """
    code = "import cv_editor.yaml_io\nfrom cv_editor import schemas\nprint(len(schemas.TAGS))\n"
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(paths.project_root()),
    )
    assert res.returncode == 0, f"import failed:\n{res.stderr}"
    assert int(res.stdout.strip()) > 0, (
        "schemas.TAGS is EMPTY when cv_editor.yaml_io is imported first. "
        "Check for a new module-level cv_editor import in yaml_io.py."
    )


def test_required_scalars_do_not_break_the_blank_scaffold():
    """`required: True` is safe on the three hard-accessed SCALARS, not on the lists.

    The distinction cost a review round. `position`/`department`/`institution`
    are filled from `BLANK_META_PLACEHOLDERS`, so a blank tree still validates.
    `address` is NOT -- `_blank_meta_body` writes `address: []` -- so marking it
    required reds `test_m5_scaffold`'s "blank tree is check_data clean at all
    severities" assertion, because `data_check` files "required" as a WARNING.
    That is why `address` uses the non-destructive `renderer_required` flag
    instead, which guards the KEY without asserting non-emptiness.
    """
    from cv_editor import validate
    from cv_editor.scaffold import _blank_meta_body
    from cv_editor.schemas import META

    blank = _blank_meta_body()
    required = [f["name"] for f in META["fields"] if f.get("required")]
    assert {"position", "department", "institution"} <= set(required)

    for name in required:
        assert blank.get(name) not in (None, "", [], {}), (
            f"{name} is required but the blank scaffold leaves it empty; "
            f"`make init` would produce a tree that fails its own data check"
        )

    # And the inverse: nothing that the scaffold leaves empty may be required.
    empty_in_blank = {k for k, v in blank.items() if v in ([], {}, "", None)}
    assert not (empty_in_blank & set(required)), (
        f"{sorted(empty_in_blank & set(required))} are required but empty in the "
        f"blank scaffold. Use `renderer_required` for those instead."
    )

    errors = validate.validate_entry(
        {f["name"]: blank.get(f["name"]) for f in META["fields"]}, META["fields"]
    )
    assert errors == {}, f"the blank scaffold fails its own validator: {errors}"
