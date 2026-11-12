from __future__ import annotations
import ast
from pathlib import Path
from typing import Any

class ReaApiInspectService:
    """Build a source-only inventory of API integrations; never contacts endpoints."""
    MAX_FILES = 120
    MAX_FILE_BYTES = 256000
    MAX_TOTAL_CHARS = 1_500_000
    HTTP_NAMES = {"get","post","put","patch","delete","request","head","options"}
    HTTP_CLIENTS = {"requests","httpx","aiohttp","urllib","urllib3","http","xmlrpc","websocket","zeep","grpc"}
    URL_KEYS = {"url","base_url","endpoint","host","uri","webhook_url"}
    SECRET_KEYS = {"token","secret","password","api_key","access_key","client_secret","authorization"}

    @staticmethod
    def _literal(node: ast.AST) -> str | None:
        if isinstance(node,ast.Constant) and isinstance(node.value,str): return node.value
        return None

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node,ast.Name): return node.id
        if isinstance(node,ast.Attribute): return node.attr
        return ""

    @staticmethod
    def _root_name(node: ast.AST) -> str:
        while isinstance(node,ast.Attribute): node=node.value
        return node.id if isinstance(node,ast.Name) else ""

    def inspect(self, *, module_path: Path, module_name: str) -> dict[str,Any]:
        root=module_path.resolve(strict=True)
        if not root.is_dir() or not (root/"__manifest__.py").is_file():
            raise ValueError("Valid addon directory and manifest required")
        files=[]; omitted=[]; findings=[]; warnings=[]; total=0
        for path in sorted(root.rglob("*.py")):
            try:
                resolved=path.resolve(strict=True); relpath=resolved.relative_to(root)
                if any(x in {".git","__pycache__","static","tests"} for x in relpath.parts): continue
                size=resolved.stat().st_size
                if size>self.MAX_FILE_BYTES: omitted.append(relpath.as_posix()); continue
                if len(files)>=self.MAX_FILES or total+size>self.MAX_TOTAL_CHARS:
                    omitted.append(relpath.as_posix()); continue
                source=resolved.read_text(encoding="utf-8",errors="replace")
            except (OSError,ValueError):
                warnings.append({"code":"FILE_UNREADABLE","path":str(path)}); continue
            total+=len(source); files.append(relpath.as_posix())
            try: tree=ast.parse(source,filename=relpath.as_posix())
            except SyntaxError as exc:
                warnings.append({"code":"PYTHON_PARSE_ERROR","path":relpath.as_posix(),"message":str(exc)[:160]}); continue
            for node in ast.walk(tree):
                if isinstance(node,ast.Call):
                    method=self._call_name(node.func); base=self._root_name(node.func)
                    if (base in self.HTTP_CLIENTS and method in self.HTTP_NAMES) or method in {"ServerProxy","urlopen","urlretrieve","create_connection","WebSocket","ClientSession"}:
                        url=None
                        if node.args: url=self._literal(node.args[0])
                        for kw in node.keywords:
                            if kw.arg in self.URL_KEYS: url=self._literal(kw.value) or url
                        kwargs={kw.arg:kw.value for kw in node.keywords if kw.arg}
                        timeout=kwargs.get("timeout")
                        verify=kwargs.get("verify")
                        findings.append({
                            "type":"outbound_client_call","client":base or method,"method":method,
                            "url_literal":url,"timeout":ast.unparse(timeout)[:120] if timeout else None,
                            "tls_verify":ast.unparse(verify)[:80] if verify else None,
                            "evidence":{"path":relpath.as_posix(),"line":node.lineno},
                        })
                    for kw in node.keywords:
                        key=(kw.arg or "").lower()
                        if key in self.SECRET_KEYS or any(part in key for part in ("token","secret","password","api_key")):
                            findings.append({"type":"credential_reference","key":kw.arg,
                                "value_kind":"literal" if self._literal(kw.value) is not None else "expression",
                                "evidence":{"path":relpath.as_posix(),"line":node.lineno}})
                elif isinstance(node,(ast.Assign,ast.AnnAssign)):
                    targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                    value=node.value
                    for target in targets:
                        names=[target.id] if isinstance(target,ast.Name) else [x.id for x in ast.walk(target) if isinstance(x,ast.Name)]
                        for name in names:
                            low=name.lower()
                            if any(k in low for k in self.URL_KEYS):
                                findings.append({"type":"url_or_endpoint_config","name":name,
                                    "value_literal":self._literal(value),
                                    "evidence":{"path":relpath.as_posix(),"line":node.lineno}})
                            elif any(k in low for k in self.SECRET_KEYS):
                                findings.append({"type":"credential_config_reference","name":name,
                                    "value_kind":"literal" if self._literal(value) is not None else "expression",
                                    "evidence":{"path":relpath.as_posix(),"line":node.lineno}})
                elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    decorators=[ast.unparse(x) for x in node.decorator_list]
                    route=[x for x in decorators if "route" in x.lower() or "http.route" in x.lower()]
                    if route:
                        findings.append({"type":"webhook_or_http_route","route_decorators":route[:4],
                            "handler":node.name,"evidence":{"path":relpath.as_posix(),"line":node.lineno}})
        # Deduplicate identical source findings while preserving order.
        unique=[]; seen=set()
        for item in findings:
            key=(item["type"],item["evidence"]["path"],item["evidence"]["line"],item.get("method"),item.get("name"),item.get("key"))
            if key not in seen: seen.add(key); unique.append(item)
        return {"status":"inspected","module":module_name,
            "summary":{"files_scanned":len(files),"files_omitted":len(omitted),"findings":len(unique),
                "outbound_calls":sum(x["type"]=="outbound_client_call" for x in unique),
                "routes":sum(x["type"]=="webhook_or_http_route" for x in unique),
                "credential_references":sum(x["type"] in {"credential_reference","credential_config_reference"} for x in unique)},
            "findings":unique[:500],"warnings":warnings[:100],"omitted_files":omitted[:100],
            "coverage":{"complete":not omitted and not warnings,"source_chars":total},
            "safety":{"network_requests_made":False,"secret_values_read":False,"dns_lookups_made":False}}
