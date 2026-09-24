from django.test import TestCase

from tournaments.models import Entry, Game, Stage, Team, Tournament
from tournaments.services import publishing, results, scheduling, structure, transitions
from tournaments.services.common import ServiceError
from tournaments.services.resolution import stage_status
from tournaments.services.standings import category_final_standings, pool_table

from .helpers import add_venue, make_tournament, play_all, playable


class RoundRobinToPlayoffsTests(TestCase):
    def setUp(self):
        self.t, self.cat = make_tournament(8)
        structure.apply_format(self.cat, "rr_playoff", {"pools": 2, "advance_per_pool": 2, "placement": "third"})
        self.pool_stage, self.playoffs = list(self.cat.stages.all())
        add_venue(self.t)

    def test_structure(self):
        pools = list(self.pool_stage.pools.all())
        self.assertEqual([p.entries.count() for p in pools], [4, 4])
        # snake seeding: A gets 1,4,5,8
        self.assertEqual([e.team.seed for e in pools[0].entries.all()], [1, 4, 5, 8])
        self.assertEqual(self.pool_stage.games.count(), 12)
        self.assertEqual(self.playoffs.games.count(), 4)  # SF1, SF2, 3rd, F
        labels = [e.source_label for e in self.playoffs.pools.get().entries.all()]
        self.assertEqual(labels, ["Pool A #1", "Pool B #1", "Pool A #2", "Pool B #2"])
        self.assertEqual(stage_status(self.playoffs), "waiting")

    def test_schedule_generation(self):
        unscheduled = scheduling.generate(self.t)
        self.assertEqual(unscheduled, [])
        self.assertEqual(scheduling.find_conflicts(self.t), {})
        pool_end = max(g.end_at for g in self.pool_stage.games.all())
        for g in self.playoffs.games.all():
            self.assertGreaterEqual(g.start_at, pool_end)
        final = self.playoffs.games.get(code="F")
        sf_end = max(g.end_at for g in self.playoffs.games.filter(code__startswith="SF"))
        self.assertGreaterEqual(final.start_at, sf_end)

    def test_results_require_publish(self):
        g = playable(self.cat)[0]
        with self.assertRaises(ServiceError):
            results.submit_result(g, 2, 1)

    def test_full_lifecycle(self):
        scheduling.generate(self.t)
        publishing.publish(self.t)
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, Tournament.Status.PUBLISHED)

        play_all(self.cat)
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, Tournament.Status.IN_PROGRESS)
        self.assertEqual(stage_status(self.pool_stage), "complete")
        table = pool_table(self.pool_stage.pools.first())
        self.assertEqual([r.team.seed for r in table], [1, 4, 5, 8])
        self.assertEqual(table[0].w, 3)

        preview = transitions.preview(self.pool_stage)
        self.assertEqual([t.seed for _, t, _ in preview], [1, 2, 4, 3])
        transitions.advance(self.pool_stage)
        self.assertEqual(stage_status(self.pool_stage), "transitioned")
        sf1 = self.playoffs.games.get(code="SF1")
        # seeds 1..4 = A1, B1, A2, B2 → SF1: A1 vs B2
        self.assertEqual((sf1.home_team.seed, sf1.away_team.seed), (1, 3))

        play_all(self.cat)
        final = self.playoffs.games.get(code="F")
        self.assertEqual(final.winner.seed, 1)
        standings = category_final_standings(self.cat)
        self.assertEqual([(p, t.seed) for p, t, _ in standings], [(1, 1), (2, 2), (3, 3), (4, 4)])
        transitions.finish(self.t)
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, Tournament.Status.COMPLETED)

    def test_editing_pool_result_after_advancement(self):
        publishing.publish(self.t)
        play_all(self.cat)
        transitions.advance(self.pool_stage)
        sf1 = self.playoffs.games.get(code="SF1")
        results.submit_result(sf1, 3, 0)

        # Flip every Pool A game involving seed 1 so seed 1 finishes last in Pool A.
        pool_a = self.pool_stage.pools.first()
        team1 = Team.objects.get(seed=1)
        games = [g for g in Game.objects.filter(pool=pool_a) if team1 in (g.home_team, g.away_team)]
        affected = []
        for g in games:
            home_is_1 = g.home_team == team1
            affected = results.submit_result(g, 0 if home_is_1 else 5, 5 if home_is_1 else 0)
            if affected:
                break
        self.assertTrue(affected, "changing qualification should require confirmation")
        self.assertEqual([a["id"] for a in affected], [sf1.id])
        self.assertIn("Team 1", affected[0]["teams"])  # describes the game as it was
        sf1.refresh_from_db()
        self.assertEqual(sf1.status, Game.Status.COMPLETED)  # rolled back

        results.submit_result(g, 0 if g.home_team == team1 else 5, 5 if g.home_team == team1 else 0, confirm=True)
        for g2 in games:
            g2.refresh_from_db()
            if g2.status == Game.Status.COMPLETED and g2.winner == team1:
                results.submit_result(g2, 0 if g2.home_team == team1 else 5, 5 if g2.home_team == team1 else 0,
                                      confirm=True)
        sf1.refresh_from_db()
        self.assertNotIn(team1, (sf1.home_team, sf1.away_team))
        self.assertEqual(sf1.status, Game.Status.SCHEDULED)

    def test_clearing_result_unadvances(self):
        publishing.publish(self.t)
        play_all(self.cat)
        transitions.advance(self.pool_stage)
        g = self.pool_stage.games.first()
        self.assertEqual(results.clear_result(g), [])
        self.assertEqual(stage_status(self.pool_stage), "in_progress")
        self.assertFalse(Entry.objects.filter(pool__stage=self.playoffs, team__isnull=False).exists())

    def test_forfeit_and_cancel(self):
        publishing.publish(self.t)
        g1, g2 = playable(self.cat)[:2]
        results.forfeit(g1, "H")
        g1.refresh_from_db()
        self.assertEqual((g1.status, g1.winner_side, g1.home_score, g1.away_score), ("forfeit", "A", 0, 1))
        results.cancel(g2)
        g2.refresh_from_db()
        self.assertEqual(g2.status, Game.Status.CANCELLED)
        sf = self.playoffs.games.get(code="SF1")
        with self.assertRaises(ServiceError):
            results.cancel(sf)

    def test_tie_needs_winner_in_elimination(self):
        publishing.publish(self.t)
        play_all(self.cat)
        transitions.advance(self.pool_stage)
        sf = self.playoffs.games.get(code="SF1")
        with self.assertRaises(ServiceError):
            results.submit_result(sf, 2, 2)
        results.submit_result(sf, 2, 2, winner_side="A")
        sf.refresh_from_db()
        self.assertEqual(sf.winner, sf.away_team)

    def test_adding_team_regenerates(self):
        Team.objects.create(category=self.cat, name="Team 9", seed=9)
        structure.teams_changed(self.cat)
        stage = self.cat.stages.first()
        self.assertEqual(sum(p.entries.count() for p in stage.pools.all()), 9)
        self.assertEqual(stage.games.count(), 6 + 10)  # pools of 5 and 4


class EliminationTests(TestCase):
    def test_double_elimination_with_byes_completes(self):
        t, cat = make_tournament(5)
        structure.apply_format(cat, "de", {"grand_final_reset": True})
        publishing.publish(t)
        played = play_all(cat)
        stage = cat.stages.get()
        self.assertEqual(stage_status(stage), "complete")
        gf2 = stage.games.get(if_necessary=True)
        self.assertEqual(gf2.status, Game.Status.CANCELLED)  # WB champion won GF1
        self.assertEqual(played, 2 * 5 - 2)
        top = category_final_standings(cat)
        self.assertEqual([(p, x.seed) for p, x, _ in top[:3]], [(1, 1), (2, 2), (3, 3)])

    def test_grand_final_reset_played_when_lb_champion_wins(self):
        t, cat = make_tournament(4)
        structure.apply_format(cat, "de", {"grand_final_reset": True})
        publishing.publish(t)
        stage = cat.stages.get()
        # Favourite wins everything except the grand final(s): seed 2 wins GF1 from the losers side.
        play_all(cat, favourite=lambda g: (g.home_team.seed < g.away_team.seed) != (g.bracket == "F" and g.round == 1))
        gf2 = stage.games.get(if_necessary=True)
        self.assertEqual(gf2.status, Game.Status.COMPLETED)
        self.assertEqual(stage_status(stage), "complete")

    def test_single_elim_all_places(self):
        t, cat = make_tournament(6)
        structure.apply_format(cat, "se", {"placement": "all"})
        publishing.publish(t)
        play_all(cat)
        places = category_final_standings(cat)
        self.assertEqual([(p, x.seed) for p, x, _ in places], [(i, i) for i in range(1, 7)])


class SwissTests(TestCase):
    def test_swiss_pairs_rounds_without_rematches(self):
        t, cat = make_tournament(6)
        structure.apply_format(cat, "swiss", {"rounds": 3})
        stage = cat.stages.get()
        r2 = stage.games.filter(round=2)
        self.assertTrue(all(g.home_team_id is None for g in r2))
        publishing.publish(t)
        play_all(cat)
        self.assertEqual(stage_status(stage), "complete")
        pairs = [frozenset((g.home_team_id, g.away_team_id)) for g in stage.games.all()]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_swiss_odd_gets_bye(self):
        t, cat = make_tournament(5)
        structure.apply_format(cat, "swiss", {"rounds": 3})
        publishing.publish(t)
        play_all(cat)
        stage = cat.stages.get()
        byes = [g.home_team_id for g in stage.games.all() if g.away_bye]
        self.assertEqual(len(byes), 3)
        self.assertEqual(len(set(byes)), 3)


class QueueTests(TestCase):
    def test_queue_assigns_next_game(self):
        t, cat = make_tournament(4, schedule_mode=Tournament.ScheduleMode.QUEUE)
        structure.apply_format(cat, "rr", {"pools": 1})
        add_venue(t, courts=2)
        publishing.publish(t)
        from tournaments.models import Space
        c1, c2 = Space.objects.all()
        g1 = scheduling.start_next(t, c1)
        g2 = scheduling.start_next(t, c2)
        self.assertIsNotNone(g1)
        self.assertIsNotNone(g2)
        self.assertTrue({g1.home_team_id, g1.away_team_id}.isdisjoint({g2.home_team_id, g2.away_team_id}))
        results.submit_result(g1, 2, 0)
        # Court 1 becomes free and picks up a game whose teams are not on court 2.
        nxt = Game.objects.filter(space=c1, status=Game.Status.IN_PROGRESS).first()
        if nxt:
            self.assertTrue({nxt.home_team_id, nxt.away_team_id}.isdisjoint({g2.home_team_id, g2.away_team_id}))


class CustomStageTests(TestCase):
    def test_custom_stage_best_third(self):
        t, cat = make_tournament(9)
        structure.apply_format(cat, "rr", {"pools": 3})
        rr = cat.stages.get()
        po = structure.add_stage(cat, "Playoffs", Stage.Kind.SE, 1, 4)
        entries = list(po.pools.get().entries.all())
        pools = list(rr.pools.all())
        for e, p in zip(entries[:3], pools):
            structure.set_entry(e, source=f"pool:{p.id}:1")
        structure.set_entry(entries[3], source=f"stage:{rr.id}:4")  # best 2nd place
        self.assertEqual(publishing.validate(t)[0], [])
        publishing.publish(t)
        play_all(cat)
        transitions.advance(rr)
        entries = list(po.pools.get().entries.select_related("team"))
        self.assertEqual([e.team.seed for e in entries[:3]], [1, 2, 3])
        # Runners-up are seeds 6, 5, 4 with identical records; the final fallback is team name.
        self.assertEqual(entries[3].team.seed, 4)
