"""Version consistency tests."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _read_version_txt():
    return (SRC / "VERSION.txt").read_text().strip()


def test_version_txt_exists():
    assert (SRC / "VERSION.txt").exists()


def test_version_txt_format():
    """VERSION.txt must be a valid semver-like string (N.N.N)."""
    ver = _read_version_txt()
    assert re.match(r"^\d+\.\d+\.\d+$", ver), f"Bad version format: {ver}"


def test_spec_version_matches():
    """PyInstaller spec CFBundleShortVersionString must match VERSION.txt."""
    ver = _read_version_txt()
    spec = (SRC / "novastar_monitor.spec").read_text()
    match = re.search(r"CFBundleShortVersionString.*?'(\d+\.\d+\.\d+)'", spec)
    assert match, "CFBundleShortVersionString not found in spec"
    assert match.group(1) == ver, f"Spec has {match.group(1)}, VERSION.txt has {ver}"


def test_spec_bundle_version_matches():
    """PyInstaller spec CFBundleVersion must match VERSION.txt."""
    ver = _read_version_txt()
    spec = (SRC / "novastar_monitor.spec").read_text()
    match = re.search(r"CFBundleVersion.*?'(\d+\.\d+\.\d+)'", spec)
    assert match, "CFBundleVersion not found in spec"
    assert match.group(1) == ver, f"Spec has {match.group(1)}, VERSION.txt has {ver}"
