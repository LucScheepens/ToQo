"""Swiss pairing (SPEC §7.4)."""
from __future__ import annotations

from typing import Hashable, Sequence

TeamId = Hashable


def first_round(ordered: Sequence[TeamId]) -> tuple[list[tuple[TeamId, TeamId]], TeamId | None]:
    """Top half vs bottom half by seed; the last team gets a bye when odd."""
    teams = list(ordered)
    bye = teams.pop() if len(teams) % 2 else None
    half = len(teams) // 2
    return [(teams[i], teams[i + half]) for i in range(half)], bye


def pair_round(
    ordered: Sequence[TeamId],
    played: set[frozenset],
    had_bye: set[TeamId] | None = None,
) -> tuple[list[tuple[TeamId, TeamId]], TeamId | None]:
    """Pair teams ordered best-first, avoiding rematches where possible.

    Returns (pairs, bye_team). The bye goes to the lowest-ranked team that has not
    had one yet.
    """
    teams = list(ordered)
    had_bye = had_bye or set()
    bye = None
    if len(teams) % 2:
        candidates = [t for t in reversed(teams) if t not in had_bye] or [teams[-1]]
        bye = candidates[0]
        teams.remove(bye)

    def solve(pool: list[TeamId], allow_rematch: bool):
        if not pool:
            return []
        head, rest = pool[0], pool[1:]
        for i, opp in enumerate(rest):
            if not allow_rematch and frozenset((head, opp)) in played:
                continue
            sub = solve(rest[:i] + rest[i + 1:], allow_rematch)
            if sub is not None:
                return [(head, opp)] + sub
        return None

    pairs = solve(teams, False)
    if pairs is None:
        pairs = solve(teams, True) or []
    return pairs, bye
