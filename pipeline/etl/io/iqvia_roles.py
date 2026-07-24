"""Fail-closed IQVIA source-role binding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CANONICAL_NSA_FILENAME = "KOR_NSA_Jun-25-2026.xlsx"
SUPPORTED_SUFFIXES = frozenset({".csv", ".xls", ".xlsx"})


class IqviaRoleContractError(ValueError):
    """Raised when an IQVIA file cannot be assigned to one unambiguous role."""


@dataclass(frozen=True)
class IqviaSource:
    path: Path
    relative_path: Path
    role: str


def bind_iqvia_sources(root: Path) -> tuple[IqviaSource, ...]:
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise IqviaRoleContractError(f"IQVIA source root is missing: {resolved_root}")

    sources: list[IqviaSource] = []
    for path in sorted(resolved_root.rglob("*")):
        if (
            not path.is_file()
            or path.name.startswith(("~$", "._"))
            or path.suffix.lower() not in SUPPORTED_SUFFIXES
        ):
            continue
        relative = path.resolve().relative_to(resolved_root)
        if len(relative.parts) < 2:
            raise IqviaRoleContractError(
                f"IQVIA source role cannot be derived from root-level file: {relative}"
            )
        role = relative.parts[0].strip().upper()
        if not role:
            raise IqviaRoleContractError(f"IQVIA source role is empty: {relative}")
        sources.append(IqviaSource(path.resolve(), relative, role))
    if not sources:
        raise IqviaRoleContractError(f"no role-bound IQVIA sources under {resolved_root}")
    return tuple(sources)


def canonical_nsa_source(sources: tuple[IqviaSource, ...]) -> IqviaSource:
    candidates = tuple(source for source in sources if source.role == "NSA")
    if len(candidates) != 1:
        names = ", ".join(source.relative_path.as_posix() for source in candidates) or "none"
        raise IqviaRoleContractError(
            f"exactly one NSA source is required; found {len(candidates)}: {names}"
        )
    source = candidates[0]
    if source.path.name != CANONICAL_NSA_FILENAME:
        raise IqviaRoleContractError(
            f"NSA source must be pinned to {CANONICAL_NSA_FILENAME}; got {source.path.name}"
        )
    return source
