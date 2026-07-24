"""Compatibility import for the orchestrator's IQVIA role gate."""

from pipeline.etl.io.iqvia_roles import (  # noqa: F401
    CANONICAL_NSA_FILENAME,
    IqviaRoleContractError,
    IqviaSource,
    bind_iqvia_sources,
    canonical_nsa_source,
)
