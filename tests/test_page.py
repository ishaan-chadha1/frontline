"""The page ships as one string built by hand, so a stray quote is a live bug.

A broken script does not fail loudly: `unlock` is never defined, the form
submits normally, and the page just reloads. Nothing in the server logs, nothing
in the tests, and the person in front of it concludes the product does not work.

These check the page parses before it can be deployed.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from frontline.app import INDEX_HTML


def scripts() -> list[str]:
    return re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)


def test_page_has_a_script():
    assert scripts(), "the page has no script block"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_javascript_parses():
    for i, js in enumerate(scripts()):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        result = subprocess.run(["node", "--check", path],
                                capture_output=True, text=True)
        Path(path).unlink(missing_ok=True)
        assert result.returncode == 0, (
            f"script block {i} is not valid JavaScript:\n{result.stderr}")


def test_every_handler_function_exists():
    """An onclick naming a function that was never defined fails silently."""
    js = "\n".join(scripts())
    referenced = set(re.findall(r'on(?:click|submit)="([a-zA-Z_]\w*)\(', INDEX_HTML))
    defined = set(re.findall(r"(?:function\s+|const\s+|let\s+)(\w+)\s*[=(]", js))
    defined |= set(re.findall(r"async\s+function\s+(\w+)", js))
    missing = referenced - defined
    assert not missing, f"handlers referenced but never defined: {sorted(missing)}"


def test_gate_submits_as_a_form():
    """A bare click handler leaves the mobile keyboard's Go key doing nothing."""
    assert "<form" in INDEX_HTML
    assert 'type="submit"' in INDEX_HTML


def test_inputs_resist_mobile_autocapitalisation():
    """iOS capitalises the first letter, turning a correct code into a wrong one."""
    assert 'autocapitalize="none"' in INDEX_HTML
