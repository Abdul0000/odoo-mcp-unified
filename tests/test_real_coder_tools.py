"""Tool-level 'real coder' behaviour: self-healing edits and message-less commits."""
import unittest

from odoo_agent_server.services.workspace_service import apply_edit, apply_edit_ex
from tests.test_workspace_service import make

CODE = "class A:\n    def run(self):\n        x = 1\n        return x\n"


class HealedEdits(unittest.TestCase):
    def test_wrong_indent_is_healed_and_reindented(self):
        # agent sent the block with no indentation at all
        out, err, healed = apply_edit_ex(
            CODE, {"path": "x", "search": "x = 1\nreturn x", "replace": "x = 2\nreturn x + 1"}
        )
        self.assertIsNone(err)
        self.assertTrue(healed)
        self.assertEqual(out, "class A:\n    def run(self):\n        x = 2\n        return x + 1\n")

    def test_extra_indent_is_healed(self):
        out, err, healed = apply_edit_ex(
            CODE, {"path": "x", "search": "            x = 1", "replace": "            x = 5"}
        )
        self.assertIsNone(err)
        self.assertTrue(healed)
        self.assertEqual(out, "class A:\n    def run(self):\n        x = 5\n        return x\n")

    def test_trailing_whitespace_in_search_is_healed(self):
        out, err, healed = apply_edit_ex(CODE, {"path": "x", "search": "        x = 1   ", "replace": "        x = 3"})
        self.assertIsNone(err)
        self.assertTrue(healed)
        self.assertIn("x = 3", out)

    def test_delete_block_via_empty_replace(self):
        out, err, healed = apply_edit_ex(CODE, {"path": "x", "search": "            x = 1", "replace": ""})
        self.assertIsNone(err)
        self.assertTrue(healed)
        self.assertEqual(out, "class A:\n    def run(self):\n        return x\n")

    def test_exact_match_is_not_reported_healed(self):
        _out, err, healed = apply_edit_ex(CODE, {"path": "x", "search": "        x = 1", "replace": "        x = 9"})
        self.assertIsNone(err)
        self.assertFalse(healed)

    def test_ambiguous_whitespace_match_is_never_guessed(self):
        text = "def a():\n    x = 1\n\ndef b():\n    x = 1\n"
        out, err, healed = apply_edit_ex(text, {"path": "x", "search": "x = 1", "replace": "x = 2"})
        # exact substring occurs twice -> normal match_count error, no healing
        self.assertEqual(err["code"], "match_count")
        self.assertFalse(healed)
        text2 = "def a():\n    x = 1\n\ndef b():\n        x = 1\n"
        out2, err2, healed2 = apply_edit_ex(text2, {"path": "x", "search": "  x  =  1", "replace": "x = 2"})
        self.assertEqual(err2["code"], "no_match")  # different inner whitespace is a real mismatch
        self.assertFalse(healed2)

    def test_content_mismatch_is_still_an_error(self):
        out, err = apply_edit(CODE, {"path": "x", "search": "x = 99", "replace": "x = 2"})
        self.assertEqual(err["code"], "no_match")

    def test_regex_edits_are_never_healed(self):
        out, err, healed = apply_edit_ex(CODE, {"path": "x", "search": r"^x = 1$", "replace": "x = 2", "regex": True})
        self.assertEqual(err["code"], "no_match")
        self.assertFalse(healed)

    def test_crlf_file_keeps_line_endings(self):
        text = CODE.replace("\n", "\r\n")
        out, err, healed = apply_edit_ex(text, {"path": "x", "search": "x = 1\nreturn x", "replace": "x = 2\nreturn x"})
        self.assertIsNone(err)
        self.assertNotIn("\n\n", out.replace("\r\n", ""))
        self.assertEqual(out, "class A:\r\n    def run(self):\r\n        x = 2\r\n        return x\r\n")


class WsEditReportsHealing(unittest.TestCase):
    def test_edit_applies_and_flags_healed(self):
        _gh, _repo, svc, _reg = make()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.edit(ws_id, [{"path": "models/sale.py", "search": "_inherit = 'sale.order'\n\nnote_x = fields.Char()", "replace": "_inherit = 'sale.order'\n\nnote_x = fields.Char()\nnote_y = fields.Integer()"}])
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(len(res["healed"]), 1)
        self.assertIn("note_y", str(svc.read(ws_id, ["models/sale.py"])))
        self.assertEqual(res["errors"], 0, res["findings"])

    def test_exact_edit_has_no_healed_key(self):
        _gh, _repo, svc, _reg = make()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.edit(ws_id, [{"path": "models/sale.py", "search": "note_x = fields.Char()", "replace": "note_x = fields.Char()\n    note_y = fields.Integer()"}])
        self.assertNotIn("healed", res)


class CommitWithoutMessage(unittest.TestCase):
    def test_message_is_generated_and_commit_lands(self):
        gh, _repo, svc, _reg = make()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        svc.edit(ws_id, [{"path": "models/sale.py", "search": "note_x = fields.Char()", "replace": "note_x = fields.Char()\n    note_y = fields.Integer()"}])
        out = svc.commit(ws_id)
        self.assertEqual(out["status"], "pushed", out)
        self.assertTrue(out["commit"])

    def test_blank_message_also_falls_back(self):
        _gh, _repo, svc, _reg = make()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        svc.edit(ws_id, [{"path": "models/sale.py", "search": "note_x = fields.Char()", "replace": "note_x = fields.Char()\n    note_y = fields.Integer()"}])
        self.assertEqual(svc.commit(ws_id, "  ")["status"], "pushed")


class HealedEditSurvivesRestart(unittest.TestCase):
    def test_healed_edit_replays_after_restart_and_commits(self):
        _gh, _repo, svc, reg = make()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.edit(ws_id, [{"path": "models/sale.py", "search": "            note_x = fields.Char()", "replace": "            note_x = fields.Char()\n            note_y = fields.Integer()"}])
        self.assertEqual(len(res["healed"]), 1)
        reg.clear()  # server restarted: workspace rebuilt from the WIP journal
        self.assertIn("    note_y = fields.Integer()", svc.read(ws_id, ["models/sale.py"])["files"][0]["text"])
        self.assertEqual(svc.commit(ws_id)["status"], "pushed")


if __name__ == "__main__":
    unittest.main()
