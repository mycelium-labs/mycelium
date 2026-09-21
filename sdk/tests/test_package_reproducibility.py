"""Tests for package reproducibility verification and archive normalization."""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / ".github" / "scripts"


def _load_script():
    script_path = SCRIPTS_DIR / "check-package-reproducibility.py"
    spec = importlib.util.spec_from_file_location("check_pkg_repro", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_pkg_repro"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


def _create_mock_wheel(
    path: Path,
    files: dict[str, bytes],
    date_time=(2020, 2, 2, 0, 0, 0),
) -> None:
    """Create a minimal mock wheel archive with specified files and timestamp."""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.external_attr = 0o644 << 16
            z.writestr(info, data)


def _create_mock_sdist(
    path: Path,
    files: dict[str, bytes],
    mtime: int = 1580601600,
    uid: int = 1000,
    gid: int = 1000,
    mode: int = 0o644,
) -> None:
    """Create a minimal mock sdist tar.gz archive with specified files and metadata."""
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            info.uid = uid
            info.gid = gid
            info.mode = mode
            t.addfile(info, io.BytesIO(data))


class TestPermissionNormalization:
    """Test standard POSIX permission normalization."""

    def test_regular_file_mode_normalization(self):
        # Non-executable permissions normalize to 0o644
        assert mod.normalize_mode(0o644, is_dir=False) == 0o644
        assert mod.normalize_mode(0o600, is_dir=False) == 0o644
        assert mod.normalize_mode(0o666, is_dir=False) == 0o644

        # Executable permissions normalize to 0o755
        assert mod.normalize_mode(0o755, is_dir=False) == 0o755
        assert mod.normalize_mode(0o700, is_dir=False) == 0o755
        assert mod.normalize_mode(0o775, is_dir=False) == 0o755

    def test_directory_mode_normalization(self):
        assert mod.normalize_mode(0o755, is_dir=True) == 0o755
        assert mod.normalize_mode(0o700, is_dir=True) == 0o755
        assert mod.normalize_mode(0o777, is_dir=True) == 0o755


class TestWheelReproducibility:
    """Test wheel comparison and normalization."""

    def test_identical_wheels_match_strictly(self, tmp_path):
        w1 = tmp_path / "pkg-1.0-py3-none-any.whl"
        payload = {
            "pkg/__init__.py": b"__version__ = '1.0'\n",
            "pkg/core.py": b"def hello(): return 'world'\n",
            "pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        }
        _create_mock_wheel(w1, payload)
        # Create w2 in separate dir
        d2 = tmp_path / "build2"
        d2.mkdir()
        w2 = d2 / "pkg-1.0-py3-none-any.whl"
        _create_mock_wheel(w2, payload)

        diff = mod.compare_artifacts(w1, w2)
        assert diff.raw_match is True
        assert diff.is_identical is True
        assert diff.is_normalized_equal is True
        assert len(diff.missing_in_2) == 0
        assert len(diff.extra_in_2) == 0
        assert len(diff.content_diffs) == 0

    def test_timestamp_differences_normalized(self, tmp_path):
        w1 = tmp_path / "pkg-1.0-py3-none-any.whl"
        d2 = tmp_path / "build2"
        d2.mkdir()
        w2 = d2 / "pkg-1.0-py3-none-any.whl"
        payload = {"pkg/__init__.py": b"__version__ = '1.0'\n"}

        _create_mock_wheel(w1, payload, date_time=(2020, 2, 2, 0, 0, 0))
        _create_mock_wheel(w2, payload, date_time=(2024, 1, 1, 12, 0, 0))

        diff = mod.compare_artifacts(w1, w2)
        # Raw archive hashes differ because zip entry timestamps differ
        assert diff.raw_match is False
        # But contents and normalized metadata match
        assert diff.is_normalized_equal is True
        assert len(diff.content_diffs) == 0

    def test_content_difference_detected(self, tmp_path):
        w1 = tmp_path / "pkg-1.0-py3-none-any.whl"
        d2 = tmp_path / "build2"
        d2.mkdir()
        w2 = d2 / "pkg-1.0-py3-none-any.whl"

        _create_mock_wheel(w1, {"pkg/core.py": b"def f(): return 1\n"})
        _create_mock_wheel(w2, {"pkg/core.py": b"def f(): return 2\n"})

        diff = mod.compare_artifacts(w1, w2)
        assert diff.raw_match is False
        assert diff.is_normalized_equal is False
        assert PurePosixPath("pkg/core.py") in diff.content_diffs
        assert PurePosixPath("pkg/core.py") in diff.text_diffs
        assert "-def f(): return 1" in diff.text_diffs[PurePosixPath("pkg/core.py")]
        assert "+def f(): return 2" in diff.text_diffs[PurePosixPath("pkg/core.py")]

    def test_missing_and_extra_members_detected(self, tmp_path):
        w1 = tmp_path / "pkg-1.0-py3-none-any.whl"
        d2 = tmp_path / "build2"
        d2.mkdir()
        w2 = d2 / "pkg-1.0-py3-none-any.whl"

        _create_mock_wheel(w1, {"pkg/a.py": b"a = 1\n", "pkg/b.py": b"b = 2\n"})
        _create_mock_wheel(w2, {"pkg/a.py": b"a = 1\n", "pkg/c.py": b"c = 3\n"})

        diff = mod.compare_artifacts(w1, w2)
        assert diff.is_normalized_equal is False
        assert PurePosixPath("pkg/b.py") in diff.missing_in_2
        assert PurePosixPath("pkg/c.py") in diff.extra_in_2


class TestSdistReproducibility:
    """Test sdist (.tar.gz) comparison and normalization."""

    def test_identical_sdists_match_strictly(self, tmp_path):
        s1 = tmp_path / "pkg-1.0.tar.gz"
        d2 = tmp_path / "build2"
        d2.mkdir()
        s2 = d2 / "pkg-1.0.tar.gz"
        payload = {
            "pkg-1.0/PKG-INFO": b"Version: 1.0\n",
            "pkg-1.0/pkg/__init__.py": b"# init\n",
        }
        _create_mock_sdist(s1, payload)
        _create_mock_sdist(s2, payload)

        diff = mod.compare_artifacts(s1, s2)
        assert diff.is_normalized_equal is True

    def test_tar_uid_gid_mtime_differences_normalized(self, tmp_path):
        s1 = tmp_path / "pkg-1.0.tar.gz"
        d2 = tmp_path / "build2"
        d2.mkdir()
        s2 = d2 / "pkg-1.0.tar.gz"
        payload = {"pkg-1.0/PKG-INFO": b"Version: 1.0\n"}

        _create_mock_sdist(s1, payload, mtime=1000000000, uid=1000, gid=1000)
        _create_mock_sdist(s2, payload, mtime=1700000000, uid=0, gid=0)

        diff = mod.compare_artifacts(s1, s2)
        # Normalization succeeds despite host uid/gid/mtime variance
        assert diff.is_normalized_equal is True

    def test_sdist_content_diff_detected(self, tmp_path):
        s1 = tmp_path / "pkg-1.0.tar.gz"
        d2 = tmp_path / "build2"
        d2.mkdir()
        s2 = d2 / "pkg-1.0.tar.gz"

        _create_mock_sdist(s1, {"pkg-1.0/file.txt": b"content A\n"})
        _create_mock_sdist(s2, {"pkg-1.0/file.txt": b"content B\n"})

        diff = mod.compare_artifacts(s1, s2)
        assert diff.is_normalized_equal is False
        assert PurePosixPath("pkg-1.0/file.txt") in diff.content_diffs


class TestDirectoryComparison:
    """Test full directory comparison covering both wheel and sdist."""

    def test_complete_matching_distribution_passes(self, tmp_path):
        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"
        dir1.mkdir()
        dir2.mkdir()

        w_payload = {"pkg/__init__.py": b"__version__ = '1.0'\n"}
        s_payload = {"pkg-1.0/PKG-INFO": b"Version: 1.0\n"}

        _create_mock_wheel(dir1 / "pkg-1.0-py3-none-any.whl", w_payload)
        _create_mock_wheel(dir2 / "pkg-1.0-py3-none-any.whl", w_payload)
        _create_mock_sdist(dir1 / "pkg-1.0.tar.gz", s_payload)
        _create_mock_sdist(dir2 / "pkg-1.0.tar.gz", s_payload)

        report = mod.compare_artifact_directories(dir1, dir2)
        assert report.success is True
        assert len(report.errors) == 0
        assert len(report.artifacts) == 2

    def test_missing_wheel_or_sdist_fails(self, tmp_path):
        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"
        dir1.mkdir()
        dir2.mkdir()

        # Only wheel, no sdist
        w_payload = {"pkg/__init__.py": b"__version__ = '1.0'\n"}
        _create_mock_wheel(dir1 / "pkg-1.0-py3-none-any.whl", w_payload)
        _create_mock_wheel(dir2 / "pkg-1.0-py3-none-any.whl", w_payload)

        report = mod.compare_artifact_directories(dir1, dir2)
        assert report.success is False
        assert any("missing sdist artifact" in err for err in report.errors)

    def test_strict_raw_mode_flags_container_variance(self, tmp_path):
        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"
        dir1.mkdir()
        dir2.mkdir()

        w_payload = {"pkg/__init__.py": b"__version__ = '1.0'\n"}
        s_payload = {"pkg-1.0/PKG-INFO": b"Version: 1.0\n"}

        # Different timestamps in wheel
        _create_mock_wheel(
            dir1 / "pkg-1.0-py3-none-any.whl",
            w_payload,
            date_time=(2020, 1, 1, 0, 0, 0),
        )
        _create_mock_wheel(
            dir2 / "pkg-1.0-py3-none-any.whl",
            w_payload,
            date_time=(2024, 1, 1, 0, 0, 0),
        )
        _create_mock_sdist(dir1 / "pkg-1.0.tar.gz", s_payload)
        _create_mock_sdist(dir2 / "pkg-1.0.tar.gz", s_payload)

        # Standard normalized comparison succeeds
        normal_report = mod.compare_artifact_directories(dir1, dir2, strict_raw=False)
        assert normal_report.success is True

        # Strict raw comparison fails because raw zip hashes differ
        strict_report = mod.compare_artifact_directories(dir1, dir2, strict_raw=True)
        assert strict_report.success is False


class TestCleanTreePreparation:
    """Test clean source tree filtering."""

    def test_copy_clean_tree_filters_caches_and_artifacts(self, tmp_path):
        src = tmp_path / "source"
        src.mkdir()
        (src / "mycelium").mkdir()
        (src / "mycelium" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        (src / "mycelium" / "byte.pyc").write_bytes(b"\x00\x00")
        (src / "mycelium" / "__pycache__").mkdir()
        (src / "mycelium" / "__pycache__" / "cached.pyc").write_bytes(b"\x00")
        (src / ".venv").mkdir()
        (src / ".venv" / "pip.txt").write_text("pip\n", encoding="utf-8")
        (src / "dist").mkdir()
        (src / "dist" / "old.whl").write_bytes(b"old")
        (src / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

        dest = tmp_path / "clean"
        mod.copy_clean_tree(src, dest)

        assert (dest / "pyproject.toml").exists()
        assert (dest / "mycelium" / "__init__.py").exists()
        assert not (dest / "mycelium" / "byte.pyc").exists()
        assert not (dest / "mycelium" / "__pycache__").exists()
        assert not (dest / ".venv").exists()
        assert not (dest / "dist").exists()


class TestReportFormatting:
    """Test report generation."""

    def test_report_text_contains_summary(self):
        diff = mod.ArtifactDiff(
            name="pkg-1.0-py3-none-any.whl",
            raw_match=True,
            raw_hash_1="abc123",
            raw_hash_2="abc123",
        )
        report = mod.ReproducibilityReport(success=True, artifacts=[diff])
        text = mod.format_report(report)
        assert "PACKAGE REPRODUCIBILITY VERIFICATION REPORT" in text
        assert "IDENTICAL (bit-for-bit raw archive match)" in text
        assert "OVERALL VERIFICATION RESULT: PASSED" in text

    def test_divergent_report_shows_diff_details(self):
        diff = mod.ArtifactDiff(
            name="pkg-1.0-py3-none-any.whl",
            raw_match=False,
            raw_hash_1="abc123",
            raw_hash_2="def456",
            missing_in_2=[PurePosixPath("pkg/deleted.py")],
            extra_in_2=[PurePosixPath("pkg/added.py")],
            content_diffs={PurePosixPath("pkg/core.py"): ("hash1", "hash2")},
            text_diffs={PurePosixPath("pkg/core.py"): "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n"},
        )
        report = mod.ReproducibilityReport(success=False, artifacts=[diff])
        text = mod.format_report(report)
        assert "DIVERGENT (reproducibility check failed)" in text
        assert "Missing in Build 2" in text
        assert "pkg/deleted.py" in text
        assert "Extra in Build 2" in text
        assert "pkg/added.py" in text
        assert "unified diff snippet:" in text
        assert "OVERALL VERIFICATION RESULT: FAILED" in text


class TestCLIExecution:
    """Test main CLI invocation."""

    def test_cli_with_matching_directories(self, tmp_path, capsys):
        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"
        dir1.mkdir()
        dir2.mkdir()

        w_payload = {"pkg/__init__.py": b"__version__ = '1.0'\n"}
        s_payload = {"pkg-1.0/PKG-INFO": b"Version: 1.0\n"}

        _create_mock_wheel(dir1 / "pkg-1.0-py3-none-any.whl", w_payload)
        _create_mock_wheel(dir2 / "pkg-1.0-py3-none-any.whl", w_payload)
        _create_mock_sdist(dir1 / "pkg-1.0.tar.gz", s_payload)
        _create_mock_sdist(dir2 / "pkg-1.0.tar.gz", s_payload)

        outdir = tmp_path / "out"
        mod.main(["--build1", str(dir1), "--build2", str(dir2), "--outdir", str(outdir)])

        captured = capsys.readouterr()
        assert "OVERALL VERIFICATION RESULT: PASSED" in captured.out
        assert (outdir / "pkg-1.0-py3-none-any.whl").exists()
        assert (outdir / "pkg-1.0.tar.gz").exists()

    def test_cli_fails_on_divergent_builds(self, tmp_path):
        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"
        dir1.mkdir()
        dir2.mkdir()

        _create_mock_wheel(dir1 / "pkg-1.0-py3-none-any.whl", {"pkg/a.py": b"1"})
        _create_mock_wheel(dir2 / "pkg-1.0-py3-none-any.whl", {"pkg/a.py": b"2"})
        _create_mock_sdist(dir1 / "pkg-1.0.tar.gz", {"pkg/a.py": b"1"})
        _create_mock_sdist(dir2 / "pkg-1.0.tar.gz", {"pkg/a.py": b"1"})

        with pytest.raises(SystemExit) as exc:
            mod.main(["--build1", str(dir1), "--build2", str(dir2)])
        assert exc.value.code == 1
