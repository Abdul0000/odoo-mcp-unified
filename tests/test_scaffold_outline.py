import ast
import unittest

from odoo_agent_server.services import lint_service as lint
from odoo_agent_server.services import outline_service, scaffold_service
from odoo_agent_server.services.github_commit_service import GitHubRepo
from odoo_agent_server.services.workspace_service import ClientTarget, WorkspaceRegistry, WorkspaceService
from tests.support.fake_github import FakeGitHub
from tests.test_workspace_service import BASE, MANIFEST

FIELDS = [
    {"name": "partner_id", "type": "Many2one", "comodel": "res.partner"},
    {"name": "state", "type": "Selection", "options": ["draft:Draft", "done:Done"], "default": "draft"},
    {"name": "amount", "type": "Monetary"},
    {"name": "line_ids", "type": "One2many", "comodel": "my.thing.line", "inverse": "thing_id"},
]


def make(files=None, branch="staging"):
    gh = FakeGitHub(dict(files or BASE), branch=branch)
    repo = GitHubRepo("o", "r", "tok", transport=gh, sleep=lambda s: None)
    svc = WorkspaceService(lambda n: ClientTarget("Acme", repo, branch), protected="", registry=WorkspaceRegistry())
    return gh, repo, svc


class Generators(unittest.TestCase):
    def test_list_tag_and_view_mode_follow_the_series(self):
        self.assertEqual((scaffold_service.list_tag(17), scaffold_service.view_mode(17)), ("tree", "tree,form"))
        self.assertEqual((scaffold_service.list_tag(18), scaffold_service.view_mode(18)), ("list", "list,form"))

    def test_field_lines_compile_and_are_correct(self):
        lines = [scaffold_service.field_line(f) for f in scaffold_service.validate_fields(FIELDS)]
        src = "from odoo import fields, models\nclass A(models.Model):\n    _name='a'\n" + "\n".join(lines) + "\n"
        ast.parse(src)
        self.assertIn("fields.Many2one('res.partner')", src)
        self.assertIn("fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')", src)
        self.assertIn("fields.One2many('my.thing.line', 'thing_id')", src)
        self.assertIn("fields.Monetary(currency_field='currency_id')", src)

    def test_bad_specs_are_rejected_with_a_fix_hint(self):
        for bad, text in [
            ({"name": "Bad Name", "type": "Char"}, "snake_case"),
            ({"name": "x", "type": "Nope"}, "unknown type"),
            ({"name": "x", "type": "Many2one"}, "comodel"),
            ({"name": "x", "type": "One2many", "comodel": "a.b"}, "inverse"),
            ({"name": "x", "type": "Selection"}, "options"),
        ]:
            with self.assertRaises(scaffold_service.SpecError) as cm:
                scaffold_service.validate_fields([bad])
            self.assertIn(text, str(cm.exception))
        with self.assertRaises(scaffold_service.SpecError):
            scaffold_service.validate_model("notamodel")


class ScaffoldFlow(unittest.TestCase):
    def test_new_model_in_existing_module_is_lint_clean_and_wired(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.scaffold(ws, "my.thing", FIELDS, menu="Things")
        self.assertEqual(res["status"], "ok", res.get("findings"))
        self.assertEqual(res["errors"], 0)
        view = svc.registry.get(ws).view()
        self.assertIn("from . import my_thing", view["my_mod/models/__init__.py"])
        manifest = ast.literal_eval(view["my_mod/__manifest__.py"])
        self.assertIn("views/my_thing_views.xml", manifest["data"])
        self.assertIn("access_my_thing_user,my.thing user,model_my_thing,base.group_user,1,1,1,1", view["my_mod/security/ir.model.access.csv"])
        self.assertEqual(res["series"]["value"], 18)
        self.assertIn("<list>", view["my_mod/views/my_thing_views.xml"])  # manifest version 18.0 -> list views
        self.assertIn('"list,form"' if False else "list,form", view["my_mod/views/my_thing_views.xml"])
        self.assertEqual(lint.lint_xml("v.xml", view["my_mod/views/my_thing_views.xml"]), [])

    def test_series_17_uses_tree(self):
        files = dict(BASE)
        files["my_mod/__manifest__.py"] = MANIFEST.replace("18.0", "17.0")
        gh, repo, svc = make(files)
        ws = svc.open("Acme", "my_mod")["ws_id"]
        svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        xml = svc.registry.get(ws).view()["my_mod/views/my_thing_views.xml"]
        self.assertIn("<tree>", xml)
        self.assertIn("tree,form", xml)

    def test_mail_adds_chatter_mixin_and_dependency(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}], mail=True)
        self.assertEqual(res["status"], "ok", res.get("findings"))
        view = svc.registry.get(ws).view()
        self.assertIn("_inherit = ['mail.thread', 'mail.activity.mixin']", view["my_mod/models/my_thing.py"])
        self.assertIn("oe_chatter", view["my_mod/views/my_thing_views.xml"])
        self.assertIn("mail", ast.literal_eval(view["my_mod/__manifest__.py"])["depends"])

    def test_brand_new_module_gets_manifest_inits_and_everything(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "fresh_mod", create=True)["ws_id"]
        bad = svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        self.assertEqual(bad["error_code"], "bad_spec")
        self.assertIn("series", bad["error"])
        res = svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}], series=18)
        self.assertEqual(res["status"], "ok", res.get("findings"))
        view = svc.registry.get(ws).view()
        manifest = ast.literal_eval(view["fresh_mod/__manifest__.py"])
        self.assertEqual(manifest["version"], "18.0.1.0.0")
        self.assertEqual(manifest["data"][0], "security/ir.model.access.csv")
        self.assertIn("from . import models", view["fresh_mod/__init__.py"])
        self.assertEqual(svc.commit(ws, "new module")["status"], "pushed")
        self.assertIn("fresh_mod/models/my_thing.py", gh.head_files("staging"))

    def test_refuses_to_clobber_and_to_duplicate_access(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        self.assertEqual(svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])["status"], "ok")
        again = svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        self.assertEqual(again["error_code"], "exists")

    def test_bad_spec_returns_structured_error_and_changes_nothing(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.scaffold(ws, "my.thing", [{"name": "x", "type": "Many2one"}])
        self.assertEqual(res["error_code"], "bad_spec")
        self.assertEqual(svc.lint(ws)["changed_files"], 0)


class ScaffoldGuards(unittest.TestCase):
    def test_name_field_is_added_when_missing(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        src = svc.registry.get(ws).view()["my_mod/models/my_thing.py"]
        self.assertIn("name = fields.Char(required=True)", src)
        self.assertLess(src.index("name = fields"), src.index("code = fields"))

    def test_existing_access_rule_is_not_duplicated(self):
        files = dict(BASE)
        files["my_mod/security/ir.model.access.csv"] += "access_my_thing_user,x,model_my_thing,base.group_user,1,1,1,1\n"
        gh, repo, svc = make(files)
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        self.assertEqual(res["error_code"], "access_exists")
        self.assertEqual(svc.lint(ws)["changed_files"], 0)


class ExtendFlow(unittest.TestCase):
    def test_second_extend_with_same_name_is_refused(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        self.assertEqual(svc.extend(ws, "res.partner", [{"name": "x_a", "type": "Char"}])["status"], "ok")
        again = svc.extend(ws, "res.partner", [{"name": "x_b", "type": "Char"}])
        self.assertEqual(again["error_code"], "exists")
        self.assertEqual(svc.extend(ws, "res.partner", [{"name": "x_b", "type": "Char"}], file_name="partner_more")["status"], "ok")

    def test_extend_existing_model_with_view_inheritance(self):
        files = dict(BASE)
        gh, repo, svc = make(files)
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.extend(ws, "sale.order", [{"name": "x_ref", "type": "Char"}, {"name": "x_flag", "type": "Boolean"}], view_ref="sale.view_order_form", after_field="partner_id")
        self.assertEqual(res["status"], "ok", res.get("findings"))
        view = svc.registry.get(ws).view()
        self.assertIn("_inherit = 'sale.order'", view["my_mod/models/sale_order_ext.py"])
        xml = view["my_mod/views/sale_order_ext_views.xml"]
        self.assertIn("//field[@name='partner_id']", xml)
        self.assertIn('ref="sale.view_order_form"', xml)
        self.assertIn("views/sale_order_ext_views.xml", ast.literal_eval(view["my_mod/__manifest__.py"])["data"])
        self.assertIn("from . import sale_order_ext", view["my_mod/models/__init__.py"])

    def test_view_ref_without_anchor_is_a_clear_error(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.extend(ws, "sale.order", [{"name": "x", "type": "Char"}], view_ref="sale.view_order_form")
        self.assertEqual(res["error_code"], "bad_spec")
        self.assertIn("after_field", res["error"])

    def test_extend_without_view_only_adds_fields(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.extend(ws, "res.partner", [{"name": "x_code", "type": "Char"}])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["generated"], ["my_mod/models/res_partner_ext.py"])


class Outline(unittest.TestCase):
    def test_outline_is_compact_and_accurate(self):
        gh, repo, svc = make()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        svc.scaffold(ws, "my.thing", FIELDS)
        out = svc.outline(ws)
        by_model = {m["model"]: m for m in out["models"]}
        self.assertIn("partner_id:Many2one(res.partner)", by_model["my.thing"]["fields"])
        self.assertEqual(by_model["my.thing"]["kind"], "define")
        self.assertEqual(by_model["sale.order"]["kind"], "extend")
        self.assertIn("note_x:Char", by_model["sale.order"]["fields"])
        self.assertEqual(out["manifest"]["depends"], ["base", "sale"])
        ids = {r["id"] for r in out["records"]}
        self.assertTrue({"view_x", "view_my_thing_form", "action_my_thing", "menu_my_thing"} <= ids)
        self.assertEqual(out["series"], 18)

    def test_outline_reports_unparseable_files_instead_of_crashing(self):
        files = dict(BASE)
        files["my_mod/models/bad.py"] = "def x(:\n"
        gh, repo, svc = make(files)
        ws = svc.open("Acme", "my_mod")["ws_id"]
        out = svc.outline(ws)
        self.assertEqual(out["unparseable"][0]["file"], "models/bad.py")

    def test_outline_is_much_smaller_than_the_source(self):
        src = "from odoo import fields, models\n\n\nclass Big(models.Model):\n    _name = 'big.model'\n" + "".join(f"    f{i} = fields.Char(string='Field number {i}', help='some long help text here')\n\n    def m{i}(self):\n        return self.f{i}\n\n" for i in range(60))
        out = outline_service.outline_python("big.py", src)
        import json
        small = json.dumps({k: v for k, v in out["classes"][0].items() if k != "_fields"})
        self.assertLess(len(small), len(src) * 0.35)


if __name__ == "__main__":
    unittest.main()


CORE = {
    "odoo/addons/base/__manifest__.py": "{'name': 'Base', 'depends': []}\n",
    "odoo/addons/base/models/__init__.py": "",
    "odoo/addons/base/models/res_partner.py": "from odoo import fields, models\n\n\nclass Partner(models.Model):\n    _name = 'res.partner'\n    name = fields.Char()\n    email = fields.Char()\n    parent_id = fields.Many2one('res.partner')\n",
    "addons/product/__manifest__.py": "{'name': 'Product', 'depends': ['base']}\n",
    "addons/product/models/product_template.py": "from odoo import fields, models\n\n\nclass ProductTemplate(models.Model):\n    _name = 'product.template'\n    list_price = fields.Float()\n",
    "addons/sale/__manifest__.py": "{'name': 'Sales', 'depends': ['base', 'product']}\n",
    "addons/sale/models/sale_order.py": "from odoo import api, fields, models\n\n\nclass SaleOrder(models.Model):\n    _name = 'sale.order'\n    partner_id = fields.Many2one('res.partner', required=True)\n    state = fields.Selection([('draft', 'Quotation'), ('sale', 'Sales Order')])\n    amount_total = fields.Monetary(compute='_compute_amount', store=True)\n",
    "addons/account/__manifest__.py": "{'name': 'Invoicing', 'depends': ['base']}\n",
    "addons/account/models/account_move.py": "from odoo import fields, models\n\n\nclass AccountMove(models.Model):\n    _name = 'account.move'\n    name = fields.Char()\n",
}


def make_with_core():
    gh, repo, svc = make()
    core_gh = FakeGitHub(dict(CORE), branch="18.0")
    from odoo_agent_server.services import symbol_service
    symbol_service._CACHE.clear()
    symbol_service._HEADS.clear()
    core_repo = GitHubRepo("odoo", "odoo", "tok", transport=core_gh, sleep=lambda s: None)
    svc.core_repo_factory = lambda ws: core_repo
    return gh, core_gh, svc


class Symbol(unittest.TestCase):
    def test_model_and_field_found_in_core_with_types(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.symbol(ws, "sale.order", "partner_id")
        self.assertTrue(res["found"] and res["field_found"])
        self.assertEqual(res["source"], "odoo/odoo@18.0")
        d = res["field_defs"][0]
        self.assertEqual((d["type"], d["comodel"], d["module"]), ("Many2one", "res.partner", "sale"))
        sel = svc.symbol(ws, "sale.order", "state")["field_defs"][0]
        self.assertEqual(sel["options"], ["draft", "sale"])

    def test_misspelled_field_gets_suggestions(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.symbol(ws, "sale.order", "partner")
        self.assertFalse(res["field_found"])
        self.assertIn("partner_id", res["did_you_mean"])

    def test_model_outside_depends_closure_is_not_found_with_actionable_hint(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.symbol(ws, "account.move")
        self.assertFalse(res["found"])
        self.assertIn("depends", res["hint"])
        self.assertIn("coverage", res)

    def test_unknown_model_suggests_close_names(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        self.assertIn("sale.order", svc.symbol(ws, "sale.ordr")["did_you_mean"])

    def test_local_models_are_visible_and_extensions_merge(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        svc.scaffold(ws, "my.thing", [{"name": "code", "type": "Char"}])
        local = svc.symbol(ws, "my.thing", "code")
        self.assertTrue(local["field_found"])
        self.assertEqual(local["defined_in"][0]["module"], "(this module)")
        merged = svc.symbol(ws, "sale.order", "note_x")  # added by the module's own _inherit
        self.assertTrue(merged["field_found"])
        self.assertEqual({d["module"] for d in merged["defined_in"]}, {"sale", "(this module)"})

    def test_second_lookup_hits_the_cache(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]
        svc.symbol(ws, "sale.order")
        core_gh.calls.clear()
        svc.symbol(ws, "res.partner", "email")
        self.assertEqual(len(core_gh.calls), 0)

    def test_series_required_for_brand_new_module(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "brand_new", create=True)["ws_id"]
        self.assertEqual(svc.symbol(ws, "res.partner")["error_code"], "bad_spec")
        self.assertTrue(svc.symbol(ws, "res.partner", series=18)["found"])


class SymbolTransitive(unittest.TestCase):
    def test_transitive_depends_are_searched(self):
        gh, core_gh, svc = make_with_core()
        ws = svc.open("Acme", "my_mod")["ws_id"]  # depends: base, sale -> sale depends on product
        res = svc.symbol(ws, "product.template", "list_price")
        self.assertTrue(res["field_found"], res)
        self.assertEqual(res["field_defs"][0]["module"], "product")
