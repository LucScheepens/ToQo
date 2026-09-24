"""Automatic schedule generation (SPEC §11.2).

Greedy list scheduling onto a time grid. Games are placed in the given priority order;
dependencies (feeder games, source pools) and team rest are respected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Hashable


@dataclass
class SGame:
    id: Hashable
    duration: int  # minutes
    teams: set = field(default_factory=set)  # team ids or placeholder keys
    deps: list = field(default_factory=list)  # game ids that must end before this starts
    priority: tuple = ()
    fixed: tuple | None = None  # (space_id, start datetime) for games kept in place


@dataclass
class Window:
    start: datetime
    end: datetime


@dataclass
class ScheduleResult:
    assignments: dict  # game id -> (space id, start)
    unscheduled: list  # game ids


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def schedule(
    games: list[SGame],
    spaces: list,
    windows: list[Window],
    interval: int,
    buffer: int = 0,
    min_rest: int = 0,
) -> ScheduleResult:
    interval = max(5, interval)
    busy: dict = {s: [] for s in spaces}  # space -> [(start, end_incl_buffer)]
    team_end: dict = {}  # team key -> latest end
    end_of: dict = {}  # game id -> end datetime
    assignments: dict = {}
    unscheduled: list = []
    rest = timedelta(minutes=min_rest)
    windows = sorted(windows, key=lambda w: w.start)

    by_id = {g.id: g for g in games}

    def place(g: SGame, space, start: datetime) -> None:
        end = start + timedelta(minutes=g.duration)
        busy.setdefault(space, []).append((start, end + timedelta(minutes=buffer)))
        for t in g.teams:
            team_end[t] = max(team_end.get(t, end), end)
        end_of[g.id] = end
        assignments[g.id] = (space, start)

    for g in games:
        if g.fixed:
            place(g, *g.fixed)

    for g in sorted((g for g in games if not g.fixed), key=lambda g: g.priority):
        earliest = None
        blocked = False
        for d in g.deps:
            if d not in by_id:
                continue
            if d not in end_of:
                blocked = True
                break
            t = end_of[d] + rest
            earliest = t if earliest is None or t > earliest else earliest
        if blocked:
            unscheduled.append(g.id)
            continue
        for t in g.teams:
            if t in team_end:
                e = team_end[t] + rest
                earliest = e if earliest is None or e > earliest else earliest
        dur = timedelta(minutes=g.duration)
        occupied = dur + timedelta(minutes=buffer)
        placed = False
        for w in windows:
            start = w.start
            while start + dur <= w.end:
                if earliest is None or start >= earliest:
                    for s in spaces:
                        if not any(_overlaps(start, start + occupied, b0, b1) for b0, b1 in busy[s]):
                            place(g, s, start)
                            placed = True
                            break
                    if placed:
                        break
                start += timedelta(minutes=interval)
            if placed:
                break
        if not placed:
            unscheduled.append(g.id)
    return ScheduleResult(assignments=assignments, unscheduled=unscheduled)
