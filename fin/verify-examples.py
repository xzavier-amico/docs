#!/usr/bin/env python3
"""Recompute reviewed examples. Run: python3 fin/verify-examples.py.

These tests cover named teaching examples, not the correctness of every chapter.
Money/AMM arithmetic uses Decimal or integers; statistical estimates use math.
"""
from decimal import Decimal as D, getcontext
from pathlib import Path
import math
import unittest

getcontext().prec = 40
ROOT = Path(__file__).resolve().parent


class Examples(unittest.TestCase):
    def test_precision_intro(self):
        self.assertEqual(D(1_250) * 4 / 10_000, D("0.5"))
        self.assertEqual(7_684_234 * 1_250, 9_605_292_500)
        self.assertEqual(D(7_684_234) / 100, D("76842.34"))

    def test_calldata_and_gas(self):
        amount = (1_000 * 10**6).to_bytes(32, "big")
        self.assertEqual(sum(b != 0 for b in amount), 3)
        self.assertEqual(sum(b == 0 for b in amount), 29)
        calldata = 64 + 368 + sum(16 if b else 4 for b in amount)
        self.assertEqual(calldata, 596)
        new = 21_000 + calldata + 5_000 + 22_100 + 1_756
        old = 21_000 + calldata + 5_000 + 5_000 + 1_756
        self.assertEqual((new, old), (50_452, 33_352))
        direct = 350 * new + 150 * old
        batch = 350 * (new - 19_500) + 150 * (old - 19_500) + 25_000
        self.assertEqual((direct, batch), (22_661_000, 12_936_000))
        self.assertEqual(D(batch) * 22 / 10**9 * 3_000, D("853.776"))

    def test_amm(self):
        effective = D(10_000) * D("0.997")
        out = D(100) * effective / (200_000 + effective)
        self.assertEqual(out.quantize(D("0.000001")), D("4.748297"))
        self.assertEqual((10_000 / out).quantize(D("0.01")), D("2106.02"))
        # Full input, not fee-adjusted input, is retained in actual reserves.
        k_after = D(210_000) * (100 - out)
        self.assertGreater(k_after, D(100 * 200_000))

    def test_sandwich(self):
        fee = D(".997")
        def buy(amount, eth, usdc):
            out = eth * amount * fee / (usdc + amount * fee)
            return out, eth-out, usdc+amount
        front, eth, usdc = buy(D(20_000), D(100), D(200_000))
        user, eth, usdc = buy(D(10_000), eth, usdc)
        returned = usdc * front * fee / (eth+front*fee)
        self.assertEqual(user.quantize(D(".000001")), D("3.942301"))
        self.assertEqual((returned-20_000).quantize(D(".000001")), D("1648.845105"))
        self.assertLess(user, D("4.70"))

    def test_quarterly_drawdown(self):
        value = peak = D(1)
        maximum = D(0)
        for r in ["-.01", "-.01", "-.01", ".18", "-.02", "-.01", "-.02", "-.02"]:
            value *= 1 + D(r)
            peak = max(peak, value)
            maximum = max(maximum, 1 - value / peak)
        self.assertEqual(maximum, D("0.06821992"))

    def test_sortino_target(self):
        # Percent-point returns; numerator and denominator both use MAR=0.
        a = [1, -1, 1, -1, 16, -1, 8, -1, -1, -1]
        b = [4, 4, 4, 4, 4, -14, 4, 4, 4, 2]
        def sortino(rs):
            downside = math.sqrt(sum(min(r, 0)**2 for r in rs)/len(rs))
            return sum(rs)/len(rs)/downside
        self.assertAlmostEqual(sortino(a), 2.581989, places=6)
        self.assertAlmostEqual(sortino(b), .451754, places=6)

    def test_repo_contract_haircut(self):
        debt = D(98_000_000) * (1 + D(".054") * 2 / 360)
        collateral = D(96_000_000)
        self.assertEqual(debt - collateral, D(2_029_400))
        self.assertEqual(debt - collateral * D(".98"), D(3_949_400))
        self.assertEqual(debt / D(".98") - collateral, D(4_030_000))

    def test_tail_probability(self):
        normal = .5 * math.erfc(7 / math.sqrt(2))
        self.assertTrue(7.8e9 < .01 / normal < 7.9e9)

    def test_options_and_roll(self):
        def call_put(s, k, t, r, vol):
            normal = lambda x: .5 * (1 + math.erf(x / math.sqrt(2)))
            d1 = (math.log(s/k) + (r + vol*vol/2)*t) / (vol*math.sqrt(t))
            d2 = d1 - vol*math.sqrt(t)
            call = s*normal(d1) - k*math.exp(-r*t)*normal(d2)
            return call, call-s+k*math.exp(-r*t)
        call, put = call_put(100, 100, .25, .04, .32)
        self.assertAlmostEqual(call, 6.8544, places=4)
        self.assertAlmostEqual(put, 5.8594, places=4)
        before, _ = call_put(100, 100, 7/365, .04, 1.2)
        after, _ = call_put(103.5, 100, 6/365, .04, .35)
        self.assertAlmostEqual((after-before)*1000, -2525.36, places=2)
        self.assertAlmostEqual(7_000_000*(70/72)**12, 4_992_114.79, places=2)
        self.assertAlmostEqual(7_000_000*(70/68)**12, 9_912_143.20, places=2)

    def test_fair_equity_sale_not_double_loss(self):
        ratio = (D("1.02") / D("1.04"))**5
        fair_before = D(8) + D(40)*ratio + D(52) - D(92)
        book_sold = D("5.8") / ratio
        fair_after = (D(40)-book_sold)*ratio + D(52) - D("78.2")
        self.assertAlmostEqual(float(fair_before), 4.2989576, places=6)
        self.assertLess(abs(fair_before-fair_after), D("1e-35"))
        self.assertEqual(163-151, 12)  # AFS mark is already in reported equity.

    def test_dividend_delta(self):
        s, k, t, r, q, vol = 100., 100., .5, .04, .03, .25
        normal = lambda x: .5*(1+math.erf(x/math.sqrt(2)))
        def call(spot):
            d1=(math.log(spot/k)+(r-q+vol*vol/2)*t)/(vol*math.sqrt(t))
            return spot*math.exp(-q*t)*normal(d1)-k*math.exp(-r*t)*normal(d1-vol*math.sqrt(t))
        d1=(math.log(s/k)+(r-q+vol*vol/2)*t)/(vol*math.sqrt(t))
        exact=math.exp(-q*t)*normal(d1)
        self.assertAlmostEqual((call(s+.0001)-call(s-.0001))/.0002, exact, places=7)

    def test_health_factor_relative_prices(self):
        hf = D(100)*D(2000)*D(".8")/D(140000)
        both = D(100)*D(1800)*D(".8")/(D(140000)*D(".9"))
        self.assertEqual(hf, both)
        self.assertGreater(D(100)*D(2000)*D(".8")/(D(140000)*D(".9")), hf)

    def test_1559(self):
        def next_fee(base, used, target=15_000_000):
            delta = base * abs(used - target) // target // 8
            return base + max(delta, 1) if used > target else base - delta
        base = 32_000_000_000
        fees = []
        for used in [30_000_000, 30_000_000, 0]:
            base = next_fee(base, used)
            fees.append(base)
        self.assertEqual(fees, [36_000_000_000, 40_500_000_000, 35_437_500_000])
        self.assertEqual(next_fee(1, 15_000_001), 2)  # minimum upward increment

    def test_source_regressions(self):
        gas = (ROOT / "14-smart-contracts-token-standards-and-aa.md").read_text()
        self.assertIn("29 × 4 + 3 × 16 = 164", gas)
        self.assertIn("12,936,000", gas)
        self.assertNotIn("净值单调上升", gas)
        amm = (ROOT / "16-dex-amm-concentrated-liquidity-and-il.md").read_text()
        self.assertIn("4.748297", amm)
        self.assertIn("210,000 USDC", amm)

    def test_engineering_source_guards(self):
        # Textual tripwires supplement (not replace) compilation/database tests.
        money = (ROOT / "20-monetary-precision-rounding-and-allocation.md").read_text()
        self.assertIn("integer := minor.BigInt()", money)
        self.assertNotIn("coefficient := minor.Coefficient()", money)
        schema = (ROOT / "23-hot-accounts-reversals-and-fx-ledger.md").read_text()
        self.assertIn("UNIQUE (entry_id, line_no)", schema)
        self.assertNotIn("PRIMARY KEY (entry_id, line_no)", schema)
        ledger = (ROOT / "22-double-entry-ledger-schema-and-concurrency.md").read_text()
        self.assertIn("bytes.Equal(storedHash, p.RequestHash)", ledger)
        self.assertIn("Comparator.comparing(UUID::toString)", ledger)
        self.assertNotIn("l.NormalDelta, l.AccountID", ledger)


if __name__ == "__main__":
    unittest.main()
