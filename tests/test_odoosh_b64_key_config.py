import base64

from odoo_agent_server.config import settings


def test_odoosh_private_key_b64_materializes_effective_key_path(tmp_path, monkeypatch):
    key_text = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----\n"
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "odoosh_ssh_key_path", None)
    monkeypatch.setattr(settings, "odoosh_ssh_private_key_b64", base64.b64encode(key_text.encode("utf-8")).decode("ascii"))

    key_path = settings.effective_odoosh_ssh_key_path

    assert key_path is not None
    assert key_path.exists()
    assert key_path.read_text(encoding="utf-8") == key_text
