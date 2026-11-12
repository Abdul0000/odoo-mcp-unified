"""In-memory GitHub Git Data API used to test the commit engine and workspace offline."""
from __future__ import annotations

import base64
import hashlib
import json
import re


def blob_sha(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeGitHub:
    def __init__(self, files: dict[str, str], branch: str = "staging"):
        self.blobs: dict[str, str] = {}
        self.trees: dict[str, dict[str, tuple[str, str]]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_next: list[tuple[int, dict, dict]] = []  # (status, headers, body) injected once
        self.subtrees: dict[str, tuple[str, str]] = {}
        self.default_branch = "main"
        self.before_patch = None
        tree = {p: ("100644", self._blob(t)) for p, t in files.items()}
        tsha = self._tree(tree)
        self.refs[branch] = self._commit(tsha, [], "initial")

    # ---- object helpers
    def _blob(self, text: str) -> str:
        sha = blob_sha(text)
        self.blobs[sha] = text
        return sha

    def _tree(self, flat) -> str:
        sha = hashlib.sha1(json.dumps(sorted(flat.items())).encode()).hexdigest()
        self.trees[sha] = dict(flat)
        return sha

    def _commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = hashlib.sha1(json.dumps([tree, parents, message, len(self.commits)]).encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def head_files(self, branch: str) -> dict[str, str]:
        tree = self.trees[self.commits[self.refs[branch]]["tree"]]
        return {p: self.blobs[s] for p, (m, s) in tree.items()}

    def push_external(self, branch: str, changes: dict[str, str | None], message="upstream change") -> str:
        parent = self.refs[branch]
        flat = dict(self.trees[self.commits[parent]["tree"]])
        for p, t in changes.items():
            if t is None:
                flat.pop(p, None)
            else:
                flat[p] = ("100644", self._blob(t))
        self.refs[branch] = self._commit(self._tree(flat), [parent], message)
        return self.refs[branch]

    def _is_ancestor(self, anc: str, desc: str) -> bool:
        stack, seen = [desc], set()
        while stack:
            cur = stack.pop()
            if cur == anc:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.commits[cur]["parents"])
        return False

    # ---- transport
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if self.fail_next:
            status, hdrs, data = self.fail_next.pop(0)
            return status, hdrs, data
        path = re.sub(r"^https://api.github.com/repos/[^/]+/[^/]+", "", url)
        payload = json.loads(body) if body else None
        if method == "GET" and path == "":
            return 200, {}, {"default_branch": self.default_branch}
        m = re.fullmatch(r"/git/ref/heads/(.+)", path)
        if method == "GET" and m:
            b = m.group(1)
            return (200, {}, {"object": {"sha": self.refs[b]}}) if b in self.refs else (404, {}, {"message": "Not Found"})
        m = re.fullmatch(r"/git/commits/([0-9a-f]+)", path)
        if method == "GET" and m:
            c = self.commits[m.group(1)]
            return 200, {}, {"sha": m.group(1), "tree": {"sha": c["tree"]}, "message": c["message"]}
        m = re.fullmatch(r"/git/trees/([0-9a-z]+)(\?recursive=1)?", path)
        if method == "GET" and m:
            ref, recursive = m.group(1), bool(m.group(2))
            root, prefix = self.subtrees.get(ref, (ref, ""))
            flat = {p[len(prefix):]: v for p, v in self.trees[root].items() if p.startswith(prefix)}
            if recursive:
                return 200, {}, {"truncated": False, "tree": [{"path": p, "mode": mo, "type": "blob", "sha": s, "size": len(self.blobs[s].encode())} for p, (mo, s) in flat.items()]}
            entries, dirs = [], set()
            for p, (mo, s) in flat.items():
                if "/" not in p:
                    entries.append({"path": p, "mode": mo, "type": "blob", "sha": s, "size": len(self.blobs[s].encode())})
                else:
                    dirs.add(p.split("/", 1)[0])
            for d in sorted(dirs):
                sub = "sub" + hashlib.sha1(f"{root}:{prefix}{d}/".encode()).hexdigest()[:30]
                self.subtrees[sub] = (root, f"{prefix}{d}/")
                entries.append({"path": d, "mode": "040000", "type": "tree", "sha": sub})
            return 200, {}, {"truncated": False, "tree": entries}
        m = re.fullmatch(r"/git/blobs/([0-9a-f]+)", path)
        if method == "GET" and m:
            return 200, {}, {"encoding": "base64", "content": base64.b64encode(self.blobs[m.group(1)].encode()).decode()}
        if method == "POST" and path == "/git/blobs":
            text = base64.b64decode(payload["content"]).decode()
            return 201, {}, {"sha": self._blob(text)}
        if method == "POST" and path == "/git/trees":
            flat = dict(self.trees[payload["base_tree"]])
            for e in payload["tree"]:
                if e["sha"] is None:
                    flat.pop(e["path"], None)
                else:
                    flat[e["path"]] = (e["mode"], e["sha"])
            return 201, {}, {"sha": self._tree(flat)}
        if method == "POST" and path == "/git/commits":
            return 201, {}, {"sha": self._commit(payload["tree"], payload["parents"], payload["message"])}
        if method == "POST" and path == "/git/refs":
            self.refs[payload["ref"].replace("refs/heads/", "")] = payload["sha"]
            return 201, {}, {"ref": payload["ref"]}
        m = re.fullmatch(r"/git/refs/heads/(.+)", path)
        if method == "PATCH" and m:
            if self.before_patch:
                hook, self.before_patch = self.before_patch, None
                hook(self)
            b = m.group(1)
            if not self._is_ancestor(self.refs[b], payload["sha"]):
                return 422, {}, {"message": "Update is not a fast forward"}
            self.refs[b] = payload["sha"]
            return 200, {}, {}
        if method == "DELETE" and m:
            self.refs.pop(m.group(1), None)
            return 204, {}, None
        m = re.fullmatch(r"/commits\?sha=(.+)&per_page=(\d+)", path)
        if method == "GET" and m:
            out, cur = [], self.refs.get(m.group(1))
            while cur and len(out) < int(m.group(2)):
                out.append({"sha": cur, "commit": {"message": self.commits[cur]["message"]}})
                cur = (self.commits[cur]["parents"] or [None])[0]
            return 200, {}, out
        return 404, {}, {"message": f"unhandled {method} {path}"}
