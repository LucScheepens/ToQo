import unittest
from collections import Counter
from datetime import datetime

from engine.bracket import double_elimination, seed_order, single_elimination
from engine.roundrobin import round_robin_rounds
from engine.scheduler import SGame, Window, schedule
from engine.standings import PointsConfig, Result, compute_table
from engine.swiss import first_round, pair_round


class RoundRobinTests(unittest.TestCase):
    def test_every_pair_meets_once(self):
        for n in range(2, 11):
            rounds = round_robin_rounds(n)
            pairs = [frozenset(p) for r in rounds for p in r]
            self.assertEqual(len(pairs), n * (n - 1) // 2)
            self.assertEqual(len(set(pairs)), len(pairs))
            for r in rounds:  # nobody plays twice in a round
                flat = [t for p in r for t in p]
                self.assertEqual(len(flat), len(set(flat)))

    def test_double_cycle_swaps_home(self):
        rounds = round_robin_rounds(4, cycles=2)
        self.assertEqual(len(rounds), 6)
        games = [p for r in rounds for p in r]
        self.assertEqual(Counter(frozenset(p) for p in games).most_common(1)[0][1], 2)
        self.assertEqual(len(set(games)), 12)  # ordered pairs all distinct

    def test_games_per_team(self):
        rounds = round_robin_rounds(8, games_per_team=3)
        counts = Counter(t for r in rounds for p in r for t in p)
        self.assertEqual(set(counts.values()), {3})

    def test_home_balance(self):
        rounds = round_robin_rounds(6)
        homes = Counter(p[0] for r in rounds for p in r)
        self.assertLessEqual(max(homes.values()) - min(homes.values()), 2)


class StandingsTests(unittest.TestCase):
    def test_basic_table(self):
        res = [Result("A", "B", 21, 10), Result("A", "C", 21, 15), Result("B", "C", 21, 19)]
        table = compute_table(["A", "B", "C"], res)
        self.assertEqual([r.team for r in table], ["A", "B", "C"])
        self.assertEqual((table[0].w, table[0].pts, table[0].pd), (2, 6, 17))

    def test_head_to_head_breaks_tie(self):
        # A, B, C all 1-1 in a cycle is resolved by PD; H2H for 2-way tie.
        res = [
            Result("A", "B", 1, 0), Result("B", "C", 5, 0), Result("C", "A", 1, 0),
            Result("A", "D", 1, 0), Result("B", "D", 1, 0), Result("C", "D", 1, 0),
        ]
        table = compute_table("ABCD", res)
        # all of A,B,C have 6 pts; H2H among the three is 3 each; PD: B +5, A 0... B first
        self.assertEqual(table[0].team, "B")
        self.assertEqual(table[-1].team, "D")

    def test_two_way_h2h(self):
        # A and B both 6 pts; A has the better PD but B won the head-to-head.
        res = [Result("A", "B", 0, 1), Result("A", "C", 10, 0), Result("B", "C", 1, 0), Result("C", "A", 0, 5)]
        table = compute_table("ABC", res, tiebreakers=["points", "head_to_head", "point_diff"])
        self.assertEqual([r.team for r in table], ["B", "A", "C"])

    def test_unresolved_tie_falls_back_to_seed(self):
        res = [Result("A", "B", 1, 1, winner_side="D")]
        table = compute_table(["A", "B"], res, tiebreakers=["points"], seeds={"A": 2, "B": 1})
        self.assertEqual(table[0].team, "B")
        self.assertTrue(table[0].tie_unresolved)

    def test_forfeit_and_bye(self):
        res = [Result("A", "B", 1, 0, kind="forfeit", winner_side="H"), Result("C", None, kind="bye")]
        table = compute_table("ABC", res, PointsConfig(3, 1, 0))
        rows = {r.team: r for r in table}
        self.assertEqual(rows["A"].pts, 3)
        self.assertEqual(rows["C"].pts, 3)
        self.assertEqual(rows["C"].gp, 0)
        self.assertEqual(rows["B"].l, 1)

    def test_shootout_winner_side(self):
        res = [Result("A", "B", 2, 2, winner_side="A")]
        table = compute_table("AB", res)
        self.assertEqual(table[0].team, "B")


class BracketTests(unittest.TestCase):
    def test_seed_order(self):
        self.assertEqual(seed_order(8), [1, 8, 4, 5, 2, 7, 3, 6])

    def test_single_elim_sizes(self):
        self.assertEqual(len(single_elimination(8)), 7)
        self.assertEqual(len(single_elimination(8, "third")), 8)
        self.assertEqual(len(single_elimination(8, "all")), 12)  # 8 teams * log2(8) / 2
        games = single_elimination(6)
        byes = [g for g in games if ("bye",) in (g.home, g.away)]
        self.assertEqual(len(byes), 2)

    def test_placement_all_covers_every_place(self):
        games = single_elimination(8, "all")
        places = sorted(p for g in games for p in (g.place_winner, g.place_loser) if p)
        self.assertEqual(places, list(range(1, 9)))

    def test_topological_order(self):
        for games in (single_elimination(13, "all"), double_elimination(13)):
            seen = set()
            for g in games:
                for src in (g.home, g.away):
                    if src[0] in ("W", "L"):
                        self.assertIn(src[1], seen)
                seen.add(g.key)

    def test_double_elim_counts(self):
        # 2N-2 games (+1 reset) for N a power of two
        self.assertEqual(len(double_elimination(8, reset=False)), 14)
        self.assertEqual(len(double_elimination(8, reset=True)), 15)
        self.assertEqual(len(double_elimination(4, reset=False)), 6)

    def test_double_elim_every_loser_used_once(self):
        games = double_elimination(16, reset=False)
        losers = Counter(src[1] for g in games for src in (g.home, g.away) if src[0] == "L")
        wb = [g.key for g in games if g.bracket == "W"]
        self.assertEqual(set(losers), set(wb))
        self.assertEqual(set(losers.values()), {1})


class SwissTests(unittest.TestCase):
    def test_first_round(self):
        pairs, bye = first_round([1, 2, 3, 4, 5])
        self.assertEqual(pairs, [(1, 3), (2, 4)])
        self.assertEqual(bye, 5)

    def test_avoids_rematch(self):
        pairs, _ = pair_round([1, 2, 3, 4], {frozenset((1, 2))})
        self.assertNotIn(frozenset((1, 2)), {frozenset(p) for p in pairs})

    def test_bye_rotates(self):
        _, bye = pair_round([1, 2, 3], set(), had_bye={3})
        self.assertEqual(bye, 2)


class SchedulerTests(unittest.TestCase):
    def test_fits_and_respects_teams(self):
        day = Window(datetime(2026, 7, 12, 9), datetime(2026, 7, 12, 12))
        games = [SGame(i, 60, teams={a, b}, priority=(i,)) for i, (a, b) in
                 enumerate([("A", "B"), ("C", "D"), ("A", "C"), ("B", "D"), ("A", "D"), ("B", "C")])]
        res = schedule(games, ["c1", "c2"], [day], interval=60)
        self.assertEqual(res.unscheduled, [])
        by_time = Counter(start for _, start in res.assignments.values())
        self.assertEqual(set(by_time.values()), {2})

    def test_dependencies_and_capacity(self):
        day = Window(datetime(2026, 7, 12, 9), datetime(2026, 7, 12, 11))
        games = [SGame("sf1", 60, priority=(0,)), SGame("sf2", 60, priority=(0,)),
                 SGame("f", 60, deps=["sf1", "sf2"], priority=(1,)), SGame("x", 60, priority=(2,), teams={"Z"}),
                 SGame("y", 60, priority=(3,), teams={"Z"})]
        res = schedule(games, ["c1", "c2"], [day], interval=60, min_rest=0)
        self.assertEqual(res.assignments["f"][1], datetime(2026, 7, 12, 10))
        self.assertIn("y", res.unscheduled)  # Z plays x at 10:00, no room after


if __name__ == "__main__":
    unittest.main()
