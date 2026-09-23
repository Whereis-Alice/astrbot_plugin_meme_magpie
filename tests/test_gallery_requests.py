"""Run the actual dashboard request handler against out-of-order responses."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_gallery_requests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard request race checks")
    script = Path(__file__).parent / "web" / "test_gallery_requests.cjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
