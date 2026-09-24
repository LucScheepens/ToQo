"""Round-robin pairings using the circle method (SPEC §7.1)."""
from __future__ import annotations


def round_robin_rounds(n: int, cycles: int = 1, games_per_team: int | None = None) -> list[list[tuple[int, int]]]:
    """Return rounds of (home, away) pairs using 0-based positions.

    Odd ``n`` gets a phantom opponent; pairings against it are dropped (a bye).
    ``cycles=2`` appends a second cycle with home/away swapped.
    ``games_per_team`` truncates to the first *k* rounds.
    """
    if n < 2:
        return []
    players: list[int | None] = list(range(n))
    if n % 2:
        players.append(None)
    m = len(players)
    single: list[list[tuple[int, int]]] = []
    for r in range(m - 1):
        pairs = []
        for i in range(m // 2):
            a, b = players[i], players[m - 1 - i]
            if a is None or b is None:
                continue
            # Alternate home/away so the fixed player doesn't always host.
            if (i == 0 and r % 2 == 1) or (i > 0 and i % 2 == 1):
                a, b = b, a
            pairs.append((a, b))
        single.append(pairs)
        # Rotate every position except the first.
        players = [players[0], players[-1]] + players[1:-1]

    rounds = list(single)
    for c in range(1, max(1, cycles)):
        rounds += [[(b, a) if c % 2 else (a, b) for a, b in rnd] for rnd in single]
    if games_per_team:
        rounds = rounds[:games_per_team]
    return rounds
