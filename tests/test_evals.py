import unittest

from tests.evals.scenarios import run_all


class Scenarios(unittest.TestCase):
    def test_every_scenario_passes_within_its_call_budget(self):
        for r in run_all():
            self.assertTrue(r.ok, f"{r.name}: {r.note}")


if __name__ == "__main__":
    unittest.main()
