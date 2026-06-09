#!/usr/bin/env python3
"""Unit tests for P0 workflow guard helpers in views.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from views import (  # noqa: E402
    _line_production_pipeline_blockers,
    _line_production_pipeline_saturated,
    _line_production_shortage,
    _trailer_line_catalog_blockers,
    _trailer_line_has_catalog_identity,
)


class _FakeItem:
    def __init__(self, article: str = '', name: str = ''):
        self.article = article
        self.name = name


class _FakeNeed:
    def __init__(self, *, id: int, status: str, need_type: str = 'CUSTOMER_ORDER', order_line_id: int | None = 1):
        self.id = id
        self.status = status
        self.need_type = need_type
        self.order_line_id = order_line_id


class _FakePrl:
    def __init__(self, *, id: int, status: str):
        self.id = id
        self.status = status


class _FakePu:
    def __init__(self, *, id: int, status: str):
        self.id = id
        self.status = status


class _FakeLine:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.line_no = kwargs.get('line_no', 1)
        self.line_type = kwargs.get('line_type', 'TRAILER')
        self.item_id = kwargs.get('item_id')
        self.article_snapshot = kwargs.get('article_snapshot')
        self.product_name_snapshot = kwargs.get('product_name_snapshot')
        self.item = kwargs.get('item')
        self.quantity = kwargs.get('quantity', 1)
        self.supply_needs = kwargs.get('supply_needs', [])
        self.production_lines = kwargs.get('production_lines', [])
        self.produced_units = kwargs.get('produced_units', [])


class CatalogGuardTests(unittest.TestCase):
    def test_trailer_line_missing_catalog(self):
        line = _FakeLine(item_id=None, article_snapshot='', product_name_snapshot='')
        self.assertFalse(_trailer_line_has_catalog_identity(line))
        blockers = _trailer_line_catalog_blockers(line)
        self.assertEqual(len(blockers), 1)
        self.assertIn('номенклатура', blockers[0])

    def test_trailer_line_ok_with_snapshots(self):
        line = _FakeLine(
            item_id=531,
            article_snapshot='002-2513E50Q14-OK60',
            product_name_snapshot='Прицеп тест',
        )
        self.assertTrue(_trailer_line_has_catalog_identity(line))
        self.assertEqual(_trailer_line_catalog_blockers(line), [])

    def test_trailer_line_ok_with_item_fallback(self):
        line = _FakeLine(
            item_id=531,
            item=_FakeItem(article='ART', name='Name'),
        )
        self.assertTrue(_trailer_line_has_catalog_identity(line))

    def test_component_line_skipped(self):
        line = _FakeLine(line_type='COMPONENT', item_id=None)
        self.assertTrue(_trailer_line_has_catalog_identity(line))
        self.assertEqual(_trailer_line_catalog_blockers(line), [])


class ProductionPipelineGuardTests(unittest.TestCase):
    def test_ready_need_blocks(self):
        line = _FakeLine(supply_needs=[_FakeNeed(id=79, status='READY')])
        self.assertTrue(_line_production_pipeline_saturated(line))
        self.assertIn('потребность #79', _line_production_pipeline_blockers(line)[0])

    def test_cancelled_need_allows(self):
        line = _FakeLine(supply_needs=[_FakeNeed(id=79, status='CANCELLED')])
        self.assertFalse(_line_production_pipeline_saturated(line))

    def test_prl_blocks(self):
        line = _FakeLine(production_lines=[_FakePrl(id=11, status='ready')])
        self.assertTrue(_line_production_pipeline_saturated(line))
        self.assertIn('производственная строка #11', _line_production_pipeline_blockers(line)[0])

    def test_produced_unit_blocks(self):
        line = _FakeLine(produced_units=[_FakePu(id=93, status='produced_no_vin')])
        self.assertTrue(_line_production_pipeline_saturated(line))
        self.assertIn('выпуск #93', _line_production_pipeline_blockers(line)[0])

    def test_shortage_zero_when_pipeline_saturated(self):
        line = _FakeLine(
            quantity=2,
            supply_needs=[_FakeNeed(id=1, status='READY')],
        )
        self.assertEqual(_line_production_shortage(line), 0)


if __name__ == '__main__':
    unittest.main()
