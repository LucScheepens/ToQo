"""Elimination bracket structures (SPEC §7.2, §7.3).

Brackets are emitted as a list of :class:`BGame` in topological order: a game only
references games that appear earlier in the list. Side sources are tuples:

* ``("seed", k)`` – the k-th entry of the flight (1-based)
* ``("bye",)``     – no opponent
* ``("W", key)`` / ``("L", key)`` – winner / loser of an earlier game
"""
from __future__ import annotations

from dataclasses import dataclass

Src = tuple


@dataclass
class BGame:
    key: str
    bracket: str  # W, L, F (grand final), P (placement)
    round: int
    index: int
    label: str
    home: Src
    away: Src
    place_winner: int | None = None
    place_loser: int | None = None
    if_necessary: bool = False

    @property
    def code(self) -> str:
        return self.key


def next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def seed_order(size: int) -> list[int]:
    """Standard bracket order: 8 -> [1, 8, 4, 5, 2, 7, 3, 6]."""
    order = [1]
    while len(order) < size:
        m = len(order) * 2 + 1
        order = [x for s in order for x in (s, m - s)]
    return order


def _seed_srcs(n: int) -> list[Src]:
    size = max(2, next_pow2(n))
    return [("seed", s) if s <= n else ("bye",) for s in seed_order(size)]


def round_name(teams_in_round: int) -> str:
    return {2: "Final", 4: "Semifinal", 8: "Quarterfinal"}.get(teams_in_round, f"Round of {teams_in_round}")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def single_elimination(n: int, placement: str = "none") -> list[BGame]:
    """Single elimination for ``n`` entries.

    placement: 'none', 'third' (3rd-place game) or 'all' (every place played out).
    """
    if n < 2:
        return []
    games: list[BGame] = []

    def build(srcs: list[Src], offset: int, main: bool) -> None:
        prefix = "" if main else f"P{offset + 1}-"
        round_srcs = srcs
        r = 1
        losers_by_round: list[list[Src]] = []
        while len(round_srcs) > 1:
            size = len(round_srcs)
            nxt, losers = [], []
            for i in range(0, size, 2):
                idx = i // 2 + 1
                if main:
                    name = round_name(size)
                    label = name if size == 2 else f"{name} {idx}"
                    key = {2: "F", 4: f"SF{idx}", 8: f"QF{idx}"}.get(size, f"R{size}-{idx}")
                else:
                    lo, hi = offset + 1, offset + len(srcs)
                    if size == 2:
                        label = f"{_ordinal(lo)} place game"
                    else:
                        label = f"{_ordinal(lo)}–{_ordinal(hi)} place, round {r} #{idx}"
                    key = f"{prefix}R{r}-{idx}" if size > 2 else f"{prefix}F"
                g = BGame(key=key, bracket="W" if main else "P", round=r, index=idx,
                          label=label, home=round_srcs[i], away=round_srcs[i + 1])
                if size == 2:
                    g.place_winner, g.place_loser = offset + 1, offset + 2
                games.append(g)
                nxt.append(("W", g.key))
                losers.append(("L", g.key))
            losers_by_round.append(losers)
            round_srcs = nxt
            r += 1
        for losers in losers_by_round[:-1]:  # the final's loser is already placed
            k = len(losers)
            if placement == "all" or (placement == "third" and main and k == 2):
                build(losers, offset + k, main=False)

    build(_seed_srcs(n), 0, main=True)
    return games


def double_elimination(n: int, reset: bool = True) -> list[BGame]:
    if n < 2:
        return []
    games: list[BGame] = []
    srcs = _seed_srcs(n)
    size = len(srcs)

    # Winners bracket.
    wb_losers: list[list[Src]] = []
    round_srcs, r = srcs, 1
    while len(round_srcs) > 1:
        m = len(round_srcs)
        nxt, losers = [], []
        for i in range(0, m, 2):
            idx = i // 2 + 1
            label = f"Winners {round_name(m).lower()}" + ("" if m == 2 else f" {idx}")
            g = BGame(key=f"W{r}-{idx}", bracket="W", round=r, index=idx, label=label,
                      home=round_srcs[i], away=round_srcs[i + 1])
            games.append(g)
            nxt.append(("W", g.key))
            losers.append(("L", g.key))
        wb_losers.append(losers)
        round_srcs = nxt
        r += 1
    wb_champion = round_srcs[0]

    # Losers bracket.
    lr = 0

    def lb_round(pairs: list[tuple[Src, Src]]) -> list[Src]:
        nonlocal lr
        lr += 1
        out = []
        for idx, (h, a) in enumerate(pairs, start=1):
            g = BGame(key=f"L{lr}-{idx}", bracket="L", round=lr, index=idx,
                      label=f"Losers round {lr} #{idx}", home=h, away=a)
            games.append(g)
            out.append(("W", g.key))
        return out

    if size == 2:
        lb_champion = None
    else:
        first = wb_losers[0]
        survivors = lb_round([(first[i], first[i + 1]) for i in range(0, len(first), 2)])
        for rr in range(1, len(wb_losers)):
            drop = list(wb_losers[rr])
            if rr % 2 == 1:
                drop.reverse()
            survivors = lb_round(list(zip(survivors, drop)))
            if len(survivors) > 1:
                survivors = lb_round([(survivors[i], survivors[i + 1]) for i in range(0, len(survivors), 2)])
        lb_champion = survivors[0]
        # Label the last losers-bracket game.
        games[-1].label = "Losers final"

    if lb_champion is None:
        # Two entries: a best-of-two style final is pointless; single game decides.
        games[-1].place_winner, games[-1].place_loser = 1, 2
        games[-1].label = "Final"
        return games

    gf = BGame(key="GF1", bracket="F", round=1, index=1, label="Grand final",
               home=wb_champion, away=lb_champion)
    games.append(gf)
    if reset:
        games.append(BGame(key="GF2", bracket="F", round=2, index=1,
                           label="Grand final (if necessary)", home=wb_champion,
                           away=lb_champion, if_necessary=True))
    else:
        gf.place_winner, gf.place_loser = 1, 2
    return games
