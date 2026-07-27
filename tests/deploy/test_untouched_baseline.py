"""Untouched-spec baseline: exclude the deploy target, keep the gate strict.

Injections ⑥ and ⑦ from the brief:
  ⑥ target NOT excluded -> the structural post-deploy failure is reproduced
  ⑦ a genuine violation -> still FAILS after the exclusion logic is in place

⑦ is the one that matters. Excluding the deploy target must not make the gate lenient
about anything else, and that is asserted with values rather than asserted in prose.
"""
from __future__ import annotations

import pytest

from pipeline.scripts.deploy.untouched_baseline import (
    BaselineError,
    exclusion_report,
    parse_baseline,
    partition,
)

# The real baseline shipped with the 2026-07-27 cache_cause deploy: 9 cronjobs + 4
# deploys. cronjob rows carry trailing fields; deploy rows end at the sha.
BASELINE = """\
cronjob/brand-activity-topic-monthly\tspec_sha256=b69220ab4936c16572313f2a3b7043911927477f61767ab4b208eb2961366310\tsuspend=False\tschedule=0 19 4 * *
cronjob/brand-activity-row-topic-monthly\tspec_sha256=5c6e365a4d9a97ff1b5494a4b6363dbaacb321e21ca34cf750e2d0c3baf6ae14\tsuspend=False\tschedule=0 22 4 * *
cronjob/jw-news-crawl-tier1-daily\tspec_sha256=10bf1cb25b48732305deeb80c515a145086baeff5e8d6030f9c65acb49f4b1d0\tsuspend=True\tschedule=10 18 * * *
cronjob/jw-news-crawl-tier1-daily-canonical\tspec_sha256=0a18a7ea52d63bdea3bd16172760f057ddb47280be27f5c2f2baf2eb637e4bf4\tsuspend=True\tschedule=10 18 * * *
cronjob/jw-news-crawl-tier2-daily-slice\tspec_sha256=016224cd945ad544bddfdac00cef9201790f6091b0ae0e102c16e965c1fabb75\tsuspend=True\tschedule=40 18 * * *
cronjob/jw-news-crawl-tier2-daily-slice-canonical\tspec_sha256=db0510c62276484b3779773e87ece8ee92ad7eedb94b2815a30ddda76ffcc88e\tsuspend=True\tschedule=40 18 * * *
cronjob/jw-news-crawl-retention-daily\tspec_sha256=d65021bca77e607832601d173c724972467a1d8633697ba8bfcb70a7c1951636\tsuspend=True\tschedule=0 19 * * *
cronjob/jw-pipeline-orchestrator-poll-daily\tspec_sha256=cb2a6c5ff56ba7d68bffaf2f14b4aabe4e5590498b7023d43298fbc2e0f0a1e5\tsuspend=True\tschedule=0 16 * * *
cronjob/jw-ingest-sweep-daily\tspec_sha256=7a49d3f9972d9d7b5a05e5fd8fa685f4ec366af95643192a65eb3308e3970492\tsuspend=True\tschedule=30 19 * * *
deploy/jw-hira-benefit-worker\tspec_sha256=2e19f53386bbb08822b438e789baa93fdd10d4d7ac40be576e738f7bef1a4dd6
deploy/jw-market-crawl-temporal-worker\tspec_sha256=5ef6fc8ce865c2784f71b983d0df8d95636ee2a8153a882c2181d0011f816a8e
deploy/jw-ingest-hook\tspec_sha256=866bea84952a716d26869223c760cd9021bc51d9de7c7d72139891e0f1758aa6
deploy/code-serving-238\tspec_sha256=981f7a3e50cfe011dfb07df4cf8b1f281fa04a9ea5c4064758b7306fcb163058
"""

TARGET = "deploy/jw-ingest-hook"
# spec hash observed on the target AFTER the 2026-07-27 deploy
TARGET_AFTER = "d46c4cbcc2e0afe3bcc3d8d217bc337d1eb68fe513d44f05b7770947a4b2186e"


def observed_specs(*, deployed: bool, extra_violation: dict | None = None) -> dict[str, str]:
    """What a live capture would return, before or after the deploy."""
    specs = dict(parse_baseline(BASELINE))
    if deployed:
        specs[TARGET] = TARGET_AFTER
    if extra_violation:
        specs.update(extra_violation)
    return specs


def evaluate(baseline_text: str, observed: dict[str, str], targets: list[str] | None):
    """The gate's U section, reduced to its decision. Returns (failures, excluded)."""
    baseline = parse_baseline(baseline_text)
    checked, excluded = partition(baseline, targets)
    failures = [ref for ref, want in checked.items() if observed.get(ref) != want]
    return failures, excluded


# -- parsing ---------------------------------------------------------------------------


def test_parse_handles_rows_with_and_without_trailing_fields():
    parsed = parse_baseline(BASELINE)
    assert len(parsed) == 13
    # cronjob row: sha must not absorb the following suspend= field
    assert parsed["cronjob/jw-ingest-sweep-daily"] == (
        "7a49d3f9972d9d7b5a05e5fd8fa685f4ec366af95643192a65eb3308e3970492")
    # deploy row: sha is the last field, must not absorb the newline
    assert parsed["deploy/code-serving-238"] == (
        "981f7a3e50cfe011dfb07df4cf8b1f281fa04a9ea5c4064758b7306fcb163058")
    assert all(len(v) == 64 and v == v.strip() for v in parsed.values())


def test_parse_rejects_a_non_sha_value():
    with pytest.raises(BaselineError, match="not a sha256"):
        parse_baseline("deploy/x\tspec_sha256=not-a-digest\n")


def test_parse_rejects_an_empty_baseline():
    with pytest.raises(BaselineError, match="no spec_sha256 entries"):
        parse_baseline("# just a comment\n\n")


def test_parse_rejects_a_conflicting_duplicate():
    dup = BASELINE + f"{TARGET}\tspec_sha256={'0' * 64}\n"
    with pytest.raises(BaselineError, match="twice with different digests"):
        parse_baseline(dup)


# -- ⑥ the structural post-deploy failure, reproduced and then removed -----------------


def test_inj6_without_exclusion_the_post_deploy_gate_fails_structurally():
    failures, excluded = evaluate(BASELINE, observed_specs(deployed=True), targets=None)
    assert failures == [TARGET]          # exactly the deploy target
    assert excluded == {}
    # ...and it passed before the deploy, which is what makes it structural
    pre_failures, _ = evaluate(BASELINE, observed_specs(deployed=False), targets=None)
    assert pre_failures == []


def test_inj6_with_exclusion_both_phases_pass():
    for deployed in (False, True):
        failures, excluded = evaluate(BASELINE, observed_specs(deployed=deployed), [TARGET])
        assert failures == [], (deployed, failures)
        assert list(excluded) == [TARGET]
        # 13 in the baseline, 1 excluded, 12 still compared
        assert len(parse_baseline(BASELINE)) - len(excluded) == 12


# -- ⑦ THE KEY ONE: real violations must still fail ------------------------------------


@pytest.mark.parametrize("victim", [
    "cronjob/brand-activity-topic-monthly",      # 2026-08-04 firing
    "cronjob/brand-activity-row-topic-monthly",  # 2026-08-04 firing
    "cronjob/jw-news-crawl-tier2-daily-slice",   # tier2 crawl
    "cronjob/jw-ingest-sweep-daily",
    "deploy/code-serving-238",
    "deploy/jw-hira-benefit-worker",
    "deploy/jw-market-crawl-temporal-worker",
])
def test_inj7_a_genuine_violation_still_fails_with_the_target_excluded(victim):
    observed = observed_specs(deployed=True, extra_violation={victim: "f" * 64})
    failures, excluded = evaluate(BASELINE, observed, [TARGET])
    assert failures == [victim]
    assert list(excluded) == [TARGET]


def test_inj7_target_exclusion_does_not_hide_a_simultaneous_violation():
    """Deploy target changed AND something else changed -> the something else fails."""
    observed = observed_specs(
        deployed=True, extra_violation={"deploy/code-serving-238": "a" * 64})
    failures, _ = evaluate(BASELINE, observed, [TARGET])
    assert failures == ["deploy/code-serving-238"]


def test_inj7_every_non_target_ref_is_still_compared():
    """Count the comparisons, so 'still strict' is a number and not a claim."""
    baseline = parse_baseline(BASELINE)
    checked, excluded = partition(baseline, [TARGET])
    assert set(checked) | set(excluded) == set(baseline)
    assert set(checked).isdisjoint(excluded)
    assert len(checked) == 12
    # each of the 12 fails on its own when perturbed
    for ref in checked:
        observed = observed_specs(deployed=True, extra_violation={ref: "b" * 64})
        failures, _ = evaluate(BASELINE, observed, [TARGET])
        assert failures == [ref], ref


def test_a_missing_observation_is_a_failure_not_a_skip():
    """If a ref cannot be read at all, that is a FAIL — absence is not agreement."""
    observed = observed_specs(deployed=True)
    del observed["deploy/code-serving-238"]
    failures, _ = evaluate(BASELINE, observed, [TARGET])
    assert failures == ["deploy/code-serving-238"]


# -- exclusion must be explicit, named, and recorded -----------------------------------


def test_a_target_absent_from_the_baseline_is_an_error_not_a_silent_noop():
    with pytest.raises(BaselineError, match="not in the baseline"):
        partition(parse_baseline(BASELINE), ["deploy/typo-does-not-exist"])


def test_excluding_everything_is_refused():
    baseline = parse_baseline(BASELINE)
    with pytest.raises(BaselineError, match="not a gate"):
        partition(baseline, list(baseline))


def test_exclusion_is_reported_with_the_reason_and_what_measures_it_instead():
    _checked, excluded = partition(parse_baseline(BASELINE), [TARGET])
    lines = exclusion_report(excluded, measured_by="R1 container image + R2 INGEST_JOB_IMAGE env")
    text = "\n".join(lines)
    assert "EXCLUDED deploy/jw-ingest-hook" in text
    assert "866bea84" in text                      # the baseline value is still shown
    assert "R1 container image" in text            # what measures it instead
    assert "NOT unmeasured" in text


def test_no_exclusions_is_also_reported():
    assert exclusion_report({}, measured_by="x") == ["untouched-set exclusions: none"]


def test_there_is_no_builtin_exclusion_list():
    """Nothing is excluded unless the caller names it."""
    checked, excluded = partition(parse_baseline(BASELINE), None)
    assert excluded == {}
    assert len(checked) == 13
