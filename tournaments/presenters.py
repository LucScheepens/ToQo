"""View models shared by organizer and public pages."""
import re
from collections import OrderedDict, defaultdict

from .models import Game
from .services.resolution import stage_status
from .services.standings import category_final_standings, pool_standings, stage_games

SECTION = {"W": "Winners bracket", "L": "Losers bracket", "F": "Grand final", "P": "Placement games"}


def _column_title(label):
    label = label.split(" · ")[-1]
    return re.sub(r"\s+(#?\d+)$", "", re.sub(r",? round \d+ #\d+$", "", label))


def bracket_sections(games, kind):
    sections = OrderedDict()
    for g in games:
        name = SECTION.get(g.bracket, "Bracket")
        if kind == "se" and g.bracket == "W":
            name = "Bracket"
        cols = sections.setdefault(name, OrderedDict())
        key = (g.round,) if g.bracket != "P" else (g.code.split("-")[0], g.round)
        cols.setdefault(key, []).append(g)
    out = []
    for name, cols in sections.items():
        columns = [{"title": _column_title(gs[0].label), "games": gs} for gs in cols.values()]
        out.append({"name": name, "columns": columns, "is_list": name == "Placement games"})
    return out


def stage_view(stage):
    games = stage_games(stage)
    pools = []
    for pool in stage.pools.all():
        pg = [g for g in games if g.pool_id == pool.id]
        entry = {"pool": pool, "rows": pool_standings(pool, pg), "games": pg}
        if stage.is_bracket:
            entry["sections"] = bracket_sections(pg, stage.kind)
        else:
            rounds = defaultdict(list)
            for g in pg:
                rounds[g.round].append(g)
            entry["rounds"] = sorted(rounds.items())
        pools.append(entry)
    done = sum(1 for g in games if g.is_resolved and not g.is_bye)
    total = sum(1 for g in games if not g.is_bye)
    return {"stage": stage, "status": stage_status(stage, games), "pools": pools, "done": done, "total": total,
            "is_swiss": stage.kind == "swiss"}


def category_view(category):
    stages = [stage_view(s) for s in category.stages.all()]
    current = next((s for s in stages if s["status"] not in ("complete", "transitioned")), None)
    finished = bool(stages) and current is None
    return {
        "category": category,
        "stages": stages,
        "current": current,
        "finished": finished,
        "final": category_final_standings(category) if finished else [],
    }


def games_by_day(games):
    days = OrderedDict()
    for g in sorted(games, key=lambda g: (g.start_at is None, g.start_at or 0, g.space.order if g.space else 0,
                                          g.number)):
        key = g.start_at.date() if g.start_at else None
        days.setdefault(key, []).append(g)
    return days


def public_games(tournament):
    return [g for g in Game.objects.filter(stage__category__tournament=tournament).select_related(
        "stage", "stage__category", "pool", "space", "space__venue", "home_team", "away_team",
        "home_entry__team", "away_entry__team", "home_entry__source_pool", "away_entry__source_pool",
        "home_entry__source_stage", "away_entry__source_stage", "home_prev", "away_prev")
        if not g.is_bye and not (g.if_necessary and g.status == Game.Status.CANCELLED)]
