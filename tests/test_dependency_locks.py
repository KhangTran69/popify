import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_runtime_lock_matches_every_exact_project_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked_versions = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", line.strip())
        if match:
            locked_versions[_normalized(match.group(1))] = match.group(2)

    mismatches = []
    for requirement in project["project"]["dependencies"]:
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^;\s]+)", requirement
        )
        assert match, f"dependency must use an exact version pin: {requirement}"
        name, expected = match.groups()
        actual = locked_versions.get(_normalized(name))
        if actual != expected:
            mismatches.append(f"{name}: pyproject={expected}, requirements.lock={actual}")

    assert not mismatches, "runtime lock is stale:\n" + "\n".join(mismatches)
