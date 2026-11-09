"""Run automated checks, including real PostgreSQL when configured locally."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    env = os.environ.copy()
    # Only an explicit test database is used; the application's database stays untouched.
    for command in (["ruff", "check", "app", "tests", "scripts"], ["pytest", "-q"]):
        result = subprocess.run([sys.executable, "-m", *command], cwd=ROOT, env=env)
        if result.returncode:
            sys.exit(result.returncode)
