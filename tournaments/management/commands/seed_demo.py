"""Create a demo tournament that is mid-event: `python manage.py seed_demo`."""
import random
from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from tournaments.models import Category, Game, HomeBlock, Space, Team, TimeWindow, Tournament, Venue
from tournaments.services import publishing, results, scheduling, structure, transitions

MEN = ["Toronto Titans", "Ottawa Eagles", "Montreal Storm", "Vancouver Wolves", "Calgary Bears", "Edmonton Knights",
       "Winnipeg Jets", "Halifax Waves", "Quebec Nordiques", "Regina Riders", "Victoria Orcas", "Hamilton Hammers"]
WOMEN = ["Lakeside Lynx", "North Shore Falcons", "Harbour Herons", "Summit Spikers", "Valley Vipers", "Coastal Comets"]


class Command(BaseCommand):
    help = "Create a demo tournament (user: demo / demo-pass-123)."

    def add_arguments(self, parser):
        parser.add_argument("--finish", action="store_true", help="Play the whole tournament to the end.")

    def handle(self, *args, finish=False, **opts):
        rng = random.Random(7)
        user, created = User.objects.get_or_create(username="demo", defaults={"email": "demo@example.com"})
        if created:
            user.set_password("demo-pass-123")
            user.save()
        Tournament.objects.filter(slug="summer-volleyball-cup").delete()
        start = date.today()
        t = Tournament.objects.create(
            name="Summer Volleyball Cup", slug="summer-volleyball-cup", sport="volleyball", start_date=start,
            end_date=start + timedelta(days=1), location="Harbourfront Sports Centre", owner=user,
            tagline="Two days, two divisions, one trophy.", default_game_minutes=45, buffer_minutes=5,
            min_rest_minutes=10, forfeit_win_score=25)
        men = Category.objects.create(tournament=t, name="Men's Competitive", order=0)
        women = Category.objects.create(tournament=t, name="Women's Competitive", order=1)
        for cat, names in ((men, MEN), (women, WOMEN)):
            for i, n in enumerate(names, 1):
                Team.objects.create(category=cat, name=n, seed=i, order=i)
        structure.apply_format(men, "rr_playoff", {"pools": 2, "advance_per_pool": 4, "placement": "third",
                                                  "consolation": False})
        structure.apply_format(women, "de", {"grand_final_reset": True})

        v = Venue.objects.create(tournament=t, name="Harbourfront Sports Centre", address="100 Queens Quay W")
        for i in range(1, 5):
            Space.objects.create(venue=v, name=f"Court {i}", order=i)
        for d in (start, start + timedelta(days=1)):
            TimeWindow.objects.create(tournament=t, date=d, start_time=time(8, 30), end_time=time(19, 0))
        HomeBlock.objects.create(tournament=t, kind="text", title="Welcome", order=0,
                                 body="Welcome to the Summer Volleyball Cup!\nPool play on day one, playoffs on day two.")
        HomeBlock.objects.create(tournament=t, kind="info", title="Important information", order=1,
                                 body="Players should arrive 30 minutes before their first scheduled game.\n"
                                      "Games are best of 1 set to 25; ties are not possible.")
        HomeBlock.objects.create(tournament=t, kind="venues", title="Venue", order=2)
        HomeBlock.objects.create(tournament=t, kind="sponsors", title="Sponsors", order=3,
                                 data=[{"label": "Lakeshore Physio", "url": "https://example.com"},
                                       {"label": "Harbour Coffee", "url": "https://example.com"}])
        scheduling.generate(t)
        publishing.publish(t)

        def play(cat, limit=None):
            n = 0
            while limit is None or n < limit:
                games = [g for g in Game.objects.filter(stage__category=cat).order_by("number")
                         if g.teams_known and not g.is_bye and not g.is_resolved
                         and not (g.if_necessary and g.status == Game.Status.CANCELLED)]
                if not games:
                    break
                g = games[0]
                fav = g.home_team.seed < g.away_team.seed
                upset = rng.random() < 0.25
                lo = rng.randint(12, 23)
                home_wins = fav != upset
                results.submit_result(g, 25 if home_wins else lo, lo if home_wins else 25, confirm=True)
                n += 1

        play(men)
        transitions.advance(men.stages.first())
        play(women, limit=6)
        if finish:
            play(men)
            play(women)
            transitions.finish(t)
        self.stdout.write(self.style.SUCCESS(
            f"Demo ready: log in as demo / demo-pass-123 and open /manage/{t.slug}/ (public: /t/{t.slug}/)"))
