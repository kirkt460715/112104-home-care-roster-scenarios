"""检查交换资料的结构和记录间引用。"""
import json
import subprocess
import sys
from pathlib import Path
import unittest

from catalog import read_table

ROOT = Path(__file__).resolve().parents[1]
TABLE_KEYS = {'requests': ['request'], 'visit_events': ['request', 'kind'], 'calendar': ['day', 'site'], 'planning_dates': ['round'], 'service_rules': ['service'], 'availability': ['capacity_id', 'site'], 'qualifications': ['worker', 'credential'], 'courses': ['course'], 'transport': ['origin', 'destination'], 'agreements': ['event', 'recorded'], 'actual_adjustments': ['reference', 'day'], 'planning_policy': ['round']}


class MaterialsTest(unittest.TestCase):
    def rows(self, name):
        return list(read_table(name))

    def assert_subset(self, left, reference, right, key):
        self.assertLessEqual({r[reference] for r in self.rows(left)},
                             {r[key] for r in self.rows(right)})

    def test_structure(self):
        for name, columns in TABLE_KEYS.items():
            with self.subTest(name=name):
                rows = self.rows(name)
                self.assertTrue(rows)
                keys = []
                for row in rows:
                    self.assertNotIn(None, row)
                    self.assertNotIn(None, row.values())
                    value = tuple(row[field] for field in columns)
                    self.assertTrue(all(value))
                    keys.append(value)
                self.assertEqual(len(keys), len(set(keys)))

    def test_catalog_reports_input_counts(self):
        result = subprocess.check_output([sys.executable, "catalog.py"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(result), {name: len(self.rows(name)) for name in TABLE_KEYS})

    def test_business_references(self):
        self.assert_subset("visit_events", "request", "requests", "request")
        self.assertGreaterEqual(len(self.rows("visit_events")), 1800)
        self.assertTrue(any(row["kind"] == "unassigned" for row in self.rows("visit_events")))
        self.assertEqual({row["site"] for row in self.rows("availability") if row["capacity_id"] == "L1"}, {"N", "S"})
