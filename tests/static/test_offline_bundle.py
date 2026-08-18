"""반입 번들이 실제로 반입될 수 있는지 검사한다 (R32).

spec §11.4의 반입 수단은 git 단방향인데 `vendor/`가 .gitignore에 있었다. 즉
`git ls-files vendor/`가 0이고, 사내에서 새로 클론하면 런북 1단계
(`pip install --no-index --find-links vendor/`)가 빈 디렉터리를 보고 실패한다.
게다가 pytest는 requirements.txt에도, 번들에도 없어 7단계
(`JIRA_FIXTURES=captured pytest`)도 실패했다. 눈으로만 볼 수 있던 이 두 가지를
스위트가 잡게 한다.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
VENDOR = ROOT / "vendor"


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(re.split(r"[=<>!~\[]", line)[0].strip().lower().replace("-", "_"))
    return names


def _wheel_names() -> set[str]:
    return {p.name.split("-")[0].lower().replace("-", "_")
            for p in VENDOR.glob("*.whl")}


def test_dev_requirements_extend_runtime_requirements_with_pytest():
    """런타임과 테스트 의존성은 구분해서 둔다 — 사내 서버에 pytest를 강제하지 않되,
    런북 7단계가 쓸 수 있어야 한다."""
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    assert "pytest" in _requirement_names(ROOT / "requirements-dev.txt")


def test_every_declared_requirement_has_a_wheel_in_the_bundle():
    missing = (_requirement_names(ROOT / "requirements.txt")
               | _requirement_names(ROOT / "requirements-dev.txt")) - _wheel_names()
    assert not missing, f"vendor/에 휠이 없다: {sorted(missing)} — `make vendor` 실행"


def test_the_wheel_bundle_is_tracked_by_git():
    """.gitignore 규칙은 유지하되(로컬 휠 오염 방지) 번들은 `git add -f`로 추적한다.
    추적되지 않으면 사내 클론에 vendor/가 존재하지 않는다."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "vendor/"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    tracked = {Path(line).name for line in out.splitlines() if line.endswith(".whl")}
    assert tracked, "git ls-files vendor/ 가 비어 있다 — `git add -f vendor/`"
    assert "pytest" in {n.split("-")[0].lower() for n in tracked}
