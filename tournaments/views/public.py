"""Participant-facing tournament site (SPEC §13)."""
import segno
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ..models import Team
from ..presenters import category_view, games_by_day, public_games
from ..services.access import public_tournament


def _render(request, t, is_admin, template, active, **ctx):
    ctx.update({"t": t, "is_admin": is_admin, "active": active, "preview": is_admin and not t.is_live})
    return render(request, template, ctx)


def home(request, slug):
    t, is_admin = public_tournament(request, slug)
    cats = [category_view(c) for c in t.categories.all()]
    champions = [(cv["category"], cv["final"][0][1]) for cv in cats if cv["final"]]
    games = public_games(t)
    live = [g for g in games if g.status == "in_progress"]
    return _render(request, t, is_admin, "public/home.html", "home",
                   blocks=t.blocks.filter(enabled=True), cats=cats, champions=champions, live=live,
                   venues=t.venues.prefetch_related("spaces"),
                   team_count=Team.objects.filter(category__tournament=t).count(),
                   done=sum(1 for g in games if g.is_resolved), total=len(games))


def teams(request, slug):
    t, is_admin = public_tournament(request, slug)
    cats = [(c, c.teams.order_by("name")) for c in t.categories.all()]
    return _render(request, t, is_admin, "public/teams.html", "teams", cats=cats)


def team_detail(request, slug, team_id):
    t, is_admin = public_tournament(request, slug)
    team = get_object_or_404(Team, pk=team_id, category__tournament=t)
    games = [g for g in public_games(t) if team.id in (g.home_team_id, g.away_team_id)]
    record = {"w": 0, "l": 0, "d": 0}
    for g in games:
        if g.has_result:
            if g.winner_side == "D":
                record["d"] += 1
            elif (g.winner_side == "H") == (g.home_team_id == team.id):
                record["w"] += 1
            else:
                record["l"] += 1
    return _render(request, t, is_admin, "public/team.html", "teams", team=team, by_day=games_by_day(games),
                   record=record)


def schedule(request, slug):
    t, is_admin = public_tournament(request, slug)
    games = public_games(t)
    f = {k: request.GET.get(k, "") for k in ("cat", "team", "space")}
    if f["cat"]:
        games = [g for g in games if str(g.stage.category_id) == f["cat"]]
    if f["team"]:
        games = [g for g in games if f["team"] in (str(g.home_team_id), str(g.away_team_id))]
    if f["space"]:
        games = [g for g in games if str(g.space_id) == f["space"]]
    from ..models import Space
    return _render(request, t, is_admin, "public/schedule.html", "schedule", by_day=games_by_day(games), f=f,
                   cats=t.categories.all(), all_teams=Team.objects.filter(category__tournament=t).order_by("name"),
                   spaces=Space.objects.filter(venue__tournament=t).select_related("venue"))


def standings(request, slug):
    t, is_admin = public_tournament(request, slug)
    cats = [category_view(c) for c in t.categories.all()]
    return _render(request, t, is_admin, "public/standings.html", "standings", cats=cats)


def qr(request, slug):
    t, _ = public_tournament(request, slug)
    url = request.build_absolute_uri(reverse("public_home", args=[t.slug]))
    svg = segno.make(url, error="m").svg_inline(scale=8)
    return HttpResponse(svg, content_type="image/svg+xml")
