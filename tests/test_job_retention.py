import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from odoo_agent_server.services.job_retention import JobSignal, prune_job_dir

ACTIVE = {"queued", "running"}


def make_jobs(directory: Path, n: int, status="passed", offset=0, size=2000):
    for i in range(n):
        (directory / f"job{offset + i:04d}.json").write_text(json.dumps({"id": f"job{offset + i}", "created_at": f"2026-01-01T00:{(offset + i) // 60:02d}:{(offset + i) % 60:02d}", "status": status, "pad": "x" * size}))


class Prune(unittest.TestCase):
    def test_under_cap_opens_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_jobs(d, 30)
            opened = []
            orig = Path.read_text
            Path.read_text = lambda self, *a, **k: (opened.append(self), orig(self, *a, **k))[1]
            try:
                self.assertEqual(prune_job_dir(d, 50, ACTIVE), 0)
            finally:
                Path.read_text = orig
            self.assertEqual(opened, [])

    def test_trims_to_low_water_mark_oldest_first_and_keeps_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_jobs(d, 1, status="running", offset=0)  # oldest, but active
            make_jobs(d, 40, offset=1)
            removed = prune_job_dir(d, 30, ACTIVE)
            left = sorted(p.name for p in d.glob("*.json"))
            self.assertEqual(len(left), 27)  # 90% of 30
            self.assertEqual(removed, 41 - 27)
            self.assertIn("job0000.json", left)  # running job survived
            self.assertNotIn("job0001.json", left)  # oldest terminal removed
            self.assertIn("job0040.json", left)

    def test_no_rescan_on_the_next_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_jobs(d, 40)
            prune_job_dir(d, 30, ACTIVE)
            make_jobs(d, 2, offset=100)
            self.assertEqual(prune_job_dir(d, 30, ACTIVE), 0)  # 29 <= 30: nothing opened, nothing removed

    def test_only_active_jobs_over_cap_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            make_jobs(d, 25, status="running")
            self.assertEqual(prune_job_dir(d, 20, ACTIVE), 0)
            self.assertEqual(len(list(d.glob("*.json"))), 25)


class Signal(unittest.TestCase):
    def test_wakes_immediately_on_write(self):
        sig = JobSignal()
        seen = sig.version("j")
        threading.Timer(0.05, lambda: sig.bump("j")).start()
        t = time.monotonic()
        self.assertTrue(sig.wait_change("j", seen, timeout=5))
        self.assertLess(time.monotonic() - t, 1.0)

    def test_times_out_without_a_write(self):
        sig = JobSignal()
        t = time.monotonic()
        self.assertFalse(sig.wait_change("j", sig.version("j"), timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - t, 0.15)

    def test_no_lost_wakeup_between_check_and_wait(self):
        sig = JobSignal()
        seen = sig.version("j")
        sig.bump("j")  # write lands after we read the version but before we wait
        self.assertTrue(sig.wait_change("j", seen, timeout=0.01))

    def test_bounded_memory(self):
        sig = JobSignal(max_tracked=10)
        for i in range(100):
            sig.bump(f"j{i}")
        self.assertLessEqual(len(sig._versions), 11)


if __name__ == "__main__":
    unittest.main()
