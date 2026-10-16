"""Running the event (SPEC §10, §11, §9): schedule, games, standings, queue board."""
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import urlencode

from ..forms import ScheduleSettingsForm, TimeWindowForm
from ..models import Game, Membership, Space, Stage, Team, TimeWindow, Tournament
from ..presenters import category_view, games_by_day
from ..services import results, scheduling, structure, transitions
from ..services.access import manage
from ..services.common import ServiceError, touch
from ..services.resolution import stage_status
from ..services.standings import pool_standings, stage_games
from ..templatetags.toqo import round_label
from .base import error, page, safe_next

SCOREKEEPER = Membership.Role.SCOREKEEPER
ADMIN_ROLES = (Membership.Role.ADMIN, Membership.Role.OWNER)


def _grid(t, games, day, spaces):
    """Rows of time slots × spaces for one day."""
    interval = timedelta(minutes=t.interval)
    windows = [w for w in t.windows.all() if w.date == day]
    times = set()
    for w in windows:
        cur = datetime.combine(day, w.start_time)
        end = datetime.combine(day, w.end_time)
        while cur < end:
            times.add(cur)
            cur += interval
    cells = {}
    for g in games:
        if g.start_at and g.start_at.date() == day and g.space_id:
            times.add(g.start_at)
            cells.setdefault((g.start_at, g.space_id), []).append(g)
    rows = []
    for tm in sorted(times):
        rows.append({"time": tm, "cells": [{"space": s, "games": cells.get((tm, s.id), [])} for s in spaces]})
    return rows


@manage(SCOREKEEPER)
def schedule(request, t):
    is_admin = request.role in ADMIN_ROLES
    settings_form = ScheduleSettingsForm(instance=t)
    window_form = TimeWindowForm(initial={"date": t.start_date, "start_time": "09:00", "end_time": "18:00"})
    if request.method == "POST" and is_admin:
        action = request.POST.get("action")
        try:
            if action == "settings":
                settings_form = ScheduleSettingsForm(request.POST, instance=t)
                if settings_form.is_valid():
                    settings_form.save()
                    if "schedule_mode" in settings_form.changed_data:
                        structure.renumber(t)
                    Game.objects.filter(stage__category__tournament=t, stage__game_minutes__isnull=True).update(
                        duration=t.default_game_minutes)
                    touch(t)
                    messages.success(request, "Scheduling settings saved.")
                    return redirect("m_schedule", t.slug)
            elif action == "add_window":
                window_form = TimeWindowForm(request.POST)
                if window_form.is_valid():
                    w = window_form.save(commit=False)
                    w.tournament = t
                    w.save()
                    return redirect("m_schedule", t.slug)
            elif action == "delete_window":
                TimeWindow.objects.filter(tournament=t, pk=request.POST.get("id")).delete()
                return redirect("m_schedule", t.slug)
            elif action == "generate":
                unscheduled = scheduling.generate(t, keep_existing=bool(request.POST.get("keep")), user=request.user)
                if unscheduled:
                    messages.warning(request, f"{len(unscheduled)} game(s) didn't fit. Add time windows or "
                                              f"{t.space_noun.lower()}s, shorten games, or reduce rest time.")
                else:
                    messages.success(request, "Schedule generated. Drag games to adjust.")
                return redirect("m_schedule", t.slug)
            elif action == "clear":
                scheduling.clear_schedule(t, request.user)
                return redirect("m_schedule", t.slug)
        except ServiceError as exc:
            error(request, exc)
            return redirect("m_schedule", t.slug)

    games = scheduling.tournament_games(t)
    shown = [g for g in games if not g.is_bye and not (g.if_necessary and g.status == Game.Status.CANCELLED)]
    conflicts = scheduling.find_conflicts(t, games) if t.schedule_mode == Tournament.ScheduleMode.TIMED else {}
    spaces = list(Space.objects.filter(venue__tournament=t).select_related("venue"))
    view = request.GET.get("view") or ("grid" if is_admin and t.schedule_mode == "timed" and spaces else "list")

    # filters (list view)
    cats = list(t.categories.all())
    f = {k: request.GET.get(k, "") for k in ("cat", "team", "space", "status", "conflicts")}
    listed = shown
    if f["cat"]:
        listed = [g for g in listed if str(g.stage.category_id) == f["cat"]]
    if f["team"]:
        listed = [g for g in listed if f["team"] in (str(g.home_team_id), str(g.away_team_id))]
    if f["space"]:
        listed = [g for g in listed if str(g.space_id) == f["space"]]
    if f["status"] == "open":
        listed = [g for g in listed if not g.is_resolved]
    elif f["status"] == "done":
        listed = [g for g in listed if g.is_resolved]
    if f["conflicts"]:
        listed = [g for g in listed if g.id in conflicts]

    days = sorted({w.date for w in t.windows.all()} | {g.start_at.date() for g in shown if g.start_at})
    day = None
    if days:
        try:
            day = date.fromisoformat(request.GET.get("day", ""))
        except ValueError:
            day = None
        if day not in days:
            day = days[0]
    grid = _grid(t, shown, day, spaces) if view == "grid" and day else []
    tray = [g for g in shown if not g.start_at and not g.is_resolved]
    return page(request, t, "manage/schedule.html", "schedule", settings_form=settings_form,
                window_form=window_form, windows=t.windows.all(), view=view, grid=grid, days=days, day=day,
                spaces=spaces, tray=tray, conflicts=conflicts, by_day=games_by_day(listed), cats=cats,
                all_teams=Team.objects.filter(category__tournament=t).order_by("name"), f=f,
                total=len(shown), scheduled=sum(1 for g in shown if g.start_at))


def _scores(values):
    """Score inputs → ints (None when empty)."""
    out = []
    for v in values:
        v = (v or "").strip()
        if v and not v.isdigit():
            raise ServiceError("Scores must be whole numbers.")
        out.append(int(v) if v else None)
    return out


@manage(SCOREKEEPER)
def game_detail(request, t, game_id):
    game = get_object_or_404(Game.objects.select_related("stage", "stage__category", "pool", "space",
                                                         "home_team", "away_team"),
                             pk=game_id, stage__category__tournament=t)
    is_admin = request.role in ADMIN_ROLES
    nxt = safe_next(request, reverse("m_schedule", args=[t.slug]) + "?view=list")
    score_rows = game.sets or [[game.home_score, game.away_score]]
    if request.method == "POST" and request.POST.get("action") == "result":
        score_rows = list(zip(request.POST.getlist("home_score"), request.POST.getlist("away_score"))) or score_rows
    tied_winner = game.winner_side if game.home_score is not None and game.home_score == game.away_score else ""
    affected, pending_action = None, None
    if request.method == "POST":
        action = request.POST.get("action")
        confirm = bool(request.POST.get("confirm"))
        admin_only = {"swap", "teams", "restore", "move", "cancel"}
        try:
            if action in admin_only and not is_admin:
                raise ServiceError("Only admins can do that.")
            if action == "result":
                games = list(zip(_scores(request.POST.getlist("home_score")),
                                 _scores(request.POST.getlist("away_score"))))
                affected = results.submit_games(game, games, request.POST.get("winner_side") or None,
                                                user=request.user, confirm=confirm)
            elif action == "forfeit":
                affected = results.forfeit(game, request.POST.get("side"), user=request.user, confirm=confirm)
            elif action == "cancel":
                affected = results.cancel(game, user=request.user, confirm=confirm)
            elif action == "clear":
                affected = results.clear_result(game, user=request.user, confirm=confirm)
            elif action == "note":
                results.set_note(game, request.POST.get("note"), user=request.user)
                affected = []
            elif action == "swap":
                results.swap_home(game, user=request.user)
                affected = []
            elif action == "teams":
                cat = game.stage.category
                home = get_object_or_404(Team, pk=request.POST.get("home"), category=cat)
                away = get_object_or_404(Team, pk=request.POST.get("away"), category=cat)
                results.change_teams(game, home, away, user=request.user)
                affected = []
            elif action == "restore":
                results.restore_teams(game, user=request.user)
                affected = []
            elif action == "move":
                if request.POST.get("unschedule"):
                    scheduling.move_game(game, unschedule=True, user=request.user)
                else:
                    space = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
                    start = datetime.fromisoformat(f"{request.POST.get('date')}T{request.POST.get('time')}")
                    scheduling.move_game(game, space, start, user=request.user)
                affected = []
            elif action == "start":
                space = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
                scheduling.start_game(game, space)
                affected = []
            elif action == "stop":
                scheduling.stop_game(game)
                affected = []
        except (ServiceError, ValueError) as exc:
            error(request, exc)
            return redirect(request.path + "?" + urlencode({"next": nxt}))
        if affected == []:
            messages.success(request, "Saved.")
            return redirect(nxt)
        if affected:
            pending_action = request.POST
            game.refresh_from_db()
    return page(request, t, "manage/game.html", "schedule", game=game, score_rows=score_rows,
                tied_winner=request.POST.get("winner_side", tied_winner), is_admin=is_admin,
                affected=affected, pending=pending_action, next=nxt,
                spaces=Space.objects.filter(venue__tournament=t).select_related("venue"),
                cat_teams=game.stage.category.teams.all(),
                is_elim=game.stage.kind in (Stage.Kind.SE, Stage.Kind.DE),
                conflicts=scheduling.find_conflicts(t).get(game.id, [])
                if t.schedule_mode == Tournament.ScheduleMode.TIMED else [])


@manage(SCOREKEEPER)
def standings(request, t):
    is_admin = request.role in ADMIN_ROLES
    if request.method == "POST" and is_admin:
        action = request.POST.get("action")
        try:
            if action == "advance":
                stage = get_object_or_404(Stage, pk=request.POST.get("stage"), category__tournament=t)
                transitions.advance(stage, request.user)
                messages.success(request, f"Teams advanced from {stage.name}.")
            elif action == "undo_advance":
                stage = get_object_or_404(Stage, pk=request.POST.get("stage"), category__tournament=t)
                affected = transitions.undo_advance(stage, request.user)
                if affected:
                    messages.error(request, "The next stage already has results. Clear them first: " +
                                   ", ".join(f"#{a['number']}" for a in affected))
            elif action == "finish":
                transitions.finish(t, request.user)
                messages.success(request, "Tournament finished. Congratulations to the winners!")
            elif action == "reopen":
                transitions.reopen(t, request.user)
        except ServiceError as exc:
            error(request, exc)
        return redirect("m_standings", t.slug)
    cats = []
    for c in t.categories.all():
        cv = category_view(c)
        for sv in cv["stages"]:
            sv["can_advance"] = transitions.can_advance(sv["stage"])
        cats.append(cv)
    return page(request, t, "manage/standings.html", "standings", cats=cats, is_admin=is_admin,
                can_finish=t.status == Tournament.Status.IN_PROGRESS and transitions.all_complete(t))


@manage()
def advance_review(request, t, stage_id):
    """Final check before filling the next stage from this stage's standings (SPEC §9)."""
    stage = get_object_or_404(Stage, pk=stage_id, category__tournament=t)
    if request.method == "POST":
        try:
            if not request.POST.get("checked"):
                raise ServiceError("Tick the box to confirm you've checked the seeding.")
            transitions.advance(stage, request.user)
            nxt = stage.category.stages.filter(order__gt=stage.order).first()
            messages.success(request, f"{nxt.name if nxt else 'Next stage'} filled from {stage.name}. "
                                      "The first-round games are ready to play.")
            if t.schedule_mode == Tournament.ScheduleMode.QUEUE:
                return redirect("m_queue", t.slug)
            return redirect(reverse("m_standings", args=[t.slug]) + (f"#stage-{nxt.id}" if nxt else ""))
        except ServiceError as exc:
            error(request, exc)
            return redirect("m_advance", t.slug, stage.id)
    if stage.advanced_at:
        messages.info(request, f"Players have already advanced from {stage.name}.")
        return redirect("m_standings", t.slug)

    games = stage_games(stage)
    records = {}
    for pool in stage.pools.all():
        for row in pool_standings(pool, [g for g in games if g.pool_id == pool.id]):
            records[row.team.id] = row
    flights, by_entry, ties, manual = OrderedDict(), {}, [], []
    for e, team, tie in transitions.preview(stage):
        if e.locked:
            manual.append(e)
        who = e.team if e.locked else team
        seed = {"entry": e, "team": who, "tie": tie, "row": records.get(who.id) if who else None}
        by_entry[e.id] = seed
        flights.setdefault(e.pool_id, {"pool": e.pool, "seeds": [], "matches": []})["seeds"].append(seed)
        if tie and who:
            ties.append(seed)
    first_round = Game.objects.filter(pool_id__in=list(flights), round=1).exclude(bracket__in=["L", "F", "P"])
    for g in first_round.order_by("pool__order", "index"):
        if g.home_entry_id or g.away_entry_id:
            flights[g.pool_id]["matches"].append((by_entry.get(g.home_entry_id), by_entry.get(g.away_entry_id)))
    can = transitions.can_advance(stage)
    return page(request, t, "manage/advance.html", "standings", stage=stage, flights=list(flights.values()),
                ties=ties, manual=manual, can=can, status=stage_status(stage, games),
                next_stage=stage.category.stages.filter(order__gt=stage.order).first())


@manage(SCOREKEEPER)
def queue(request, t):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "start_next":
                space = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
                if not scheduling.start_next(t, space):
                    messages.info(request, "No game is ready to start.")
            elif action == "fill_all":
                for space in Space.objects.filter(venue__tournament=t):
                    scheduling.start_next(t, space)
            elif action == "start":
                game = get_object_or_404(Game, pk=request.POST.get("game"), stage__category__tournament=t)
                space = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
                scheduling.start_game(game, space)
            elif action == "stop":
                game = get_object_or_404(Game, pk=request.POST.get("game"), stage__category__tournament=t)
                scheduling.stop_game(game)
            elif action == "auto_queue":
                t.auto_queue = request.POST.get("on") == "1"
                t.save(update_fields=["auto_queue"])
                touch(t)
                messages.success(request, "Automatic queueing is " + ("on." if t.auto_queue else "off."))
        except ServiceError as exc:
            error(request, exc)
        return redirect("m_queue", t.slug)
    games = scheduling.tournament_games(t)
    spaces = OrderedDict((s, None) for s in Space.objects.filter(venue__tournament=t).select_related("venue"))
    for g in games:
        if g.status == Game.Status.IN_PROGRESS and g.space in spaces:
            spaces[g.space] = g
    ready = scheduling.ready_games(t)
    waiting = [g for g in sorted(games, key=lambda g: g.queue_order)
               if g.status == Game.Status.SCHEDULED and not g.is_bye and g not in ready
               and not (g.if_necessary and g.status == Game.Status.CANCELLED)]
    recent = sorted([g for g in games if g.has_result], key=lambda g: g.finished_at or datetime.min,
                    reverse=True)[:8]
    waiting_groups = OrderedDict()
    for g in waiting:
        name = re.sub(r"\s+#?\d+$", "", round_label(g)) if g.bracket else round_label(g)
        group = waiting_groups.setdefault((g.pool_id, g.bracket, g.round),
                                          {"pool": g.pool, "name": name, "games": []})
        group["games"].append(g)
    legend = OrderedDict()
    for g in ready + waiting:
        entry = legend.setdefault(g.pool_id, {"pool": g.pool, "ready": 0})
        entry["ready"] += g in ready
    free = sum(1 for g in spaces.values() if g is None)
    advanceable = [s for s in Stage.objects.filter(category__tournament=t, advanced_at__isnull=True)
                   if transitions.can_advance(s)]
    return page(request, t, "manage/queue.html", "queue", spaces=list(spaces.items()), ready=ready,
                waiting_groups=list(waiting_groups.values()), recent=recent[:6], live=t.is_live,
                legend=list(legend.values()), free=free, advanceable=advanceable,
                is_admin=request.role in ADMIN_ROLES)
