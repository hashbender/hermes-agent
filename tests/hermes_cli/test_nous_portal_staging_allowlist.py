"""Regression tests for the Nous Portal host allowlist accepting staging.

Real incident (2026-07): a hosted agent provisioned on nous-account-service's
`staging` Vercel environment is stamped with
``portal_base_url=https://portal.staging-nousresearch.com`` in its bootstrap
``auth.json`` (see ``buildBootstrapAuthJson`` / ``env.BASE_URL`` in
nous-account-service). ``resolve_nous_access_token``'s host-allowlist guard
(``_NOUS_PORTAL_ALLOWED_HOSTS``) only recognised the production portal host,
so on the very first refresh it silently rewrote ``portal_base_url`` back to
prod and then replayed a staging-issued refresh token against the PROD token
endpoint. Prod correctly rejected it with ``invalid_grant``, which triggered
``_quarantine_nous_oauth_state`` and wiped the entire credential pool —
turning a simple config mismatch into a full relogin requirement on every
staging hosted-agent instance.

These tests verify the staging portal host is accepted by the allowlist and
is NOT silently rewritten to the production default.
"""

from __future__ import annotations

from hermes_cli.auth import (
    DEFAULT_NOUS_PORTAL_URL,
    _NOUS_PORTAL_ALLOWED_HOSTS,
)


class TestPortalAllowlistIncludesStaging:
    def test_prod_host_in_allowlist(self):
        assert "portal.nousresearch.com" in _NOUS_PORTAL_ALLOWED_HOSTS

    def test_staging_host_in_allowlist(self):
        """The staging Nous Portal host must be accepted — hosted agents on
        the `staging` NAS environment persist this host to auth.json and
        must be able to refresh against it without being force-rewritten to
        prod (which would replay a staging refresh token against the prod
        token endpoint and fail with invalid_grant)."""
        assert "portal.staging-nousresearch.com" in _NOUS_PORTAL_ALLOWED_HOSTS

    def test_localhost_dev_hosts_still_present(self):
        """Guard against accidentally dropping the existing dev entries
        while adding the staging host."""
        assert "localhost" in _NOUS_PORTAL_ALLOWED_HOSTS
        assert "127.0.0.1" in _NOUS_PORTAL_ALLOWED_HOSTS

    def test_default_portal_url_host_is_allowlisted(self):
        """Sanity check mirroring the inference-host allowlist's own sanity
        test: the default portal URL's host must itself be in the
        allowlist, otherwise every install would break."""
        from urllib.parse import urlparse

        host = urlparse(DEFAULT_NOUS_PORTAL_URL).hostname
        assert host in _NOUS_PORTAL_ALLOWED_HOSTS

    def test_attacker_host_not_allowlisted(self):
        """The allowlist must stay tight — only the documented hosts."""
        assert "attacker.com" not in _NOUS_PORTAL_ALLOWED_HOSTS
        assert "evil.portal.nousresearch.com" not in _NOUS_PORTAL_ALLOWED_HOSTS


class TestResolveAccessTokenAcceptsStagingPortal:
    """End-to-end: resolve_nous_access_token must refresh against a stored
    staging portal_base_url rather than silently rewriting it to prod."""

    def test_staging_portal_url_not_rewritten_on_refresh(self, monkeypatch, tmp_path):
        import json
        import logging

        import hermes_cli.auth as auth

        staging_portal = "https://portal.staging-nousresearch.com"
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_provider": "nous",
                    "providers": {
                        "nous": {
                            "portal_base_url": staging_portal,
                            "access_token": "expired-access",
                            "refresh_token": "staging-refresh",
                            "client_id": "hermes-cli-vps",
                            "expires_at": "2000-01-01T00:00:00+00:00",
                        }
                    },
                }
            )
        )

        seen_portal_urls = []

        def _fake_refresh(*, client, portal_base_url, client_id, refresh_token):
            seen_portal_urls.append(portal_base_url)
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }

        monkeypatch.setattr(auth, "_refresh_access_token", _fake_refresh)

        caplog_records = []
        with_caplog = logging.getLogger("hermes_cli.auth")
        handler = logging.Handler()
        handler.emit = lambda record: caplog_records.append(record.getMessage())
        with_caplog.addHandler(handler)
        try:
            auth.resolve_nous_access_token()
        finally:
            with_caplog.removeHandler(handler)

        assert seen_portal_urls == [staging_portal], (
            "refresh must target the stored staging portal, not be "
            f"silently rewritten to prod; saw {seen_portal_urls!r}"
        )
        assert not any(
            "ignoring invalid portal_base_url" in msg for msg in caplog_records
        ), "staging portal host must not trip the allowlist-rejection warning"
