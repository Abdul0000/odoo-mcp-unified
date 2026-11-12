"""python -m tests.evals.run  - prints the scenario table; exit code 1 if any scenario fails."""
import sys

from tests.evals.scenarios import render, run_all

if __name__ == "__main__":
    results = run_all()
    print(render(results))
    sys.exit(0 if all(r.ok for r in results) else 1)
