"""Formats, stages, pools, slots and game generation (SPEC §5, §6, §7)."""
import string

from django.db import transaction
from django.db.models import Max

from engine.bracket import double_elimination, single_elimination
from engine.roundrobin import round_robin_rounds

from ..models import Category, Entry, Game, Pool, Stage, Team
from .common import ServiceError
from .resolution import recompute_category

TEMPLATED = {"rr", "se", "de", "swiss", "rr_playoff"}


def ordered_teams(category):
    teams = list(category.teams.all())
    return sorted(teams, key=lambda t: (t.seed is None, t.seed or 0, t.order, t.id))


def pool_names(n):
    return [f"Pool {string.ascii_uppercase[i]}" if i < 26 else f"Pool {i + 1}" for i in range(n)]


def snake(teams, n_pools):
    buckets = [[] for _ in range(n_pools)]
    for i, t in enumerate(teams):
        r, j = divmod(i, n_pools)
        buckets[j if r % 2 == 0 else n_pools - 1 - j].append(t)
    return buckets


def _int(params, key, default, lo=None, hi=None):
    try:
        v = int(params.get(key, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def normalize_params(kind, params, n_teams):
    p = {}
    if kind in ("rr", "rr_playoff"):
        p["pools"] = _int(params, "pools", 1, 1, max(1, n_teams // 2))
        p["cycles"] = _int(params, "cycles", 1, 1, 2)
        gpt = params.get("games_per_team")
        p["games_per_team"] = _int(params, "games_per_team", 0, 0) or None if gpt not in (None, "") else None
    if kind in ("se", "rr_playoff"):
        p["placement"] = params.get("placement") if params.get("placement") in ("none", "third", "all") else "none"
    if kind == "de":
        p["grand_final_reset"] = bool(params.get("grand_final_reset", True))
    if kind == "swiss":
        p["rounds"] = _int(params, "rounds", 3, 1, max(1, n_teams - 1))
    if kind == "rr_playoff":
        min_pool = n_teams // p["pools"]
        p["advance_per_pool"] = _int(params, "advance_per_pool", 2, 1, max(1, min_pool))
        p["playoff_kind"] = "de" if params.get("playoff_kind") == "de" else "se"
        p["grand_final_reset"] = bool(params.get("grand_final_reset", False))
        p["consolation"] = bool(params.get("consolation", False))
    return p


def _stage(category, name, order, kind, config):
    return Stage.objects.create(category=category, name=name, order=order, kind=kind, config=config)


def _fill(pool, teams):
    for i, t in enumerate(teams, start=1):
        Entry.objects.create(pool=pool, position=i, team=t)


@transaction.atomic
def apply_format(category, kind, params=None):
    """(Re)generate the whole structure of a category from a format template."""
    params = params or {}
    teams = ordered_teams(category)
    if kind in TEMPLATED and len(teams) < 2:
        raise ServiceError("Add at least two teams before choosing a format.")
    p = normalize_params(kind, params, len(teams))
    category.stages.all().delete()
    category.format_kind = kind
    category.format_params = p

    if kind in ("rr", "rr_playoff"):
        n = p["pools"]
        rr = _stage(category, "Pool play" if n > 1 else "Round robin", 1, Stage.Kind.RR,
                    {"cycles": p["cycles"], "games_per_team": p["games_per_team"]})
        pools = [Pool.objects.create(stage=rr, name=name, order=i) for i, name in enumerate(pool_names(n))]
        for pool, members in zip(pools, snake(teams, n)):
            _fill(pool, members)
        if kind == "rr_playoff":
            k = p["advance_per_pool"]
            sizes = [len(m) for m in snake(teams, n)]
            po = _stage(category, "Playoffs", 2, p["playoff_kind"],
                        {"placement": p["placement"], "grand_final_reset": p["grand_final_reset"]})
            flights = [("Championship", range(1, k + 1))]
            if p["consolation"] and min(sizes) > k:
                flights.append(("Consolation", range(k + 1, min(2 * k, min(sizes)) + 1)))
            for fi, (fname, ranks) in enumerate(flights):
                flight = Pool.objects.create(stage=po, name=fname if len(flights) > 1 else "Bracket", order=fi)
                pos = 1
                for rank in ranks:
                    for pool in pools:
                        Entry.objects.create(pool=flight, position=pos, source_pool=pool, source_rank=rank)
                        pos += 1
    elif kind in ("se", "de"):
        s = _stage(category, "Bracket", 1, kind,
                   {"placement": p.get("placement", "none"), "grand_final_reset": p.get("grand_final_reset", True)})
        _fill(Pool.objects.create(stage=s, name="Bracket", order=0), teams)
    elif kind == "swiss":
        s = _stage(category, "Swiss", 1, Stage.Kind.SWISS, {"rounds": p["rounds"]})
        _fill(Pool.objects.create(stage=s, name="Swiss", order=0), teams)
    elif kind != "custom":
        raise ServiceError("Unknown format.")

    category.save()
    for stage in category.stages.all():
        build_games(stage, recompute=False)
    renumber(category.tournament)
    recompute_category(category)


def build_games(stage, recompute=True):
    """Delete and regenerate all games of a stage from its pools/slots/config."""
    stage.games.all().delete()
    cfg = stage.config or {}
    multi = stage.pools.count() > 1
    for pool in stage.pools.all():
        entries = list(pool.entries.order_by("position"))
        n = len(entries)
        common = dict(stage=stage, pool=pool, duration=stage.duration)
        if stage.kind == Stage.Kind.RR:
            counter = 0
            for r, pairs in enumerate(round_robin_rounds(n, cfg.get("cycles") or 1, cfg.get("games_per_team")), 1):
                for i, (h, a) in enumerate(pairs, 1):
                    counter += 1
                    Game.objects.create(**common, round=r, index=i, code=f"{pool.short}{counter}",
                                        label=f"{pool.name} · Round {r}",
                                        home_entry=entries[h], away_entry=entries[a])
        elif stage.kind == Stage.Kind.SWISS:
            for r in range(1, (cfg.get("rounds") or 3) + 1):
                for i in range(1, (n + 1) // 2 + 1):
                    Game.objects.create(**common, round=r, index=i, code=f"S{r}-{i}", label=f"Swiss round {r}")
        else:
            if stage.kind == Stage.Kind.SE:
                bgames = single_elimination(n, cfg.get("placement") or "none")
            else:
                bgames = double_elimination(n, bool(cfg.get("grand_final_reset", True)))
            made = {}
            prefix = f"{pool.short}-" if multi else ""
            for bg in bgames:
                g = Game(**common, bracket=bg.bracket, round=bg.round, index=bg.index,
                         code=f"{prefix}{bg.key}", label=(f"{pool.name} · " if multi else "") + bg.label,
                         place_winner=bg.place_winner, place_loser=bg.place_loser, if_necessary=bg.if_necessary)
                for side, src in (("home", bg.home), ("away", bg.away)):
                    if src[0] == "seed":
                        setattr(g, f"{side}_entry", entries[src[1] - 1])
                    elif src[0] in ("W", "L"):
                        setattr(g, f"{side}_prev", made[src[1]])
                        setattr(g, f"{side}_prev_outcome", src[0])
                g.save()
                made[bg.key] = g
    stage.advanced_at = None
    stage.save(update_fields=["advanced_at"])
    if recompute:
        renumber(stage.category.tournament)
        recompute_category(stage.category)


def renumber(tournament):
    """Assign Game #n by start time (scheduled first), then structural order (SPEC §11.2)."""
    games = list(Game.objects.filter(stage__category__tournament=tournament)
                 .select_related("stage", "stage__category", "pool"))

    def key(g):
        return (g.start_at is None, g.start_at or 0, g.stage.order, g.round if not g.bracket else 0,
                g.id if g.bracket else 0, g.stage.category.order, g.pool.order, g.index, g.id)

    for n, g in enumerate(sorted(games, key=key), start=1):
        if g.number != n or g.queue_order != n:
            g.number = g.queue_order = n
            g.save(update_fields=["number", "queue_order"])


# -- manual/custom structure --------------------------------------------------

@transaction.atomic
def add_stage(category, name, kind, pools, slots):
    order = (category.stages.aggregate(m=Max("order"))["m"] or 0) + 1
    defaults = {"rr": {"cycles": 1}, "se": {"placement": "none"}, "de": {"grand_final_reset": True},
                "swiss": {"rounds": 3}}
    stage = _stage(category, name, order, kind, defaults.get(kind, {}))
    names = pool_names(pools) if kind in ("rr", "swiss") else (
        ["Bracket"] if pools == 1 else [f"Flight {i + 1}" for i in range(pools)])
    for i, pname in enumerate(names):
        pool = Pool.objects.create(stage=stage, name=pname, order=i)
        for pos in range(1, slots + 1):
            Entry.objects.create(pool=pool, position=pos)
    if category.format_kind != "custom":
        category.format_kind = "custom"
        category.save(update_fields=["format_kind"])
    build_games(stage)
    return stage


@transaction.atomic
def delete_stage(stage):
    category = stage.category
    Entry.objects.filter(source_pool__stage=stage).update(source_pool=None, source_rank=None)
    Entry.objects.filter(source_stage=stage).update(source_stage=None, source_rank=None)
    stage.delete()
    for i, s in enumerate(category.stages.all(), start=1):
        if s.order != i:
            s.order = i
            s.save(update_fields=["order"])
    category.format_kind = "custom"
    category.save(update_fields=["format_kind"])
    recompute_category(category)


@transaction.atomic
def resize_pool(pool, slots):
    """Change the number of slots in a pool/flight and rebuild the stage's games."""
    entries = list(pool.entries.all())
    for e in entries[slots:]:
        e.delete()
    for pos in range(len(entries) + 1, slots + 1):
        Entry.objects.create(pool=pool, position=pos)
    _mark_custom(pool.stage.category)
    build_games(pool.stage)


@transaction.atomic
def add_pool(stage):
    n = stage.pools.count()
    name = pool_names(n + 1)[-1] if stage.kind in ("rr", "swiss") else f"Flight {n + 1}"
    Pool.objects.create(stage=stage, name=name, order=n)
    _mark_custom(stage.category)
    build_games(stage)


@transaction.atomic
def delete_pool(pool):
    stage = pool.stage
    if stage.pools.count() <= 1:
        raise ServiceError("A stage needs at least one pool.")
    pool.delete()
    _mark_custom(stage.category)
    build_games(stage)


def _mark_custom(category):
    if category.format_kind != "custom":
        category.format_kind = "custom"
        category.save(update_fields=["format_kind"])


def set_entry(entry, *, team=None, source=None, lock=None):
    """Assign a team (swapping if already placed in this stage) or a source to a slot.

    ``source`` is "pool:<id>:<rank>", "stage:<id>:<rank>" or "" (none).
    """
    stage = entry.pool.stage
    is_first = stage.order == min(s.order for s in stage.category.stages.all())
    with transaction.atomic():
        if source is not None:
            entry.source_pool = entry.source_stage = entry.source_rank = None
            if source:
                kind, pk, rank = source.split(":")
                target = Pool if kind == "pool" else Stage
                obj = target.objects.get(pk=pk)
                src_stage = obj.stage if kind == "pool" else obj
                if src_stage.category_id != stage.category_id or src_stage.order >= stage.order:
                    raise ServiceError("A slot can only be fed from an earlier stage of the same category.")
                setattr(entry, "source_pool" if kind == "pool" else "source_stage", obj)
                entry.source_rank = int(rank)
                if not entry.locked:
                    entry.team = None
        if team is not None:
            new_team = team or None
            if new_team and new_team.category_id != stage.category_id:
                raise ServiceError("That team belongs to another category.")
            if new_team:
                other = Entry.objects.filter(pool__stage=stage, team=new_team).exclude(pk=entry.pk).first()
                if other:
                    other.team = entry.team
                    other.save(update_fields=["team"])
            entry.team = new_team
            if not is_first:
                entry.locked = bool(new_team)
        if lock is not None and not is_first:
            entry.locked = lock
        entry.save()
        if is_first and stage.category.format_kind in TEMPLATED and team is not None:
            # Manual pool placement diverges from the template; keep it.
            _mark_custom(stage.category)


def teams_changed(category):
    """Regenerate a templated format after the team list changed (SPEC §5)."""
    if category.format_kind in TEMPLATED:
        if category.teams.count() < 2:
            category.stages.all().delete()
            category.format_kind = ""
            category.save(update_fields=["format_kind"])
        else:
            apply_format(category, category.format_kind, category.format_params)
    elif category.format_kind == "custom":
        recompute_category(category)


def category_has_results(category):
    return Game.objects.filter(stage__category=category,
                               status__in=[Game.Status.COMPLETED, Game.Status.FORFEIT]).exists()


def unassigned_teams(category):
    first = category.stages.first()
    if not first:
        return []
    placed = set(Entry.objects.filter(pool__stage=first).values_list("team_id", flat=True))
    return [t for t in category.teams.all() if t.id not in placed]


def bulk_add_teams(category, text):
    names, created = [], 0
    existing = set(category.teams.values_list("name", flat=True))
    order = (category.teams.aggregate(m=Max("order"))["m"] or 0)
    for line in text.splitlines():
        name = line.strip()[:80]
        if name and name not in existing and name not in names:
            names.append(name)
    for name in names:
        order += 1
        Team.objects.create(category=category, name=name, order=order)
        created += 1
    return created


def create_default_category(tournament):
    return Category.objects.create(tournament=tournament, name="Open", order=0)
