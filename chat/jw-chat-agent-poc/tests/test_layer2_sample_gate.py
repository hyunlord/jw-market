from __future__ import annotations

from scripts.layer2_sample_gate import evaluate_layer2_round, select_cases


def _population(size: int = 40) -> list[dict[str, str]]:
    categories = ("portal_equivalence", "overblocking", "general_regression")
    return [
        {"case_id": f"case-{index:02d}", "category": categories[index % len(categories)]}
        for index in range(size)
    ]


def test_layer2_selects_one_tenth_with_a_minimum_of_three_deterministically() -> None:
    cases = _population(40)

    first = select_cases(cases, seed="round2-20260722")
    repeated = select_cases(reversed(cases), seed="round2-20260722")

    assert len(first) == 4
    assert first == repeated
    assert len(select_cases(_population(8), seed="small")) == 3


def test_layer2_records_seed_round_and_recently_never_selected_cases() -> None:
    population = _population(30)
    selected = select_cases(population, seed="seed-1")
    results = {case_id: {"passed": True} for case_id in selected}

    report, ledger = evaluate_layer2_round(
        population=population,
        results=results,
        seed="seed-1",
        round_id="round-001",
        previous_ledger=None,
    )

    assert report["passed"] is True
    assert report["seed"] == "seed-1"
    assert report["sample_size"] == 3
    assert ledger["rounds"] == [
        {"round_id": "round-001", "seed": "seed-1", "selected_case_ids": selected}
    ]
    assert set(ledger["never_selected_last_10"]) == {
        row["case_id"] for row in population
    } - set(selected)


def test_layer2_promotes_a_sample_failure_to_permanent_layer1() -> None:
    population = _population(30)
    selected = select_cases(population, seed="seed-2")
    failed = selected[1]
    results = {
        case_id: {"passed": case_id != failed, "reason": "mismatch" if case_id == failed else None}
        for case_id in selected
    }

    report, ledger = evaluate_layer2_round(
        population=population,
        results=results,
        seed="seed-2",
        round_id="round-002",
        previous_ledger={"rounds": [], "permanent_layer1": ["historical-case"]},
    )

    assert report["passed"] is False
    assert report["failed_case_ids"] == [failed]
    assert ledger["permanent_layer1"] == [failed, "historical-case"]


def test_layer2_fails_closed_when_a_selected_result_is_missing() -> None:
    population = _population(30)
    selected = select_cases(population, seed="seed-3")
    results = {case_id: {"passed": True} for case_id in selected[:-1]}

    report, _ledger = evaluate_layer2_round(
        population=population,
        results=results,
        seed="seed-3",
        round_id="round-003",
        previous_ledger=None,
    )

    assert report["passed"] is False
    assert report["missing_result_case_ids"] == [selected[-1]]
