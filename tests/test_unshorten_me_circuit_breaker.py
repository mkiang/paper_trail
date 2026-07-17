"""Post-batch (2026-05-25): unshorten.me 429 circuit breaker.

After Stage B / I9 dropped the failure-TTL (every Resolve click re-
attempts), strategy 4 (unshorten.me) was getting hammered on a flaky
home-network sweep: their free tier is ~10 requests/hour for new URLs,
so once we hit 429 every subsequent call within the hour also 429s.

The fix: in-memory cooldown timer. On 429 (or rate-limit payload),
trip the cooldown for 10 min (default) or the value from a Retry-After
header if the API sends one. While cooldown is active,
resolve_via_unshorten_me skips the HTTP call entirely and returns a
synthetic failed_rate_limit with the remaining seconds in the error.

Verified rate limit: free tier is 10 req/hour for new URLs (already-
cached URLs in their DB are unlimited). 60s cooldown was wrong (would
re-trigger 429 immediately since the hour window hasn't reset); 10 min
is a small safety margin over the rolling 6-min average slot opening.
"""

from __future__ import annotations

import io
import urllib.error

import pytest
from cv_editor import altmetric_client


@pytest.fixture(autouse=True)
def reset_cooldown():
    """Every test starts with a clean circuit breaker."""
    altmetric_client._reset_unshorten_me_cooldown()
    yield
    altmetric_client._reset_unshorten_me_cooldown()


class _Resp:
    """Minimal urlopen response stub."""

    def __init__(self, body=b"", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]


# ---- The cooldown state machine ----


def test_cooldown_starts_inactive():
    assert altmetric_client._unshorten_me_on_cooldown() is False
    assert altmetric_client._unshorten_me_cooldown_remaining() == 0


def test_trip_cooldown_uses_default_when_seconds_not_given():
    altmetric_client._trip_unshorten_me_cooldown()
    assert altmetric_client._unshorten_me_on_cooldown() is True
    # Default is 10 minutes = 600 seconds; should be within 1s of that.
    remaining = altmetric_client._unshorten_me_cooldown_remaining()
    assert 595 <= remaining <= 605


def test_trip_cooldown_honors_explicit_seconds():
    altmetric_client._trip_unshorten_me_cooldown(seconds=3600)
    remaining = altmetric_client._unshorten_me_cooldown_remaining()
    assert 3595 <= remaining <= 3605


def test_cooldown_decays_after_window_with_now_override():
    # Set cooldown at t=1000 for 60s; check status at t=1000, 1059, 1061.
    altmetric_client._trip_unshorten_me_cooldown(seconds=60, now=1000.0)
    assert altmetric_client._unshorten_me_on_cooldown(now=1000.0) is True
    assert altmetric_client._unshorten_me_on_cooldown(now=1059.0) is True
    assert altmetric_client._unshorten_me_on_cooldown(now=1061.0) is False


# ---- Retry-After header parsing ----


def test_parse_retry_after_integer_seconds():
    assert altmetric_client._parse_retry_after("60") == 60
    assert altmetric_client._parse_retry_after("3600") == 3600
    assert altmetric_client._parse_retry_after("0") == 0


def test_parse_retry_after_rejects_negative():
    assert altmetric_client._parse_retry_after("-1") is None


def test_parse_retry_after_empty_or_none():
    assert altmetric_client._parse_retry_after(None) is None
    assert altmetric_client._parse_retry_after("") is None
    assert altmetric_client._parse_retry_after("   ") is None


def test_parse_retry_after_invalid_string():
    assert altmetric_client._parse_retry_after("not a date") is None
    assert altmetric_client._parse_retry_after("Mon, 99 Foo") is None


def test_parse_retry_after_http_date():
    # HTTP-date — parse to seconds-from-now. We control `now` so the
    # result is deterministic.
    import time as _time
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    now = _time.time()
    target = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=120)
    http_date = format_datetime(target, usegmt=True)
    out = altmetric_client._parse_retry_after(http_date, now=now)
    assert out is not None
    # Allow a few seconds slop for parsing + clock skew.
    assert 115 <= out <= 125


# ---- The resolve_via_unshorten_me integration ----


def test_resolve_skips_http_when_on_cooldown(monkeypatch):
    """The whole point: when cooldown is active, no HTTP call is made.
    Verify by making urlopen raise — if the function still returns
    cleanly, it means urlopen was never called."""

    def must_not_call(*args, **kwargs):
        raise AssertionError("urlopen should not be called during cooldown")

    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        must_not_call,
    )
    altmetric_client._trip_unshorten_me_cooldown(seconds=300)
    result = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert result.status == "failed_rate_limit"
    assert "cooldown" in (result.error or "").lower()
    assert "skipped HTTP call" in (result.error or "")


def test_resolve_trips_cooldown_on_http_429(monkeypatch):
    """A real 429 response trips the cooldown for subsequent calls."""

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        boom,
    )
    # Pre-call: not on cooldown.
    assert altmetric_client._unshorten_me_on_cooldown() is False
    result = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert result.status == "failed_rate_limit"
    # Post-call: cooldown active.
    assert altmetric_client._unshorten_me_on_cooldown() is True


def test_resolve_honors_retry_after_header_on_429(monkeypatch):
    """If the 429 response carries Retry-After, use that value not the default."""

    class _Headers:
        def get(self, k, default=None):
            return "120" if k == "Retry-After" else default

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=429,
            msg="Too Many Requests",
            hdrs=_Headers(),
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        boom,
    )
    altmetric_client.resolve_via_unshorten_me("http://t/x")
    remaining = altmetric_client._unshorten_me_cooldown_remaining()
    # Should be 120s (Retry-After value), not the 600s default.
    assert 115 <= remaining <= 125


def test_resolve_trips_cooldown_on_payload_rate_limit(monkeypatch):
    """If the API returns HTTP 200 with `success: false, error: 'rate limit ...'`,
    that ALSO trips the cooldown (no Retry-After available here)."""
    body = b'{"success": false, "error": "Rate limit exceeded for this hour"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    result = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert result.status == "failed_rate_limit"
    assert altmetric_client._unshorten_me_on_cooldown() is True
    # Default cooldown applies (no Retry-After in payload responses).
    remaining = altmetric_client._unshorten_me_cooldown_remaining()
    assert 595 <= remaining <= 605


def test_resolve_success_does_not_trip_cooldown(monkeypatch):
    """A successful resolve must NOT set the cooldown."""
    body = b'{"success": true, "resolved_url": "https://example.com/article"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    result = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert result.is_resolved
    assert altmetric_client._unshorten_me_on_cooldown() is False


def test_resolve_non_rate_limit_payload_failure_does_not_trip(monkeypatch):
    """A 'success: false' payload WITHOUT rate-limit wording is just
    a normal failure — don't trip the cooldown."""
    body = b'{"success": false, "error": "URL malformed"}'
    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(body),
    )
    result = altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert result.status == "failed_network"
    assert altmetric_client._unshorten_me_on_cooldown() is False


def test_resolve_non_429_http_error_does_not_trip(monkeypatch):
    """A 500 or 503 isn't a rate limit; don't trip the cooldown."""

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(
        altmetric_client.urllib.request,
        "urlopen",
        boom,
    )
    altmetric_client.resolve_via_unshorten_me("http://t/x")
    assert altmetric_client._unshorten_me_on_cooldown() is False
