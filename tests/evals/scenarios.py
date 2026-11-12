"""Scripted end-to-end tasks for the ws_* tooling, run against in-memory fakes of GitHub.

This measures the TOOLS (tool calls, API calls, lint outcome, push) - it does not measure a language model.
To evaluate a model, replay the same task prompts through the real connector and compare against these budgets.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from typing import Callable

from odoo_agent_server.services import symbol_service
from odoo_agent_server.services.github_commit_service import GitHubRepo
from odoo_agent_server.services.workspace_service import ClientTarget, WorkspaceRegistry, WorkspaceService
from tests.support.fake_github import FakeGitHub
from tests.test_scaffold_outline import CORE
from tests.test_workspace_service import BASE


@dataclass
class Env:
    gh: FakeGitHub
    repo: GitHubRepo
    svc: WorkspaceService
    reg: WorkspaceRegistry
    tool_calls: int = 0
    log: list[str] = field(default_factory=list)

    def call(self, name: str, *args, **kwargs):
        self.tool_calls += 1
        self.log.append(name)
        return getattr(self.svc, name)(*args, **kwargs)


def make_env() -> Env:
    gh = FakeGitHub(dict(BASE), branch="staging")
    repo = GitHubRepo("o", "r", "tok", transport=gh, sleep=lambda s: None)
    reg = WorkspaceRegistry()
    svc = WorkspaceService(lambda n: ClientTarget("Acme", repo, "staging"), protected="", registry=reg)
    symbol_service._CACHE.clear()
    symbol_service._HEADS.clear()
    core = GitHubRepo("odoo", "odoo", "tok", transport=FakeGitHub(dict(CORE), branch="18.0"), sleep=lambda s: None)
    svc.core_repo_factory = lambda ws: core
    return Env(gh, repo, svc, reg)


@dataclass
class Result:
    name: str
    ok: bool
    tool_calls: int
    api_calls: int
    seconds: float
    note: str = ""


def s_new_model(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    res = e.call("scaffold", ws, "my.ticket", [{"name": "partner_id", "type": "Many2one", "comodel": "res.partner"}, {"name": "state", "type": "Selection", "options": ["new:New", "done:Done"]}], mail=True, menu="Tickets")
    assert res["status"] == "ok" and res["errors"] == 0, res
    out = e.call("commit", ws, "add ticket model")
    assert out["status"] == "pushed", out
    files = e.gh.head_files("staging")
    assert "views/my_ticket_views.xml" in ast.literal_eval(files["my_mod/__manifest__.py"])["data"]
    return f"{len(res['generated'])} files generated from a 2-field spec"


def s_extend_with_verified_field(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    chk = e.call("symbol", ws, "sale.order", "partner_id")
    assert chk["field_found"], chk
    res = e.call("extend", ws, "sale.order", [{"name": "x_ref", "type": "Char"}], view_ref="sale.view_order_form", after_field="partner_id")
    assert res["status"] == "ok", res
    assert e.call("commit", ws, "x_ref on sale.order")["status"] == "pushed"
    return "anchor field verified against upstream source before the view inherit"


def s_catch_wrong_field_before_push(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    bad = e.call("symbol", ws, "sale.order", "partner")
    assert not bad["field_found"] and "partner_id" in bad["did_you_mean"], bad
    return "typo 'partner' caught, suggestion partner_id (no failed Odoo.sh build)"


def s_fix_loop_after_lint_error(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    bad = e.call("edit", ws, [{"path": "views/sale_views.xml", "search": "<form/>", "replace": "<form><xpath expr=\"//div[\" position=\"after\"/></form>"}])
    assert bad["status"] == "ok_with_errors" and any(f["code"] == "xpath_syntax" for f in bad["findings"])
    assert e.call("commit", ws, "x")["status"] == "blocked_by_lint"
    fixed = e.call("edit", ws, [{"path": "views/sale_views.xml", "search": "<xpath expr=\"//div[\" position=\"after\"/>", "replace": "<field name=\"name\"/>"}])
    assert fixed["status"] == "ok", fixed
    assert e.call("commit", ws, "fix view")["status"] == "pushed"
    return "lint error caught in 1 call, fixed in 1 call, never reached Odoo.sh"


def s_delete_view_unwires(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    assert e.call("edit", ws, [{"path": "views/sale_views.xml", "op": "delete"}])["errors"] == 0
    assert e.call("commit", ws, "drop view")["status"] == "pushed"
    assert "views/sale_views.xml" not in ast.literal_eval(e.gh.head_files("staging")["my_mod/__manifest__.py"])["data"]
    return "manifest entry removed automatically"


def s_upstream_moved(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    e.call("edit", ws, [{"path": "models/sale.py", "search": "Char()", "replace": "Text()"}])
    e.gh.push_external("staging", {"other/__init__.py": "# teammate\n"})
    out = e.call("commit", ws, "text field")
    assert out["status"] == "pushed" and out["rebased_onto_newer_head"], out
    return "teammate pushed mid-task; edits replayed on the new head"


def s_restart_mid_task(e: Env) -> str:
    ws = e.call("open", "Acme", "my_mod")["ws_id"]
    e.call("scaffold", ws, "my.note", [{"name": "body", "type": "Text"}])
    e.reg.clear()  # Render spun down
    out = e.call("commit", ws, "note model")
    assert out["status"] == "pushed", out
    assert "my_mod/models/my_note.py" in e.gh.head_files("staging")
    return "workspace rebuilt from the WIP branch after losing all RAM"


SCENARIOS: list[tuple[str, Callable[[Env], str], dict[str, int]]] = [
    ("new_model_with_views_and_chatter", s_new_model, {"tool_calls": 3, "api_calls": 32}),
    ("extend_model_verify_then_inherit_view", s_extend_with_verified_field, {"tool_calls": 4, "api_calls": 30}),
    ("catch_wrong_field_before_push", s_catch_wrong_field_before_push, {"tool_calls": 2, "api_calls": 12}),
    ("fix_loop_after_lint_error", s_fix_loop_after_lint_error, {"tool_calls": 5, "api_calls": 30}),
    ("delete_view_unwires_manifest", s_delete_view_unwires, {"tool_calls": 3, "api_calls": 26}),
    ("upstream_moved_during_task", s_upstream_moved, {"tool_calls": 3, "api_calls": 32}),
    ("restart_mid_task", s_restart_mid_task, {"tool_calls": 3, "api_calls": 46}),
]


def run_all() -> list[Result]:
    results = []
    for name, fn, budget in SCENARIOS:
        env = make_env()
        start = time.time()
        note, ok = "", True
        try:
            note = fn(env)
        except AssertionError as exc:
            ok, note = False, f"assertion failed: {str(exc)[:160]}"
        api = env.repo.calls  # calls to the client's repo (upstream-source lookups use a separate repo)
        if ok and (env.tool_calls > budget["tool_calls"] or api > budget["api_calls"]):
            ok, note = False, f"over budget: {env.tool_calls} tool calls (max {budget['tool_calls']}), {api} API calls (max {budget['api_calls']})"
        results.append(Result(name, ok, env.tool_calls, api, time.time() - start, note))
    return results


def render(results: list[Result]) -> str:
    rows = [f"{'scenario':42s} {'ok':3s} {'tools':>5s} {'api':>4s}  note"]
    for r in results:
        rows.append(f"{r.name:42s} {'yes' if r.ok else 'NO':3s} {r.tool_calls:5d} {r.api_calls:4d}  {r.note}")
    passed = sum(r.ok for r in results)
    rows.append(f"\n{passed}/{len(results)} scenarios passed within budget")
    return "\n".join(rows)
