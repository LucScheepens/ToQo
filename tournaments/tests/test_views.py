import io
import json

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from tournaments.models import Entry, Game, Membership, Space, Stage, Tournament


class DemoPagesTests(TestCase):
    """Every organizer and public page renders for a mid-event tournament."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=io.StringIO())
        cls.t = Tournament.objects.get(slug="summer-volleyball-cup")

    def setUp(self):
        self.client.login(username="demo", password="demo-pass-123")

    def test_organizer_pages(self):
        t = self.t
        cat = t.categories.first()
        stage = Stage.objects.filter(category__tournament=t).last()
        game = Game.objects.filter(stage__category__tournament=t).first()
        team = cat.teams.first()
        urls = [
            reverse("home"), reverse("m_new"),
            reverse("m_dashboard", args=[t.slug]), reverse("m_settings", args=[t.slug]),
            reverse("m_categories", args=[t.slug]), reverse("m_teams", args=[t.slug]),
            reverse("m_team_edit", args=[t.slug, team.id]), reverse("m_format", args=[t.slug]),
            reverse("m_format_category", args=[t.slug, cat.id]), reverse("m_add_stage", args=[t.slug, cat.id]),
            reverse("m_stage", args=[t.slug, stage.id]),
            reverse("m_stage", args=[t.slug, cat.stages.last().id]),
            reverse("m_venues", args=[t.slug]),
            reverse("m_schedule", args=[t.slug]), reverse("m_schedule", args=[t.slug]) + "?view=list",
            reverse("m_schedule", args=[t.slug]) + "?view=setup",
            reverse("m_game", args=[t.slug, game.id]), reverse("m_standings", args=[t.slug]),
            reverse("m_queue", args=[t.slug]), reverse("m_home", args=[t.slug]),
            reverse("m_admins", args=[t.slug]), reverse("m_review", args=[t.slug]),
            reverse("m_share", args=[t.slug]), reverse("m_activity", args=[t.slug]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_public_pages_anonymous(self):
        self.client.logout()
        t = self.t
        team = t.categories.first().teams.first()
        for url in [reverse("public_home", args=[t.slug]), reverse("public_teams", args=[t.slug]),
                    reverse("public_team", args=[t.slug, team.id]), reverse("public_schedule", args=[t.slug]),
                    reverse("public_standings", args=[t.slug]), reverse("public_qr", args=[t.slug]),
                    reverse("api_tournament", args=[t.slug]), reverse("api_version", args=[t.slug]),
                    reverse("api_schedule", args=[t.slug]), reverse("api_standings", args=[t.slug])]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_manage_requires_membership(self):
        User.objects.create_user("stranger", "s@example.com", "password123")
        self.client.login(username="stranger", password="password123")
        self.assertEqual(self.client.get(reverse("m_dashboard", args=[self.t.slug])).status_code, 404)

    def test_scorekeeper_limits(self):
        sk = User.objects.create_user("sk", "sk@example.com", "password123")
        Membership.objects.create(tournament=self.t, user=sk, role=Membership.Role.SCOREKEEPER)
        self.client.login(username="sk", password="password123")
        self.assertEqual(self.client.get(reverse("m_schedule", args=[self.t.slug])).status_code, 200)
        self.assertEqual(self.client.get(reverse("m_teams", args=[self.t.slug])).status_code, 404)
        game = Game.objects.filter(stage__category__tournament=self.t, status="scheduled").exclude(
            home_team=None).exclude(away_team=None).first()
        res = self.client.post(reverse("api_move", args=[self.t.slug, game.id]),
                               data=json.dumps({"unschedule": True}), content_type="application/json")
        self.assertEqual(res.status_code, 403)

    def test_api_result_and_conflict(self):
        game = next(g for g in Game.objects.filter(stage__category__tournament=self.t, status="scheduled")
                    if g.teams_known and not g.is_bye)
        url = reverse("api_result", args=[self.t.slug, game.id])
        res = self.client.post(url, data=json.dumps({"home_score": 25, "away_score": 20}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)
        game.refresh_from_db()
        self.assertEqual(game.status, "completed")
        bad = self.client.post(url, data=json.dumps({"home_score": "x"}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)


class OrganizerFlowTests(TestCase):
    """The full create → publish → run flow through the HTML forms."""

    def test_flow(self):
        c = self.client
        c.post(reverse("signup"), {"username": "org", "email": "org@example.com",
                                   "password1": "S3cure-pass!", "password2": "S3cure-pass!"})
        res = c.post(reverse("m_new"), {"name": "Spring Cup", "sport": "soccer", "start_date": "2026-05-01",
                                        "end_date": "2026-05-01", "visibility": "public"})
        t = Tournament.objects.get(name="Spring Cup")
        self.assertRedirects(res, reverse("m_dashboard", args=[t.slug]))
        cat = t.categories.get()
        c.post(reverse("m_teams", args=[t.slug]) + f"?cat={cat.id}",
               {"action": "bulk", "names": "\n".join(f"Club {i}" for i in range(1, 7))})
        self.assertEqual(cat.teams.count(), 6)
        c.post(reverse("m_format_category", args=[t.slug, cat.id]),
               {"kind": "rr_playoff", "pools": 2, "cycles": 1, "advance_per_pool": 2, "playoff_kind": "se",
                "placement": "third"})
        cat.refresh_from_db()
        self.assertEqual(cat.format_kind, "rr_playoff")
        self.assertEqual(cat.stages.count(), 2)
        c.post(reverse("m_venues", args=[t.slug]), {"action": "add_venue", "name": "Park", "spaces": 2})
        self.assertEqual(Space.objects.filter(venue__tournament=t).count(), 2)
        self.assertEqual(Space.objects.filter(venue__tournament=t).first().name, "Field 1")
        c.post(reverse("m_schedule", args=[t.slug]), {"action": "add_window", "date": "2026-05-01",
                                                      "start_time": "09:00", "end_time": "18:00"})
        c.post(reverse("m_schedule", args=[t.slug]), {"action": "generate", "keep": "1"})
        self.assertFalse(Game.objects.filter(stage__category=cat, start_at=None, home_bye=False,
                                             away_bye=False).exists())
        res = c.post(reverse("m_review", args=[t.slug]), {"action": "publish"})
        t.refresh_from_db()
        self.assertEqual(t.status, "published")

        for g in Game.objects.filter(stage__order=1, stage__category=cat):
            res = c.post(reverse("m_game", args=[t.slug, g.id]), {"action": "result", "home_score": 3,
                                                                   "away_score": 1})
            self.assertEqual(res.status_code, 302)
        pool_stage = cat.stages.first()
        c.post(reverse("m_standings", args=[t.slug]), {"action": "advance", "stage": pool_stage.id})
        pool_stage.refresh_from_db()
        self.assertIsNotNone(pool_stage.advanced_at)
        sf = Game.objects.get(stage__category=cat, code="SF1")
        self.assertTrue(sf.teams_known)

        # Tied elimination game without a winner is rejected with a message, not a crash.
        res = c.post(reverse("m_game", args=[t.slug, sf.id]), {"action": "result", "home_score": 1,
                                                                "away_score": 1}, follow=True)
        self.assertContains(res, "pick the winner")
        c.post(reverse("m_game", args=[t.slug, sf.id]), {"action": "result", "home_score": 1, "away_score": 1,
                                                          "winner_side": "A"})
        sf.refresh_from_db()
        self.assertEqual(sf.winner_side, "A")

        # Editing a pool result that changes qualification shows a confirmation page.
        pool_game = Game.objects.filter(stage=pool_stage, home_team=sf.home_team).first() or \
            Game.objects.filter(stage=pool_stage, away_team=sf.home_team).first()
        flip = {"home_score": 0, "away_score": 9} if pool_game.home_team_id == sf.home_team_id else \
            {"home_score": 9, "away_score": 0}
        res = c.post(reverse("m_game", args=[t.slug, pool_game.id]), {"action": "result", **flip})
        if res.status_code == 200:
            self.assertContains(res, "affects results that were already entered")
            res = c.post(reverse("m_game", args=[t.slug, pool_game.id]), {"action": "result", "confirm": "1", **flip})
        self.assertEqual(res.status_code, 302)


class QueueBoardTests(TestCase):
    def test_drag_to_court_and_back(self):
        from tournaments.models import Team
        from tournaments.services import publishing, structure

        from .helpers import add_venue, make_tournament
        t, cat = make_tournament(12, schedule_mode=Tournament.ScheduleMode.QUEUE)
        Team.objects.filter(category=cat, seed__gt=6).update(group=2)
        structure.apply_format(cat, "swiss_tiers", {"rounds": 3})
        add_venue(t, courts=2)
        publishing.publish(t)
        self.client.login(username="owner", password="password123")
        url = reverse("m_queue", args=[t.slug])
        page = self.client.get(url)
        self.assertContains(page, "data-court=")
        self.assertContains(page, "tone-4")  # upper poule
        self.assertContains(page, "tone-5")  # lower poule
        c1, c2 = Space.objects.all()
        g1, g2 = Game.objects.filter(stage__category=cat, round=1, pool__order=0)[:2]
        self.client.post(url, {"action": "start", "game": g1.id, "space": c1.id})
        g1.refresh_from_db()
        self.assertEqual((g1.status, g1.space_id), (Game.Status.IN_PROGRESS, c1.id))
        # Court 1 is busy now.
        self.client.post(url, {"action": "start", "game": g2.id, "space": c1.id})
        g2.refresh_from_db()
        self.assertEqual(g2.status, Game.Status.SCHEDULED)
        self.client.post(url, {"action": "stop", "game": g1.id})
        g1.refresh_from_db()
        self.assertEqual((g1.status, g1.space_id), (Game.Status.SCHEDULED, None))


class AdvanceReviewTests(TestCase):
    def test_review_then_fill_brackets(self):
        from tournaments.models import Team
        from tournaments.services import publishing, structure

        from .helpers import make_tournament, play_all
        t, cat = make_tournament(12, schedule_mode=Tournament.ScheduleMode.QUEUE)
        Team.objects.filter(category=cat, seed__gt=6).update(group=2)
        structure.apply_format(cat, "swiss_tiers", {"rounds": 2})
        publishing.publish(t)
        seeding, knockouts = cat.stages.all()
        self.client.login(username="owner", password="password123")
        url = reverse("m_advance", args=[t.slug, seeding.id])
        self.assertContains(self.client.get(url), "isn't finished yet")
        play_all(cat)
        self.assertContains(self.client.get(reverse("m_queue", args=[t.slug])), url)
        page = self.client.get(url)
        self.assertContains(page, "Fill the brackets")
        self.assertContains(page, "Upper poule #1")
        self.client.post(url)  # checkbox not ticked
        seeding.refresh_from_db()
        self.assertIsNone(seeding.advanced_at)
        resp = self.client.post(url, {"checked": "1"})
        self.assertRedirects(resp, reverse("m_queue", args=[t.slug]))
        seeding.refresh_from_db()
        self.assertIsNotNone(seeding.advanced_at)
        self.assertFalse(Entry.objects.filter(pool__stage=knockouts, team__isnull=True).exists())
