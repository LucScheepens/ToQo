"""Schedule generation, manual edits, conflicts and queue mode (SPEC §11)."""
from collections import defaultdict
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from engine.scheduler import SGame, Window, schedule

from ..models import Game, Space, Tournament
from .common import ServiceError, log, touch
from .structure import renumber


def tournament_games(tournament):
    return list(Game.objects.filter(stage__category__tournament=tournament).select_related(
        "stage", "stage__category", "pool", "space", "space__venue", "home_team", "away_team",
        "home_entry", "away_entry", "home_prev", "away_prev",
        "home_entry__team", "away_entry__team", "home_entry__source_pool", "away_entry__source_pool",
        "home_entry__source_stage", "away_entry__source_stage"))


def is_playable(g):
    return not g.is_bye and g.status != Game.Status.CANCELLED


def _dependencies(games):
    """Game id -> set of game ids that must finish before it may start."""
    by_pool, by_stage, by_round = defaultdict(list), defaultdict(list), defaultdict(list)
    for g in games:
        by_pool[g.pool_id].append(g.id)
        by_stage[g.stage_id].append(g.id)
        if not g.bracket:
            by_round[(g.pool_id, g.round)].append(g.id)
    gf1 = {g.stage_id: g.id for g in games if g.bracket == "F" and g.round == 1}
    deps = {}
    for g in games:
        d = set()
        for side in ("home", "away"):
            prev = getattr(g, f"{side}_prev_id")
            if prev:
                d.add(prev)
            entry = getattr(g, f"{side}_entry")
            if entry is not None:
                if entry.source_pool_id:
                    d.update(by_pool[entry.source_pool_id])
                elif entry.source_stage_id:
                    d.update(by_stage[entry.source_stage_id])
        if g.stage.kind == "swiss" and g.round > 1:
            d.update(by_round[(g.pool_id, g.round - 1)])
        if g.if_necessary and g.stage_id in gf1:
            d.add(gf1[g.stage_id])
        d.discard(g.id)
        deps[g.id] = d
    return deps


def _team_keys(g):
    keys = set()
    for side in ("home", "away"):
        team_id = getattr(g, f"{side}_team_id")
        entry_id = getattr(g, f"{side}_entry_id")
        if team_id:
            keys.add(("t", team_id))
        elif entry_id:
            keys.add(("e", entry_id))
    return keys


def _windows(tournament):
    return [Window(datetime.combine(w.date, w.start_time), datetime.combine(w.date, w.end_time))
            for w in tournament.windows.all()]


def generate(tournament, keep_existing=True, user=None):
    """Run the automatic scheduler. Returns list of unscheduled games."""
    spaces = list(Space.objects.filter(venue__tournament=tournament).values_list("id", flat=True))
    windows = _windows(tournament)
    if not spaces:
        raise ServiceError(f"Add at least one {tournament.space_noun.lower()} first.")
    if not windows:
        raise ServiceError("Add at least one time window (day and hours) first.")
    games = tournament_games(tournament)
    deps = _dependencies(games)
    by_id = {g.id: g for g in games}

    depth_cache = {}

    def depth(gid):
        if gid not in depth_cache:
            depth_cache[gid] = 0
            depth_cache[gid] = 1 + max((depth(d) for d in deps[gid] if d in by_id), default=-1)
        return depth_cache[gid]

    sgames = []
    for g in games:
        if not is_playable(g):
            continue
        fixed = None
        started = g.has_result or g.status == Game.Status.IN_PROGRESS
        if g.start_at and g.space_id and (keep_existing or started):
            fixed = (g.space_id, g.start_at)
        sgames.append(SGame(
            id=g.id, duration=g.duration, teams=_team_keys(g),
            deps=[d for d in deps[g.id] if d in by_id and is_playable(by_id[d])],
            priority=(g.stage.order, depth(g.id), g.round, g.pool.order, g.stage.category.order, g.index, g.id),
            fixed=fixed,
        ))
    result = schedule(sgames, spaces, windows, tournament.interval, tournament.buffer_minutes,
                      tournament.min_rest_minutes)
    with transaction.atomic():
        for g in games:
            if g.id in result.assignments:
                space_id, start = result.assignments[g.id]
                g.space_id, g.start_at = space_id, start
            else:
                g.space, g.start_at = None, None
            g.save(update_fields=["space", "start_at"])
        renumber(tournament)
        touch(tournament)
        log(tournament, user, "schedule",
            f"Schedule generated: {len(result.assignments)} games placed, {len(result.unscheduled)} unscheduled")
    return [by_id[i] for i in result.unscheduled]


def clear_schedule(tournament, user=None):
    Game.objects.filter(stage__category__tournament=tournament, status=Game.Status.SCHEDULED).update(
        space=None, start_at=None)
    renumber(tournament)
    touch(tournament)
    log(tournament, user, "schedule", "Schedule cleared")


def move_game(game, space=None, start=None, *, unschedule=False, user=None):
    """Place a game on (space, start); swaps with a game already there (SPEC §11.3)."""
    t = game.stage.category.tournament
    with transaction.atomic():
        if unschedule:
            game.space, game.start_at = None, None
            game.save(update_fields=["space", "start_at"])
        else:
            if space is None or start is None:
                raise ServiceError("Pick a time and a space.")
            if space.venue.tournament_id != t.id:
                raise ServiceError("Unknown space.")
            other = Game.objects.filter(stage__category__tournament=t, space=space, start_at=start).exclude(
                pk=game.pk).first()
            if other:
                other.space, other.start_at = game.space, game.start_at
                other.save(update_fields=["space", "start_at"])
            game.space, game.start_at = space, start
            game.save(update_fields=["space", "start_at"])
        renumber(t)
        touch(t)


def find_conflicts(tournament, games=None):
    """game id -> [messages]."""
    games = games if games is not None else tournament_games(tournament)
    out = defaultdict(list)
    placed = [g for g in games if g.start_at and g.space_id and is_playable(g)]
    rest = timedelta(minutes=tournament.min_rest_minutes)

    by_space = defaultdict(list)
    for g in placed:
        by_space[g.space_id].append(g)
    for lst in by_space.values():
        lst.sort(key=lambda g: g.start_at)
        for a, b in zip(lst, lst[1:]):
            if b.start_at < a.end_at:
                out[a.id].append(f"Overlaps game #{b.number} on {b.space.name}")
                out[b.id].append(f"Overlaps game #{a.number} on {a.space.name}")

    by_team = defaultdict(list)
    names = {}
    for g in placed:
        for key in _team_keys(g):
            by_team[key].append(g)
        for side in ("home", "away"):
            if getattr(g, f"{side}_team_id"):
                names[("t", getattr(g, f"{side}_team_id"))] = getattr(g, f"{side}_team").name
    for key, lst in by_team.items():
        lst.sort(key=lambda g: g.start_at)
        name = names.get(key, "A slot")
        for a, b in zip(lst, lst[1:]):
            if b.start_at < a.end_at:
                out[b.id].append(f"{name} is also playing game #{a.number}")
            elif rest and b.start_at < a.end_at + rest:
                out[b.id].append(f"{name} has less than {tournament.min_rest_minutes} min rest after #{a.number}")

    deps = _dependencies(games)
    by_id = {g.id: g for g in games}
    for g in placed:
        for d in deps[g.id]:
            dg = by_id.get(d)
            if dg and is_playable(dg) and dg.start_at and dg.end_at > g.start_at:
                out[g.id].append(f"Starts before game #{dg.number} it depends on has finished")
                break

    windows = _windows(tournament)
    if windows:
        for g in placed:
            if not any(w.start <= g.start_at and g.end_at <= w.end for w in windows):
                out[g.id].append("Outside the available time windows")
    return dict(out)


# -- queue mode (SPEC §11.4) ----------------------------------------------------

def _busy_teams(tournament):
    busy = set()
    for g in Game.objects.filter(stage__category__tournament=tournament, status=Game.Status.IN_PROGRESS):
        busy.update([g.home_team_id, g.away_team_id])
    return busy


def ready_games(tournament):
    busy = _busy_teams(tournament)
    games = [g for g in tournament_games(tournament)
             if g.status == Game.Status.SCHEDULED and g.teams_known and not g.is_bye]
    gf1_done = {g.stage_id: g for g in tournament_games(tournament) if g.bracket == "F" and g.round == 1}
    out = []
    for g in sorted(games, key=lambda g: g.queue_order):
        if g.if_necessary:
            gf1 = gf1_done.get(g.stage_id)
            if not gf1 or not gf1.has_result or gf1.winner_side != "A":
                continue
        if g.home_team_id in busy or g.away_team_id in busy:
            continue
        out.append(g)
    return out


def start_game(game, space):
    t = game.stage.category.tournament
    if Game.objects.filter(stage__category__tournament=t, space=space, status=Game.Status.IN_PROGRESS).exists():
        raise ServiceError(f"{space.name} is busy.")
    if game.status != Game.Status.SCHEDULED or not game.teams_known:
        raise ServiceError("This game can't start yet.")
    game.status = Game.Status.IN_PROGRESS
    game.space = space
    game.started_at = timezone.now()
    game.save(update_fields=["status", "space", "started_at"])
    touch(t)


def start_next(tournament, space):
    if tournament.status in (Tournament.Status.DRAFT, Tournament.Status.COMPLETED):
        return None
    if Game.objects.filter(stage__category__tournament=tournament, space=space,
                           status=Game.Status.IN_PROGRESS).exists():
        return None
    ready = ready_games(tournament)
    if not ready:
        return None
    start_game(ready[0], space)
    return ready[0]


def stop_game(game):
    """Take a game off its court without a result (back to the queue)."""
    if game.status == Game.Status.IN_PROGRESS:
        game.status = Game.Status.SCHEDULED
        game.started_at = None
        game.space = None
        game.save(update_fields=["status", "started_at", "space"])
        touch(game.stage.category.tournament)
