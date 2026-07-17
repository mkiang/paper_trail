"""M1 (2026-05-29): SSRF guard for the editor's server-side fetchers.

`url_helpers.is_safe_fetch_target` / `host_is_public` / `safe_urlopen`
reject non-public targets (loopback, private, link-local incl. the
169.254.169.254 cloud-metadata endpoint, reserved, multicast, 0.0.0.0)
and re-validate redirect hops. Tests are hermetic: DNS is monkeypatched,
no real network.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from cv_editor import url_helpers, url_title_fetcher


def _fake_getaddrinfo(ip):
    def gai(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port or 0))]

    return gai


# ----- _ip_is_blocked -------------------------------------------------


@pytest.mark.parametrize(
    "ip,blocked",
    [
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("172.16.3.4", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),  # cloud metadata
        ("0.0.0.0", True),
        ("::1", True),
        ("fc00::1", True),
        ("::ffff:127.0.0.1", True),  # ipv4-mapped loopback
        ("::ffff:169.254.169.254", True),  # ipv4-mapped metadata
        ("::ffff:10.0.0.1", True),  # ipv4-mapped private
        ("100.64.0.1", True),  # CGNAT (RFC 6598) — see _EXTRA_BLOCKED_V4
        ("100.127.255.255", True),  # CGNAT upper edge
        ("224.0.0.1", True),  # multicast
        ("not-an-ip", True),  # unparseable -> blocked
        ("8.8.8.8", False),
        ("100.128.0.1", False),  # just outside CGNAT range
        ("1.1.1.1", False),
        ("2606:4700:4700::1111", False),
    ],
)
def test_ip_is_blocked(ip, blocked):
    assert url_helpers._ip_is_blocked(ip) is blocked


# ----- host_is_public -------------------------------------------------


def test_host_is_public_true_for_public_ip(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))
    assert url_helpers.host_is_public("example.com") is True


def test_host_is_public_false_for_private_ip(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    assert url_helpers.host_is_public("internal.example") is False


def test_host_is_public_false_when_unresolvable(monkeypatch):
    import socket as _socket

    def boom(*a, **k):
        raise _socket.gaierror("nope")

    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", boom)
    assert url_helpers.host_is_public("nx.example") is False


def test_host_is_public_false_for_blank():
    assert url_helpers.host_is_public("") is False
    assert url_helpers.host_is_public(None) is False


def test_host_is_public_false_if_ANY_address_is_private(monkeypatch):
    # A host that resolves to both a public and a private address must be
    # rejected (the classic dual-record DNS-rebind setup).
    def gai(host, port, *a, **k):
        return [
            (2, 1, 6, "", ("8.8.8.8", port or 0)),
            (2, 1, 6, "", ("127.0.0.1", port or 0)),
        ]

    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", gai)
    assert url_helpers.host_is_public("dual.example") is False


# ----- is_safe_fetch_target -------------------------------------------


def test_is_safe_fetch_target_rejects_bad_scheme_without_dns():
    # Scheme failure short-circuits before DNS.
    assert url_helpers.is_safe_fetch_target("ftp://example.com/x") is False
    assert url_helpers.is_safe_fetch_target("file:///etc/passwd") is False
    assert url_helpers.is_safe_fetch_target("javascript:alert(1)") is False


def test_is_safe_fetch_target_blocks_private_host(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    assert url_helpers.is_safe_fetch_target("https://looks-fine.example/x") is False


def test_is_safe_fetch_target_allows_public_host(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert url_helpers.is_safe_fetch_target("https://example.com/x") is True


# ----- is_safe_fetch_url (unchanged scheme-only behavior) -------------


def test_is_safe_fetch_url_is_scheme_only_and_unchanged():
    # The cheap scheme guard must NOT do DNS (it's used in non-fetch
    # contexts); it still returns True for a loopback host.
    assert url_helpers.is_safe_fetch_url("http://127.0.0.1/x") is True
    assert url_helpers.is_safe_fetch_url("https://example.com/x") is True
    assert url_helpers.is_safe_fetch_url("ftp://example.com/x") is False
    assert url_helpers.is_safe_fetch_url("not a url") is False


# ----- safe_urlopen ----------------------------------------------------


def test_safe_urlopen_raises_on_blocked_target(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    req = urllib.request.Request("http://localhost/x")
    with pytest.raises(urllib.error.URLError):
        url_helpers.safe_urlopen(req, timeout=1)


def test_safe_urlopen_accepts_string_url(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.1"))
    with pytest.raises(urllib.error.URLError):
        url_helpers.safe_urlopen("http://internal/x", timeout=1)


# ----- redirect handler -----------------------------------------------


def test_redirect_handler_blocks_redirect_to_private(monkeypatch):
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.9"))
    handler = url_helpers._SSRFSafeRedirectHandler()
    req = urllib.request.Request("https://public.example/start")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(
            req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://10.0.0.9/internal",
        )


# ----- malformed port must not leak ValueError ------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:99999/",  # out of range
        "http://8.8.8.8:abc/",  # non-numeric
        "http://8.8.8.8:-1/",  # negative
    ],
)
def test_is_safe_fetch_target_bad_port_returns_false_not_raises(url):
    # urlsplit().port raises ValueError on these; the guard must catch it
    # and return False (fail closed), never leak the exception.
    assert url_helpers.is_safe_fetch_target(url) is False


# ----- end-to-end: the fetcher itself rejects a private host ----------


def test_fetch_title_rejects_private_host_end_to_end(monkeypatch):
    # Wiring check: fetch_title -> safe_urlopen -> host check. Patch DNS
    # (not safe_urlopen) so the real guard runs and rejects the target.
    monkeypatch.setattr(url_helpers.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert url_title_fetcher.fetch_title("http://localhost:1234/admin") is None
