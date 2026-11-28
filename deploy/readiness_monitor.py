"""Queue one triage record per sustained connector readiness outage."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError
from urllib.request import urlopen

THRESHOLD = 3
KEEP = 50
SHA = re.compile(r"[a-f0-9]{40}")


def probe():
    try:
        try:
            response = urlopen("http://127.0.0.1:8080/ready", timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            data = json.loads(response.read(16384))
            commit = (data.get("runtime") or {}).get("commit_sha")
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                commit = None
            return response.status == 200 and data.get("status") == "ok", commit
    except (OSError, ValueError, AttributeError):
        return False, None


def advance(state, *, ready, commit, now):
    incidents = state.setdefault("incidents", [])
    active = next((item for item in reversed(incidents) if item["recovered_at"] is None), None)
    if ready:
        if active:
            active["recovered_at"] = now
        state.update(streak=0, first_failure=None, last_ready_commit=commit)
        return ("recovered", active["id"]) if active else None
    state["streak"] = min(THRESHOLD, state.get("streak", 0) + 1)
    state["first_failure"] = state.get("first_failure") or now
    if state["streak"] == THRESHOLD and active is None:
        incident_id = hashlib.sha256(state["first_failure"].encode()).hexdigest()[:16]
        incidents.append({
            "id": "mcp-ready-" + incident_id,
            "opened_at": now,
            "first_failure_at": state["first_failure"],
            "recovered_at": None,
            "observed_commit": commit,
            "last_ready_commit": state.get("last_ready_commit"),
            "failed_probes": THRESHOLD,
            "title": "Connector readiness failed on three consecutive probes",
            "proposed_outcome": "Restore connector readiness and capture the cause in a regression check.",
        })
        del incidents[:-KEEP]
        return "opened", incidents[-1]["id"]
    return None


def record(root, *, sample=probe):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file = root / "readiness-incidents.json"
    with (root / "readiness-incidents.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        ready, commit = sample()
        event = advance(state, ready=ready, commit=commit, now=datetime.now(timezone.utc).isoformat())
        with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
            json.dump(state, stream)
            temp = Path(stream.name)
        temp.replace(state_file)
        if event:
            print(f"Readiness incident {event[0]} id={event[1]}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    args = parser.parse_args()
    record(args.state_dir)


if __name__ == "__main__":
    main()
