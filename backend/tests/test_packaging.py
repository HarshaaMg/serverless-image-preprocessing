"""Packaging checks: the job-API Lambda zip does NOT contain Pillow.

These would have caught the real-AWS failure "No module named 'PIL'" that the
mocked tests missed (Pillow is installed on the developer machine).
"""

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# INTERVIEW:
# This test exists because of a real production bug: the job-API Lambda crashed on AWS
# with "No module named 'PIL'" although all mocked tests passed, since Pillow is
# installed on the developer machine. The script below imports the module in a NEW
# Python process where any attempt to import PIL is blocked, imitating the job-API
# Lambda package (which does not contain Pillow).
BLOCK_PIL = """
import sys
class BlockPIL:
    def find_spec(self, name, path=None, target=None):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("No module named 'PIL'")
sys.meta_path.insert(0, BlockPIL())
sys.path.insert(0, {src!r})
import imgproc.{module}
print("ok")
"""


def _import_without_pil(module: str) -> subprocess.CompletedProcess:
    code = BLOCK_PIL.format(src=str(SRC), module=module)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_job_api_imports_without_pillow():
    result = _import_without_pil("job_api")
    assert result.returncode == 0, result.stderr


def test_validation_and_tokens_import_without_pillow():
    for module in ("validation", "tokens", "jobs", "config", "limits"):
        result = _import_without_pil(module)
        assert result.returncode == 0, f"{module}: {result.stderr}"


def test_block_actually_works():
    # Sanity check: the processor needs Pillow, so it must fail under the block.
    result = _import_without_pil("processor")
    assert result.returncode != 0 and "PIL" in result.stderr
