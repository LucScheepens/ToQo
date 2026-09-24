"""Standings tables and tiebreakers (SPEC §8).

Standings are derived only from results + rules; nothing here is stored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable, Iterable

TeamId = Hashable

TIEBREAKERS = {
    "points": "Points",
    "wins": "Wins",
    "win_pct": "Win %",
    "head_to_head": "Head-to-head",
    "h2h_point_diff": "Head-to-head point differential",
    "point_diff": "Point differential",
    "points_for": "Points scored",
    "points_against": "Fewest points allowed",
    "buchholz": "Buchholz (opponents' points)",
    "seed": "Seed",
}
DEFAULT_RR_TIEBREAKERS = ["points", "head_to_head", "point_diff", "points_for"]
DEFAULT_SWISS_TIEBREAKERS = ["points", "buchholz", "point_diff", "points_for"]


@dataclass
class Result:
    """One resolved game. ``kind`` is 'played', 'forfeit' or 'bye'.

    For 'bye' only ``home`` is meaningful. For 'forfeit' ``forfeit_side`` is 'H'/'A'.
    ``winner_side`` ('H', 'A', 'D') is authoritative for W/D/L (it may differ from the
    score when a tie was decided by shootout/overtime).
    """
    home: TeamId
    away: TeamId | None
    home_score: int = 0
    away_score: int = 0
    kind: str = "played"
    winner_side: str | None = None


@dataclass
class PointsConfig:
    win: int = 3
    draw: int = 1
    loss: int = 0


@dataclass
class Row:
    team: TeamId
    gp: int = 0
    w: int = 0
    d: int = 0
    l: int = 0
    pf: int = 0
    pa: int = 0
    pts: int = 0
    byes: int = 0
    buchholz: int = 0
    rank: int = 0
    tie_unresolved: bool = False
    opponents: list = field(default_factory=list)

    @property
    def pd(self) -> int:
        return self.pf - self.pa

    @property
    def win_pct(self) -> float:
        n = self.gp + self.byes
        return (self.w + 0.5 * self.d) / n if n else 0.0


def _winner_side(r: Result) -> str:
    if r.winner_side:
        return r.winner_side
    if r.home_score > r.away_score:
        return "H"
    if r.away_score > r.home_score:
        return "A"
    return "D"


def compute_table(
    teams: Iterable[TeamId],
    results: Iterable[Result],
    points: PointsConfig | None = None,
    tiebreakers: list[str] | None = None,
    seeds: dict | None = None,
    names: dict | None = None,
) -> list[Row]:
    points = points or PointsConfig()
    tiebreakers = tiebreakers or DEFAULT_RR_TIEBREAKERS
    seeds = seeds or {}
    names = names or {}
    rows = {t: Row(team=t) for t in teams}
    results = [r for r in results if r.home in rows and (r.kind == "bye" or r.away in rows)]

    for r in results:
        if r.kind == "bye":
            row = rows[r.home]
            row.byes += 1
            row.w += 1
            row.pts += points.win
            continue
        h, a = rows[r.home], rows[r.away]
        h.gp += 1
        a.gp += 1
        h.pf += r.home_score
        h.pa += r.away_score
        a.pf += r.away_score
        a.pa += r.home_score
        h.opponents.append(r.away)
        a.opponents.append(r.home)
        side = _winner_side(r)
        if side == "H":
            h.w, a.l = h.w + 1, a.l + 1
            h.pts += points.win
            a.pts += points.loss
        elif side == "A":
            a.w, h.l = a.w + 1, h.l + 1
            a.pts += points.win
            h.pts += points.loss
        else:
            h.d, a.d = h.d + 1, a.d + 1
            h.pts += points.draw
            a.pts += points.draw

    for row in rows.values():
        row.buchholz = sum(rows[o].pts for o in row.opponents if o in rows)

    def mini(group: list[TeamId]) -> dict:
        """Head-to-head points and point differential among ``group``."""
        g = set(group)
        acc = {t: [0, 0] for t in group}
        for r in results:
            if r.kind == "bye" or r.home not in g or r.away not in g:
                continue
            side = _winner_side(r)
            acc[r.home][1] += r.home_score - r.away_score
            acc[r.away][1] += r.away_score - r.home_score
            if side == "H":
                acc[r.home][0] += points.win
                acc[r.away][0] += points.loss
            elif side == "A":
                acc[r.away][0] += points.win
                acc[r.home][0] += points.loss
            else:
                acc[r.home][0] += points.draw
                acc[r.away][0] += points.draw
        return acc

    def key_fn(criterion: str, group: list[TeamId]):
        if criterion in ("head_to_head", "h2h_point_diff"):
            m = mini(group)
            idx = 0 if criterion == "head_to_head" else 1
            return lambda t: m[t][idx]
        getters = {
            "points": lambda t: rows[t].pts,
            "wins": lambda t: rows[t].w,
            "win_pct": lambda t: rows[t].win_pct,
            "point_diff": lambda t: rows[t].pd,
            "points_for": lambda t: rows[t].pf,
            "points_against": lambda t: -rows[t].pa,
            "buchholz": lambda t: rows[t].buchholz,
            "seed": lambda t: -(seeds.get(t) or 10**6),
        }
        return getters[criterion]

    unresolved: set = set()

    def order(group: list[TeamId], criteria: list[str]) -> list[TeamId]:
        if len(group) <= 1:
            return group
        if not criteria:
            unresolved.update(group)
            return sorted(group, key=lambda t: (seeds.get(t) or 10**6, str(names.get(t, t))))
        k = key_fn(criteria[0], group)
        buckets: dict = {}
        for t in group:
            buckets.setdefault(k(t), []).append(t)
        out: list[TeamId] = []
        for value in sorted(buckets, reverse=True):
            out += order(buckets[value], criteria[1:])
        return out

    criteria = [c for c in tiebreakers if c in TIEBREAKERS]
    ranked = order(list(rows), criteria)
    table = []
    for i, t in enumerate(ranked, start=1):
        row = rows[t]
        row.rank = i
        row.tie_unresolved = t in unresolved and row.gp + row.byes > 0
        table.append(row)
    return table
