#!/usr/bin/env python3
"""Signed GitHub push receiver; deployment is delegated to a fixed systemd unit."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = os.environ.get("DEPLOY_REPOSITORY", "Abdul0000/odoo-mcp-unified")
BRANCH = os.environ.get("DEPLOY_BRANCH", "master")
SECRET_FILE = Path(os.environ.get("DEPLOY_WEBHOOK_SECRET_FILE", "/etc/odoo-mcp/webhook.secret"))
DEPLOY_UNIT = os.environ.get("DEPLOY_SYSTEMD_UNIT", "odoo-mcp-deploy@")
MAX_BODY = 1_000_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_seen: set[str] = set()
_seen_lock = threading.Lock()


def valid_signature(body: bytes, supplied: str, secret: bytes) -> bool:
    if not supplied.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


class Handler(BaseHTTPRequestHandler):
    server_version = "OdooMCPDeploy/1.1"

    def respond(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.respond(404, {"error": "not_found"})
            return
        self.respond(200, {"status": "ok", "service": "github-deploy-receiver"})

    def do_POST(self) -> None:
        if self.path != "/github-webhook":
            self.respond(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.respond(400, {"error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_BODY:
            self.respond(413, {"error": "payload_too_large_or_empty"})
            return
        body = self.rfile.read(length)
        try:
            secret = SECRET_FILE.read_bytes().strip()
        except OSError:
            self.respond(503, {"error": "webhook_secret_not_configured"})
            return
        if len(secret) < 32 or not valid_signature(body, self.headers.get("X-Hub-Signature-256", ""), secret):
            self.respond(401, {"error": "invalid_signature"})
            return
        event = self.headers.get("X-GitHub-Event", "")
        delivery = self.headers.get("X-GitHub-Delivery", "")
        if event != "push":
            self.respond(202, {"status": "ignored", "reason": "unsupported_event"})
            return
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.respond(400, {"error": "invalid_json"})
            return
        if payload.get("repository", {}).get("full_name") != REPO or payload.get("ref") != f"refs/heads/{BRANCH}":
            self.respond(202, {"status": "ignored", "reason": "repository_or_branch_mismatch"})
            return
        commit = str(payload.get("after", ""))
        if not SHA_RE.fullmatch(commit) or commit == "0" * 40:
            self.respond(400, {"error": "invalid_commit"})
            return
        if not delivery or len(delivery) > 128:
            self.respond(400, {"error": "missing_delivery_id"})
            return
        with _seen_lock:
            if delivery in _seen:
                self.respond(202, {"status": "duplicate", "delivery": delivery})
                return
            _seen.add(delivery)
            if len(_seen) > 2000:
                _seen.clear()
                _seen.add(delivery)
        try:
            # The instance template accepts only a validated hexadecimal commit
            # as its sole argument. The unprivileged receiver cannot execute
            # arbitrary commands as root.
            subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", f"{DEPLOY_UNIT}{commit}.service"],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            with _seen_lock:
                _seen.discard(delivery)
            self.respond(503, {"error": "could_not_queue_deployment"})
            return
        self.respond(202, {"status": "accepted", "commit": commit, "delivery": delivery})

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    host = os.environ.get("DEPLOY_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("DEPLOY_WEBHOOK_PORT", "9001"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
