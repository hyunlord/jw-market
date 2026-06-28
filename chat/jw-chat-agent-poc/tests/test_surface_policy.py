from __future__ import annotations

from jw_chat_agent_poc.orchestrator.surface_policy import CagrOperands, DeltaOperands, can_surface_derived_value


def test_can_surface_delta_when_displayed_operands_reproduce_value() -> None:
    allowed = can_surface_derived_value(
        0.5279,
        required_period="2025-07→2026-04",
        delta_operands=DeltaOperands(from_value=4.789, to_value=5.319, delta_value=0.5279),
    )

    assert allowed is True


def test_blocks_delta_when_displayed_operands_do_not_reproduce_value() -> None:
    allowed = can_surface_derived_value(
        0.53,
        required_period="2025-07→2026-04",
        delta_operands=DeltaOperands(from_value=4.79, to_value=5.31, delta_value=0.53),
    )

    assert allowed is False


def test_blocks_delta_without_operands_or_period() -> None:
    assert can_surface_derived_value(
        0.53,
        required_period="",
        delta_operands=DeltaOperands(from_value=4.79, to_value=5.32, delta_value=0.53),
    ) is False
    assert can_surface_derived_value(
        0.53,
        required_period="2025-07→2026-04",
        delta_operands=DeltaOperands(from_value=None, to_value=5.32, delta_value=0.53),
    ) is False


def test_can_surface_cagr_when_displayed_operands_reproduce_value() -> None:
    allowed = can_surface_derived_value(
        14.87,
        cagr_operands=CagrOperands(
            start_period="2021",
            start_value=100.0,
            end_period="2026",
            end_value=200.0,
            year_count=5,
            formula="((200 / 100) ** (1 / 5) - 1) * 100",
        ),
    )

    assert allowed is True


def test_blocks_cagr_without_displayed_operands() -> None:
    assert can_surface_derived_value(31.22, cagr_operands=CagrOperands()) is False


def test_blocks_cagr_when_displayed_operands_do_not_reproduce_value() -> None:
    allowed = can_surface_derived_value(
        31.22,
        cagr_operands=CagrOperands(
            start_period="2021",
            start_value=100.0,
            end_period="2026",
            end_value=200.0,
            year_count=5,
            formula="((200 / 100) ** (1 / 5) - 1) * 100",
        ),
    )

    assert allowed is False
