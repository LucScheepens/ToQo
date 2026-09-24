"""Running the event (SPEC §10, §11, §9): schedule, games, standings, queue board."""
from collections import OrderedDict
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import urlencode

from ..forms import ResultForm, ScheduleSettingsForm, TimeWindowForm
from ..models import Game, Membership, Space, Stage, Team, TimeWindow, Tournament
from ..presenters import category_view, games_by_day
from ..services import results, scheduling, transitions
from ..services.access import manage
from ..services.common import ServiceError, touch
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


@manage(SCOREKEEPER)
def game_detail(request, t, game_id):
    game = get_object_or_404(Game.objects.select_related("stage", "stage__category", "pool", "space",
                                                         "home_team", "away_team"),
                             pk=game_id, stage__category__tournament=t)
    is_admin = request.role in ADMIN_ROLES
    nxt = safe_next(request, reverse("m_schedule", args=[t.slug]) + "?view=list")
    initial = {"home_score": game.home_score, "away_score": game.away_score,
               "winner_side": game.winner_side if game.home_score == game.away_score else ""}
    form = ResultForm(request.POST or None, initial=initial)
    affected, pending_action = None, None
    if request.method == "POST":
        action = request.POST.get("action")
        confirm = bool(request.POST.get("confirm"))
        admin_only = {"swap", "teams", "restore", "move", "cancel"}
        try:
            if action in admin_only and not is_admin:
                raise ServiceError("Only admins can do that.")
            if action == "result":
                if form.is_valid():
                    d = form.cleaned_data
                    affected = results.submit_result(game, d["home_score"], d["away_score"], d["winner_side"] or None,
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
    return page(request, t, "manage/game.html", "schedule", game=game, form=form, is_admin=is_admin,
                affected=affected, pending=pending_action, next=nxt,
                spaces=Space.objects.filter(venue__tournament=t).select_related("venue"),
                cat_teams=game.stage.category.teams.all(),
                is_elim=game.stage.kind in (Stage.Kind.SE, Stage.Kind.DE),
                conflicts=scheduling.find_conflicts(t).get(game.id, []))


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
            if sv["can_advance"] or sv["status"] == "transitioned":
                sv["preview"] = transitions.preview(sv["stage"])
        cats.append(cv)
    return page(request, t, "manage/standings.html", "standings", cats=cats, is_admin=is_admin,
                can_finish=t.status == Tournament.Status.IN_PROGRESS and transitions.all_complete(t))


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
    return page(request, t, "manage/queue.html", "queue", spaces=list(spaces.items()), ready=ready,
                waiting=waiting[:30], recent=recent, live=t.is_live)
