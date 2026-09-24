"""Derived standings, bracket placements and rankings (SPEC §8)."""
from dataclasses import dataclass

from engine.standings import (
    DEFAULT_RR_TIEBREAKERS,
    DEFAULT_SWISS_TIEBREAKERS,
    PointsConfig,
    Result,
    compute_table,
)

from ..models import Game, Stage


def stage_games(stage):
    return list(
        stage.games.select_related("home_team", "away_team", "pool", "space", "space__venue",
                                   "home_entry__team", "away_entry__team", "home_prev", "away_prev")
        .order_by("id")
    )


def points_config(tournament):
    return PointsConfig(tournament.points_win, tournament.points_draw, tournament.points_loss)


def tiebreakers_for(stage):
    if stage.tiebreakers:
        return stage.tiebreakers
    return DEFAULT_SWISS_TIEBREAKERS if stage.kind == Stage.Kind.SWISS else DEFAULT_RR_TIEBREAKERS


def game_results(games):
    out = []
    for g in games:
        if g.is_bye and (g.home_team_id or g.away_team_id) and not (g.home_bye and g.away_bye):
            team = g.home_team_id or g.away_team_id
            out.append(Result(home=team, away=None, kind="bye"))
        elif g.has_result and g.teams_known:
            out.append(Result(
                home=g.home_team_id, away=g.away_team_id,
                home_score=g.home_score or 0, away_score=g.away_score or 0,
                kind="forfeit" if g.status == Game.Status.FORFEIT else "played",
                winner_side={"H": "H", "A": "A", "D": "D"}.get(g.winner_side),
            ))
    return out


@dataclass
class TableRow:
    rank: int
    team: object
    gp: int = 0
    w: int = 0
    d: int = 0
    l: int = 0
    pf: int = 0
    pa: int = 0
    pd: int = 0
    pts: int = 0
    buchholz: int = 0
    tie_unresolved: bool = False
    shared: bool = False
    alive: bool = False


def pool_table(pool, games=None, entries=None):
    """Round-robin/Swiss table for a pool."""
    stage = pool.stage
    entries = entries if entries is not None else list(pool.entries.select_related("team"))
    teams = {e.team_id: e.team for e in entries if e.team_id}
    seeds = {e.team_id: e.position for e in entries if e.team_id}
    games = games if games is not None else [g for g in stage_games(stage) if g.pool_id == pool.id]
    rows = compute_table(
        list(teams),
        game_results(games),
        points_config(stage.category.tournament),
        tiebreakers_for(stage),
        seeds=seeds,
        names={k: t.name for k, t in teams.items()},
    )
    return [
        TableRow(rank=r.rank, team=teams[r.team], gp=r.gp, w=r.w, d=r.d, l=r.l, pf=r.pf, pa=r.pa,
                 pd=r.pd, pts=r.pts, buchholz=r.buchholz, tie_unresolved=r.tie_unresolved)
        for r in rows
    ]


def _outcome_teams(g):
    """(winner, loser) team objects for a decided game, bye-aware."""
    if g.home_bye or g.away_bye:
        other = g.away_team if g.home_bye else g.home_team
        return other, None
    if g.has_result:
        return g.winner, g.loser
    return None, None


def bracket_placements(pool, games=None, entries=None):
    """Final/provisional placements for a bracket flight (SPEC §8.3)."""
    entries = entries if entries is not None else list(pool.entries.select_related("team"))
    seeds = {e.team_id: e.position for e in entries if e.team_id}
    teams = {e.team_id: e.team for e in entries if e.team_id}
    games = games if games is not None else [g for g in stage_games(pool.stage) if g.pool_id == pool.id]
    games = sorted(games, key=lambda g: g.id)

    places = {}
    gf1 = next((g for g in games if g.bracket == "F" and g.round == 1), None)
    gf2 = next((g for g in games if g.if_necessary), None)
    for g in games:
        w, l = _outcome_teams(g)
        if g.place_winner and w:
            places[w.id] = g.place_winner
            if l:
                places[l.id] = g.place_loser
    if gf1 and gf2:
        final = gf2 if gf2.has_result else (gf1 if gf1.has_result and gf1.winner_side == "H" else None)
        if final:
            places[final.winner.id], places[final.loser.id] = 1, 2

    # Elimination depth for the rest: the last game each team appears in.
    last = {}
    for pos, g in enumerate(games):
        for t in (g.home_team_id, g.away_team_id):
            if t in teams:
                last[t] = (pos, g)

    rows = [TableRow(rank=places[t], team=teams[t]) for t in teams if t in places]
    groups = {}
    for t in teams:
        if t in places:
            continue
        pos_g = last.get(t)
        if pos_g is None:
            key = ("alive", 0, 0)
        else:
            _, g = pos_g
            eliminated = g.has_result and _outcome_teams(g)[1] and _outcome_teams(g)[1].id == t
            if eliminated:
                first_of_round = min(p for p, x in enumerate(games) if x.bracket == g.bracket and x.round == g.round)
                key = ("out", first_of_round, 0)
            else:
                key = ("alive", 0, 0)
        groups.setdefault(key, []).append(t)

    def group_sort(k):
        return (0, 0) if k[0] == "alive" else (1, -k[1])

    cur = len(rows) + 1
    for key in sorted(groups, key=group_sort):
        members = sorted(groups[key], key=lambda t: seeds.get(t, 999))
        for t in members:
            rows.append(TableRow(rank=cur, team=teams[t], shared=len(members) > 1, alive=key[0] == "alive"))
        cur += len(members)
    rows.sort(key=lambda r: (r.rank, seeds.get(r.team.id, 999)))
    return rows


def pool_standings(pool, games=None, entries=None):
    if pool.stage.is_bracket:
        return bracket_placements(pool, games, entries)
    return pool_table(pool, games, entries)


def pool_ranking(pool, games=None):
    """Unique ordering of teams in a pool: list of (team, tie_flag)."""
    return [(r.team, r.tie_unresolved or r.shared) for r in pool_standings(pool, games)]


def stage_overall_ranking(stage, games=None):
    """Rank teams across all pools: pool rank first, then per-game performance."""
    games = games if games is not None else stage_games(stage)
    candidates = []
    for pool in stage.pools.all():
        rows = pool_standings(pool, [g for g in games if g.pool_id == pool.id])
        for r in rows:
            n = max(1, r.gp)
            candidates.append((r.rank, -r.pts / n, -(r.pd / n), -(r.pf / n), r.team.name, r))
    candidates.sort(key=lambda c: c[:5])
    return [(c[5].team, c[5].tie_unresolved or c[5].shared) for c in candidates]


def category_final_standings(category):
    """Final placements: last stage flights stacked (SPEC §8.3)."""
    stages = list(category.stages.all())
    if not stages:
        return []
    last = stages[-1]
    games = stage_games(last)
    out, offset = [], 0
    for pool in last.pools.all():
        rows = pool_standings(pool, [g for g in games if g.pool_id == pool.id])
        for r in rows:
            out.append((offset + r.rank, r.team, pool.name))
        offset += len(rows)
    return out
