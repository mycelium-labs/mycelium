"""Tests for the wheel/sdist reproducibility check (#190).

The comparison logic is exercised with small synthetic archives, so these tests
need no network and no build toolchain. The end-to-end run (two clean
environments, two real builds) is performed by the ``reproducible-build`` CI job.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check-reproducible-build.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_reproducible_build", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_reproducible_build"] = module  # dataclasses needs this
    spec.loader.exec_module(module)
    return module


repro = _load_script()

METADATA = (
    "Metadata-Version: 2.4\n"
    "Name: demo\n"
    "Version: 1.0\n"
    "Requires-Dist: pydantic>=2\n"
    "Requires-Dist: pyyaml>=6\n"
    "\n"
    "Long description.\n"
)


# --------------------------------------------------------------------------- #
# Synthetic archives
# --------------------------------------------------------------------------- #


def make_wheel(
    path: Path,
    files: dict[str, bytes | str],
    *,
    date_time=(2020, 2, 2, 0, 0, 0),
    executable: frozenset[str] = frozenset(),
    reverse: bool = False,
) -> Path:
    names = list(files)[::-1] if reverse else list(files)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            data = files[name]
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.external_attr = (0o755 if name in executable else 0o644) << 16
            archive.writestr(info, data.encode() if isinstance(data, str) else data)
    return path


def make_sdist(
    path: Path,
    files: dict[str, bytes | str],
    *,
    mtime: int = 1_000_000,
    uid: int = 0,
    uname: str = "",
    gzip_mtime: int = 0,
    executable: frozenset[str] = frozenset(),
    reverse: bool = False,
) -> Path:
    names = list(files)[::-1] if reverse else list(files)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name in names:
            data = files[name]
            payload = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = mtime
            info.uid = info.gid = uid
            info.uname = info.gname = uname
            info.mode = 0o755 if name in executable else 0o644
            archive.addfile(info, io.BytesIO(payload))
    with open(path, "wb") as handle, gzip.GzipFile(
        fileobj=handle, mode="wb", mtime=gzip_mtime, filename=""
    ) as compressed:
        compressed.write(raw.getvalue())
    return path


def wheel_files(**overrides: str) -> dict[str, bytes | str]:
    files: dict[str, bytes | str] = {
        "pkg/__init__.py": "VALUE = 1\n",
        "pkg/.hidden": "dotfile\n",
        "demo-1.0.dist-info/METADATA": METADATA,
        "demo-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: hatchling 1\n",
        "demo-1.0.dist-info/RECORD": "pkg/__init__.py,sha256=aaa,10\n",
    }
    files.update(overrides)
    return files


def compare_wheels(tmp_path: Path, a: dict, b: dict, **kw):
    kw_a = {k[2:]: v for k, v in kw.items() if k.startswith("a_")}
    kw_b = {k[2:]: v for k, v in kw.items() if k.startswith("b_")}
    wheel_a = make_wheel(tmp_path / "a.whl", a, **kw_a)
    wheel_b = make_wheel(tmp_path / "b.whl", b, **kw_b)
    return repro.compare_archives(repro.read_wheel(wheel_a), repro.read_wheel(wheel_b))


def compare_sdists(tmp_path: Path, a: dict, b: dict, **kw):
    kw_a = {k[2:]: v for k, v in kw.items() if k.startswith("a_")}
    kw_b = {k[2:]: v for k, v in kw.items() if k.startswith("b_")}
    sdist_a = make_sdist(tmp_path / "a.tar.gz", a, **kw_a)
    sdist_b = make_sdist(tmp_path / "b.tar.gz", b, **kw_b)
    return repro.compare_archives(repro.read_sdist(sdist_a), repro.read_sdist(sdist_b))


# --------------------------------------------------------------------------- #
# Expected differences are normalized
# --------------------------------------------------------------------------- #


def test_identical_wheels_are_byte_identical(tmp_path):
    result = compare_wheels(tmp_path, wheel_files(), wheel_files())
    assert result.reproducible and result.bytes_identical
    assert result.normalized == []


def test_wheel_timestamps_and_order_are_normalized(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(),
        wheel_files(),
        b_date_time=(2031, 5, 6, 7, 8, 10),
        b_reverse=True,
    )
    assert result.reproducible
    assert not result.bytes_identical
    assert "zip member timestamps differ" in result.normalized
    assert "member order differs" in result.normalized


def test_sdist_mtime_ownership_and_gzip_header_are_normalized(tmp_path):
    files = {"demo-1.0/PKG-INFO": METADATA, "demo-1.0/pkg/a.py": "x = 1\n"}
    result = compare_sdists(
        tmp_path,
        files,
        files,
        b_mtime=2_000_000,
        b_uid=1000,
        b_uname="builder",
        b_gzip_mtime=1234567,
    )
    assert result.reproducible
    assert not result.bytes_identical
    assert "tar member mtimes differ" in result.normalized
    assert "tar ownership (uid/gid/uname/gname) differs" in result.normalized
    assert "gzip header (mtime/OS) differs" in result.normalized


def test_dotfile_names_are_preserved(tmp_path):
    archive = repro.read_wheel(make_wheel(tmp_path / "a.whl", wheel_files()))
    assert "pkg/.hidden" in archive.members


# --------------------------------------------------------------------------- #
# Unexpected differences fail
# --------------------------------------------------------------------------- #


def test_changed_content_fails_with_member_and_diff(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/_build_info.py": "BUILT_AT = 100\n"}),
        wheel_files(**{"pkg/_build_info.py": "BUILT_AT = 200\n"}),
    )
    assert not result.reproducible
    [difference] = result.differences
    assert difference.kind == "content"
    assert difference.member == "pkg/_build_info.py"
    assert "-BUILT_AT = 100" in difference.diff
    assert "+BUILT_AT = 200" in difference.diff


def test_binary_content_difference_reports_hashes(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/blob.bin": b"\xff\x00\x01"}),
        wheel_files(**{"pkg/blob.bin": b"\xff\x00\x02"}),
    )
    [difference] = result.differences
    assert "binary" in difference.detail
    assert difference.diff == []


def test_missing_and_extra_members_fail(tmp_path):
    only_in_a = wheel_files(**{"pkg/a_only.py": "1\n"})
    only_in_b = wheel_files(**{"pkg/b_only.py": "1\n"})
    result = compare_wheels(tmp_path, only_in_a, only_in_b)
    kinds = {(d.kind, d.member) for d in result.differences}
    assert kinds == {("missing", "pkg/a_only.py"), ("extra", "pkg/b_only.py")}


def test_executable_bit_change_fails(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(),
        wheel_files(),
        b_executable=frozenset({"pkg/__init__.py"}),
    )
    [difference] = result.differences
    assert difference.kind == "mode"
    assert difference.member == "pkg/__init__.py"


def test_sdist_content_change_fails(tmp_path):
    result = compare_sdists(
        tmp_path,
        {"demo-1.0/pkg/a.py": "x = 1\n"},
        {"demo-1.0/pkg/a.py": "x = 2\n"},
    )
    assert [d.kind for d in result.differences] == ["content"]


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_metadata_header_order_is_flagged_as_ordering(tmp_path):
    reordered = METADATA.replace(
        "Requires-Dist: pydantic>=2\nRequires-Dist: pyyaml>=6\n",
        "Requires-Dist: pyyaml>=6\nRequires-Dist: pydantic>=2\n",
    )
    result = compare_wheels(
        tmp_path,
        wheel_files(),
        wheel_files(**{"demo-1.0.dist-info/METADATA": reordered}),
    )
    [difference] = result.differences
    assert difference.kind == "metadata"
    assert any("different order" in line for line in difference.diff)


def test_metadata_value_change_lists_old_and_new_header(tmp_path):
    changed = METADATA.replace("pyyaml>=6", "pyyaml>=7")
    result = compare_sdists(
        tmp_path,
        {"demo-1.0/PKG-INFO": METADATA},
        {"demo-1.0/PKG-INFO": changed},
    )
    [difference] = result.differences
    assert difference.kind == "metadata"
    assert "- Requires-Dist: pyyaml>=6" in difference.diff
    assert "+ Requires-Dist: pyyaml>=7" in difference.diff


def test_record_difference_is_labelled_as_a_consequence(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/x.py": "1\n", "demo-1.0.dist-info/RECORD": "x,sha256=a,1\n"}),
        wheel_files(**{"pkg/x.py": "2\n", "demo-1.0.dist-info/RECORD": "x,sha256=b,1\n"}),
    )
    by_member = {d.member: d for d in result.differences}
    assert "a consequence" in by_member["demo-1.0.dist-info/RECORD"].detail
    assert "a consequence" not in by_member["pkg/x.py"].detail


def test_long_diffs_are_truncated(tmp_path):
    a = "".join(f"line {i}\n" for i in range(200))
    b = "".join(f"changed {i}\n" for i in range(200))
    result = compare_wheels(
        tmp_path, wheel_files(**{"pkg/big.py": a}), wheel_files(**{"pkg/big.py": b})
    )
    [difference] = result.differences
    assert len(difference.diff) <= repro.MAX_DIFF_LINES + 1
    assert difference.diff[-1].endswith("more diff lines")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _report(comparisons, *, strict=False, roots=()):
    return repro.format_report(
        comparisons,
        commit="abc123",
        source_date_epoch="1",
        requirements=Path("reproducible-build-requirements.txt"),
        strict_bytes=strict,
        build_roots=list(roots),
    )


def test_passing_report_says_which_kind_of_match(tmp_path):
    same = compare_wheels(tmp_path, wheel_files(), wheel_files())
    assert "PASSED" in _report([same])
    assert "byte-identical" in _report([same])

    (tmp_path / "x").mkdir()
    equivalent = compare_wheels(
        tmp_path / "x", wheel_files(), wheel_files(), b_date_time=(2031, 5, 6, 7, 8, 10)
    )
    text = _report([equivalent])
    assert "PASSED" in text and "equivalent after normalization" in text
    assert "normalized (expected): zip member timestamps differ" in text


def test_failing_report_is_actionable(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/_build_info.py": "T = 1\n"}),
        wheel_files(**{"pkg/_build_info.py": "T = 2\n"}),
    )
    text = _report([result])
    assert "FAILED" in text
    assert "[content] pkg/_build_info.py" in text
    assert "-T = 1" in text and "+T = 2" in text
    assert "What to do:" in text
    assert "--keep-artifacts" in text


def test_report_flags_an_embedded_build_directory(tmp_path):
    root = tmp_path / "work"
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/p.py": f"ROOT = {str(root / 'a')!r}\n"}),
        wheel_files(**{"pkg/p.py": f"ROOT = {str(root / 'b')!r}\n"}),
    )
    assert "embeds the source path" in _report([result], roots=[root])
    assert "embeds the source path" not in _report([result], roots=[tmp_path / "other"])


def test_require_identical_bytes_turns_equivalent_builds_into_a_failure(tmp_path):
    equivalent = compare_wheels(
        tmp_path, wheel_files(), wheel_files(), b_date_time=(2031, 5, 6, 7, 8, 10)
    )
    assert "PASSED" in _report([equivalent], strict=False)
    strict = _report([equivalent], strict=True)
    assert "FAILED" in strict and "--require-identical-bytes" in strict


def test_json_report_shape(tmp_path):
    result = compare_wheels(
        tmp_path,
        wheel_files(**{"pkg/x.py": "1\n"}),
        wheel_files(**{"pkg/x.py": "2\n"}),
    )
    payload = repro.to_json([result], commit="abc123", strict_bytes=False)
    assert json.loads(json.dumps(payload))["reproducible"] is False
    [artifact] = payload["artifacts"]
    assert artifact["kind"] == "wheel"
    assert artifact["differences"][0]["member"] == "pkg/x.py"
    assert artifact["build_a"]["sha256"] != artifact["build_b"]["sha256"]


# --------------------------------------------------------------------------- #
# Pinned toolchain
# --------------------------------------------------------------------------- #


def test_checked_in_toolchain_is_fully_pinned_and_covers_the_build_backend():
    pins = repro.read_pins(repro.DEFAULT_REQUIREMENTS)
    names = {repro._requirement_name(pin) for pin in pins}
    assert {"build", "hatchling"} <= names
    requires = repro.build_system_requires(ROOT / "sdk" / "pyproject.toml")
    repro.check_pins_cover_build_system(pins, requires)  # raises when uncovered


@pytest.mark.parametrize(
    "line", ["hatchling", "hatchling>=1.0", "hatchling~=1.0", "hatchling==1.*", "-e ."]
)
def test_unpinned_toolchain_lines_are_rejected(tmp_path, line):
    requirements = tmp_path / "req.txt"
    requirements.write_text(f"# comment\nbuild==1.0\n{line}\n", encoding="utf-8")
    with pytest.raises(repro.CheckError, match="not an exact pin"):
        repro.read_pins(requirements)


def test_build_requirement_without_a_pin_is_rejected():
    with pytest.raises(repro.CheckError, match="without a pin.*flit-core"):
        repro.check_pins_cover_build_system(["hatchling==1.0"], ["hatchling", "flit_core"])


def test_environment_markers_are_allowed_on_pins(tmp_path):
    requirements = tmp_path / "req.txt"
    requirements.write_text('tomli==2.4.1; python_version < "3.11"\n', encoding="utf-8")
    assert repro.read_pins(requirements) == ['tomli==2.4.1; python_version < "3.11"']


# --------------------------------------------------------------------------- #
# Build orchestration (no real builds)
# --------------------------------------------------------------------------- #


def test_build_once_uses_pinned_isolated_environment(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict | None, Path | None]] = []
    monkeypatch.setattr(repro, "export_commit", lambda root, sha, dest: dest.mkdir(parents=True))

    def fake_toolchain(venv: Path, requirements: Path) -> Path:
        calls.append((["toolchain", str(venv), str(requirements)], None, None))
        return venv / "python"

    monkeypatch.setattr(repro, "create_toolchain", fake_toolchain)
    monkeypatch.setattr(
        repro, "_run", lambda cmd, cwd=None, env=None: calls.append((cmd, env, cwd))
    )
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    requirements = tmp_path / "requirements.txt"
    out = repro.build_once(
        "a",
        repo_root=tmp_path,
        sha="abcdef1234567",
        source_date_epoch="1700000000",
        requirements=requirements,
        workdir=tmp_path,
        variation={"TZ": "UTC", "LC_ALL": "C"},
    )

    assert out == tmp_path / "out-a"
    assert calls[0][0] == ["toolchain", str(tmp_path / "venv-a"), str(requirements)]
    command, env, cwd = calls[1]
    assert command[1:3] == ["-m", "build"]
    assert "--no-isolation" in command  # only the pinned toolchain is visible
    assert cwd == tmp_path / "source-a-abcdef1" / "sdk"
    assert env["SOURCE_DATE_EPOCH"] == "1700000000"
    assert env["TZ"] == "UTC" and env["LC_ALL"] == "C"
    assert "PYTHONHASHSEED" not in env  # hash randomization stays on purpose


def test_the_two_builds_use_different_environments_and_directories():
    (env_a, env_b) = repro.BUILD_ENVIRONMENTS
    assert env_a["TZ"] != env_b["TZ"]


def test_pick_artifacts_requires_exactly_one_of_each(tmp_path):
    (tmp_path / "one.whl").write_bytes(b"")
    with pytest.raises(repro.CheckError, match="exactly one wheel and one sdist"):
        repro.pick_artifacts(tmp_path)
    (tmp_path / "one.tar.gz").write_bytes(b"")
    wheel, sdist = repro.pick_artifacts(tmp_path)
    assert wheel.name == "one.whl" and sdist.name == "one.tar.gz"
    (tmp_path / "two.whl").write_bytes(b"")
    with pytest.raises(repro.CheckError):
        repro.pick_artifacts(tmp_path)


def test_main_reports_a_missing_toolchain_file_without_building(tmp_path, capsys):
    status = repro.main(["--requirements", str(tmp_path / "nope.txt")])
    assert status == 2
    assert "pinned toolchain file not found" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Git export (uses the repository these tests run in)
# --------------------------------------------------------------------------- #


@pytest.fixture
def in_git_checkout():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("requires a git checkout")


def test_commit_facts_resolves_sha_and_commit_time(in_git_checkout):
    sha, epoch = repro.commit_facts(ROOT, "HEAD")
    assert len(sha) == 40 and epoch.isdigit()


def test_commit_facts_rejects_unknown_revisions(in_git_checkout):
    with pytest.raises(repro.CheckError):
        repro.commit_facts(ROOT, "definitely-not-a-revision")


def test_export_commit_extracts_only_tracked_files(in_git_checkout, tmp_path):
    sha, _ = repro.commit_facts(ROOT, "HEAD")
    destination = tmp_path / "export"
    repro.export_commit(ROOT, sha, destination)
    assert (destination / "sdk" / "pyproject.toml").is_file()
    assert not (destination / ".git").exists()
    assert not list(destination.rglob("__pycache__"))
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", sha],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "sdk/pyproject.toml" in listing
