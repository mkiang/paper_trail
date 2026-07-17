"""
Helpers for the rich notes + open_access editors.

`notes` field shape (per publications.yml docstring + actual data):
    list of dicts. type ∈ {commentary, letter, response, media,
    editorial, contributions, note}. Per-type fields:
      - commentary | letter | response  -> citation: str
      - media                            -> outlets: list of (str | dict{name, url?, highlighted?})
      - editorial | contributions | note -> text: str
    Any note can carry highlighted: true to gate it behind
    --input show_highlighted=true at render time.

`open_access` shape (rare — one entry currently uses it):
    dict with optional keys paper/code/data, each value = true OR a URL string.

These helpers convert between the YAML/ruamel shape and the plain-dict
shape passed to/from the form (which serializes via JSON in a hidden
input).
"""

from __future__ import annotations

from ruamel.yaml.comments import CommentedMap, CommentedSeq

# All note types the renderer understands (kept for round-trip integrity —
# existing entries may use any of these and must continue to load/save).
NOTE_TYPES = ["commentary", "letter", "response", "media", "editorial", "contributions", "note"]

# Primary types offered in the editor's "add new note" dropdown (2026-05-15
# user request). Legacy types remain in NOTE_TYPES for round-trip; the
# template includes the current type in the dropdown if it's not primary.
PRIMARY_NOTE_TYPES = ["media", "contributions", "note"]

# Display label override (mostly cosmetic). Map type -> human label.
NOTE_TYPE_LABEL = {
    "note": "note (generic)",
    "media": "media",
    "contributions": "contributions",
    "commentary": "commentary",
    "letter": "letter",
    "response": "response",
    "editorial": "editorial",
}

# Per-type primary content field. media is special-cased (outlets list).
TYPE_TO_CONTENT_FIELD = {
    "commentary": "citation",
    "letter": "citation",
    "response": "citation",
    "editorial": "text",
    "contributions": "text",
    "note": "text",
}


# ---- notes: YAML <-> form ----


def note_yaml_to_form(n) -> dict:
    """Convert a single note from YAML/ruamel shape to plain dict for the form."""
    if not isinstance(n, dict):
        return {"type": "note", "text": str(n), "highlighted": False, "outlets": []}
    out = {
        "type": n.get("type", "note"),
        "highlighted": bool(n.get("highlighted", False)),
        "citation": "",
        "text": "",
        "outlets": [],
    }
    if out["type"] == "media":
        for o in n.get("outlets") or []:
            if isinstance(o, dict):
                out["outlets"].append(
                    {
                        "name": o.get("name", ""),
                        "url": o.get("url", ""),
                        "highlighted": bool(o.get("highlighted", False)),
                    }
                )
            else:
                out["outlets"].append({"name": str(o), "url": "", "highlighted": False})
    elif out["type"] in TYPE_TO_CONTENT_FIELD:
        field = TYPE_TO_CONTENT_FIELD[out["type"]]
        out[field] = n.get(field, "")
    return out


def notes_yaml_to_form(notes) -> list:
    if not notes:
        return []
    return [note_yaml_to_form(n) for n in notes]


def _outlet_to_dict(o) -> dict:
    """Coerce a YAML outlet (str OR dict) to a uniform dict view."""
    if isinstance(o, dict):
        return {
            "name": str(o.get("name") or "").strip(),
            "url": str(o.get("url") or "").strip(),
            "highlighted": bool(o.get("highlighted")),
        }
    return {
        "name": str(o or "").strip(),
        "url": "",
        "highlighted": False,
    }


def group_outlets_for_display(outlets) -> list[dict]:
    """Dedup outlets by case-insensitive trimmed name and assign per-outlet
    1-based local indices.

    Returns a list of groups in first-occurrence order. Each group:

        {
            "name": str,           # canonical name (first-occurrence casing preserved)
            "urls": [              # one per visible outlet entry in YAML order
                {"local_idx": int, "url": str, "highlighted": bool},
                ...
            ],
        }

    Key is `urls`, NOT `items` — `items` would collide with Jinja's
    attribute-resolution (Python's dict.items() method shadows the key).

    Render contract (mirror in templates/bespoke/render.typ + entry_view.html):

    * `local_idx == 1` represents the first article for that outlet.
      Rendered as the BARE outlet name (hyperlinked to urls[0].url).
    * `local_idx >= 2` renders as a parenthesized digit list `(2, 3, …)`
      after the bare name; each digit hyperlinks to that article's URL.

    Worked example (2026-05-17 update — per-outlet first-is-bare style):
        Input:  [NBC, CBS, AOL(a), Yahoo(a), Newsbreak,
                 AOL(b), AOL(c), Yahoo(b), Yahoo(c)]
        Output: NBC(1), CBS(1), AOL(1,2,3), Yahoo(1,2,3), Newsbreak(1)
        Rendered:
          "NBC, CBS, AOL (2, 3), Yahoo (2, 3), Newsbreak"

    Singletons render as just `Name`. Multi-article groups render
    `Name (digits-2-and-up)`.

    Empty / falsy inputs return []. Outlets with empty names are dropped
    (they wouldn't render meaningfully).

    T1.4 invariant: `highlighted: true` outlets are excluded from index
    assignment entirely (matches Typst `format-media-outlets` filter
    and JS `groupOutletsPreview`). With outlets [normal, hidden, normal],
    all three implementations agree on local indices [1, 2] for the
    visible outlets — local 2 belongs to the second visible outlet, not
    the third raw outlet.
    """
    if not outlets:
        return []
    groups: list[dict] = []
    by_key: dict[str, dict] = {}
    for o in outlets:
        view = _outlet_to_dict(o)
        if not view["name"]:
            continue
        if view["highlighted"]:
            # Hidden outlets don't render in the PDF (default show flags)
            # so they MUST NOT consume a local index.
            continue
        key = view["name"].casefold()
        existing = by_key.get(key)
        if existing is None:
            url_entry = {
                "local_idx": 1,
                "url": view["url"],
                "highlighted": False,
            }
            group = {"name": view["name"], "urls": [url_entry]}
            groups.append(group)
            by_key[key] = group
        else:
            url_entry = {
                "local_idx": len(existing["urls"]) + 1,
                "url": view["url"],
                "highlighted": False,
            }
            existing["urls"].append(url_entry)
    return groups


def form_outlet_to_yaml(o: dict):
    """Single outlet (dict from form) -> YAML shape (str OR ruamel CommentedMap)."""
    name = (o.get("name") or "").strip()
    url = (o.get("url") or "").strip()
    highlighted = bool(o.get("highlighted"))
    if not url and not highlighted:
        return name  # plain string is the common case
    cm = CommentedMap()
    cm["name"] = name
    if url:
        cm["url"] = url
    if highlighted:
        cm["highlighted"] = True
    return cm


def _outlet_url_key(url: str) -> str:
    """Normalized key for URL-based outlet dedup (2026-05-25, Stage C / I7).

    Per user direction: case-insensitive, trailing-slash-ignoring.
    Do NOT strip fragments or query strings — different `?utm=...` or
    `#section` URLs may point to genuinely different articles and the
    user can dedup those manually if needed.
    """
    return (url or "").strip().rstrip("/").lower()


def dedup_outlets_by_url(outlets: list) -> list:
    """Drop later outlet entries whose URL matches an earlier one
    (case-insensitive, trailing-slash-ignoring). First occurrence wins
    (preserves outlet name + position). Outlets WITHOUT a URL are
    preserved as-is — they aren't duplicates of anything.

    Stage C / I7 (2026-05-25): user reported pasting Altmetric
    Explorer mentions often yields duplicate URLs across rows; this
    dedup runs at save-time so YAML stays clean without manual
    triage. Uses a sequential filter (NOT dict.fromkeys) to preserve
    URL-less rows.
    """
    seen: set[str] = set()
    kept: list = []
    for o in outlets or []:
        url = (o.get("url") or "").strip()
        if not url:
            kept.append(o)
            continue
        key = _outlet_url_key(url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(o)
    return kept


class UnknownNoteTypeError(ValueError):
    """Raised when a note's type isn't in NOTE_TYPES. Surface to the user
    rather than silently coercing — silent coercion historically lost
    notes when the schema grew."""


def form_note_to_yaml(form: dict):
    """Single note (form dict) -> ruamel CommentedMap. Raises
    UnknownNoteTypeError on a type not in NOTE_TYPES."""
    cm = CommentedMap()
    t = form.get("type") or "note"
    if t not in NOTE_TYPES:
        raise UnknownNoteTypeError(f"unknown note type {t!r}; expected one of {NOTE_TYPES}")
    cm["type"] = t
    if t == "media":
        # Stage C / I7 (2026-05-25): dedup by normalized URL BEFORE
        # building the YAML list. Drops later outlets whose URL matches
        # an earlier one (case-insensitive, trailing-slash-ignoring).
        # First occurrence wins. URL-less rows are preserved.
        outlets = CommentedSeq()
        for o in dedup_outlets_by_url(form.get("outlets") or []):
            if (o.get("name") or "").strip():
                outlets.append(form_outlet_to_yaml(o))
        if outlets:
            cm["outlets"] = outlets
    else:
        field = TYPE_TO_CONTENT_FIELD[t]
        v = (form.get(field) or "").strip()
        if v:
            cm[field] = v
    if form.get("highlighted"):
        cm["highlighted"] = True
    return cm


def notes_form_to_yaml(notes_form: list):
    """Form list -> ruamel CommentedSeq. Drops empty notes (no content).
    Propagates UnknownNoteTypeError from form_note_to_yaml so the save
    handler can surface it as a validation error.

    V23-A (2026-05-25): cross-note outlet URL dedup. After per-note dedup
    runs inside `form_note_to_yaml` (Stage C / I7), a second pass tracks
    seen URLs across ALL media notes of the same entry and drops later
    duplicates. A media note that loses every outlet to cross-note dedup
    is dropped entirely (matches the existing empty-note filter). Scope
    is per-publication only — cross-publication URL collisions are
    legitimate (one press piece covering multiple papers)."""
    out = CommentedSeq()
    seen_urls: set[str] = set()
    for n in notes_form or []:
        t = n.get("type")
        if t not in NOTE_TYPES:
            raise UnknownNoteTypeError(f"unknown note type {t!r}; expected one of {NOTE_TYPES}")
        if t == "media":
            if not any((o.get("name") or "").strip() for o in n.get("outlets") or []):
                continue
        else:
            field = TYPE_TO_CONTENT_FIELD[t]
            if not (n.get(field) or "").strip():
                continue
        yaml_note = form_note_to_yaml(n)
        if yaml_note.get("type") == "media":
            outlets = yaml_note.get("outlets") or []
            kept = CommentedSeq()
            for o in outlets:
                url = o.get("url") if isinstance(o, dict) else ""
                key = _outlet_url_key(url or "")
                if key and key in seen_urls:
                    continue
                if key:
                    seen_urls.add(key)
                kept.append(o)
            if len(kept) == 0:
                continue
            yaml_note["outlets"] = kept
        out.append(yaml_note)
    return out


# ---- simple notes (presentations + service): YAML <-> form ----
#
# Shape on disk: list of (str | dict{text, highlighted?}). No `type` field.
# Form shape: list of {text, highlighted}. Empty rows dropped on save.


def simple_notes_yaml_to_form(notes) -> list:
    if not notes:
        return []
    out = []
    for n in notes:
        if isinstance(n, dict):
            out.append(
                {
                    "text": str(n.get("text", "")),
                    "highlighted": bool(n.get("highlighted", False)),
                }
            )
        else:
            out.append({"text": str(n), "highlighted": False})
    return out


def simple_notes_form_to_yaml(notes_form: list):
    """Form list -> ruamel CommentedSeq. Drops empty rows.

    A note with `highlighted: false` and non-empty text round-trips as a
    plain string (the common case in YAML); `highlighted: true` forces
    the dict shape.
    """
    out = CommentedSeq()
    for n in notes_form or []:
        text = (n.get("text") or "").strip()
        if not text:
            continue
        if n.get("highlighted"):
            cm = CommentedMap()
            cm["text"] = text
            cm["highlighted"] = True
            out.append(cm)
        else:
            out.append(text)
    return out


# ---- open_access: YAML <-> form ----

OA_KEYS = ["paper", "code", "data"]


def open_access_yaml_to_form(oa) -> dict:
    """YAML dict -> form dict {paper: {enabled, url}, code: ..., data: ...}."""
    out = {k: {"enabled": False, "url": ""} for k in OA_KEYS}
    if not isinstance(oa, dict):
        return out
    for k in OA_KEYS:
        v = oa.get(k)
        if v is True:
            out[k] = {"enabled": True, "url": ""}
        elif isinstance(v, str) and v.strip():
            out[k] = {"enabled": True, "url": v.strip()}
    return out


def open_access_form_to_yaml(form: dict):
    """Form dict -> ruamel CommentedMap, or None if all keys disabled."""
    cm = CommentedMap()
    for k in OA_KEYS:
        entry = form.get(k) or {}
        if not entry.get("enabled"):
            continue
        url = (entry.get("url") or "").strip()
        cm[k] = url if url else True
    return cm if cm else None


# ---- Self-position check for the contributions-needed warning ----


def _author_name(a) -> str:
    if isinstance(a, dict):
        return str(a.get("name", "")).strip()
    return str(a).strip()


def self_author_position(authors, self_name: str) -> str:
    """Return one of: 'first', 'co_first', 'last', 'co_senior', 'middle', 'absent'.

    'first' / 'last' = positionally first or last in the author list.
    'co_first' / 'co_senior' = marked with the dict-form flag AND the
        flag is `is_lead_eligible=True` in `cv_editor.author_flags`.
    'middle' = present but not in a leading/senior position.
    'absent' = not in the author list at all (e.g., corporate-author entries).

    The V18-A `group_authorship` flag is INTENTIONALLY ignored for
    position classification: group authorship is orthogonal to
    individual lead-author status. The V20 B1 refactor codifies this
    as `AuthorFlag.is_lead_eligible=False` on `group_authorship` so the
    skip is enforced by the spec, not a comment.
    """
    from cv_editor.author_flags import LEAD_FLAG_KEYS

    if not authors:
        return "absent"
    self_name = (self_name or "").strip()
    me_idx = None
    my_lead_flag: str | None = None
    for i, a in enumerate(authors):
        nm = _author_name(a)
        if nm == self_name:
            me_idx = i
            if isinstance(a, dict):
                # First lead-eligible flag wins (today only one of
                # co_first / co_senior is ever set per author).
                for key in LEAD_FLAG_KEYS:
                    if a.get(key):
                        my_lead_flag = key
                        break
    if me_idx is None:
        return "absent"
    if me_idx == 0:
        return "first"
    if me_idx == len(authors) - 1:
        return "last"
    if my_lead_flag is not None:
        return my_lead_flag
    return "middle"


def needs_contribution_note(entry, self_name: str) -> bool:
    """True iff the self author is a middle author AND there is no
    `contributions` note.

    Used to flag the entry with a non-blocking warning. Returns False when the
    self author is first / co-first / last / co-senior, or is not on the author
    list (corporate authorship), since those cases don't warrant a
    contributions sub-bullet.
    """
    status = self_author_position(entry.get("authors") or [], self_name)
    if status != "middle":
        return False
    notes = entry.get("notes") or []
    for n in notes:
        if isinstance(n, dict) and n.get("type") == "contributions":
            return False
    return True
