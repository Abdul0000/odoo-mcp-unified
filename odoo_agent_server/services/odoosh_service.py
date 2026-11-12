from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import get_odoo_clients, settings
from .client_resolver_service import ClientResolverService, ResolvedClient
from ..schemas import CommandResult, FileContent, OdooResult, OdooShLogName


# ---------------------------------------------------------------------------
# Fix 5: SSH ControlMaster connection pool.
# One persistent SSH master per (host, port, user) reuses the mux socket for
# all subsequent calls — eliminating the 2-3 sec TCP/crypto handshake per call.
# ---------------------------------------------------------------------------

class _SshMasterPool:
    """Singleton pool of SSH ControlMaster background processes, one per target."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._masters: dict[str, tuple[subprocess.Popen, Path]] = {}
        self._tmpdir = Path(tempfile.mkdtemp(prefix="odoosh_ssh_pool_"))

    def _key(self, host: str, port: int, user: str, key_path: Path | None = None) -> str:
        # Different clients may share host/user while using different keys.
        # Never reuse a multiplexed authenticated session across those clients.
        key_identity = str(key_path.resolve()) if key_path else "default-key"
        return f"{user}@{host}:{port}:{key_identity}"

    def _socket_path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(":", "_").replace("@", "_")
        return self._tmpdir / f"cm_{safe}.sock"

    def ensure_master(
        self,
        host: str,
        port: int,
        user: str,
        key_path: Path | None,
        timeout: int = 15,
    ) -> Path | None:
        """Ensure a ControlMaster is alive; return socket path or None on failure."""
        key = self._key(host, port, user, key_path)
        with self._lock:
            entry = self._masters.get(key)
            if entry is not None:
                proc, sock = entry
                if proc.poll() is None and sock.exists():
                    return sock  # healthy
            sock = self._socket_path(key)
            sock.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                # Do not use -f here: it forks SSH and makes the Popen parent
                # exit, so the pool incorrectly treats a live master as dead.
                "ssh", "-N", "-M",
                "-S", str(sock),
                "-p", str(port),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
            ]
            if key_path:
                cmd.extend(["-i", str(key_path), "-o", "IdentitiesOnly=yes"])
            cmd.append(f"{user}@{host}")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if sock.exists():
                        self._masters[key] = (proc, sock)
                        return sock
                    time.sleep(0.2)
            except Exception:
                pass
            return None

    def mux_args(self, sock: Path | None) -> list[str]:
        """Extra ssh args to route a call through an existing ControlMaster."""
        if sock and sock.exists():
            return ["-o", f"ControlPath={sock}", "-o", "ControlMaster=no"]
        return []

    def close_master(self, host: str, port: int, user: str, key_path: Path | None = None) -> None:
        key = self._key(host, port, user, key_path)
        with self._lock:
            entry = self._masters.pop(key, None)
            if entry:
                proc, sock = entry
                proc.terminate()
                sock.unlink(missing_ok=True)


_SSH_POOL = _SshMasterPool()


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    port: int = 22
    key_path: Path | None = None


class OdooShService:
    def __init__(self, client: ResolvedClient | None = None) -> None:
        self.client = client

    FAILURE_PATTERNS = [
        "not installable",
        "inconsistent states",
        "Traceback",
        "CRITICAL",
        "ParseError",
        "ValidationError",
        "ModuleNotFoundError",
        "ImportError",
        "Failed to load registry",
    ]

    def _active_client(self) -> ResolvedClient | None:
        if self.client:
            return self.client
        try:
            return ClientResolverService().resolve_client(require_complete=False)
        except Exception:
            return None

    def _allow_legacy_global_settings(self) -> bool:
        return self.client is None and len(get_odoo_clients()) <= 1

    def _target(self) -> SshTarget:
        client = self._active_client()
        if client is None and not self._allow_legacy_global_settings():
            raise ValueError("client_name is required when multiple ODOO_CLIENTS entries are configured")
        legacy = self._allow_legacy_global_settings()
        host = (client.odoosh_ssh_host if client else None) or ((os.environ.get("ODOOSH_SSH_HOST") or settings.odoosh_ssh_host) if legacy else None)
        if not host:
            raise ValueError("ODOOSH_SSH_HOST is not configured; add odoosh_ssh_host to the selected ODOO_CLIENTS entry or set global ODOOSH_SSH_HOST for single-client mode")
        user = (client.odoosh_ssh_user if client else None) or ((os.environ.get("ODOOSH_SSH_USER") or settings.odoosh_ssh_user) if legacy else None) or "odoo"
        port = (client.odoosh_ssh_port if client else None) or int(((os.environ.get("ODOOSH_SSH_PORT") or settings.odoosh_ssh_port) if legacy else None) or 22)
        key_path = self._effective_ssh_key_path(client)
        return SshTarget(
            host=host,
            user=user,
            port=int(port),
            key_path=key_path,
        )

    def _effective_ssh_key_path(self, client: ResolvedClient | None) -> Path | None:
        explicit = (client.odoosh_ssh_key_path if client else None) or ((os.environ.get("ODOOSH_SSH_KEY_PATH") or settings.odoosh_ssh_key_path) if self._allow_legacy_global_settings() else None)
        if explicit:
            return Path(explicit).expanduser().resolve()
        key_b64 = (client.odoosh_ssh_private_key_b64 if client else None) or ((os.environ.get("ODOOSH_SSH_PRIVATE_KEY_B64") or settings.odoosh_ssh_private_key_b64) if self._allow_legacy_global_settings() else None)
        if not key_b64:
            return None
        key_dir = settings.state_path / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key_b64.encode("ascii")).hexdigest()[:16]
        key_path = key_dir / f"odoosh_key_{digest}"
        if not key_path.exists():
            raw = base64.b64decode(key_b64.encode("ascii"))
            key_path.write_bytes(raw)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        return key_path

    def _db_name(self) -> str | None:
        client = self._active_client()
        return (client.odoosh_db_name if client else None) or (client.odoo_db if client else None) or ((os.environ.get("ODOOSH_DB_NAME") or settings.odoosh_db_name) if self._allow_legacy_global_settings() else None)

    def _odoo_path(self) -> str:
        client = self._active_client()
        return (client.odoosh_odoo_path if client else None) or ((os.environ.get("ODOOSH_ODOO_PATH") or settings.odoosh_odoo_path) if self._allow_legacy_global_settings() else settings.odoosh_odoo_path)

    def _user_addons_path(self) -> str:
        client = self._active_client()
        return (client.odoosh_user_addons_path if client else None) or ((os.environ.get("ODOOSH_USER_ADDONS_PATH") or settings.odoosh_user_addons_path) if self._allow_legacy_global_settings() else settings.odoosh_user_addons_path)

    def _base_ssh_command(self, target: SshTarget) -> list[str]:
        # Fix 5: try to reuse a ControlMaster socket; fall back to a fresh connection.
        sock = _SSH_POOL.ensure_master(
            host=target.host,
            port=target.port,
            user=target.user,
            key_path=target.key_path,
        )
        command = [
            "ssh",
            "-p", str(target.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
        ]
        if target.key_path:
            command.extend(["-i", str(target.key_path), "-o", "IdentitiesOnly=yes"])
        # Inject mux args when a master socket is available
        command.extend(_SSH_POOL.mux_args(sock))
        command.append(f"{target.user}@{target.host}")
        return command

    def run_shell(self, remote_script: str, timeout_seconds: int = 60) -> CommandResult:
        target = self._target()
        # Send the script over stdin instead of embedding it in the remote
        # command argument. OpenSSH joins remote argv into one shell command;
        # nested quotes/heredocs can otherwise become an unterminated string.
        command = self._base_ssh_command(target) + ["sh", "-s"]
        script_input = remote_script if remote_script.endswith("\n") else remote_script + "\n"
        try:
            completed = subprocess.run(
                command,
                input=script_input,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(
                command=self._redacted_command(command),
                cwd="odoosh:ssh",
                exit_code=completed.returncode,
                stdout=completed.stdout[-20000:],
                stderr=completed.stderr[-20000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=self._redacted_command(command),
                cwd="odoosh:ssh",
                exit_code=124,
                stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                command=self._redacted_command(command),
                cwd="odoosh:ssh",
                exit_code=127,
                stderr=f"SSH executable not found: {exc}",
            )

    def health(self) -> CommandResult:
        script = "set -e; echo ODOOSH_SSH_OK; hostname; pwd; test -d ~/src/user && echo SRC_USER_OK; test -d ~/logs && echo LOGS_OK"
        return self.run_shell(script, timeout_seconds=30)

    def git_status(self) -> CommandResult:
        script = "set -e; cd ~/src/user; git status --short --branch; git rev-parse --abbrev-ref HEAD || true; git rev-parse HEAD"
        return self.run_shell(script, timeout_seconds=30)

    def deployed_sha(self) -> CommandResult:
        script = "set -e; cd ~/src/user; git rev-parse HEAD"
        return self.run_shell(script, timeout_seconds=30)

    # NON-BLOCKING single check — MCP agents poll this instead of blocking wait
    @staticmethod
    def adaptive_poll_interval(attempt: int) -> float:
        """Adaptive polling: fast early, slower later to reduce SSH calls."""
        if attempt < 3:
            return 5.0   # first 3 checks: every 5s
        elif attempt < 8:
            return 10.0  # next 5 checks: every 10s
        else:
            return 15.0  # remaining: every 15s

    def check_commit_deployed(self, expected_sha: str) -> CommandResult:
        """Return immediately with deployed=true/false. Never blocks.
        MCP agents should call this in a polling loop with short delays."""
        result = self.deployed_sha()
        current = result.stdout.strip()
        deployed = (
            result.exit_code == 0
            and bool(current)
            and (current.startswith(expected_sha) or expected_sha.startswith(current))
        )
        payload = json.dumps({
            "deployed": deployed,
            "current_sha": current or "unknown",
            "expected_sha": expected_sha,
            "message": "Source checkout has the pushed SHA. Verify Odoo.sh build/test success before module update." if deployed
                       else "Not yet in source checkout. Use adaptive_poll_interval: attempts 0-2→wait 5s, 3-7→wait 10s, 8+→wait 15s, then call again.",
        })
        return CommandResult(
            command=["odoosh.check_commit", expected_sha],
            cwd="odoosh:ssh",
            exit_code=0 if deployed else 1,
            stdout=payload,
        )

    @staticmethod
    def _check_result_deployed(result: CommandResult) -> bool:
        try:
            payload = json.loads(result.stdout or "{}")
            return result.exit_code == 0 and bool(payload.get("deployed"))
        except Exception:
            return result.exit_code == 0 and '"deployed": true' in (result.stdout or "").lower()

    def poll_deployed_commit(
        self,
        expected_sha: str,
        timeout_seconds: int = 300,
        interval_seconds: float = 10.0,
        progress_cb=None,
    ) -> CommandResult:
        """Poll Odoo.sh until ``expected_sha`` is active in ``~/src/user``.

        This shared polling loop is intentionally separate from ``wait_for_commit``
        because post-push automation needs structured attempt counts, elapsed
        timing, and resumable timeout metadata in the background job record.
        ``progress_cb`` receives one dict per poll attempt so callers can append
        live job steps without blocking the original HTTP response.
        """
        expected = expected_sha.strip()
        timeout_seconds = max(int(timeout_seconds), 0)
        interval_seconds = max(float(interval_seconds), 0.0)
        started = time.monotonic()
        deadline = started + timeout_seconds
        attempts: list[dict[str, object]] = []
        attempt = 0

        while True:
            attempt += 1
            adaptive_interval = (
                max(interval_seconds, OdooShService.adaptive_poll_interval(attempt - 1))
                if timeout_seconds >= 60
                else interval_seconds
            )
            check_result = self.check_commit_deployed(expected)
            deployed = OdooShService._check_result_deployed(check_result)
            elapsed_seconds = round(time.monotonic() - started, 3)
            attempt_payload = {
                "attempt": attempt,
                "exit_code": check_result.exit_code,
                "stdout": check_result.stdout,
                "stderr": check_result.stderr,
                "deployed": deployed,
                "expected_sha": expected,
                "poll_interval_seconds": adaptive_interval,
                "elapsed_seconds": elapsed_seconds,
            }
            attempts.append(attempt_payload)
            if progress_cb is not None:
                try:
                    progress_cb(attempt_payload)
                except Exception:
                    pass

            if deployed:
                return check_result.model_copy(update={
                    "command": ["odoosh.check_commit_poll", expected],
                    "stdout": json.dumps({
                        "deployed": True,
                        "expected_sha": expected,
                        "attempts": attempt,
                        "poll_interval_seconds": adaptive_interval,
                        "elapsed_seconds": elapsed_seconds,
                        "message": "Odoo.sh deployed the pushed commit. Proceeding to module upgrade.",
                        "last_check": check_result.stdout,
                    }),
                })

            now = time.monotonic()
            if now >= deadline:
                elapsed_seconds = round(now - started, 3)
                return CommandResult(
                    command=["odoosh.check_commit_poll", expected],
                    cwd="odoosh:ssh",
                    exit_code=124,
                    stdout=json.dumps({
                        "deployed": False,
                        "expected_sha": expected,
                        "attempts": attempt,
                        "poll_interval_seconds": adaptive_interval,
                        "elapsed_seconds": elapsed_seconds,
                        "timeout_seconds": timeout_seconds,
                        "recent_checks": attempts[-10:],
                    }),
                    stderr=f"Odoo.sh did not deploy pushed commit {expected} within {timeout_seconds}s.",
                    timed_out=True,
                )

            sleep_for = min(adaptive_interval, max(deadline - now, 0.0))
            if sleep_for > 0:
                time.sleep(sleep_for)

    # Backward-compatible wrapper for legacy internal/direct routes. Keep one
    # polling implementation so timing, timeout metadata, and SSH behavior do
    # not diverge between wait_for_commit and safe deploy.
    def wait_for_commit(self, expected_sha: str, timeout_seconds: int | None = None, interval_seconds: int | None = None) -> CommandResult:
        return self.poll_deployed_commit(
            expected_sha,
            timeout_seconds or settings.odoosh_wait_timeout_seconds,
            float(interval_seconds or settings.odoosh_wait_interval_seconds),
        )


    def _source_root_path(self, base: str) -> str:
        value = (base or "odoo_addons").strip()
        if value == "odoo_addons":
            return f"{self._odoo_path().rstrip('/')}/addons"
        if value == "enterprise":
            return "/home/odoo/src/enterprise"
        if value == "user":
            return self._user_addons_path().rstrip("/")
        raise ValueError("Unknown Odoo.sh source base")

    def _safe_source_relative_path(self, relative_path: str) -> str:
        rel = str(relative_path or "").replace("\\", "/").strip("/")
        if rel.startswith("../") or "/../" in rel or rel == ".git" or rel.startswith(".git/"):
            raise ValueError("Path must stay inside the selected Odoo.sh source root")
        return rel

    def read_core_file(self, base: str, relative_path: str, max_bytes: int = 500000) -> FileContent:
        """Read exact Odoo.sh installed source under /home/odoo/src/odoo/addons, enterprise, or user."""
        root = self._source_root_path(base)
        rel = self._safe_source_relative_path(relative_path)
        safe_max = max(1, min(int(max_bytes), 2_000_000))
        script = (
            "python3 - <<'PY'\n"
            "import base64, json, os\n"
            f"base = {root!r}\n"
            f"rel = {rel!r}\n"
            f"limit = {safe_max!r}\n"
            "base_real = os.path.realpath(base)\n"
            "target = os.path.realpath(os.path.join(base_real, rel))\n"
            "if not (target == base_real or target.startswith(base_real + os.sep)):\n"
            "    print(json.dumps({'ok': False, 'error': 'path escapes source root', 'path': target, 'base': base_real}))\n"
            "elif not os.path.isfile(target):\n"
            "    print(json.dumps({'ok': False, 'error': 'file not found', 'path': target, 'base': base_real}))\n"
            "else:\n"
            "    size = os.path.getsize(target)\n"
            "    with open(target, 'rb') as fh:\n"
            "        raw = fh.read(limit + 1)\n"
            "    truncated = len(raw) > limit\n"
            "    raw = raw[:limit]\n"
            "    print(json.dumps({'ok': True, 'path': target, 'relative_path': rel, 'size': size, 'truncated': truncated, 'content_b64': base64.b64encode(raw).decode('ascii'), 'base': base_real}))\n"
            "PY"
        )
        result = self.run_shell(script, timeout_seconds=30)
        fallback_path = f"{root.rstrip('/')}/{rel}" if rel else root
        if result.exit_code != 0:
            return FileContent(path=fallback_path, content="", size=0, extra={"source": "odoosh_core", "status": "failed", "base": base, "ssh": result.model_dump(mode="json")})
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except Exception:
            return FileContent(path=fallback_path, content="", size=0, extra={"source": "odoosh_core", "status": "failed", "base": base, "raw": result.stdout[-4000:]})
        if not payload.get("ok"):
            return FileContent(path=str(payload.get("path") or fallback_path), content="", size=0, extra={"source": "odoosh_core", "status": "missing", "error": payload.get("error"), "base": payload.get("base") or root, "relative_path": rel})
        import base64
        raw = base64.b64decode(str(payload.get("content_b64") or "").encode("ascii"))
        text = raw.decode("utf-8", errors="replace")
        return FileContent(path=str(payload.get("path")), content=text, size=int(payload.get("size") or len(raw)), extra={"source": "odoosh_core", "relative_path": payload.get("relative_path") or rel, "truncated": bool(payload.get("truncated")), "base": payload.get("base") or root})

    def list_core_files(self, base: str, relative_path: str = "", max_items: int = 300) -> dict:
        """List exact Odoo.sh installed source files under /home/odoo/src/odoo/addons, enterprise, or user."""
        root = self._source_root_path(base)
        rel = self._safe_source_relative_path(relative_path)
        safe_max = max(1, min(int(max_items), 3000))
        script = (
            "python3 - <<'PY'\n"
            "import json, os\n"
            f"base = {root!r}\n"
            f"rel = {rel!r}\n"
            f"limit = {safe_max!r}\n"
            "base_real = os.path.realpath(base)\n"
            "target = os.path.realpath(os.path.join(base_real, rel))\n"
            "if not (target == base_real or target.startswith(base_real + os.sep)):\n"
            "    print(json.dumps({'found': False, 'error': 'path escapes source root', 'base': base_real, 'target': target, 'files': []}))\n"
            "elif not os.path.isdir(target):\n"
            "    print(json.dumps({'found': False, 'error': 'directory not found', 'base': base_real, 'target': target, 'files': []}))\n"
            "else:\n"
            "    files = []\n"
            "    for current, dirs, names in os.walk(target):\n"
            "        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']\n"
            "        for name in names:\n"
            "            full = os.path.join(current, name)\n"
            "            try:\n"
            "                st = os.stat(full)\n"
            "            except OSError:\n"
            "                continue\n"
            "            files.append({'path': full, 'relative_path': os.path.relpath(full, base_real).replace(os.sep, '/'), 'size': st.st_size})\n"
            "            if len(files) >= limit:\n"
            "                break\n"
            "        if len(files) >= limit:\n"
            "            break\n"
            "    print(json.dumps({'found': True, 'base': base_real, 'target': target, 'count': len(files), 'truncated': len(files) >= limit, 'files': files}))\n"
            "PY"
        )
        result = self.run_shell(script, timeout_seconds=60)
        if result.exit_code != 0:
            return {"found": False, "source": "odoosh_core", "base": base, "relative_path": rel, "error": result.stderr or result.stdout, "files": []}
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except Exception:
            return {"found": False, "source": "odoosh_core", "base": base, "relative_path": rel, "error": "Unable to parse SSH output", "raw": result.stdout[-4000:], "files": []}
        payload["source"] = "odoosh_core"
        payload["requested_base"] = base
        payload["relative_path"] = rel
        return payload

    def verify_module_manifest(self, module_name: str) -> CommandResult:
        safe_module = shlex.quote(module_name)
        user_addons = shlex.quote(self._user_addons_path())
        script = (
            "set -e; "
            f"test -f {user_addons}/{safe_module}/__manifest__.py; "
            f"grep -E \"['\\\"']installable['\\\"'] *: *True\" {user_addons}/{safe_module}/__manifest__.py >/dev/null; "
            f"echo MODULE_MANIFEST_OK:{module_name}"
        )
        return self.run_shell(script, timeout_seconds=30)

    def _safe_user_relative_path(self, relative_path: str) -> str:
        rel = str(relative_path).replace("\\", "/").strip("/")
        if (
            not rel
            or rel.startswith("../")
            or "/../" in rel
            or rel == ".git"
            or rel.startswith(".git/")
        ):
            raise ValueError("Path must stay inside /home/odoo/src/user")
        return rel

    def read_user_file(self, relative_path: str, max_bytes: int = 300000) -> FileContent:
        """Read the actual deployed custom-source file from Odoo.sh /home/odoo/src/user."""
        rel = self._safe_user_relative_path(relative_path)
        safe_max = max(1, min(int(max_bytes), 1_000_000))
        script = (
            "python3 - <<'PY'\n"
            "import base64, json, os\n"
            f"base = {self._user_addons_path()!r}\n"
            f"rel = {rel!r}\n"
            f"limit = {safe_max!r}\n"
            "base_real = os.path.realpath(base)\n"
            "target = os.path.realpath(os.path.join(base_real, rel))\n"
            "if not (target == base_real or target.startswith(base_real + os.sep)):\n"
            "    print(json.dumps({'ok': False, 'error': 'path escapes /home/odoo/src/user', 'path': target}))\n"
            "elif not os.path.isfile(target):\n"
            "    print(json.dumps({'ok': False, 'error': 'file not found', 'path': target}))\n"
            "else:\n"
            "    size = os.path.getsize(target)\n"
            "    with open(target, 'rb') as fh:\n"
            "        raw = fh.read(limit + 1)\n"
            "    truncated = len(raw) > limit\n"
            "    raw = raw[:limit]\n"
            "    print(json.dumps({'ok': True, 'path': target, 'relative_path': rel, 'size': size, 'truncated': truncated, 'content_b64': base64.b64encode(raw).decode('ascii')}))\n"
            "PY"
        )
        result = self.run_shell(script, timeout_seconds=30)
        if result.exit_code != 0:
            return FileContent(
                path=f"{self._user_addons_path().rstrip('/')}/{rel}",
                content="",
                size=0,
                extra={"source": "odoosh_user", "status": "failed", "ssh": result.model_dump(mode="json")},
            )
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except Exception:
            return FileContent(
                path=f"{self._user_addons_path().rstrip('/')}/{rel}",
                content="",
                size=0,
                extra={"source": "odoosh_user", "status": "failed", "raw": result.stdout[-4000:]},
            )
        if not payload.get("ok"):
            return FileContent(
                path=str(payload.get("path") or f"{self._user_addons_path().rstrip('/')}/{rel}"),
                content="",
                size=0,
                extra={"source": "odoosh_user", "status": "missing", "error": payload.get("error"), "relative_path": rel},
            )
        import base64

        raw = base64.b64decode(str(payload.get("content_b64") or "").encode("ascii"))
        text = raw.decode("utf-8", errors="replace")
        return FileContent(
            path=str(payload.get("path")),
            content=text,
            size=int(payload.get("size") or len(raw)),
            extra={
                "source": "odoosh_user",
                "relative_path": payload.get("relative_path") or rel,
                "truncated": bool(payload.get("truncated")),
                "base": self._user_addons_path(),
            },
        )

    def list_user_files(self, module_name: str, subdir: str = "", max_items: int = 200) -> dict:
        """List actual deployed files from Odoo.sh /home/odoo/src/user/<module>."""
        safe_module = self._safe_user_relative_path(module_name)
        safe_subdir = self._safe_user_relative_path(f"{module_name}/{subdir}") if subdir else safe_module
        safe_max = max(1, min(int(max_items), 2000))
        script = (
            "python3 - <<'PY'\n"
            "import json, os\n"
            f"base = {self._user_addons_path()!r}\n"
            f"module_name = {safe_module!r}\n"
            f"rel = {safe_subdir!r}\n"
            f"limit = {safe_max!r}\n"
            "base_real = os.path.realpath(base)\n"
            "target = os.path.realpath(os.path.join(base_real, rel))\n"
            "module_path = os.path.realpath(os.path.join(base_real, module_name))\n"
            "if not (target == base_real or target.startswith(base_real + os.sep)):\n"
            "    print(json.dumps({'found': False, 'error': 'path escapes /home/odoo/src/user', 'module_name': module_name, 'files': []}))\n"
            "elif not os.path.isdir(target):\n"
            "    print(json.dumps({'found': False, 'error': 'directory not found', 'module_name': module_name, 'module_path': module_path, 'target': target, 'files': []}))\n"
            "else:\n"
            "    files = []\n"
            "    for current, dirs, names in os.walk(target):\n"
            "        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']\n"
            "        for name in names:\n"
            "            full = os.path.join(current, name)\n"
            "            try:\n"
            "                st = os.stat(full)\n"
            "            except OSError:\n"
            "                continue\n"
            "            files.append({'path': full, 'relative_path': os.path.relpath(full, base_real).replace(os.sep, '/'), 'size': st.st_size})\n"
            "            if len(files) >= limit:\n"
            "                break\n"
            "        if len(files) >= limit:\n"
            "            break\n"
            "    print(json.dumps({'found': True, 'module_name': module_name, 'module_path': module_path, 'target': target, 'base': base_real, 'files': files, 'total_found': len(files), 'truncated': len(files) >= limit}))\n"
            "PY"
        )
        result = self.run_shell(script, timeout_seconds=45)
        if result.exit_code != 0:
            return {"found": False, "module_name": module_name, "files": [], "error": result.stderr or result.stdout, "ssh": result.model_dump(mode="json")}
        try:
            return json.loads((result.stdout or "").strip().splitlines()[-1])
        except Exception:
            return {"found": False, "module_name": module_name, "files": [], "error": "Could not parse Odoo.sh file list", "raw": result.stdout[-4000:]}

    def verify_user_file_hashes(self, expected_hashes: dict[str, str]) -> CommandResult:
        """Verify changed user-addon files on Odoo.sh match the pushed checkout.

        Odoo.sh can briefly show a matching git SHA while the build still fails
        or while the live user source is not the code the agent just wrote. This
        verifies the concrete changed files under /home/odoo/src/user before any
        install/upgrade step runs.
        """
        if not expected_hashes:
            return CommandResult(
                command=["odoosh.verify_user_file_hashes"],
                cwd="odoosh:ssh",
                exit_code=0,
                stdout=json.dumps({"verified": True, "count": 0, "message": "No changed user files to verify."}),
            )

        entries: list[tuple[str, str]] = []
        for rel_path, expected_hash in expected_hashes.items():
            rel = str(rel_path).replace("\\", "/").strip("/")
            digest = str(expected_hash).strip().lower()
            if (
                not rel
                or rel.startswith("../")
                or "/../" in rel
                or rel == ".git"
                or rel.startswith(".git/")
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                return CommandResult(
                    command=["odoosh.verify_user_file_hashes"],
                    cwd="odoosh:ssh",
                    exit_code=2,
                    stderr=f"Unsafe or invalid user file hash entry: {rel_path}",
                )
            entries.append((rel, digest))

        user_addons = self._user_addons_path().rstrip("/")
        script_lines = [
            "set +e",
            "failures=0",
            "echo ODOOSH_USER_FILE_VERIFY_START",
        ]
        for rel, expected_hash in entries:
            remote_path = f"{user_addons}/{rel}"
            script_lines.extend([
                f"rel={shlex.quote(rel)}",
                f"file={shlex.quote(remote_path)}",
                f"expected={shlex.quote(expected_hash)}",
                'if [ ! -f "$file" ]; then echo "MISSING $rel"; failures=1; else '
                "actual=$(sha256sum \"$file\" 2>/dev/null | awk '{print $1}'); "
                'if [ "$actual" = "$expected" ]; then echo "OK $rel $actual"; '
                'else echo "MISMATCH $rel expected=$expected actual=$actual"; failures=1; fi; fi',
            ])
        script_lines.extend([
            'if [ "$failures" -eq 0 ]; then echo ODOOSH_USER_FILE_VERIFY_OK; else echo ODOOSH_USER_FILE_VERIFY_FAILED; fi',
            "exit $failures",
        ])
        return self.run_shell("; ".join(script_lines), timeout_seconds=45)

    def verify_deployed_changes(
        self,
        expected_hashes: dict[str, str],
        *,
        module_name: str,
        verify_manifest: bool = False,
    ) -> CommandResult:
        """Verify deployed files and manifest through the same proven helpers used by resume flows.

        Keeping the checks sequential avoids nested shell-quoting differences while
        preserving the exact verification semantics and failure markers.
        """
        file_result = self.verify_user_file_hashes(expected_hashes)
        if file_result.exit_code != 0 or not verify_manifest:
            return file_result
        return self.verify_module_manifest(module_name)

    def tail_log(self, log_name: OdooShLogName, tail_lines: int = 200) -> CommandResult:
        safe_lines = max(20, min(tail_lines, 2000))
        filename = f"~/logs/{log_name.value}.log"
        script = f"test -f {filename} && tail -n {safe_lines} {filename} || echo 'Log not found: {filename}'"
        return self.run_shell(script, timeout_seconds=30)

    def grep_errors(self, tail_lines: int = 150) -> CommandResult:
        """Return only recent ERROR/CRITICAL records and complete tracebacks.

        Filtering happens on Odoo.sh before output is transferred. INFO and
        WARNING records are skipped, while non-timestamped traceback frames and
        exception lines following an error remain available for diagnosis.
        """
        safe_lines = max(100, min(tail_lines, 2000))
        script = (
            "set +e; "
            "for f in ~/logs/*.log; do "
            "  [ -f \"$f\" ] || continue; "
            f"  tail -n {safe_lines} \"$f\" | awk -v file=\"$f\" '"
            "function emit(line) { "
            "  if (!emitted) { print \"__ODOO_MCP_LOG_FILE__:\" file; emitted=1 } "
            "  print line "
            "} "
            "/Traceback \\(most recent call last\\)/ { in_trace=1; emit($0); next } "
            "in_trace && /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][[:space:]]/ { in_trace=0 } "
            "in_trace { emit($0); next } "
            "/(^|[[:space:]])(ERROR|CRITICAL)([[:space:]:]|$)/ { emit($0) }"
            "'; "
            "done; exit 0"
        )
        result = self.run_shell(script, timeout_seconds=45)
        if result.exit_code != 0:
            return result
        blocks = self._extract_error_blocks(result.stdout)
        summary = "\n\n".join(blocks[-20:]) if blocks else "No recent traceback/error blocks found in tailed Odoo.sh logs."
        return result.model_copy(update={"stdout": summary[-20000:]})

    def recent_build_failure(self, since_epoch: int, tail_lines: int = 150) -> CommandResult:
        """Detect Odoo.sh build/runtime failures logged after ``since_epoch``.

        Filtering is applied to each timestamped log record rather than the log
        file mtime, so failures from an earlier build cannot block a newly
        observed deployment of the expected SHA.
        """
        safe_lines = max(100, min(tail_lines, 2000))
        safe_since = max(0, int(since_epoch))
        cutoff_text = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(safe_since))
        script = (
            "set +e; "
            "for f in ~/logs/*.log; do "
            "  [ -f \"$f\" ] || continue; "
            f"  tail -n {safe_lines} \"$f\" | awk -v file=\"$f\" -v cutoff=\"{cutoff_text}\" '"
            "function emit(line) { "
            "  if (!emitted) { print \"__ODOO_MCP_LOG_FILE__:\" file; emitted=1 } "
            "  print line "
            "} "
            "function timestamped(line) { return line ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][[:space:]][0-9][0-9]:[0-9][0-9]:[0-9][0-9]/ } "
            "timestamped($0) { record_recent=(substr($0,1,19) >= cutoff); if (!record_recent) in_trace=0 } "
            "record_recent && /Traceback \\(most recent call last\\)/ { in_trace=1; emit($0); next } "
            "in_trace && timestamped($0) { in_trace=0 } "
            "in_trace && record_recent { emit($0); next } "
            "record_recent && /(^|[[:space:]])(ERROR|CRITICAL)([[:space:]:]|$)/ { emit($0); next } "
            "record_recent && /Test:[[:space:]]*Failed|Build[[:space:]]+failed|Failed to load registry|"
            "ParseError|ValidationError|ModuleNotFoundError|ImportError/ { emit($0) }"
            "'; "
            "done; exit 0"
        )
        result = self.run_shell(script, timeout_seconds=45)
        if result.exit_code != 0:
            return result
        text = result.stdout or ""
        raw_blocks = self._extract_error_blocks(text)
        blocks = [block for block in raw_blocks if self._is_actionable_build_failure(block)]
        failure_trigger = re.compile(
            r"(Test:\s*Failed|Build\s+failed|Failed to load registry|"
            r"Traceback \(most recent call last\)|ParseError|ValidationError|"
            r"ModuleNotFoundError|ImportError|CRITICAL)",
            re.I,
        )
        if not blocks and failure_trigger.search(text) and self._is_actionable_build_failure(text[-8000:]):
            blocks = ["Recent Odoo.sh log failure marker found:\n" + text[-8000:]]
        payload = {
            "failed": bool(blocks),
            "ignored_non_blocking_blocks": max(0, len(raw_blocks) - len(blocks)),
            "since_epoch": safe_since,
            "message": (
                "Recent Odoo.sh failure evidence found. Do not upgrade this build."
                if blocks
                else "No recent Odoo.sh failure evidence found in modified logs."
            ),
            "failure_blocks": blocks[-10:],
        }
        return CommandResult(
            command=["odoosh.recent_build_failure", str(safe_since)],
            cwd="odoosh:ssh",
            exit_code=1 if blocks else 0,
            stdout=json.dumps(payload, ensure_ascii=False)[-20000:],
        )

    def _is_actionable_build_failure(self, block: str) -> bool:
        """Return True only for deploy/build blockers, not noisy runtime warnings.

        A frontend asset 404 like /web/assets/.../undefined or schema warnings can
        appear in logs modified after the pushed SHA and previously blocked the
        entire safe-deploy flow.  This guard now blocks only errors that can make
        the pushed build unusable: registry load failures, tracebacks, imports,
        XML parse/validation errors, failed tests/builds, or critical logs.
        """
        text = block or ""
        lowered = text.lower()
        benign_patterns = [
            "/web/assets/",
            "undefined http/",
            "parsing asset bundle undefined",
            "not enough values to unpack (expected 2, got 1)",
            "missing not-null constraint",
            "werkzeug:",
            " 404 ",
            "generating a new asset bundle",
            "deleting attachments",
            "initiating shutdown",
            "connectionpool",
        ]
        if any(pattern in lowered for pattern in benign_patterns) and not re.search(
            r"Traceback \(most recent call last\)|Failed to load registry|ParseError|ValidationError|ModuleNotFoundError|ImportError|CRITICAL|Build\s+failed|Test:\s*Failed",
            text,
            re.I,
        ):
            return False
        return bool(re.search(
            r"Traceback \(most recent call last\)|Failed to load registry|ParseError|ValidationError|ModuleNotFoundError|ImportError|CRITICAL|Build\s+failed|Test:\s*Failed",
            text,
            re.I,
        ))


    def _extract_error_blocks(self, text: str) -> list[str]:
        lines = text.splitlines()
        blocks: list[str] = []
        current_file = "unknown"
        i = 0
        trigger = re.compile(r"(Traceback \(most recent call last\)|ERROR|CRITICAL|ParseError|ValidationError|ModuleNotFoundError|ImportError|Failed to load registry|Exception)", re.I)
        while i < len(lines):
            line = lines[i]
            if line.startswith("__ODOO_MCP_LOG_FILE__:"):
                current_file = line.split(":", 1)[1]
                i += 1
                continue
            if "Traceback (most recent call last)" in line:
                start = max(0, i - 3)
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith("__ODOO_MCP_LOG_FILE__:"):
                        break
                    if j > i + 2 and re.match(r"^\d{4}-\d{2}-\d{2} ", lines[j]) and not lines[j].lstrip().startswith(('File ', 'raise ', 'return ', 'if ', 'elif ', 'else:', 'for ', 'while ', 'class ', 'def ')):
                        # stop when next timestamped log record begins after traceback body
                        break
                    j += 1
                blocks.append(f"### {current_file}\n" + "\n".join(lines[start:j]))
                i = j
                continue
            if trigger.search(line):
                start = max(0, i - 6)
                end = min(len(lines), i + 18)
                blocks.append(f"### {current_file}\n" + "\n".join(lines[start:end]))
                i = end
                continue
            i += 1
        # de-duplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for block in blocks:
            key = block[-1000:]
            if key not in seen:
                unique.append(block)
                seen.add(key)
        return unique


    def error_pack(self, tail_lines: int = 150) -> CommandResult:
        """Return traceback blocks plus lightweight root-cause hints."""
        base = self.grep_errors(tail_lines)
        text = f"{base.stdout}\n{base.stderr}"
        hints: list[str] = []
        file_matches = sorted(set(re.findall(r"File \"([^\"]+)\"", text)))
        xml_matches = sorted(set(re.findall(r"while parsing ([^:]+):(\d+)", text, flags=re.I)))
        missing_model = sorted(set(re.findall(r"(?:Object|Model|KeyError: )['\"]?([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z0-9_]+)['\"]?", text)))
        if "ModuleNotFoundError" in text or "ImportError" in text:
            hints.append("Python import failure: check __init__.py imports, renamed files, and manifest dependencies.")
        if "ParseError" in text or xml_matches:
            hints.append("XML parse/load failure: check the referenced XML file/line and make sure actions are defined before menus that reference them.")
        if "Access Denied" in text:
            hints.append("Access rights failure: use SSH module update or add missing ir.model.access.csv rows/groups.")
        if missing_model:
            hints.append("Missing registry model: module likely failed to load/upgrade; fix earlier traceback then rerun odoosh.module_update.")
        pack = {
            "summary": "Odoo.sh traceback/error pack",
            "files": file_matches[-20:],
            "xml_locations": [f"{f}:{line}" for f, line in xml_matches[-20:]],
            "models_mentioned": missing_model[-20:],
            "hints": hints or ["No strong pattern detected; inspect traceback blocks below."],
        }
        stdout = "MCP_ERROR_PACK\n" + json.dumps(pack, indent=2) + "\n\nTRACEBACK_BLOCKS\n" + base.stdout
        return base.model_copy(update={"command": ["odoosh.error_pack", str(tail_lines)], "stdout": stdout[-20000:]})

    def correlated_logs(self, correlation_id: str, tail_lines: int = 150) -> CommandResult:
        """Return only correlation-marker lines plus ERROR/CRITICAL tracebacks."""
        marker = str(correlation_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,80}", marker):
            return CommandResult(
                command=["odoosh.correlated_logs"], cwd="odoosh:ssh", exit_code=2,
                stderr="correlation_id must be a safe 3-80 character identifier",
            )
        safe_lines = max(100, min(int(tail_lines or 150), 2000))
        quoted_marker = shlex.quote(marker)
        script = (
            "set +e; marker=" + quoted_marker + "; "
            "for f in ~/logs/*.log; do [ -f \"$f\" ] || continue; "
            f"tail -n {safe_lines} \"$f\" | awk -v file=\"$f\" -v marker=\"$marker\" '"
            "function emit(line) { if (!emitted) { print \"__ODOO_MCP_LOG_FILE__:\" file; emitted=1 } print line } "
            "index($0, marker) { emit($0); next } "
            "/Traceback \\(most recent call last\\)/ { in_trace=1; emit($0); next } "
            "in_trace && /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][[:space:]]/ { in_trace=0 } "
            "in_trace { emit($0); next } "
            "/(^|[[:space:]])(ERROR|CRITICAL)([[:space:]:]|$)/ { emit($0) }'; "
            "done; exit 0"
        )
        result = self.run_shell(script, timeout_seconds=45)
        if result.exit_code != 0:
            return result
        lines = (result.stdout or "").splitlines()
        bounded = "\n".join(lines[-150:])
        return result.model_copy(update={
            "command": ["odoosh.correlated_logs", marker, str(safe_lines)],
            "stdout": bounded[-20000:] or "No correlated marker or fresh error lines found.",
        })


    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str | None = None,
        context: dict | None = None,
        timeout_seconds: int = 120,
        groupby: list[str] | None = None,
        lazy: bool = False,
    ) -> OdooResult:
        """Read Odoo records through Odoo.sh SSH using ``odoo-bin shell``.

        This is the read-only fallback for environments where Odoo shell login is
        unavailable or rejected. Inputs are serialized as JSON/base64 and then
        passed to a fixed Odoo shell snippet; no user text is interpolated as
        Python code.
        """
        if not self._db_name():
            return OdooResult(status="failed", error="ODOOSH_DB_NAME/odoosh_db_name is not configured for SSH query.")
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or ""):
            return OdooResult(status="failed", error="Invalid model name for SSH query.")
        aggregate_re = re.compile(r"^[a-zA-Z0-9_]+(?::(?:sum|avg|min|max|count|count_distinct|bool_and|bool_or|array_agg))?$")
        field_re = aggregate_re if groupby else re.compile(r"^[a-zA-Z0-9_]+$")
        safe_fields: list[str] = []
        for field in fields or []:
            if not isinstance(field, str) or not field_re.fullmatch(field):
                return OdooResult(status="failed", error=f"Invalid field name for SSH query: {field!r}")
            safe_fields.append(field)
        groupby_re = re.compile(r"^[a-zA-Z0-9_]+(?::(?:day|week|month|quarter|year))?$")
        safe_groupby: list[str] = []
        for field in groupby or []:
            if not isinstance(field, str) or not groupby_re.fullmatch(field):
                return OdooResult(status="failed", error=f"Invalid groupby field for SSH query: {field!r}")
            safe_groupby.append(field)
        if order and not re.fullmatch(r"[a-zA-Z0-9_,.\s]+(?:\s+(?:asc|desc))?", order, flags=re.I):
            return OdooResult(status="failed", error="Invalid order string for SSH query.")

        payload = {
            "model": model,
            "domain": domain or [],
            "fields": safe_fields,
            "limit": max(1, min(int(limit or 20), 200)),
            "offset": max(0, int(offset or 0)),
            "order": order or None,
            "context": context or {},
            "groupby": safe_groupby,
            "lazy": bool(lazy),
        }
        payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        addons_setup = self._addons_path_shell_setup()
        marker = "MCP_ODOOSH_QUERY_JSON:"
        script = (
            "set +e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import base64, datetime, json, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            "marker = 'MCP_ODOOSH_QUERY_JSON:'\n"
            "def norm(value):\n"
            "    if isinstance(value, (datetime.date, datetime.datetime)):\n"
            "        return value.isoformat()\n"
            "    if isinstance(value, tuple):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, list):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, dict):\n"
            "        return {str(k): norm(v) for k, v in value.items()}\n"
            "    if isinstance(value, bytes):\n"
            "        return value.decode('utf-8', 'replace')\n"
            "    return value\n"
            "try:\n"
            "    model = env[payload['model']].with_context(**(payload.get('context') or {}))\n"
            "    if payload.get('groupby'):\n"
            "        records = model.read_group(payload.get('domain') or [], payload.get('fields') or [], payload['groupby'], lazy=bool(payload.get('lazy')))\n"
            "    else:\n"
            "        records = model.search_read(\n"
            "            payload.get('domain') or [], payload.get('fields') or [],\n"
            "            offset=int(payload.get('offset') or 0), limit=int(payload.get('limit') or 20),\n"
            "            order=payload.get('order') or None,\n"
            "        )\n"
            "    print(marker + json.dumps({'status': 'ok', 'result': norm(records), 'count': len(records)}, ensure_ascii=False))\n"
            "except Exception as exc:\n"
            "    print(marker + json.dumps({'status': 'failed', 'error': str(exc), 'traceback': traceback.format_exc()[-4000:]}, ensure_ascii=False))\n"
            "PY\n"
        )
        result = self.run_shell(script, timeout_seconds=timeout_seconds)
        payload_line = ""
        for line in reversed((result.stdout or "").splitlines()):
            if line.startswith(marker):
                payload_line = line[len(marker):]
                break
        if not payload_line:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=(result.stderr or result.stdout or "Odoo.sh SSH query did not return a parseable result.")[-4000:],
                result={"ssh": result.model_dump(mode="json")},
            )
        try:
            parsed = json.loads(payload_line)
        except Exception as exc:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=f"Could not parse Odoo.sh SSH query JSON: {exc}",
                result={"raw": payload_line[-4000:], "ssh": result.model_dump(mode="json")},
            )
        if parsed.get("status") != "ok" or result.exit_code != 0:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=parsed.get("error") or result.stderr or f"Odoo.sh SSH query failed with exit code {result.exit_code}",
                result={"payload": parsed, "ssh": result.model_dump(mode="json")},
            )
        return OdooResult(
            status="ok",
            database=self._db_name(),
            result=parsed.get("result") or [],
        )

    def runtime_replay(
        self,
        replay: dict,
        *,
        correlation_id: str,
    ) -> OdooResult:
        """Invoke one explicitly authorized public record method through Odoo shell."""
        if not self._db_name():
            return OdooResult(status="failed", error="ODOOSH_DB_NAME/odoosh_db_name is not configured for runtime replay.")
        model = str(replay.get("model") or "")
        method = str(replay.get("method") or "")
        record_ids = replay.get("record_ids") or []
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]+", model):
            return OdooResult(status="failed", error="Invalid model name for runtime replay.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", method) or method.startswith("_"):
            return OdooResult(status="failed", error="Runtime replay requires a public Odoo method.")
        if (
            not isinstance(record_ids, list)
            or not record_ids
            or len(record_ids) > 20
            or any(not isinstance(value, int) or value <= 0 for value in record_ids)
        ):
            return OdooResult(status="failed", error="Runtime replay requires 1-20 positive record IDs.")
        if replay.get("commit", True) and replay.get("confirm_mutation") is not True:
            return OdooResult(status="failed", error="Committed runtime replay requires confirm_mutation=true.")

        payload = {
            "model": model,
            "record_ids": record_ids,
            "method": method,
            "args": replay.get("args") or [],
            "kwargs": replay.get("kwargs") or {},
            "context": {
                **(replay.get("context") or {}),
                "odoo_mcp_correlation_id": correlation_id,
            },
            "user_id": replay.get("user_id"),
            "company_id": replay.get("company_id"),
            "commit": bool(replay.get("commit", True)),
            "correlation_id": correlation_id,
        }
        payload_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        addons_setup = self._addons_path_shell_setup()
        result_marker = "MCP_RUNTIME_REPLAY_JSON:"
        begin_marker = f"MCP_RUNTIME_REPLAY_BEGIN:{correlation_id}"
        end_marker = f"MCP_RUNTIME_REPLAY_END:{correlation_id}"
        script = (
            "set +e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import base64, datetime, json, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            f"result_marker = {result_marker!r}\n"
            f"begin_marker = {begin_marker!r}\n"
            f"end_marker = {end_marker!r}\n"
            "def norm(value):\n"
            "    if hasattr(value, '_name') and hasattr(value, 'ids'):\n"
            "        return {'model': value._name, 'ids': list(value.ids)}\n"
            "    if isinstance(value, (datetime.date, datetime.datetime)):\n"
            "        return value.isoformat()\n"
            "    if isinstance(value, tuple):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, list):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, dict):\n"
            "        return {str(k): norm(v) for k, v in value.items()}\n"
            "    if isinstance(value, bytes):\n"
            "        return value.decode('utf-8', 'replace')\n"
            "    if isinstance(value, (str, int, float, bool)) or value is None:\n"
            "        return value\n"
            "    return repr(value)[:2000]\n"
            "print(begin_marker, flush=True)\n"
            "try:\n"
            "    records = env[payload['model']].browse(payload['record_ids']).exists()\n"
            "    if set(records.ids) != set(payload['record_ids']):\n"
            "        missing = sorted(set(payload['record_ids']) - set(records.ids))\n"
            "        raise ValueError('Runtime replay records not found: %s' % missing)\n"
            "    if payload.get('user_id'):\n"
            "        user = env['res.users'].browse(int(payload['user_id'])).exists()\n"
            "        if not user:\n"
            "            raise ValueError('Runtime replay user not found')\n"
            "        records = records.with_user(user)\n"
            "    if payload.get('company_id'):\n"
            "        company = env['res.company'].browse(int(payload['company_id'])).exists()\n"
            "        if not company:\n"
            "            raise ValueError('Runtime replay company not found')\n"
            "        records = records.with_company(company)\n"
            "    records = records.with_context(**(payload.get('context') or {}))\n"
            "    value = getattr(records, payload['method'])(*(payload.get('args') or []), **(payload.get('kwargs') or {}))\n"
            "    if payload.get('commit'):\n"
            "        env.cr.commit()\n"
            "    else:\n"
            "        env.cr.rollback()\n"
            "    data = {'status': 'ok', 'action_executed': True, 'committed': bool(payload.get('commit')), 'returned': norm(value)}\n"
            "except Exception as exc:\n"
            "    env.cr.rollback()\n"
            "    data = {'status': 'failed', 'action_executed': True, 'committed': False, 'error': str(exc), 'traceback': traceback.format_exc()[-8000:]}\n"
            "print(result_marker + json.dumps(data, ensure_ascii=False), flush=True)\n"
            "print(end_marker, flush=True)\n"
            "PY\n"
        )
        result = self.run_shell(
            script,
            timeout_seconds=max(10, min(int(replay.get("timeout_seconds") or 120), 600)),
        )
        stdout = result.stdout or ""
        payload_line = ""
        for line in reversed(stdout.splitlines()):
            if line.startswith(result_marker):
                payload_line = line[len(result_marker):]
                break
        if not payload_line:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=(result.stderr or stdout or "Odoo.sh runtime replay returned no parseable result.")[-4000:],
                result={
                    "action_executed": False,
                    "committed": False,
                    "correlation_id": correlation_id,
                    "ssh_exit_code": result.exit_code,
                },
            )
        try:
            parsed = json.loads(payload_line)
        except Exception as exc:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=f"Could not parse Odoo.sh runtime replay JSON: {exc}",
                result={"action_executed": False, "committed": False, "correlation_id": correlation_id},
            )

        begin_at = stdout.find(begin_marker)
        result_at = stdout.find(result_marker, max(begin_at, 0))
        action_output = stdout[begin_at + len(begin_marker):result_at] if begin_at >= 0 and result_at >= 0 else ""
        fresh_source = "\n".join(filter(None, (action_output, result.stderr or "")))
        fresh_blocks = self._extract_error_blocks(fresh_source)
        selected_blocks: list[str] = []
        remaining_lines = 150
        for block in reversed(fresh_blocks):
            lines = block.splitlines()
            if remaining_lines <= 0:
                break
            selected_blocks.append("\n".join(lines[-remaining_lines:]))
            remaining_lines -= min(len(lines), remaining_lines)
        parsed["fresh_errors"] = list(reversed(selected_blocks))
        parsed["fresh_error_count"] = len(fresh_blocks)
        parsed["correlation_id"] = correlation_id
        parsed["ssh_exit_code"] = result.exit_code
        parsed["passed"] = bool(
            parsed.get("status") == "ok"
            and parsed.get("action_executed") is True
            and result.exit_code == 0
            and not fresh_blocks
        )
        return OdooResult(
            status="ok" if parsed["passed"] else "failed",
            database=self._db_name(),
            result=parsed,
            error=None if parsed["passed"] else str(parsed.get("error") or "Runtime replay emitted fresh errors.")[:4000],
        )

    def report_replay(self, replay: dict, *, correlation_id: str) -> OdooResult:
        """Render one exact report action read-only and assert its PDF artifact."""
        if not self._db_name():
            return OdooResult(status="failed", error="ODOOSH_DB_NAME/odoosh_db_name is not configured for report replay.")
        report_xml_id = str(replay.get("report_xml_id") or "").strip()
        if report_xml_id and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*", report_xml_id):
            return OdooResult(status="failed", error="Invalid report_xml_id for report replay.")
        record_ids = replay.get("record_ids") or []
        if not isinstance(record_ids, list) or not record_ids or len(record_ids) > 20 or any(not isinstance(value, int) or value <= 0 for value in record_ids):
            return OdooResult(status="failed", error="Report replay requires 1-20 positive record IDs.")

        payload = {**replay, "correlation_id": correlation_id}
        payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        marker = "MCP_REPORT_REPLAY_JSON:"
        begin_marker = f"MCP_REPORT_REPLAY_BEGIN:{correlation_id}"
        script = (
            "set +e; "
            f"{self._addons_path_shell_setup()}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import base64, hashlib, io, json, re, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            f"marker = {marker!r}\n"
            f"print({begin_marker!r}, flush=True)\n"
            "try:\n"
            "    action_value = None\n"
            "    trigger = payload.get('trigger') or {}\n"
            "    if trigger:\n"
            "        records = env[trigger['model']].browse(trigger['record_ids']).exists()\n"
            "        if set(records.ids) != set(trigger['record_ids']):\n"
            "            raise ValueError('Report trigger records not found')\n"
            "        if trigger.get('user_id'):\n"
            "            records = records.with_user(int(trigger['user_id']))\n"
            "        if trigger.get('company_id'):\n"
            "            records = records.with_company(int(trigger['company_id']))\n"
            "        records = records.with_context(**(trigger.get('context') or {}), odoo_mcp_correlation_id=payload['correlation_id'])\n"
            "        action_value = getattr(records, trigger['method'])(*(trigger.get('args') or []), **(trigger.get('kwargs') or {}))\n"
            "    report_ref = payload.get('report_xml_id')\n"
            "    render_data = payload.get('data') or {}\n"
            "    if isinstance(action_value, dict):\n"
            "        report_ref = report_ref or action_value.get('report_name')\n"
            "        render_data = action_value.get('data') or render_data\n"
            "    if not report_ref:\n"
            "        raise ValueError('The trigger did not return a renderable report action')\n"
            "    report_model = env['ir.actions.report']\n"
            "    if payload.get('user_id'):\n"
            "        report_model = report_model.with_user(int(payload['user_id']))\n"
            "    if payload.get('company_id'):\n"
            "        report_model = report_model.with_company(int(payload['company_id']))\n"
            "    report_model = report_model.with_context(**(payload.get('context') or {}), odoo_mcp_correlation_id=payload['correlation_id'])\n"
            "    pdf, _fmt = report_model._render_qweb_pdf(report_ref, res_ids=payload['record_ids'], data=render_data)\n"
            "    reader_cls = None\n"
            "    try:\n"
            "        from pypdf import PdfReader as reader_cls\n"
            "    except ImportError:\n"
            "        from PyPDF2 import PdfReader as reader_cls\n"
            "    reader = reader_cls(io.BytesIO(pdf))\n"
            "    text = re.sub(r'\\s+', ' ', ' '.join((page.extract_text() or '') for page in reader.pages)).strip()\n"
            "    candidate = text if payload.get('case_sensitive') else text.casefold()\n"
            "    required = payload.get('required_text') or []\n"
            "    forbidden = payload.get('forbidden_text') or []\n"
            "    def present(fragment):\n"
            "        value = fragment if payload.get('case_sensitive') else fragment.casefold()\n"
            "        return value in candidate\n"
            "    required_checks = {value: present(value) for value in required}\n"
            "    forbidden_checks = {value: not present(value) for value in forbidden}\n"
            "    pages = len(reader.pages)\n"
            "    page_ok = (payload.get('expected_pages_min') is None or pages >= payload['expected_pages_min']) and (payload.get('expected_pages_max') is None or pages <= payload['expected_pages_max'])\n"
            "    checks_passed = all(required_checks.values()) and all(forbidden_checks.values()) and page_ok\n"
            "    data = {'status': 'ok', 'action_executed': True, 'committed': False, 'report_ref': report_ref, 'page_count': pages, 'pdf_bytes': len(pdf), 'pdf_sha256': hashlib.sha256(pdf).hexdigest(), 'required_text': required_checks, 'forbidden_text': forbidden_checks, 'page_count_ok': page_ok, 'artifact_assertion_passed': checks_passed}\n"
            "except Exception as exc:\n"
            "    data = {'status': 'failed', 'action_executed': True, 'committed': False, 'artifact_assertion_passed': False, 'error': str(exc), 'traceback': traceback.format_exc()[-8000:]}\n"
            "finally:\n"
            "    env.cr.rollback()\n"
            "print(marker + json.dumps(data, ensure_ascii=False), flush=True)\n"
            "PY\n"
        )
        result = self.run_shell(script, timeout_seconds=max(10, min(int(replay.get("timeout_seconds") or 180), 600)))
        payload_line = next((line[len(marker):] for line in reversed((result.stdout or "").splitlines()) if line.startswith(marker)), "")
        if not payload_line:
            return OdooResult(status="failed", database=self._db_name(), error=(result.stderr or result.stdout or "Report replay returned no parseable result.")[-4000:])
        try:
            parsed = json.loads(payload_line)
        except Exception as exc:
            return OdooResult(status="failed", database=self._db_name(), error=f"Could not parse report replay JSON: {exc}")
        begin_at = (result.stdout or "").find(begin_marker)
        result_at = (result.stdout or "").find(marker, max(begin_at, 0))
        action_output = (
            (result.stdout or "")[begin_at + len(begin_marker):result_at]
            if begin_at >= 0 and result_at >= 0
            else ""
        )
        shell_blocks = self._extract_error_blocks(
            "\n".join(filter(None, (action_output, result.stderr or "")))
        )
        correlated = self.correlated_logs(correlation_id, 150)
        correlated_text = "\n".join(
            line for line in (correlated.stdout or "").splitlines()
            if line and "No correlated marker" not in line
        )
        correlated_blocks = [
            block for block in self._extract_error_blocks(correlated_text)
            if correlation_id in block
        ]
        fresh_blocks = list(dict.fromkeys([*shell_blocks, *correlated_blocks]))
        parsed["fresh_errors"] = fresh_blocks[-10:]
        parsed["fresh_error_count"] = len(fresh_blocks)
        parsed["correlated_log_lines"] = [
            line for line in correlated_text.splitlines() if correlation_id in line
        ][-50:]
        parsed["correlation_id"] = correlation_id
        parsed["passed"] = bool(
            parsed.get("status") == "ok"
            and parsed.get("artifact_assertion_passed") is True
            and result.exit_code == 0
            and not fresh_blocks
        )
        return OdooResult(
            status="ok" if parsed["passed"] else "failed",
            database=self._db_name(),
            result=parsed,
            error=None if parsed["passed"] else str(parsed.get("error") or "Report artifact assertion failed.")[:4000],
        )

    def read_group(
        self,
        model: str,
        domain: list | None,
        fields: list[str],
        groupby: list[str],
        lazy: bool = False,
        context: dict | None = None,
        timeout_seconds: int = 120,
    ) -> OdooResult:
        return self.search_read(
            model, domain or [], fields, context=context, timeout_seconds=timeout_seconds,
            groupby=groupby, lazy=lazy,
        )

    def feature_inspect(
        self,
        models: list[str],
        sample_fields: dict[str, list[str]] | None = None,
        sample_limit: int = 0,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        """Inspect multiple feature models in one Odoo shell/SSH connection."""
        if not self._db_name():
            return OdooResult(status="failed", error="ODOOSH_DB_NAME/odoosh_db_name is not configured for feature inspection.")
        safe_models = list(dict.fromkeys(str(value) for value in models))[:10]
        if not safe_models or any(not re.fullmatch(r"[A-Za-z0-9_.]+", value) for value in safe_models):
            return OdooResult(status="failed", error="Feature inspection requires 1-10 exact technical model names.")
        safe_sample_fields: dict[str, list[str]] = {}
        for model, fields in (sample_fields or {}).items():
            if model not in safe_models:
                continue
            safe = [str(field) for field in fields if re.fullmatch(r"[A-Za-z0-9_]+", str(field))]
            safe_sample_fields[model] = list(dict.fromkeys(safe))[:20]
        payload = {
            "models": safe_models,
            "sample_fields": safe_sample_fields,
            "sample_limit": max(0, min(int(sample_limit or 0), 10)),
        }
        payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        marker = "MCP_ODOOSH_FEATURE_JSON:"
        script = (
            "set +e; "
            f"{self._addons_path_shell_setup()}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import base64, datetime, json, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            f"marker = {marker!r}\n"
            "def norm(value):\n"
            "    if isinstance(value, (datetime.date, datetime.datetime)):\n"
            "        return value.isoformat()\n"
            "    if isinstance(value, tuple):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, list):\n"
            "        return [norm(v) for v in value]\n"
            "    if isinstance(value, dict):\n"
            "        return {str(k): norm(v) for k, v in value.items()}\n"
            "    if isinstance(value, bytes):\n"
            "        return value.decode('utf-8', 'replace')\n"
            "    return value\n"
            "try:\n"
            "    names = payload['models']\n"
            "    meta = env['ir.model'].sudo().search_read([('model', 'in', names)], ['name', 'model'], limit=20, order='model')\n"
            "    existing = [row['model'] for row in meta]\n"
            "    excluded = {'id','display_name','create_uid','create_date','write_uid','write_date','__last_update'}\n"
            "    prefixes = ('message_', 'activity_', 'website_', 'rating_', 'my_activity_')\n"
            "    fields = env['ir.model.fields'].sudo().search_read(\n"
            "        [('model', 'in', existing)],\n"
            "        ['model','name','field_description','ttype','relation','required','readonly','store','index'],\n"
            "        limit=500, order='model,name')\n"
            "    fields = [row for row in fields if row['name'] not in excluded and not row['name'].startswith(prefixes)]\n"
            "    bounded_fields, field_counts = [], {}\n"
            "    for row in fields:\n"
            "        count = field_counts.get(row['model'], 0)\n"
            "        if count < 20:\n"
            "            bounded_fields.append(row)\n"
            "            field_counts[row['model']] = count + 1\n"
            "    fields = bounded_fields\n"
            "    views = env['ir.ui.view'].sudo().search_read([('model', 'in', existing)], ['name','model','type','inherit_id','priority','active'], limit=100, order='model,priority,id')\n"
            "    actions = env['ir.actions.act_window'].sudo().search_read([('res_model', 'in', existing)], ['name','res_model','view_mode','domain','context'], limit=100, order='res_model,name')\n"
            "    action_refs = ['ir.actions.act_window,%s' % row['id'] for row in actions]\n"
            "    menus = env['ir.ui.menu'].sudo().search_read([('action', 'in', action_refs)], ['name','parent_id','action','groups_id','sequence','active'], limit=100, order='sequence,id') if action_refs else []\n"
            "    access = env['ir.model.access'].sudo().search_read([('model_id.model', 'in', existing)], ['name','model_id','group_id','perm_read','perm_write','perm_create','perm_unlink','active'], limit=100, order='name')\n"
            "    rules = env['ir.rule'].sudo().search_read([('model_id.model', 'in', existing)], ['name','model_id','groups','domain_force','active','perm_read','perm_write','perm_create','perm_unlink'], limit=100, order='name')\n"
            "    selection_fields = env['ir.model.fields'].sudo().search([('model', 'in', existing), ('ttype', '=', 'selection')])\n"
            "    selections = env['ir.model.fields.selection'].sudo().search_read([('field_id', 'in', selection_fields.ids)], ['field_id','value','name','sequence'], limit=300, order='field_id,sequence,id')\n"
            "    counts, samples, errors = {}, {}, {}\n"
            "    for name in existing:\n"
            "        try:\n"
            "            counts[name] = env[name].sudo().search_count([])\n"
            "            wanted = payload.get('sample_fields', {}).get(name) or []\n"
            "            if payload.get('sample_limit') and wanted:\n"
            "                samples[name] = env[name].sudo().search_read([], wanted, limit=payload['sample_limit'])\n"
            "        except Exception as exc:\n"
            "            errors[name] = str(exc)\n"
            "    result = {'execution': {'ssh_shell_startups': 1, 'business_field_limit_per_model': 20}, 'models': meta, 'missing_models': [n for n in names if n not in existing], 'business_fields': fields, 'views': views, 'actions': actions, 'menus': menus, 'access': access, 'rules': rules, 'selections': selections, 'record_counts': counts, 'samples': samples, 'model_errors': errors}\n"
            "    print(marker + json.dumps({'status': 'ok', 'result': norm(result)}, ensure_ascii=False))\n"
            "except Exception as exc:\n"
            "    print(marker + json.dumps({'status': 'failed', 'error': str(exc), 'traceback': traceback.format_exc()[-4000:]}, ensure_ascii=False))\n"
            "PY\n"
        )
        command = self.run_shell(script, timeout_seconds=max(15, min(int(timeout_seconds), 180)))
        payload_line = ""
        for line in reversed((command.stdout or "").splitlines()):
            if line.startswith(marker):
                payload_line = line[len(marker):]
                break
        if not payload_line:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=(command.stderr or command.stdout or "Feature inspection returned no parseable result.")[-4000:],
            )
        try:
            parsed = json.loads(payload_line)
        except Exception as exc:
            return OdooResult(status="failed", database=self._db_name(), error=f"Could not parse feature inspection JSON: {exc}")
        if parsed.get("status") != "ok" or command.exit_code != 0:
            return OdooResult(
                status="failed",
                database=self._db_name(),
                error=parsed.get("error") or command.stderr or "Feature inspection failed.",
                result={"payload": parsed},
            )
        return OdooResult(status="ok", database=self._db_name(), result=parsed.get("result") or {})

    def _dependency_guard(self, module_name: str) -> CommandResult | None:
        try:
            from .odoo_service import OdooService

            deps = OdooService(self._active_client()).module_dependencies_status(module_name)
        except Exception:
            return None
        if deps.status != "ok" or not isinstance(deps.result, dict):
            return None
        missing = deps.result.get("missing") or []
        if missing:
            names = ", ".join(sorted({str(row.get("name")) for row in missing}))
            return CommandResult(
                command=["odoosh.dependency_guard", module_name],
                cwd="odoo:odoo_shell",
                exit_code=2,
                stderr=f"Missing/uninstalled dependencies for {module_name}: {names}",
            )
        return None

    def module_install_ssh(self, module_name: str, timeout_seconds: int = 300) -> CommandResult:
        """Install module via SSH using -i flag (first-time install only)."""
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        return self._run_module_cli(module_name, "-i", timeout_seconds)

    def module_update(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        if not self._db_name():
            return CommandResult(
                command=["odoosh_module_update"],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        return self._run_module_cli(module_name, "-u", timeout_seconds)

    def run_module_tests(
        self,
        module_name: str,
        test_tags: list[str] | None = None,
        timeout_seconds: int = 600,
    ) -> CommandResult:
        """Run one addon's Odoo-native tests through a fixed, non-shell-extensible command."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", module_name or ""):
            return CommandResult(
                command=["odoosh.module_tests", module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="Invalid Odoo module technical name.",
            )
        if self._active_client() and self._active_client().environment != "test":
            return CommandResult(
                command=["odoosh.module_tests", module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="Odoo-native tests require an ODOO_CLIENTS entry with environment=test.",
            )
        tags = list(dict.fromkeys(test_tags or [f"/{module_name}"]))
        if any(not re.fullmatch(r"[-+*A-Za-z0-9_./:]+", tag or "") for tag in tags):
            return CommandResult(
                command=["odoosh.module_tests", module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="Unsafe Odoo test tag selector.",
            )
        if not self._db_name():
            return CommandResult(
                command=["odoosh.module_tests", module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured for the selected test client.",
            )
        safe_db = shlex.quote(self._db_name())
        safe_module = shlex.quote(module_name)
        safe_tags = shlex.quote(",".join(tags))
        odoo_path = shlex.quote(self._odoo_path())
        script = (
            "set -e; "
            f"{self._addons_path_shell_setup()}"
            f"cd {odoo_path}; "
            f"./odoo-bin -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" "
            f"-u {safe_module} --test-tags {safe_tags} --stop-after-init --log-level=test"
        )
        result = self._classify_module_update_result(
            self.run_shell(script, timeout_seconds=timeout_seconds)
        )
        return result.model_copy(
            update={"command": ["odoosh.module_tests", module_name, ",".join(tags)]}
        )

    def module_deploy_ssh(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        """Fast hybrid deploy: cheap SQL state probe, one Odoo process.

        The common path uses a read-only PostgreSQL query (no Odoo startup) and
        then exactly one ``odoo-bin -i/-u`` process. If psql is unavailable, one
        ``odoo-bin shell`` process performs update_list, state detection and the
        appropriate ORM button. A module failure is never retried through the
        alternate path, avoiding duplicate/partial upgrades.
        """
        state = self._module_state_via_psql(module_name)
        if state is None:
            return self._run_module_shell_deploy(module_name, timeout_seconds)
        # These states can be stale when a manifest/dependency was fixed in a
        # newly deployed commit. Refresh the Apps registry inside the same Odoo
        # process before deciding that source is actually uninstallable/missing.
        if state in {"uninstallable", "not_found"}:
            return self._run_module_shell_deploy(module_name, timeout_seconds)
        flag = "-u" if state in {"installed", "to upgrade", "to remove"} else "-i"
        result = self._run_module_cli(module_name, flag, timeout_seconds)
        failure_text = f"{result.stdout}\n{result.stderr}".lower()
        if result.exit_code != 0 and "not installable" in failure_text:
            diagnostic = self._diagnose_deployed_manifest(module_name)
            result = result.model_copy(update={
                "stderr": (result.stderr or "") + "\nMCP_MODULE_DEPLOY_DIAGNOSTIC:" + json.dumps(diagnostic, ensure_ascii=False),
            })
        prefix = f"MCP_HYBRID_DEPLOY probe=psql mode={'upgrade' if flag == '-u' else 'install'} state={state} module={module_name}\n"
        return result.model_copy(update={"command": ["odoosh.module_deploy_ssh", module_name, flag], "stdout": prefix + (result.stdout or "")})

    def _diagnose_deployed_manifest(self, module_name: str) -> dict:
        """Read deployed manifest and dependency states without starting Odoo."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", module_name or ""):
            return {"failure_kind": "invalid_module_name", "source_repair_allowed": False}
        marker = "MCP_DEPLOYED_MANIFEST_JSON:"
        script = (
            "python3 - <<'PY'\n"
            "import ast, json, os, subprocess\n"
            f"base = {self._user_addons_path()!r}\n"
            f"module = {module_name!r}\n"
            f"database = {self._db_name()!r}\n"
            f"marker = {marker!r}\n"
            "path = os.path.realpath(os.path.join(base, module, '__manifest__.py'))\n"
            "allowed = os.path.realpath(base) + os.sep\n"
            "manifest = {}\n"
            "parse_error = None\n"
            "exists = path.startswith(allowed) and os.path.isfile(path)\n"
            "try:\n"
            "    if not exists:\n"
            "        raise FileNotFoundError(path)\n"
            "    with open(path, 'r', encoding='utf-8') as handle:\n"
            "        parsed = ast.literal_eval(handle.read())\n"
            "    if not isinstance(parsed, dict):\n"
            "        raise ValueError('manifest is not a dictionary')\n"
            "    manifest = parsed\n"
            "except Exception as exc:\n"
            "    parse_error = str(exc)\n"
            "depends = [str(name) for name in (manifest.get('depends') or [])]\n"
            "dependency_states = []\n"
            "for name in depends:\n"
            "    escaped = name.replace(chr(39), chr(39) * 2)\n"
            "    sql = \"SELECT state FROM ir_module_module WHERE name = '%s' LIMIT 1;\" % escaped\n"
            "    proc = subprocess.run(['psql', '-X', '-d', database, '-Atq', '-v', 'ON_ERROR_STOP=1', '-c', sql], capture_output=True, text=True)\n"
            "    state = (proc.stdout or '').strip().splitlines()[-1] if proc.returncode == 0 and (proc.stdout or '').strip() else 'not_found'\n"
            "    dependency_states.append({'name': name, 'state': state})\n"
            "installable = manifest.get('installable', True) if parse_error is None else None\n"
            "data = {'status': 'failed', 'failure_kind': 'cli_not_installable', 'module': module, 'deployed_manifest': {'path': path, 'exists': exists, 'installable': installable, 'depends': depends, 'parse_error': parse_error}, 'dependency_states': dependency_states, 'source_repair_allowed': bool(parse_error or installable is False), 'next_action': 'Use this deployed manifest evidence. Do not edit source when installable is true; resolve a missing dependency or obtain the concrete runtime traceback.'}\n"
            "print(marker + json.dumps(data, ensure_ascii=False))\n"
            "PY"
        )
        result = self.run_shell(script, timeout_seconds=30)
        for line in reversed((result.stdout or "").splitlines()):
            if line.startswith(marker):
                try:
                    parsed = json.loads(line[len(marker):])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    break
        return {
            "status": "failed",
            "failure_kind": "deployed_manifest_diagnostic_failed",
            "module": module_name,
            "diagnostic_error": (result.stderr or result.stdout or "No diagnostic output")[-2000:],
            "source_repair_allowed": False,
        }

    def _module_state_via_psql(self, module_name: str) -> str | None:
        """Return module state without starting Odoo; None means probe unavailable."""
        if not self._db_name() or not re.fullmatch(r"[A-Za-z0-9_.-]+", module_name or ""):
            return None
        safe_db = shlex.quote(self._db_name() or "")
        sql_module = module_name.replace("'", "''")
        sql = f"SELECT state FROM ir_module_module WHERE name = '{sql_module}' LIMIT 1;"
        script = f"psql -X -d {safe_db} -Atq -v ON_ERROR_STOP=1 -c {shlex.quote(sql)}"
        result = self.run_shell(script, timeout_seconds=15)
        if result.exit_code != 0:
            return None
        states = {"installed", "uninstalled", "to install", "to upgrade", "to remove", "uninstallable"}
        for line in reversed((result.stdout or "").splitlines()):
            state = line.strip()
            if state in states:
                return state
        return "not_found"

    def _run_module_shell_deploy(self, module_name: str, timeout_seconds: int) -> CommandResult:
        """Single-process ORM fallback when the lightweight SQL probe is unavailable."""
        if not self._db_name():
            return CommandResult(
                command=["odoosh.module_shell_deploy", module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        addons_setup = self._addons_path_shell_setup()
        payload_b64 = base64.b64encode(json.dumps({"module": module_name}).encode("utf-8")).decode("ascii")
        marker = "MCP_MODULE_SHELL_DEPLOY_JSON:"
        script = (
            "set +e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import ast, base64, json, os, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            f"marker = {marker!r}\n"
            "try:\n"
            "    modules = env['ir.module.module'].sudo()\n"
            "    modules.update_list()\n"
            "    module = modules.search([('name', '=', payload['module'])], limit=1)\n"
            "    if not module:\n"
            "        raise RuntimeError('Module not found after update_list: %s' % payload['module'])\n"
            "    before = module.state\n"
            "    if before == 'uninstallable':\n"
            f"        manifest_path = os.path.join({self._user_addons_path()!r}, payload['module'], '__manifest__.py')\n"
            "        manifest = {}\n"
            "        manifest_error = None\n"
            "        try:\n"
            "            with open(manifest_path, 'r', encoding='utf-8') as manifest_file:\n"
            "                parsed_manifest = ast.literal_eval(manifest_file.read())\n"
            "            manifest = parsed_manifest if isinstance(parsed_manifest, dict) else {}\n"
            "        except Exception as manifest_exc:\n"
            "            manifest_error = str(manifest_exc)\n"
            "        dep_names = [str(name) for name in (manifest.get('depends') or [])]\n"
            "        dep_rows = modules.search_read([('name', 'in', dep_names)], ['name', 'state']) if dep_names else []\n"
            "        dep_by_name = {row['name']: row.get('state') for row in dep_rows}\n"
            "        dependency_states = [{'name': name, 'state': dep_by_name.get(name, 'not_found')} for name in dep_names]\n"
            "        data = {'status': 'failed', 'failure_kind': 'registry_uninstallable_after_refresh', 'module': payload['module'], 'state_before': before, 'deployed_manifest': {'path': manifest_path, 'exists': os.path.isfile(manifest_path), 'installable': manifest.get('installable', True) if manifest_error is None else None, 'depends': dep_names, 'parse_error': manifest_error}, 'dependency_states': dependency_states, 'source_repair_allowed': False, 'next_action': 'Do not edit the manifest from this state alone. If installable is true, diagnose any missing dependency or obtain the real Odoo traceback/log error first.'}\n"
            "        raise RuntimeError('MCP_UNINSTALLABLE_DIAGNOSTIC:' + json.dumps(data, ensure_ascii=False))\n"
            "    operation = 'upgrade' if before in ('installed', 'to upgrade', 'to remove') else 'install'\n"
            "    if operation == 'upgrade':\n"
            "        module.button_immediate_upgrade()\n"
            "    else:\n"
            "        module.button_immediate_install()\n"
            "    env.cr.commit()\n"
            "    data = {'status': 'ok', 'module': payload['module'], 'state_before': before, 'operation': operation}\n"
            "except Exception as exc:\n"
            "    env.cr.rollback()\n"
            "    raw_error = str(exc)\n"
            "    if raw_error.startswith('MCP_UNINSTALLABLE_DIAGNOSTIC:'):\n"
            "        data = json.loads(raw_error.split(':', 1)[1])\n"
            "    else:\n"
            "        data = {'status': 'failed', 'failure_kind': 'odoo_runtime_error', 'module': payload['module'], 'error': raw_error, 'traceback': traceback.format_exc()[-12000:]}\n"
            "print(marker + json.dumps(data, ensure_ascii=False))\n"
            "PY\n"
        )
        result = self.run_shell(script, timeout_seconds=timeout_seconds)
        payload_line = next(
            (line[len(marker):] for line in reversed((result.stdout or "").splitlines()) if line.startswith(marker)),
            "",
        )
        if not payload_line:
            return self._classify_module_update_result(result.model_copy(update={
                "command": ["odoosh.module_shell_deploy", module_name],
            }))
        try:
            payload = json.loads(payload_line)
        except json.JSONDecodeError:
            payload = {"status": "failed", "error": "Could not parse module shell deploy result.", "raw": payload_line[-4000:]}
        if payload.get("status") != "ok" or result.exit_code != 0:
            diagnostic = "MCP_MODULE_DEPLOY_DIAGNOSTIC:" + json.dumps(payload, ensure_ascii=False)
            return result.model_copy(update={
                "command": ["odoosh.module_shell_deploy", module_name],
                "exit_code": result.exit_code or 3,
                "stderr": diagnostic + "\n" + str(payload.get("traceback") or payload.get("error") or ""),
            })
        prefix = f"MCP_HYBRID_DEPLOY probe=odoo_shell mode={payload.get('operation')} state={payload.get('state_before')} module={module_name}\n"
        return result.model_copy(update={
            "command": ["odoosh.module_shell_deploy", module_name],
            "stdout": prefix + (result.stdout or ""),
        })

    def _module_state_best_effort(self, module_name: str) -> str | None:
        try:
            from .odoo_service import OdooService

            status = OdooService(self._active_client()).module_status(module_name)
            if status.status == "ok" and status.result:
                return (status.result[0] or {}).get("state")
            if status.status == "ok" and status.result == []:
                return "not_found"
        except Exception:
            pass

        if not self._db_name():
            return None
        probe = self._run_module_state_probe(module_name)
        match = re.search(r"MCP_MODULE_STATE=([^\s]+)", probe.stdout or "")
        return match.group(1) if match else None

    def _run_module_state_probe(self, module_name: str) -> CommandResult:
        safe_db = shlex.quote(self._db_name() or "")
        odoo_path = shlex.quote(self._odoo_path())
        addons_setup = self._addons_path_shell_setup()
        module_literal = repr(module_name)
        script = (
            "set +e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            f"module_name = {module_literal}\n"
            "mod = env['ir.module.module'].search([('name', '=', module_name)], limit=1)\n"
            "print('MCP_MODULE_STATE=%s' % (mod.state if mod else 'not_found'))\n"
            "PY"
        )
        return self.run_shell(script, timeout_seconds=120)

    def _addons_path(self) -> str:
        odoo_path = self._odoo_path().rstrip("/")
        source_root = odoo_path.rsplit("/", 1)[0] if "/" in odoo_path else odoo_path
        # Odoo.sh keeps Enterprise and design themes beside the Community
        # checkout. Omitting these paths makes an otherwise valid installed
        # addon look uninstallable when its temporary -i/-u process builds the
        # registry and encounters Enterprise dependencies.
        paths = [
            f"{self._odoo_path()}/odoo/addons",
            f"{self._odoo_path()}/addons",
            f"{source_root}/enterprise",
            f"{source_root}/themes",
            f"{source_root}/design-themes",
            self._user_addons_path(),
        ]
        return ",".join(dict.fromkeys(path.rstrip("/") for path in paths if path))

    def _addons_path_shell_setup(self) -> str:
        """Select only addon directories that exist on the remote Odoo.sh build."""
        candidates = " ".join(shlex.quote(path) for path in self._addons_path().split(",") if path)
        return (
            "MCP_ADDONS_PATH=''; "
            f"for MCP_ADDON_DIR in {candidates}; do "
            "if [ -d \"$MCP_ADDON_DIR\" ]; then "
            "if [ -n \"$MCP_ADDONS_PATH\" ]; then MCP_ADDONS_PATH=\"$MCP_ADDONS_PATH,$MCP_ADDON_DIR\"; "
            "else MCP_ADDONS_PATH=\"$MCP_ADDON_DIR\"; fi; fi; done; "
        )

    def _run_module_cli(self, module_name: str, flag: str, timeout_seconds: int) -> CommandResult:
        if flag not in {"-i", "-u"}:
            raise ValueError("flag must be -i or -u")
        if not self._db_name():
            return CommandResult(
                command=["odoosh_module_cli", flag, module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        safe_module = shlex.quote(module_name)
        safe_db = shlex.quote(self._db_name())
        odoo_path = shlex.quote(self._odoo_path())
        addons_setup = self._addons_path_shell_setup()
        script = (
            "set -e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" {flag} {safe_module} --stop-after-init --log-level=info"
        )
        result = self.run_shell(script, timeout_seconds=timeout_seconds)
        return self._classify_module_update_result(result)

    def _classify_module_update_result(self, result: CommandResult) -> CommandResult:
        text = f"{result.stdout}\n{result.stderr}"
        matched = [pattern for pattern in self.FAILURE_PATTERNS if pattern in text]
        if result.exit_code == 0 and matched:
            return result.model_copy(update={
                "exit_code": 3,
                "stderr": (result.stderr or "") + "\nMCP_CLASSIFIED_FAILURE: " + ", ".join(matched),
            })
        return result

    @staticmethod
    def _redacted_command(command: list[str]) -> list[str]:
        redacted: list[str] = []
        skip_next = False
        for item in command:
            if skip_next:
                redacted.append("<redacted>")
                skip_next = False
                continue
            redacted.append(item)
            if item == "-i":
                skip_next = True
        return redacted
