from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFIER = REPO_ROOT / "scripts" / "classify-ci-changes.sh"
TEST_RUNNER = REPO_ROOT / "scripts" / "run-python-tests.py"


def _parse_kv(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def _clean_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    return env


def _run_classifier_files(paths: list[str]) -> dict[str, str]:
    proc = subprocess.run(
        ["sh", str(CLASSIFIER), "--files"],
        input="\n".join(paths) + ("\n" if paths else ""),
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return _parse_kv(proc.stdout)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_fixture_repo(root: Path, version: str = "0.1.0-alpha.7") -> str:
    _git(root, "init", "-b", "main")
    (root / "app").mkdir()
    (root / "app" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / ".release-please-manifest.json").write_text(
        json.dumps({".": version}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{version}](https://example.invalid/compare/v0.1.0-alpha.6...v{version}) (2026-11-02)\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "chore: initial fixture")
    return _git(root, "rev-parse", "HEAD")


def _classify_git(repo: Path, base_sha: str, head_sha: str) -> dict[str, str]:
    proc = subprocess.run(
        ["sh", str(CLASSIFIER), "--git", base_sha, head_sha],
        cwd=str(repo),
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return _parse_kv(proc.stdout)


class ClassifyCiChangesTests(unittest.TestCase):
    def test_docs_only_paths_skip_both_suites(self) -> None:
        result = _run_classifier_files(
            [
                "README.md",
                "AGENTS.md",
                "docs/architecture.md",
                ".github/ISSUE_TEMPLATE/bug.yml",
                ".github/PULL_REQUEST_TEMPLATE.md",
            ]
        )
        self.assertEqual(
            result,
            {
                "python": "false",
                "web": "false",
                "release_metadata": "false",
                "must_run_main": "false",
            },
        )

    def test_legal_and_root_and_vite_contract_paths_fail_closed_to_full(self) -> None:
        for path in (
            "LICENSE",
            "NOTICE.md",
            ".github/workflows/ci.yml",
            "scripts/classify-ci-changes.sh",
            "Dockerfile",
            "tests/browser_qa.mjs",
            "web/vite.config.js",
            "web/index.html",
            "web/package.json",
            "web/package-lock.json",
            "unknown-root-file.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    _run_classifier_files([path]),
                    {
                        "python": "true",
                        "web": "true",
                        "release_metadata": "false",
                        "must_run_main": "false",
                    },
                )

    def test_release_files_always_require_full_ci_and_main_execution(self) -> None:
        for paths in (
            [".release-please-manifest.json", "VERSION", "CHANGELOG.md"],
            ["VERSION"],
            [".release-please-manifest.json"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(
                    _run_classifier_files(paths),
                    {
                        "python": "true",
                        "web": "true",
                        "release_metadata": "true",
                        "must_run_main": "true",
                    },
                )

    def test_empty_input_fails_closed_to_full(self) -> None:
        self.assertEqual(
            _run_classifier_files([]),
            {
                "python": "true",
                "web": "true",
                "release_metadata": "false",
                "must_run_main": "true",
            },
        )

    def test_web_only_and_python_only_scoping(self) -> None:
        self.assertEqual(
            _run_classifier_files(["web/src/App.jsx", "README.md"]),
            {
                "python": "false",
                "web": "true",
                "release_metadata": "false",
                "must_run_main": "false",
            },
        )
        self.assertEqual(
            _run_classifier_files(
                ["app/web.py", "tests/test_web.py", "scripts/run-python-tests.py", "README.md"]
            ),
            {
                "python": "true",
                "web": "false",
                "release_metadata": "false",
                "must_run_main": "false",
            },
        )
        self.assertEqual(
            _run_classifier_files(["app/web.py", "web/src/App.jsx"]),
            {
                "python": "true",
                "web": "true",
                "release_metadata": "false",
                "must_run_main": "false",
            },
        )

    def test_rename_to_markdown_and_non_ascii_paths_classify_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_sha = _init_fixture_repo(repo)
            _git(repo, "checkout", "-b", "rename-test")
            _git(repo, "mv", "app/sample.py", "docs-note.md")
            _git(repo, "commit", "-m", "refactor: rename code file to md")
            head_sha = _git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                _classify_git(repo, base_sha, head_sha),
                {
                    "python": "true",
                    "web": "false",
                    "release_metadata": "false",
                    "must_run_main": "false",
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            base_sha = _init_fixture_repo(repo)
            _git(repo, "checkout", "-b", "non-ascii-docs")
            (repo / "docs").mkdir()
            (repo / "docs" / "中文说明.md").write_text("# 说明\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "docs: add chinese guide")
            head_sha = _git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                _classify_git(repo, base_sha, head_sha),
                {
                    "python": "false",
                    "web": "false",
                    "release_metadata": "false",
                    "must_run_main": "false",
                },
            )

    def test_diverged_main_uses_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_fixture_repo(repo)
            _git(repo, "checkout", "-b", "docs-branch")
            (repo / "README.md").write_text("updated\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: update readme")
            head_sha = _git(repo, "rev-parse", "HEAD")

            _git(repo, "checkout", "main")
            (repo / "app" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
            _git(repo, "add", "app/sample.py")
            _git(repo, "commit", "-m", "feat: update app on main")
            base_sha = _git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                _classify_git(repo, base_sha, head_sha),
                {
                    "python": "false",
                    "web": "false",
                    "release_metadata": "false",
                    "must_run_main": "false",
                },
            )


class ParallelPythonTestRunnerTests(unittest.TestCase):
    def test_runs_split_isolated_modules_and_propagates_top_level_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "subproject"
            tests_dir = sub / "tests"
            tests_dir.mkdir(parents=True)
            (sub / "helper_pkg.py").write_text("MAGIC = 42\n", encoding="utf-8")
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_alpha.py").write_text(
                textwrap.dedent(
                    """
                    import os
                    import unittest
                    import helper_pkg

                    os.environ["ISOLATION_MARKER"] = "alpha"

                    class AlphaTests(unittest.TestCase):
                        def test_1(self): self.assertEqual(helper_pkg.MAGIC, 42)
                        def test_2(self): self.assertEqual(os.environ.get("ISOLATION_MARKER"), "alpha")
                        def test_3(self): self.assertEqual(os.environ.get("ISOLATION_MARKER"), "alpha")
                        def test_4(self): self.assertEqual(os.environ.get("ISOLATION_MARKER"), "alpha")
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (tests_dir / "test_beta.py").write_text(
                textwrap.dedent(
                    """
                    import os
                    import unittest

                    class BetaTests(unittest.TestCase):
                        def test_isolated_env(self):
                            self.assertIsNone(os.environ.get("ISOLATION_MARKER"))
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(TEST_RUNNER),
                    "--start-dir",
                    "subproject/tests",
                    "--top-level-dir",
                    "subproject",
                    "--workers",
                    "2",
                    "--split-threshold",
                    "2",
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("Ran 5/5 tests across 3 isolated units", proc.stdout)

    def test_handles_setupclass_skip_and_fails_on_subtest_or_import_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_class_skip.py").write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class SkippedClassTests(unittest.TestCase):
                        @classmethod
                        def setUpClass(cls):
                            raise unittest.SkipTest("optional dependency unavailable")

                        def test_a(self):
                            self.fail("should be skipped")
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(TEST_RUNNER), "--workers", "2"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertIn("class_or_module_skips=1", proc.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_subtest_fail.py").write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class SubTestFail(unittest.TestCase):
                        def test_cases(self):
                            for value in (1, 2):
                                with self.subTest(value=value):
                                    self.assertEqual(value, 1)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(TEST_RUNNER), "--workers", "2"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("FAILED: tests.test_subtest_fail", proc.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_import_error.py").write_text(
                "import nonexistent_module_for_test_only\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(TEST_RUNNER), "--workers", "2"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("import/load errors", proc.stderr)
            self.assertIn("nonexistent_module_for_test_only", proc.stderr)


if __name__ == "__main__":
    unittest.main()
