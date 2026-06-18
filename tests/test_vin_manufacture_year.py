#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class VinManufactureYearTests(unittest.TestCase):
    def test_year_from_vin_position_10(self):
        from views import _manufacture_year_from_vin

        self.assertEqual(_manufacture_year_from_vin('MX4000002S0002411'), 2025)
        self.assertEqual(_manufacture_year_from_vin('MX4000002T0000000'), 2026)
        self.assertEqual(_manufacture_year_from_vin('MX4000002R0001425'), 2024)
        self.assertEqual(_manufacture_year_from_vin('mx4000002s0002411'), 2025)

    def test_invalid_or_short_vin_returns_none(self):
        from views import _manufacture_year_from_vin

        self.assertIsNone(_manufacture_year_from_vin(''))
        self.assertIsNone(_manufacture_year_from_vin('MX4000002'))
        self.assertIsNone(_manufacture_year_from_vin('MX4000002Z0000000'))


if __name__ == '__main__':
    unittest.main()
