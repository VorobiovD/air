"""Check L: a caller's SHA pin must pin air's code. Each mutation below is a
regression that slipped past an earlier version of the guard: four from the first
audit (string grep, comments included) and three from the second (first-match key
lookup, a skippable resolver, multi-line output). Every one must now fail, and
the real workflows must pass."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_workflow_pins as cwp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = [os.path.join(ROOT, ".github", "workflows", n)
             for n in ("managed-review.yml", "air-learn-cron.yml")]


def _read(path):
    with open(path) as f:
        return f.read()


def test_real_workflows_pass():
    for wf in WORKFLOWS:
        assert cwp.check(_read(wf), wf) == [], wf


def test_unquoted_main_default_fails():
    for wf in WORKFLOWS:
        text = _read(wf)
        # target the air_ref input's own default, not the first `default: ''` in the file
        bad = re.sub(r"(\n      air_ref:\n(?:        .*\n|          .*\n)*?        default: )''",
                     r"\1main", text, count=1)
        assert bad != text, f"fixture did not apply to {wf}"
        assert any("air_ref" in e for e in cwp.check(bad, wf)), wf


def test_caller_workflow_sha_instead_of_job_workflow_sha_fails():
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace("WF_SHA: ${{ job.workflow_sha }}", "WF_SHA: ${{ github.workflow_sha }}")
        assert bad != text
        assert any("WF_SHA" in e for e in cwp.check(bad, wf)), wf


def test_checkout_ref_main_with_expression_left_in_a_comment_fails():
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace("ref: ${{ steps.air_code.outputs.ref }}",
                           "ref: main  # ref: ${{ steps.air_code.outputs.ref }}")
        assert bad != text
        assert any("with.ref" in e for e in cwp.check(bad, wf)), wf


def test_resolver_that_hardcodes_main_fails():
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace('ref="${OVERRIDE:-${WF_SHA:-main}}"', 'ref="main"')
        assert bad != text
        errs = cwp.check(bad, wf)
        assert any("resolver produced 'main'" in e for e in errs), (wf, errs)


def test_resolver_that_ignores_the_override_fails():
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace('ref="${OVERRIDE:-${WF_SHA:-main}}"', 'ref="${WF_SHA:-main}"')
        assert bad != text
        assert any("override wins" in e for e in cwp.check(bad, wf)), wf


def test_resolver_with_no_fallback_fails():
    """An empty workflow SHA must fall back to an EXPLICIT main, matching the old
    default; leaving the ref empty hands the choice to actions/checkout, which
    differs for a same-repo call."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace('ref="${OVERRIDE:-${WF_SHA:-main}}"', 'ref="${OVERRIDE:-$WF_SHA}"')
        assert bad != text
        assert any("explicit main" in e for e in cwp.check(bad, wf)), wf


def test_cli_exit_codes(tmp_path):
    good = WORKFLOWS[0]
    assert cwp.main(["x", good]) == 0
    bad = tmp_path / "bad.yml"
    bad.write_text(_read(good).replace("WF_SHA: ${{ job.workflow_sha }}", "WF_SHA: x"))
    assert cwp.main(["x", str(bad)]) == 1


def test_resolver_that_overwrites_its_ref_later_fails():
    """$GITHUB_OUTPUT is last-write-wins: a correct ref followed by `ref=main`
    runs main, so the checker must read the final value."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace('echo "ref=$ref" >> "$GITHUB_OUTPUT"',
                           'echo "ref=$ref" >> "$GITHUB_OUTPUT"\n          echo "ref=main" >> "$GITHUB_OUTPUT"')
        assert bad != text
        assert any("resolver produced 'main'" in e for e in cwp.check(bad, wf)), wf


# --- audit round 2: three more bypasses of the structural check ---

def test_decoy_expression_in_env_with_real_with_ref_main_fails():
    """A first-match key lookup read the decoy `env.ref`; only `with.ref` counts."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace(
            "          ref: ${{ steps.air_code.outputs.ref }}",
            "          ref: main",
        ).replace(
            "      - uses: actions/checkout@v4\n        with:\n          repository: VorobiovD/air",
            "      - uses: actions/checkout@v4\n        env:\n          ref: ${{ steps.air_code.outputs.ref }}\n        with:\n          repository: VorobiovD/air",
        ).replace(
            "        uses: actions/checkout@v4\n        with:\n          repository: VorobiovD/air",
            "        uses: actions/checkout@v4\n        env:\n          ref: ${{ steps.air_code.outputs.ref }}\n        with:\n          repository: VorobiovD/air",
        )
        assert "env:\n          ref: ${{ steps.air_code.outputs.ref }}" in bad, wf
        assert any("with.ref" in e for e in cwp.check(bad, wf)), (wf, cwp.check(bad, wf))


def test_conditionally_skipped_resolver_fails():
    """With the normal empty override this resolver is skipped, its output is
    empty, and the cross-repo checkout floats on air's default branch."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace("        id: air_code\n",
                           "        id: air_code\n        if: ${{ inputs.air_ref != '' }}\n", 1)
        assert bad != text
        assert any("must always run" in e for e in cwp.check(bad, wf)), wf


def test_multiline_output_overwrite_fails():
    """GitHub's `name<<DELIM` format is a write too, and the last write wins."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace(
            'echo "ref=$ref" >> "$GITHUB_OUTPUT"',
            'echo "ref=$ref" >> "$GITHUB_OUTPUT"\n          printf \'ref<<EOF\\nmain\\nEOF\\n\' >> "$GITHUB_OUTPUT"')
        assert bad != text
        assert any("resolver produced 'main'" in e for e in cwp.check(bad, wf)), (wf, cwp.check(bad, wf))


def test_unterminated_multiline_output_fails():
    """A format the guard can't judge must fail, not pass."""
    for wf in WORKFLOWS:
        text = _read(wf)
        bad = text.replace('echo "ref=$ref" >> "$GITHUB_OUTPUT"',
                           'echo "ref=$ref" >> "$GITHUB_OUTPUT"\n          echo "ref<<EOF" >> "$GITHUB_OUTPUT"')
        assert bad != text
        assert any("resolver produced None" in e for e in cwp.check(bad, wf)), wf


def test_output_parser_matches_runner_rules():
    assert cwp._parse_outputs("ref=a\nref=b\n") == {"ref": "b"}
    assert cwp._parse_outputs("ref<<X\nline1\nline2\nX\n") == {"ref": "line1\nline2"}
    assert cwp._parse_outputs("ref=a<<b\n") == {"ref": "a<<b"}        # `=` first → plain value
    assert cwp._parse_outputs("ref<<X\nunterminated\n") is None


def test_continue_on_error_false_is_allowed_but_true_is_not():
    for wf in WORKFLOWS:
        text = _read(wf)
        ok = text.replace("        id: air_code\n", "        id: air_code\n        continue-on-error: false\n", 1)
        assert ok != text and cwp.check(ok, wf) == [], (wf, cwp.check(ok, wf))
        bad = text.replace("        id: air_code\n", "        id: air_code\n        continue-on-error: true\n", 1)
        assert any("not be allowed to fail" in e for e in cwp.check(bad, wf)), wf
