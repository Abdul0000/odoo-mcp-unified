import hashlib
import hmac

from odoo_agent_server.config import settings
from odoo_agent_server.services import oauth_service


def test_default_token_never_signs_with_a_public_key(monkeypatch):
    monkeypatch.setattr(settings, "oauth_token_secret", None)
    monkeypatch.setattr(settings, "api_token", "change-me")
    key = oauth_service._token_secret()
    assert key != b"odoo-agent-development-token-secret"
    assert key != b"change-me"
    assert len(key) >= 32


def test_derived_key_is_stable_and_not_the_raw_api_token(monkeypatch):
    monkeypatch.setattr(settings, "oauth_token_secret", None)
    monkeypatch.setattr(settings, "api_token", "s3cret-admin-token")
    key = oauth_service._token_secret()
    assert key == oauth_service._token_secret()
    assert key != b"s3cret-admin-token"
    assert key == hmac.new(b"s3cret-admin-token", b"odoo-mcp/oauth-token-signing/v1", hashlib.sha256).digest()


def test_explicit_oauth_secret_wins(monkeypatch):
    monkeypatch.setattr(settings, "oauth_token_secret", "explicit")
    monkeypatch.setattr(settings, "api_token", "other")
    assert oauth_service._token_secret() == b"explicit"
