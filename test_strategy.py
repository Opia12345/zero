import unittest

from strategy import Quote, best_quote, break_even_win_rate, contract_menu, expected_cost, rtp, win_rate_z


class StrategyTests(unittest.TestCase):
    def test_menu_probabilities(self):
        menu = {(c.contract_type, c.barrier): c.win_prob for c in contract_menu()}
        self.assertAlmostEqual(menu[("DIGITOVER", "0")], 0.9)
        self.assertAlmostEqual(menu[("DIGITOVER", "8")], 0.1)
        self.assertAlmostEqual(menu[("DIGITUNDER", "9")], 0.9)
        self.assertAlmostEqual(menu[("DIGITUNDER", "1")], 0.1)
        self.assertAlmostEqual(menu[("DIGITDIFF", "3")], 0.9)
        self.assertAlmostEqual(menu[("DIGITMATCH", "3")], 0.1)
        self.assertAlmostEqual(menu[("DIGITEVEN", None)], 0.5)
        self.assertEqual(len(menu), 9 + 9 + 10 + 10 + 2)

    def test_rtp_and_cost(self):
        self.assertAlmostEqual(rtp(0.9, 0.35, 0.364), 0.9 * 0.364 / 0.35)
        self.assertAlmostEqual(expected_cost(0.95, 1.0), 0.05)
        self.assertAlmostEqual(break_even_win_rate(0.35, 0.364), 0.35 / 0.364)

    def test_best_quote_picks_highest_rtp_and_respects_floor(self):
        by = {(c.contract_type, c.barrier): c for c in contract_menu()}
        cheap = Quote("R_10", by[("DIGITEVEN", None)], 1.0, 1.95)  # rtp 0.975
        dear = Quote("R_10", by[("DIGITOVER", "0")], 1.0, 1.04)  # rtp 0.936
        self.assertIs(best_quote([dear, cheap], 0.95), cheap)
        self.assertIsNone(best_quote([dear, cheap], 0.98))
        self.assertIsNone(best_quote([], 0.0))

    def test_tie_prefers_higher_win_probability(self):
        by = {(c.contract_type, c.barrier): c for c in contract_menu()}
        a = Quote("R_10", by[("DIGITMATCH", "1")], 1.0, 9.5)  # 0.95
        b = Quote("R_10", by[("DIGITEVEN", None)], 1.0, 1.9)  # 0.95
        self.assertIs(best_quote([a, b], 0.9), b)

    def test_z_score(self):
        self.assertEqual(win_rate_z(0, []), 0.0)
        self.assertAlmostEqual(win_rate_z(90, [0.9] * 100), 0.0)
        self.assertLess(win_rate_z(70, [0.9] * 100), -5)
        self.assertAlmostEqual(win_rate_z(50, [0.9] * 50 + [0.1] * 50), 0.0)


if __name__ == "__main__":
    unittest.main()
