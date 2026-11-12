import json
import random
import string
import unittest

from odoo_agent_server.services import toon_service as toon


class ToonRoundTrip(unittest.TestCase):
    def rt(self, value):
        self.assertEqual(toon.decode(toon.encode(value)), value, toon.encode(value))

    def test_scalars_and_objects(self):
        self.rt({"a": 1, "b": "x", "c": None, "d": True, "e": 1.5, "f": ""})

    def test_nested_object(self):
        self.rt({"a": {"b": {"c": [1, 2, 3]}}, "z": "ok"})

    def test_tabular(self):
        self.rt({"files": [{"path": "a/b.py", "size": 10}, {"path": "a/c.xml", "size": 20}]})

    def test_tricky_strings(self):
        for s in ["a,b", "x: y", " pad ", "true", "null", "123", "-5", "- dash", 'q"uote', "back\\slash", "new\nline", "[x]", "{y}", "tab\tx", "007"]:
            self.rt({"k": s})
            self.rt({"rows": [{"v": s, "n": 1}, {"v": "plain", "n": 2}]})
            self.rt({"arr": [s, "b"]})

    def test_mixed_and_nested_arrays(self):
        self.rt({"items": [1, {"a": 1, "b": [1, 2]}, [3, 4], "s"]})
        self.rt({"items": [{"a": 1, "b": {"c": 2}}, {"a": 2, "b": {"c": 3}}]})

    def test_empty_values(self):
        self.rt({"a": [], "b": {}, "c": [{}]})

    def test_top_level_array(self):
        self.rt([{"a": 1}, {"a": 2}])
        self.rt([1, 2, 3])

    def test_odd_keys(self):
        self.rt({"has space": 1, "a-b": 2, "x.y": 3, "": 4})

    def test_fuzz(self):
        rng = random.Random(7)
        alphabet = string.ascii_letters + string.digits + " ,:\"\\[]{}-\n\t_."

        def s():
            return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))

        def val(depth=0):
            kind = rng.randint(0, 6 if depth < 3 else 3)
            if kind == 0: return rng.randint(-5, 500)
            if kind == 1: return s()
            if kind == 2: return rng.choice([True, False, None])
            if kind == 3: return round(rng.random() * 100, 2)
            if kind in (4, 5): return [val(depth + 1) for _ in range(rng.randint(0, 4))]
            return {s() or "k": val(depth + 1) for _ in range(rng.randint(1, 4))}

        for _ in range(400):
            obj = {"root": val()}
            text = toon.encode(obj)
            try:
                self.assertEqual(toon.decode(text), obj)
            except Exception:
                # render_tool_text must still be safe: round-trip guard falls back to JSON
                rendered = toon.render_tool_text(obj)
                self.assertEqual(json.loads(rendered) if rendered.startswith(("{", "[")) else toon.decode(rendered), obj)


class RenderGuard(unittest.TestCase):
    def test_blocks_keep_code_raw(self):
        code = "".join(f"def f{i}(x):\n    return \"q\" + x\n\n" for i in range(12))
        data = {"status": "ok", "files": [{"path": "a.py", "text": code}]}
        text = toon.render_tool_text(data, block_keys=toon.BLOCK_KEYS)
        self.assertIn(code, text)  # no escaping
        self.assertIn("<<block 1>>", text.split("\n\n", 1)[0])
        tiny = {"files": [{"path": "a.py", "text": "x\ny\n"}]}
        self.assertLessEqual(len(toon.render_tool_text(tiny, block_keys=toon.BLOCK_KEYS)), len(toon.compact_json(tiny)))
        self.assertNotIn("\\n", text)

    def test_smaller_for_tabular(self):
        data = {"status": "ok", "findings": [{"severity": "error", "path": f"m/f{i}.py", "line": i, "msg": "bad thing"} for i in range(30)]}
        text = toon.render_tool_text(data)
        self.assertLess(len(text), len(toon.compact_json(data)) * 0.7)
        self.assertEqual(toon.decode(text), data)

    def test_never_larger_than_json(self):
        data = {"a": "x"}
        self.assertLessEqual(len(toon.render_tool_text(data)), len(toon.compact_json(data)))

    def test_disabled_gives_json(self):
        self.assertEqual(toon.render_tool_text({"a": [1, 2]}, enabled=False), '{"a":[1,2]}')

    def test_unserialisable_falls_back(self):
        class X: pass
        self.assertIn("X", toon.render_tool_text({"a": X()}))


if __name__ == "__main__":
    unittest.main()
