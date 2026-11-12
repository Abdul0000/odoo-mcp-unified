from __future__ import annotations
import hashlib
import hmac
import unittest
from deploy.webhook_receiver import valid_signature

class WebhookSignatureTests(unittest.TestCase):
    def test_accepts_valid_sha256_signature(self):
        secret=b"a"*32
        body=b'{"ref":"refs/heads/master"}'
        signature="sha256="+hmac.new(secret,body,hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature(body,signature,secret))
    def test_rejects_wrong_secret(self):
        body=b"{}"
        signature="sha256="+hmac.new(b"a"*32,body,hashlib.sha256).hexdigest()
        self.assertFalse(valid_signature(body,signature,b"b"*32))
    def test_rejects_malformed_signature(self):
        self.assertFalse(valid_signature(b"{}","sha1=abc",b"a"*32))
        self.assertFalse(valid_signature(b"{}","",b"a"*32))

if __name__ == "__main__":
    unittest.main()
