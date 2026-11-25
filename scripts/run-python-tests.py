#!/usr/bin/env python3
"""Run discovered unittest cases across isolated parallel OS worker processes.

Each worker executes tests from at most one test module so module-level
environment setup (such as ``tests/test_web.py``) can never leak across
modules. Large test modules (for example ``tests.test_web``, where every login
invokes ``scrypt``) are partitioned across multiple workers so all runner
cores stay utilized without altering application behavior or adding external
test dependencies.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


DISCOVERY_SNIPPET = r"""
import json
import sys
import unittest

result_path, start_dir, pattern, top_level_dir = (
    sys.argv[1],
    sys.argv[2],
    sys.argv[3],
    sys.argv[4],
)
suite = unittest.defaultTestLoader.discover(
    start_dir=start_dir,
    pattern=pattern,
    top_level_dir=top_level_dir,
)

test_ids = []
load_errors = []

def walk(item):
    if isinstance(item, unittest.TestSuite):
        for child in item:
            walk(child)
        return
    if item.__class__.__name__ == "_FailedTest":
        exc = getattr(item, "_exception", None)
        load_errors.append({"id": item.id(), "error": str(exc) if exc is not None else ""})
        return
    test_ids.append(item.id())

walk(suite)
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump({"test_ids": test_ids, "load_errors": load_errors}, handle)
"""

WORKER_SNIPPET = r"""
import io
import json
import sys
import unittest

result_path = sys.argv[1]
top_level_dir = sys.argv[2]
test_ids = sys.argv[3:]

if top_level_dir and top_level_dir not in sys.path:
    sys.path.insert(0, top_level_dir)

loader = unittest.defaultTestLoader
suite = loader.loadTestsFromNames(test_ids)
stream = io.StringIO()
runner = unittest.TextTestRunner(stream=stream, verbosity=1)
result = runner.run(suite)

class_or_module_skips = sum(
    1 for test, _reason in result.skipped if test.__class__.__name__ == "_ErrorHolder"
)

payload = {
    "tests_run": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "class_or_module_skips": class_or_module_skips,
    "unexpected_successes": len(result.unexpectedSuccesses),
    "was_successful": result.wasSuccessful(),
    "output": stream.getvalue(),
}
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)

sys.exit(0 if result.wasSuccessful() else 1)
"""


@dataclass(frozen=True)
class WorkUnit:
    label: str
    test_ids: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class UnitResult:
    unit: WorkUnit
    duration_seconds: float
    returncode: int
    tests_run: int
    failures: int
    errors: int
    skipped: int
    class_or_module_skips: int
    unexpected_successes: int
    was_successful: bool
    output: str
    stderr: str

    def count_valid(self) -> bool:
        expected = len(self.unit.test_ids)
        if self.tests_run == expected:
            return True
        # unittest reports testsRun=0 for methods skipped via setUpClass/setUpModule SkipTest.
        return (
            self.was_successful
            and self.class_or_module_skips > 0
            and 0 <= self.tests_run < expected
        )


def _module_of(test_id: str) -> str:
    parts = test_id.rsplit(".", 2)
    return parts[0] if len(parts) == 3 else test_id


def build_work_units(
    test_ids: list[str],
    *,
    workers: int,
    split_threshold: int,
) -> list[WorkUnit]:
    """Group test IDs into single-module isolated work units.

    Modules with more than ``split_threshold`` tests are split round-robin
    across up to ``workers`` slices so heavy modules (like ``tests.test_web``)
    run in parallel while still preserving strict module isolation.
    """
    by_module: dict[str, list[str]] = defaultdict(list)
    for test_id in test_ids:
        by_module[_module_of(test_id)].append(test_id)

    units: list[WorkUnit] = []
    max_slices = max(1, workers)
    for module_name, module_tests in sorted(by_module.items()):
        count = len(module_tests)
        if max_slices > 1 and count > split_threshold:
            slice_count = min(max_slices, math.ceil(count / max(1, split_threshold)))
            buckets: list[list[str]] = [[] for _ in range(slice_count)]
            for idx, test_id in enumerate(module_tests):
                buckets[idx % slice_count].append(test_id)
            for idx, bucket in enumerate(buckets, start=1):
                if not bucket:
                    continue
                units.append(
                    WorkUnit(
                        label=f"{module_name} [{idx}/{slice_count}]",
                        test_ids=tuple(bucket),
                        # Schedule split heavy modules first to prevent tail stragglers.
                        priority=count * 10 + len(bucket),
                    )
                )
        else:
            units.append(
                WorkUnit(
                    label=module_name,
                    test_ids=tuple(module_tests),
                    priority=count,
                )
            )

    units.sort(key=lambda u: (-u.priority, u.label))
    return units


def discover_tests(
    *,
    start_dir: str,
    pattern: str,
    top_level_dir: str,
    cwd: Path,
    result_dir: Path,
) -> list[str]:
    discovery_path = result_dir / "discovery.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            DISCOVERY_SNIPPET,
            str(discovery_path),
            start_dir,
            pattern,
            top_level_dir,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not discovery_path.is_file():
        raise RuntimeError(
            f"unittest discovery failed (exit {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    data = json.loads(discovery_path.read_text(encoding="utf-8"))
    load_errors = data.get("load_errors") or []
    if load_errors:
        details = "\n".join(
            f"- {entry.get('id', '<unknown>')}: {entry.get('error', '').strip()}"
            for entry in load_errors
        )
        raise RuntimeError(f"unittest discovery reported import/load errors:\n{details}")
    test_ids = data.get("test_ids") or []
    if not test_ids:
        raise RuntimeError("unittest discovery returned zero tests")
    if len(set(test_ids)) != len(test_ids):
        raise RuntimeError("unittest discovery returned duplicate test IDs")
    return list(test_ids)


def run_work_unit(
    unit: WorkUnit,
    *,
    cwd: Path,
    top_level_path: str,
    result_dir: Path,
    index: int,
) -> UnitResult:
    result_path = result_dir / f"unit-{index:04d}.json"
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            WORKER_SNIPPET,
            str(result_path),
            top_level_path,
            *unit.test_ids,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    duration = time.monotonic() - started
    if not result_path.is_file():
        return UnitResult(
            unit=unit,
            duration_seconds=duration,
            returncode=proc.returncode or 1,
            tests_run=0,
            failures=0,
            errors=1,
            skipped=0,
            class_or_module_skips=0,
            unexpected_successes=0,
            was_successful=False,
            output=proc.stdout,
            stderr=proc.stderr or "worker exited without writing a result payload",
        )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    return UnitResult(
        unit=unit,
        duration_seconds=duration,
        returncode=proc.returncode,
        tests_run=int(payload.get("tests_run", 0)),
        failures=int(payload.get("failures", 0)),
        errors=int(payload.get("errors", 0)),
        skipped=int(payload.get("skipped", 0)),
        class_or_module_skips=int(payload.get("class_or_module_skips", 0)),
        unexpected_successes=int(payload.get("unexpected_successes", 0)),
        was_successful=bool(payload.get("was_successful", False)),
        output=str(payload.get("output", "")),
        stderr=proc.stderr,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_workers = min(os.cpu_count() or 1, 4)
    parser = argparse.ArgumentParser(
        description="Run unittest suite in isolated parallel worker processes."
    )
    parser.add_argument("--start-dir", default="tests")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--top-level-dir", default=".")
    parser.add_argument("--workers", type=int, default=default_workers)
    parser.add_argument("--split-threshold", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 2
    if args.split_threshold < 1:
        print("--split-threshold must be >= 1", file=sys.stderr)
        return 2

    cwd = Path.cwd()
    top_level_path = str((cwd / args.top_level_dir).resolve())
    wall_start = time.monotonic()

    results: list[UnitResult] = []
    with tempfile.TemporaryDirectory(prefix="telegram-bot-pytests-") as tmp:
        result_dir = Path(tmp)
        try:
            test_ids = discover_tests(
                start_dir=args.start_dir,
                pattern=args.pattern,
                top_level_dir=args.top_level_dir,
                cwd=cwd,
                result_dir=result_dir,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        units = build_work_units(
            test_ids,
            workers=args.workers,
            split_threshold=args.split_threshold,
        )
        planned_count = sum(len(u.test_ids) for u in units)
        if planned_count != len(test_ids):
            print(
                f"ERROR: planned test count ({planned_count}) != discovered ({len(test_ids)})",
                file=sys.stderr,
            )
            return 1

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    run_work_unit,
                    unit,
                    cwd=cwd,
                    top_level_path=top_level_path,
                    result_dir=result_dir,
                    index=idx,
                ): unit
                for idx, unit in enumerate(units)
            }
            for future in as_completed(futures):
                results.append(future.result())

    wall_elapsed = time.monotonic() - wall_start
    total_run = sum(r.tests_run for r in results)
    total_failures = sum(r.failures for r in results)
    total_errors = sum(r.errors for r in results)
    total_skipped = sum(r.skipped for r in results)
    total_class_skips = sum(r.class_or_module_skips for r in results)
    total_unexpected = sum(r.unexpected_successes for r in results)
    failed_units = [
        r
        for r in results
        if r.returncode != 0 or not r.was_successful or not r.count_valid()
    ]

    for res in sorted(failed_units, key=lambda r: r.unit.label):
        print(f"=== FAILED: {res.unit.label} ({res.duration_seconds:.2f}s) ===", file=sys.stderr)
        if not res.count_valid():
            print(
                f"Expected {len(res.unit.test_ids)} tests, executed {res.tests_run}",
                file=sys.stderr,
            )
        if res.output.strip():
            print(res.output.rstrip(), file=sys.stderr)
        if res.stderr.strip():
            print(res.stderr.rstrip(), file=sys.stderr)

    slowest = sorted(results, key=lambda r: -r.duration_seconds)[:6]
    slowest_summary = ", ".join(
        f"{r.unit.label}={r.duration_seconds:.2f}s" for r in slowest
    )
    print(
        f"Ran {total_run}/{len(test_ids)} tests across {len(units)} isolated units "
        f"({args.workers} workers) in {wall_elapsed:.2f}s "
        f"(failures={total_failures}, errors={total_errors}, "
        f"skipped={total_skipped}, class_or_module_skips={total_class_skips}, "
        f"unexpected_successes={total_unexpected})"
    )
    print(f"Slowest units: {slowest_summary}")

    count_ok = (total_run == len(test_ids)) or (
        total_class_skips > 0 and all(r.count_valid() for r in results)
    )
    if failed_units or not count_ok or total_failures or total_errors or total_unexpected:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
