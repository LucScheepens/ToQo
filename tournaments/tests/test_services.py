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


class SwissTiersTests(TestCase):
    def setUp(self):
        self.t, self.cat = make_tournament(48)
        Team.objects.filter(category=self.cat, seed__gt=24).update(group=2)
        Team.objects.filter(category=self.cat, seed__lte=24).update(group=1)
        structure.apply_format(self.cat, "swiss_tiers", {"rounds": 3})
        self.seeding, self.knockouts = list(self.cat.stages.all())

    def test_structure(self):
        up, low = self.seeding.pools.all()
        self.assertEqual({e.team.group for e in up.entries.all()}, {1})
        self.assertEqual({e.team.group for e in low.entries.all()}, {2})
        self.assertEqual(self.seeding.games.count(), 2 * 3 * 12)
        flights = {p.name: [e.source_label for e in p.entries.all()] for p in self.knockouts.pools.all()}
        self.assertEqual([len(v) for v in flights.values()], [16, 16, 16])
        self.assertEqual(flights["Advanced"][-1], "Upper poule #16")
        self.assertEqual(flights["Intermediate"][:2], ["Upper poule #17", "Upper poule #18"])
        self.assertEqual(flights["Intermediate"][-1], "Lower poule #8")
        self.assertEqual(flights["Beginner"][0], "Lower poule #9")

    def test_round_one_random_within_poule_then_swiss(self):
        r1 = list(self.seeding.games.filter(round=1))
        self.assertTrue(all(g.home_team.group == g.away_team.group for g in r1))
        self.assertNotEqual(sorted((g.home_team.seed, g.away_team.seed) for g in r1),
                            [(i, i + 12) for i in range(1, 13)] + [(i, i + 12) for i in range(25, 37)])
        before = [(g.home_team_id, g.away_team_id) for g in r1]
        structure.recompute_category(self.cat)
        self.assertEqual(before, [(g.home_team_id, g.away_team_id) for g in self.seeding.games.filter(round=1)])

    def test_full_lifecycle(self):
        publishing.publish(self.t)
        play_all(self.cat)
        self.assertEqual(stage_status(self.seeding), "complete")
        pairs = [frozenset((g.home_team_id, g.away_team_id)) for g in self.seeding.games.all()]
        self.assertEqual(len(pairs), len(set(pairs)))
        transitions.advance(self.seeding)
        for flight in self.knockouts.pools.all():
            self.assertEqual(flight.entries.filter(team__isnull=True).count(), 0)
        inter = self.knockouts.pools.get(name="Intermediate")
        self.assertEqual([e.team.group for e in inter.entries.all()], [1] * 8 + [2] * 8)
        play_all(self.cat)
        self.assertEqual(stage_status(self.knockouts), "complete")
        self.assertEqual(len(category_final_standings(self.cat)), 48)

    def test_add_and_remove_unpaired_rounds_mid_event(self):
        publishing.publish(self.t)
        g = playable(self.cat)[0]
        results.submit_result(g, 11, 4)
        r1 = {x.id: (x.home_team_id, x.away_team_id) for x in self.seeding.games.filter(round=1)}
        structure.set_swiss_rounds(self.seeding, 2)  # drop round 3
        self.assertEqual(sorted(r["round"] for r in structure.swiss_rounds(self.seeding)), [1, 2])
        structure.set_swiss_rounds(self.seeding, 4)  # add rounds 3 and 4
        self.seeding.refresh_from_db()
        self.assertEqual(self.seeding.config["rounds"], 4)
        self.assertEqual(self.seeding.games.filter(round=4).count(), 24)
        self.assertEqual(r1, {x.id: (x.home_team_id, x.away_team_id) for x in self.seeding.games.filter(round=1)})
        g.refresh_from_db()
        self.assertEqual(g.home_score, 11)
        with self.assertRaises(ServiceError):
            structure.set_swiss_rounds(self.seeding, 0)
        play_all(self.cat)  # finishes the 4 seeding rounds (knockouts wait for advancement)
        with self.assertRaises(ServiceError):
            structure.set_swiss_rounds(self.seeding, 3)  # round 4 is played
        self.assertEqual(stage_status(self.seeding), "complete")

    def test_remove_paired_round_without_results(self):
        publishing.publish(self.t)
        for g in playable(self.cat):
            if g.round == 1:
                results.submit_result(g, 11, 4)
        self.assertTrue(structure.swiss_rounds(self.seeding)[1]["paired"])
        structure.set_swiss_rounds(self.seeding, 1)  # round 2 is paired but unplayed
        self.assertEqual([r["round"] for r in structure.swiss_rounds(self.seeding)], [1])
        self.assertEqual(stage_status(self.seeding), "complete")
        structure.set_swiss_rounds(self.seeding, 2)
        g = self.seeding.games.filter(round=2).exclude(home_team=None).first()
        from tournaments.models import Space
        add_venue(self.t, courts=1)
        scheduling.start_game(g, Space.objects.first())
        with self.assertRaisesMessage(ServiceError, "being played"):
            structure.set_swiss_rounds(self.seeding, 1)
        results.submit_result(g, 11, 2)
        with self.assertRaisesMessage(ServiceError, "already has results"):
            structure.set_swiss_rounds(self.seeding, 1)

    def test_needs_both_poules(self):
        Team.objects.filter(category=self.cat).update(group=1)
        with self.assertRaises(ServiceError):
            structure.apply_format(self.cat, "swiss_tiers", {"rounds": 3})


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


class QueueAndBestOfTests(TestCase):
    def setUp(self):
        self.t, self.cat = make_tournament(8, schedule_mode=Tournament.ScheduleMode.QUEUE)
        structure.apply_format(self.cat, "rr", {"pools": 1})
        add_venue(self.t, courts=1)
        publishing.publish(self.t)
        from tournaments.models import Space
        self.court = Space.objects.get()

    def test_auto_queue_toggle(self):
        g = scheduling.start_next(self.t, self.court)
        results.submit_result(g, 11, 5)
        self.assertTrue(Game.objects.filter(space=self.court, status=Game.Status.IN_PROGRESS).exists())
        self.t.auto_queue = False
        self.t.save()
        live = Game.objects.get(space=self.court, status=Game.Status.IN_PROGRESS)
        results.submit_result(live, 11, 9)
        self.assertFalse(Game.objects.filter(space=self.court, status=Game.Status.IN_PROGRESS).exists())

    def test_best_of_three(self):
        g = playable(self.cat)[0]
        results.submit_games(g, [(11, 7), (9, 11), (11, 5)])
        g.refresh_from_db()
        self.assertEqual((g.home_score, g.away_score, g.winner_side), (2, 1, "H"))
        self.assertEqual(g.sets, [[11, 7], [9, 11], [11, 5]])
        self.assertEqual(g.sets_label, "11–7, 9–11, 11–5")
        results.submit_games(g, [(8, 11)], confirm=True)  # back to a single game
        g.refresh_from_db()
        self.assertEqual((g.home_score, g.away_score, g.sets), (8, 11, []))
        with self.assertRaises(ServiceError):
            results.submit_games(g, [(11, 7), (10, 10)], confirm=True)
        with self.assertRaises(ServiceError):
            results.submit_games(g, [(11, 7), (None, 3)], confirm=True)
