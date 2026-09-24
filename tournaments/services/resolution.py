"""Re-derive a category from results (SPEC §1.2, §7, §9).

``recompute_category`` is the single entry point: it resolves every game's sides
(entries, winner/loser sources, byes), pairs Swiss rounds, and keeps advanced slots
consistent with current standings. It returns descriptors of *other* recorded results
that had to be cleared because their teams changed.
"""
from django.db.models import Q

from engine.swiss import first_round, pair_round

from ..models import Entry, Game, Stage
from .standings import pool_ranking, pool_table, stage_games, stage_overall_ranking

BYE = object()


def _describe(g):
    score = f"{g.home_score}–{g.away_score}" if g.home_score is not None else g.get_status_display()
    return {"id": g.id, "number": g.number, "title": g.title, "stage": g.stage.name,
            "teams": f"{g.home_label} vs {g.away_label}", "result": score}


def _clear_result(g):
    g.status = Game.Status.SCHEDULED
    g.home_score = g.away_score = None
    g.winner_side = g.forfeit_side = ""
    g.started_at = g.finished_at = None


def _outcome(p, outcome):
    """(team, is_bye) for winner/loser of game p (whose sides are already resolved)."""
    if p.home_bye and p.away_bye:
        return None, True
    if p.home_bye or p.away_bye:
        other = p.away_team if p.home_bye else p.home_team
        if outcome == "W":
            return other, False
        return None, True
    if p.has_result:
        return (p.winner if outcome == "W" else p.loser), False
    return None, False


def _side(g, side, by_id, entries):
    entry_id = getattr(g, f"{side}_entry_id")
    prev_id = getattr(g, f"{side}_prev_id")
    if entry_id:
        e = entries.get(entry_id)
        return (e.team if e and e.team_id else None), False
    if prev_id:
        return _outcome(by_id[prev_id], getattr(g, f"{side}_prev_outcome"))
    return None, True


def _set_sides(g, home, away):
    """Write resolved sides; returns True if anything changed."""
    (ht, hb), (at, ab) = home, away
    new = (ht.id if ht else None, hb, at.id if at else None, ab)
    old = (g.home_team_id, g.home_bye, g.away_team_id, g.away_bye)
    if new == old:
        return False
    g.home_team, g.home_bye, g.away_team, g.away_bye = ht, hb, at, ab
    return True


def resolve_stage(stage):
    affected = []
    games = stage_games(stage)
    by_id = {g.id: g for g in games}
    entries = {e.id: e for e in Entry.objects.filter(pool__stage=stage).select_related("team")}
    gf1 = next((g for g in games if g.bracket == "F" and g.round == 1), None)
    for g in games:
        changed = False
        if not g.manual_teams:
            home = _side(g, "home", by_id, entries)
            away = _side(g, "away", by_id, entries)
            before = _describe(g)
            if _set_sides(g, home, away):
                changed = True
                if g.has_result or g.status == Game.Status.IN_PROGRESS:
                    affected.append(before)
                    _clear_result(g)
                if g.is_bye:
                    g.space, g.start_at = None, None
        if g.if_necessary and gf1 is not None:
            needed = gf1.has_result and gf1.winner_side == "A"
            if not needed and gf1.has_result and g.status != Game.Status.CANCELLED:
                if g.has_result:
                    affected.append(_describe(g))
                _clear_result(g)
                g.status = Game.Status.CANCELLED
                g.note = "Not necessary"
                changed = True
            elif (needed or not gf1.has_result) and g.status == Game.Status.CANCELLED:
                g.status = Game.Status.SCHEDULED
                g.note = ""
                changed = True
        if changed:
            g.save()
    return affected


def resolve_swiss(stage):
    games = stage_games(stage)
    pool = stage.pools.first()
    if pool is None:
        return
    entries = list(pool.entries.select_related("team").order_by("position"))
    filled = entries and all(e.team_id for e in entries)
    rounds = {}
    for g in games:
        rounds.setdefault(g.round, []).append(g)
    for r in sorted(rounds):
        gs = sorted(rounds[r], key=lambda g: g.index)
        if any(g.has_result for g in gs):
            continue
        prev = [g for g in games if g.round < r]
        ready = filled and all(game_done(g) for g in prev)
        assignments = []
        if ready:
            if r == 1:
                pairs, bye = first_round([e.team for e in entries])
            else:
                table = pool_table(pool, prev, entries)
                played = {frozenset((g.home_team_id, g.away_team_id)) for g in prev if g.teams_known}
                had_bye = {g.home_team_id for g in prev if g.away_bye}
                ids = {e.team_id: e.team for e in entries}
                pairs_ids, bye_id = pair_round([row.team.id for row in table], played, had_bye)
                pairs = [(ids[a], ids[b]) for a, b in pairs_ids]
                bye = ids.get(bye_id)
            assignments = [((h, False), (a, False)) for h, a in pairs]
            if bye is not None:
                assignments.append(((bye, False), (None, True)))
        for i, g in enumerate(gs):
            sides = assignments[i] if i < len(assignments) else ((None, False), (None, False))
            if _set_sides(g, *sides):
                if g.is_bye:
                    g.space, g.start_at = None, None
                g.save()


def game_done(g):
    """Resolved, or a bye whose present side is known (it auto-advances)."""
    if g.is_resolved or (g.home_bye and g.away_bye):
        return True
    if g.home_bye:
        return g.away_team_id is not None
    if g.away_bye:
        return g.home_team_id is not None
    return False


def stage_complete(stage, games=None):
    games = games if games is not None else stage_games(stage)
    if not games:
        return False
    if Entry.objects.filter(pool__stage=stage, team__isnull=True).exists():
        return False
    return all(game_done(g) for g in games)


def stage_status(stage, games=None):
    games = games if games is not None else stage_games(stage)
    if Entry.objects.filter(pool__stage=stage, team__isnull=True).exists():
        return "waiting"
    if stage_complete(stage, games):
        return "transitioned" if stage.advanced_at else "complete"
    if any(g.has_result or g.status == Game.Status.IN_PROGRESS for g in games):
        return "in_progress"
    return "not_started"


def downstream_entries(stage):
    return Entry.objects.filter(Q(source_pool__stage=stage) | Q(source_stage=stage)).select_related(
        "pool", "pool__stage", "source_pool", "source_stage", "team")


def qualifying_map(stage, games=None):
    """[(entry, team_or_None, tie_flag)] for every slot sourced from ``stage`` (SPEC §9)."""
    games = games if games is not None else stage_games(stage)
    rankings, overall = {}, None
    out = []
    for e in downstream_entries(stage).order_by("pool__stage__order", "pool__order", "position"):
        if e.source_pool_id:
            if e.source_pool_id not in rankings:
                rankings[e.source_pool_id] = pool_ranking(
                    e.source_pool, [g for g in games if g.pool_id == e.source_pool_id])
            ranking = rankings[e.source_pool_id]
        else:
            if overall is None:
                overall = stage_overall_ranking(stage, games)
            ranking = overall
        idx = (e.source_rank or 0) - 1
        team, tie = ranking[idx] if 0 <= idx < len(ranking) else (None, False)
        out.append((e, team, tie))
    return out


def recompute_category(category):
    affected = []
    for stage in category.stages.all():
        if stage.kind == Stage.Kind.SWISS:
            resolve_swiss(stage)
        else:
            affected += resolve_stage(stage)
        if stage.advanced_at:
            games = stage_games(stage)
            if stage_complete(stage, games):
                for e, team, _ in qualifying_map(stage, games):
                    if not e.locked and e.team_id != (team.id if team else None):
                        e.team = team
                        e.save(update_fields=["team"])
            else:
                for e in downstream_entries(stage).filter(locked=False, team__isnull=False):
                    e.team = None
                    e.save(update_fields=["team"])
                stage.advanced_at = None
                stage.save(update_fields=["advanced_at"])
    return affected
