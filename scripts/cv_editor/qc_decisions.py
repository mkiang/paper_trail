"""V23-B Phase 1: QC decisions sidecar (qc/qc_decisions.json).

Records per-finding-id triage decisions (apply / keep_yaml / defer)
made via the /qc/triage UI. Sibling of the Phase 0 read-only sidecar
qc/report.json.

Why a separate sidecar (not in qc/report.json):
  qc/report.json is regenerated from scratch every sweep. Decisions
  must persist across sweeps. Same pattern as V19's
  publications_pubmed_sync.json.

Re-surface contract (also pinned by effective_findings in qc_sync):
  - decision == "apply":
      MISMATCH/VARIANT: silenced unless current_canonical != canonical_value_at_decision
      ID_ENRICHMENT: silenced (id_enrichment row will drop from next sweep)
  - decision == "keep_yaml":
      MISMATCH/VARIANT: silenced unless current_yaml != yaml_value_at_decision
      ID_ENRICHMENT: silenced unless current_suggested_value != suggested_value_at_decision
  - decision == "defer": never silenced (re-surfaces every sweep)

Tombstones:
  When a finding_id disappears from a sweep's qc/report.json (e.g.,
  user fixed YAML by hand), the decision is tombstoned (moved into
  the `tombstones` block with `pruned_at` timestamp) for 30 days
  rather than deleted. Allows re-surface-via-banner if the same
  (entity_key, field) reappears under a new ID.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cv_editor.atomic_json import atomic_write_json
from cv_editor.versioned_json import load_versioned

SCHEMA_VERSION = 1
TOMBSTONE_TTL_DAYS = 30
VALID_DECISIONS = frozenset(("apply", "keep_yaml", "defer"))


@dataclass
class Decision:
    """One triage decision keyed by finding_id."""

    decision: str  # "apply" | "keep_yaml" | "defer"
    finding_type: str  # "MISMATCH" | "VARIANT" | "ID_ENRICHMENT"
    decided_at: str  # ISO-8601 UTC
    reason: Optional[str] = None
    yaml_value_at_decision: Optional[str] = None  # MISMATCH/VARIANT only
    canonical_value_at_decision: Optional[str] = None  # MISMATCH/VARIANT
    suggested_value_at_decision: Optional[str] = None  # ID_ENRICHMENT only

    def to_json(self) -> dict:
        out = {
            "decision": self.decision,
            "finding_type": self.finding_type,
            "decided_at": self.decided_at,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.yaml_value_at_decision is not None:
            out["yaml_value_at_decision"] = self.yaml_value_at_decision
        if self.canonical_value_at_decision is not None:
            out["canonical_value_at_decision"] = self.canonical_value_at_decision
        if self.suggested_value_at_decision is not None:
            out["suggested_value_at_decision"] = self.suggested_value_at_decision
        return out

    @classmethod
    def from_json(cls, raw: dict) -> "Decision":
        return cls(
            decision=str(raw["decision"]),
            finding_type=str(raw.get("finding_type", "")),
            decided_at=str(raw.get("decided_at", "")),
            reason=raw.get("reason"),
            yaml_value_at_decision=raw.get("yaml_value_at_decision"),
            canonical_value_at_decision=raw.get("canonical_value_at_decision"),
            suggested_value_at_decision=raw.get("suggested_value_at_decision"),
        )


@dataclass
class Tombstone:
    """A pruned decision kept for 30 days for audit + re-surface
    detection. When a tombstone's `(entity_key, field)` matches a new
    finding, the triage UI surfaces a banner-info noting the resurface."""

    pruned_at: str
    decision: dict  # serialized snapshot of the original Decision

    def to_json(self) -> dict:
        return {"pruned_at": self.pruned_at, "decision": dict(self.decision)}

    @classmethod
    def from_json(cls, raw: dict) -> "Tombstone":
        return cls(
            pruned_at=str(raw.get("pruned_at", "")),
            decision=dict(raw.get("decision") or {}),
        )


@dataclass
class Decisions:
    """In-memory container for the decisions sidecar."""

    decisions: dict[str, Decision] = field(default_factory=dict)
    tombstones: dict[str, Tombstone] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "Decisions":
        return cls()

    def set(self, finding_id: str, **kwargs) -> None:
        """Set or replace a decision for `finding_id`. Stamps
        `decided_at` automatically if not provided."""
        if "decided_at" not in kwargs:
            kwargs["decided_at"] = _now_iso()
        self.decisions[finding_id] = Decision(**kwargs)

    def get(self, finding_id: str) -> Optional[Decision]:
        return self.decisions.get(finding_id)

    def remove(self, finding_id: str) -> None:
        """Tombstone a decision (don't delete outright). Removes from
        `decisions` and adds to `tombstones` with pruned_at timestamp."""
        if finding_id not in self.decisions:
            return
        snap = self.decisions.pop(finding_id)
        self.tombstones[finding_id] = Tombstone(
            pruned_at=_now_iso(),
            decision=snap.to_json(),
        )

    def prune_expired_tombstones(self, *, now: Optional[datetime] = None) -> int:
        """Drop tombstones older than TOMBSTONE_TTL_DAYS. Returns the
        count of pruned tombstones."""
        ref = now or datetime.now(timezone.utc)
        cutoff = ref - timedelta(days=TOMBSTONE_TTL_DAYS)
        to_drop = []
        for fid, tomb in self.tombstones.items():
            try:
                ts = datetime.fromisoformat(tomb.pruned_at)
            except (ValueError, TypeError):
                # Malformed pruned_at → drop (can't audit it anyway)
                to_drop.append(fid)
                continue
            if ts < cutoff:
                to_drop.append(fid)
        for fid in to_drop:
            self.tombstones.pop(fid, None)
        return len(to_drop)

    def to_json(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "decisions": {fid: d.to_json() for fid, d in self.decisions.items()},
            "tombstones": {fid: t.to_json() for fid, t in self.tombstones.items()},
        }

    def save_atomic(self, path: Path) -> None:
        """Atomic write via cv_editor/atomic_json. Verifies JSON
        round-trips before swap."""
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, self.to_json())


def load(path: Path, *, silent: bool = False) -> Decisions:
    """Load decisions sidecar. Returns Decisions.empty() on missing,
    corrupt, or version-mismatched file."""
    raw = load_versioned(
        path,
        expected_version=SCHEMA_VERSION,
        component_name="qc_decisions",
        silent=silent,
    )
    if raw is None:
        return Decisions.empty()
    out = Decisions()
    for fid, d in (raw.get("decisions") or {}).items():
        try:
            out.decisions[str(fid)] = Decision.from_json(d)
        except (KeyError, TypeError) as e:
            if not silent:
                print(
                    f"[qc_decisions] WARNING: skipping malformed decision {fid!r}: {e}",
                    file=sys.stderr,
                )
    for fid, t in (raw.get("tombstones") or {}).items():
        try:
            out.tombstones[str(fid)] = Tombstone.from_json(t)
        except (KeyError, TypeError):
            pass
    return out


def _now_iso() -> str:
    """ISO-8601 UTC with seconds precision (matches generated_at in
    qc/report.json)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----- Re-surface predicates (per-finding-type) -----


def is_silenced_mismatch(
    finding: dict,
    decision: Optional[Decision],
    *,
    current_yaml_value: Optional[str] = None,
) -> bool:
    """Return True if a MISMATCH finding should be silenced from the
    triage page given its decision and the current YAML state.

    `current_yaml_value` is what the YAML field currently holds for
    this finding's (entity, field). Passing None means "use the value
    in finding['yaml_value']" (no live re-read happened).
    """
    if decision is None:
        return False
    if decision.decision == "defer":
        return False
    if decision.decision == "apply":
        # Silenced unless the canonical value changed since the decision.
        canon = str(finding.get("canonical_value") or "")
        return canon == str(decision.canonical_value_at_decision or "")
    if decision.decision == "keep_yaml":
        live = (
            current_yaml_value
            if current_yaml_value is not None
            else str(finding.get("yaml_value") or "")
        )
        return str(live) == str(decision.yaml_value_at_decision or "")
    return False


def is_silenced_variant(
    finding: dict,
    decision: Optional[Decision],
    *,
    current_yaml_value: Optional[str] = None,
) -> bool:
    """VARIANT findings use the same re-surface logic as MISMATCH —
    canonical drift re-surfaces apply decisions; YAML drift re-surfaces
    keep_yaml decisions."""
    return is_silenced_mismatch(
        finding,
        decision,
        current_yaml_value=current_yaml_value,
    )


def is_silenced_id_enrichment(
    finding: dict,
    decision: Optional[Decision],
) -> bool:
    """ID_ENRICHMENT re-surface logic (correctness reviewer C-H5 fix):

    A keep_yaml decision is silenced only if the suggested value is
    UNCHANGED since the decision. If PubMed now suggests a different
    DOI for the same entity, the prior keep_yaml decision is moot —
    re-surface so the user can re-decide on the new suggestion.

    An apply decision is silenced trivially: applying the suggested ID
    adds it to the YAML, so the next sweep's id_enrichment row
    disappears entirely (the entity now HAS the field).
    """
    if decision is None:
        return False
    if decision.decision == "defer":
        return False
    if decision.decision == "apply":
        # Trivially silenced — but if the same suggested_field re-appears
        # (e.g. the user removed the applied ID by hand), the new
        # finding's suggested_value may differ; re-surface in that case.
        cur = str(finding.get("suggested_value") or "")
        return cur == str(decision.suggested_value_at_decision or "")
    if decision.decision == "keep_yaml":
        cur = str(finding.get("suggested_value") or "")
        return cur == str(decision.suggested_value_at_decision or "")
    return False


def is_silenced_self_absent(
    finding: dict,
    decision: Optional[Decision],
) -> bool:
    """SELF_ABSENT findings (author name not detected in the author list)
    are *acknowledged*, not applied — there is nothing to auto-fix; the
    user is asserting the paper legitimately has no self-author.

    Silenced once any non-`defer` decision is recorded against the
    finding id. The `SA:` id is entity-based (PMID > DOI > title), so an
    acknowledged paper stays acknowledged across sweeps; if the author is
    later added, the finding drops from the sweep entirely (nothing to
    silence). `defer` never silences (re-surfaces every sweep).
    """
    if decision is None:
        return False
    if decision.decision == "defer":
        return False
    return True
