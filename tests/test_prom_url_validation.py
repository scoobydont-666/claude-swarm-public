"""F1: Prometheus URL SSRF guard tests.

Covers <hydra-project-path>/plans/claude-swarm-peripherals-dod-2026-04-18.md §Phase F1.

Rule: config-loaded prometheus_url must be http/https + loopback or private IP.
Named hosts and public IPs require explicit HEALTH_MONITOR_ALLOW_PUBLIC_PROM opt-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from health_monitor import _validate_prometheus_url


class TestSchemeValidation:
    def test_accepts_http_loopback(self):
        assert _validate_prometheus_url("http://127.0.0.1:9090") == "http://127.0.0.1:9090"

    def test_accepts_https_loopback(self):
        assert _validate_prometheus_url("https://127.0.0.1:9090")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="scheme 'file'"):
            _validate_prometheus_url("file:///etc/passwd")

    def test_rejects_gopher_scheme(self):
        with pytest.raises(ValueError, match="scheme 'gopher'"):
            _validate_prometheus_url("gopher://127.0.0.1/")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="scheme 'ftp'"):
            _validate_prometheus_url("ftp://127.0.0.1/")


class TestHostValidation:
    def test_rejects_empty_host(self):
        with pytest.raises(ValueError, match="no hostname"):
            _validate_prometheus_url("http:///path")

    def test_rejects_credentials_in_url(self):
        with pytest.raises(ValueError, match="must not embed credentials"):
            _validate_prometheus_url("http://user:pass@127.0.0.1:9090")

    def test_accepts_localhost(self):
        _validate_prometheus_url("http://localhost:9090")

    def test_accepts_rfc1918(self):
        _validate_prometheus_url("http://192.168.1.5:9090")
        _validate_prometheus_url("http://10.0.0.1:9090")
        _validate_prometheus_url("http://172.16.0.1:9090")

    def test_accepts_link_local(self):
        _validate_prometheus_url("http://169.254.1.1:9090")

    def test_rejects_public_ip_by_default(self):
        with pytest.raises(ValueError, match="public IP"):
            _validate_prometheus_url("http://8.8.8.8:9090")

    def test_rejects_named_host_by_default(self):
        with pytest.raises(ValueError, match="not an IP and not loopback"):
            _validate_prometheus_url("http://prometheus.svc.cluster.local:9090")


class TestPublicOptIn:
    def test_named_host_allowed_with_env(self, monkeypatch):
        monkeypatch.setenv("HEALTH_MONITOR_ALLOW_PUBLIC_PROM", "1")
        _validate_prometheus_url("http://prometheus.example.com:9090")

    def test_public_ip_allowed_with_env(self, monkeypatch):
        monkeypatch.setenv("HEALTH_MONITOR_ALLOW_PUBLIC_PROM", "1")
        _validate_prometheus_url("http://1.1.1.1:9090")

    def test_scheme_check_still_enforced_with_env(self, monkeypatch):
        """Opt-in only relaxes host rules, not scheme rules."""
        monkeypatch.setenv("HEALTH_MONITOR_ALLOW_PUBLIC_PROM", "1")
        with pytest.raises(ValueError, match="scheme 'file'"):
            _validate_prometheus_url("file:///etc/shadow")
