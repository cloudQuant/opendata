"""The frontend collector guard has to be able to fail (C29).

``make frontend-collection`` runs ``scripts/quality/frontend_test_collection.py``
inside ``make gate``. That only proves the guard ran — a guard that had rotted
into always reporting "nothing silenced" would also let the gate pass. So the
rules themselves are asserted here, and the shipped config is asserted against
them: if anyone re-adds a ``src/`` exclusion, this plane goes red whether or not
the collector happens to be reachable that day.
"""

from pathlib import Path

from scripts.quality import frontend_test_collection as guard

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"

SILENCED = "src/components/common/__tests__/StatCard.test.ts"


def test_a_file_the_collector_admits_is_not_a_finding() -> None:
    verdict = guard.judge([SILENCED], [SILENCED], sorted(guard.ALLOWED_EXCLUDES))

    assert verdict.ok
    assert verdict.reasons == ()


def test_a_file_on_disk_that_is_not_collected_is_named_with_its_rule() -> None:
    verdict = guard.judge(
        [SILENCED],
        [],
        ["e2e/**", "src/components/common/__tests__/**"],
    )

    assert len(verdict.reasons) == 2
    silenced = verdict.reasons[0]
    assert silenced.startswith("SILENCED:")
    assert SILENCED in silenced
    assert "src/components/common/__tests__/**" in silenced


def test_a_missing_file_without_a_matching_rule_is_still_a_finding() -> None:
    # A file can also disappear from the run for a reason this config does not
    # state (a broken `include`, a renamed directory). "Who silenced it" is not
    # the question the gate asks; "it never ran" is.
    verdict = guard.judge([SILENCED], [], ["e2e/**"])

    assert verdict.reasons[0].startswith("SILENCED:")
    assert "matched by no exclude rule" in verdict.reasons[0]


def test_a_new_exclusion_is_rejected_before_it_hides_anything() -> None:
    # The A0 rule sat in the config for four rounds hiding five files. Had the
    # rule been checked only when it hid something, the diff that added it would
    # have passed a plane that was already green.
    verdict = guard.judge([SILENCED], [SILENCED], ["e2e/**", "src/legacy/**"])

    assert len(verdict.reasons) == 1
    assert verdict.reasons[0].startswith("UNAUTHORISED:")
    assert "src/legacy/**" in verdict.reasons[0]


def test_the_stated_boundaries_are_not_findings() -> None:
    # e2e is a separate runner, node_modules and dist are not source. Reading
    # them as silences would turn a declared boundary into a permanent defect.
    verdict = guard.judge(["src/app.test.ts"], ["src/app.test.ts"], sorted(guard.ALLOWED_EXCLUDES))

    assert verdict.reasons == ()


def test_no_test_file_on_disk_is_read_as_nothing_measured_rather_than_a_pass() -> None:
    verdict = guard.judge([], [], ["e2e/**"])

    assert verdict.vacuous
    assert not verdict.ok


def test_the_coverage_exclusions_do_not_read_as_silenced_tests() -> None:
    # ``coverage.exclude`` shrinks the denominator; it does not stop a file from
    # running. The first version of the census merged the two arrays and
    # reported eight silencers where there were four.
    config = (
        "export default defineConfig({\n"
        "  test: {\n"
        "    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],\n"
        "    coverage: {\n"
        "      exclude: ['src/main.ts', 'src/**/*.d.ts'],\n"
        "    },\n"
        "  },\n"
        "})\n"
    )

    globs, readable = guard.parse_test_excludes(config)

    assert readable
    assert globs == ["e2e/**", "node_modules/**", "dist/**"]


def test_a_config_that_changed_shape_is_unreadable_not_empty() -> None:
    globs, readable = guard.parse_test_excludes(
        "export default defineConfig({ test: { globals: true } })"
    )

    assert not readable
    assert globs == []


def test_the_shipped_config_declares_only_allowed_boundaries() -> None:
    globs, readable = guard.parse_test_excludes(CONFIG.read_text(encoding="utf-8"))

    assert readable, "the guard could not read the config it is supposed to police"
    assert set(globs) == set(guard.ALLOWED_EXCLUDES), (
        "a collector exclusion outside the stated boundaries has come back; "
        "every test file under src/ must be collected"
    )


def test_the_shipped_src_plane_has_files_to_collect() -> None:
    on_disk = guard.on_disk_tests(REPO_ROOT / "frontend")

    assert on_disk, "there is no frontend unit plane for the guard to compare"
    assert all(path.startswith("src/") for path in on_disk)
