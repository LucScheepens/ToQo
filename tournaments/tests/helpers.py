from datetime import date, time

from django.contrib.auth.models import User

from tournaments.models import Game, Space, Team, TimeWindow, Tournament, Venue
from tournaments.services import results
from tournaments.services.structure import create_default_category


def make_tournament(n_teams=8, owner=None, **kw):
    owner = owner or User.objects.create_user("owner", "owner@example.com", "password123")
    t = Tournament.objects.create(name="Summer Cup", slug=kw.pop("slug", "summer-cup"),
                                  start_date=date(2026, 7, 12), end_date=date(2026, 7, 13), owner=owner, **kw)
    cat = create_default_category(t)
    for i in range(1, n_teams + 1):
        Team.objects.create(category=cat, name=f"Team {i}", seed=i, order=i)
    return t, cat


def add_venue(t, courts=2):
    v = Venue.objects.create(tournament=t, name="Sports Centre")
    for i in range(1, courts + 1):
        Space.objects.create(venue=v, name=f"Court {i}", order=i)
    TimeWindow.objects.create(tournament=t, date=date(2026, 7, 12), start_time=time(9), end_time=time(20))
    TimeWindow.objects.create(tournament=t, date=date(2026, 7, 13), start_time=time(9), end_time=time(20))


def playable(category):
    return [g for g in Game.objects.filter(stage__category=category).select_related("home_team", "away_team",
                                                                                     "stage", "stage__category",
                                                                                     "stage__category__tournament")
            .order_by("id")
            if g.teams_known and not g.is_bye and not g.is_resolved
            and not (g.if_necessary and g.status == Game.Status.CANCELLED)]


def play_all(category, favourite=lambda g: g.home_team.seed < g.away_team.seed, confirm=True):
    """Play every currently playable game: the better seed wins 2–1."""
    played = 0
    while True:
        batch = playable(category)
        if not batch:
            return played
        for g in batch:
            g.refresh_from_db()
            if g.is_resolved or not g.teams_known:
                continue
            home_wins = favourite(g)
            results.submit_result(g, 2 if home_wins else 1, 1 if home_wins else 2, confirm=confirm)
            played += 1
