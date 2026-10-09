"""The test-weakening detector: what a change set takes away from the suite, counted from the diff."""
from __future__ import annotations

from skilllayer.weakened_tests import analyze_diff


def _diff(path: str, removed: list[str], added: list[str], *, new_file: bool = False) -> str:
    src = "/dev/null" if new_file else f"a/{path}"
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- {src}\n+++ b/{path}\n@@ -10,{len(removed)} +10,{len(added)} @@\n{body}"


def _kinds(result: dict) -> list[str]:
    return sorted(f["kind"] for f in result["findings"])


def test_a_deleted_assertion_is_counted():
    result = analyze_diff(_diff("tests/test_promo.py", ["    assert apply(100) == 90"], []))
    assert _kinds(result) == ["assertions_removed"]
    assert result["counts"]["assertions"] == {"added": 0, "removed": 1}


def test_a_skip_marker_and_a_tautology_are_found_with_their_line():
    result = analyze_diff(_diff("tests/test_promo.py", [], ["@pytest.mark.skip(reason='flaky')", "    assert True"]))
    assert _kinds(result) == ["skip_added", "tautological_assertion"]
    assert {f["kind"]: f["line"] for f in result["findings"]} == {"skip_added": 10, "tautological_assertion": 11}


def test_javascript_skips_and_trivial_expectations_count_too():
    result = analyze_diff(_diff("src/cart.test.ts", [], ["it.skip('applies promo', () => {", "  expect(true).toBe(true)"]))
    assert _kinds(result) == ["skip_added", "tautological_assertion"]


def test_a_test_moved_between_files_is_not_a_weakening():
    moved = "def test_promo():\n    assert apply(100) == 90".split("\n")
    diff = _diff("tests/test_old.py", moved, []) + _diff("tests/test_new.py", [], moved, new_file=True)
    assert analyze_diff(diff)["findings"] == []


def test_fewer_test_functions_overall_is_reported():
    diff = _diff("tests/test_promo.py", ["def test_cap():", "    assert apply(1000) == 950"], [])
    assert _kinds(analyze_diff(diff)) == ["assertions_removed", "tests_removed"]


def test_a_lowered_coverage_threshold_is_found_and_a_raised_one_is_not():
    lowered = analyze_diff(_diff("pyproject.toml", ["fail_under = 90"], ["fail_under = 60"]))
    assert [(f["kind"], f["detail"]) for f in lowered["findings"]] == [("threshold_lowered", "fail_under: 90 → 60")]
    assert analyze_diff(_diff("pyproject.toml", ["fail_under = 60"], ["fail_under = 90"]))["findings"] == []


def test_deselecting_tests_in_ci_or_config_is_found():
    ci = analyze_diff(_diff(".gitlab-ci.yml", [], ["    - pytest -q --deselect tests/test_promo.py::test_cap"]))
    k = analyze_diff(_diff("pytest.ini", [], ['addopts = -k "not cap"']))
    assert _kinds(ci) == ["tests_deselected"] and _kinds(k) == ["tests_deselected"]


def test_a_deleted_test_file_is_reported_when_the_suite_shrinks():
    diff = _diff("tests/test_promo.py", ["def test_cap():", "    assert apply(1000) == 950"], [])
    result = analyze_diff(diff, deleted_paths=["tests/test_promo.py", "src/promo.py"])
    assert ("test_file_deleted", "tests/test_promo.py") in [(f["kind"], f["file"]) for f in result["findings"]]


def test_a_test_file_moved_elsewhere_is_not_a_deletion():
    tests = ["def test_cap():", "    assert apply(1000) == 950"]
    diff = _diff("tests/test_promo.py", tests, []) + _diff("tests/promo/test_cap.py", [], tests, new_file=True)
    assert analyze_diff(diff, deleted_paths=["tests/test_promo.py"])["findings"] == []


def test_a_new_test_that_arrives_with_a_conditional_skip_is_not_a_weakening():
    added = ["@pytest.mark.skipif(sys.platform == 'win32', reason='posix only')", "def test_pager():", "    assert run() == 0"]
    assert analyze_diff(_diff("tests/test_pager.py", [], added))["findings"] == []


def test_a_reformat_that_removes_and_re_adds_a_skip_is_not_a_weakening():
    diff = _diff("tests/test_promo.py", ["@pytest.mark.skip(reason='x')"], ['@pytest.mark.skip(reason="x")'])
    assert analyze_diff(diff)["findings"] == []


def test_code_changes_and_new_tests_are_clean():
    diff = _diff("src/promo.py", ["    return total"], ["    return round(total * 0.9, 2)"]) + _diff(
        "tests/test_promo.py", [], ["def test_cap():", "    assert apply(1000) == 950"]
    )
    assert analyze_diff(diff)["findings"] == []


def test_assert_lines_outside_test_files_do_not_count():
    assert analyze_diff(_diff("src/promo.py", ["    assert total >= 0"], []))["findings"] == []


def test_a_multi_line_skipif_on_a_new_test_is_not_a_weakening():
    added = ["@pytest.mark.skipif(", "    shutil.which('cat') is None,", "    reason='cat not available',", ")", "def test_pager_cat():", "    assert run() == 0"]
    assert analyze_diff(_diff("tests/test_pager.py", [], added))["findings"] == []


def test_skipping_an_existing_block_of_tests_is_still_caught():
    """From express: describe('…') became describe.skip('…') with a comment that the test fails."""
    diff = _diff("test/res.status.js", ["    describe('when code is undefined', function () {"],
                 ["    // This test fails in node 4.0.0", "    describe.skip('when code is undefined', function () {"])
    assert [f["kind"] for f in analyze_diff(diff)["findings"]] == ["skip_added"]


def test_a_skipif_above_a_long_parametrize_on_a_new_test_is_not_a_weakening():
    cases = [f"        ({i}, {i}),"  for i in range(20)]
    added = ["@pytest.mark.skipif(WIN, reason='posix only')", "@pytest.mark.parametrize(", "    ('a', 'b'),", "    [", *cases, "    ],", ")", "def test_many(a, b):", "    assert a == b"]
    assert analyze_diff(_diff("tests/test_many.py", [], added))["findings"] == []
