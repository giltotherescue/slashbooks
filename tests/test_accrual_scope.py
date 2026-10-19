import sqlite3
import unittest

from bookkeeping.accrual import scope


def _conn(basis, schema):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('basis', ?)", (basis,))
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(schema),))
    return conn


class ScopeTests(unittest.TestCase):
    def test_valid_combinations_from_config(self):
        for view, cap, discipline in (("cash", 0, "off"), ("cash", 1, "all"), ("accrual", 1, "all")):
            with self.subTest(view=view, cap=cap):
                config = {"basis": view, "accrual_capability_version": cap}
                self.assertEqual(scope.default_view(config), view)
                self.assertEqual(scope.capability(config), cap)
                self.assertEqual(scope.discipline(config), discipline)

    def test_missing_keys_mean_cash_and_off(self):
        self.assertEqual((scope.default_view({}), scope.capability({}), scope.discipline({})), ("cash", 0, "off"))

    def test_connection_reads_meta(self):
        for view, schema, cap, discipline in (("cash", 1, 0, "off"), ("cash", 2, 1, "all"), ("accrual", 2, 1, "all")):
            with self.subTest(view=view, schema=schema):
                conn = _conn(view, schema)
                self.assertEqual(scope.default_view(conn), view)
                self.assertEqual(scope.capability(conn), cap)
                self.assertEqual(scope.discipline(conn), discipline)

    def test_write_scope_accepts_any_view_with_capability(self):
        for view in ("accrual", "cash"):
            scope.require_write_scope({"basis": view, "accrual_capability_version": 1})

    def test_tracking_start_selects_per_account(self):
        config = {"basis": "cash", "accrual_capability_version": 1, "accrual_tracking_start": "2026-03-01"}
        self.assertEqual(scope.discipline(config), "per_account")
        self.assertEqual(scope.tracking_start(config), "2026-03-01")
        self.assertEqual(scope.discipline({"basis": "cash", "accrual_tracking_start": "2026-03-01"}), "off")
        with self.assertRaises(ValueError):
            scope.tracking_start({"accrual_tracking_start": "March"})
        conn = _conn("cash", 2)
        conn.execute("INSERT INTO meta VALUES ('accrual_tracking_start', '2026-03-01')")
        self.assertEqual(scope.discipline(conn), "per_account")

    def test_write_scope_refuses_without_capability(self):
        for config in ({"basis": "accrual", "accrual_capability_version": 0}, {"basis": "accrual"},
                       {"basis": "cash", "accrual_capability_version": 0}):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "Accrual writes require"):
                scope.require_write_scope(config)

    def test_accrual_without_capability_stays_readable_but_not_writable(self):
        config = {"basis": "accrual", "accrual_capability_version": 0}
        self.assertTrue(scope.accrual_active(config))
        self.assertEqual(scope.capability(config), 0)

    def test_invalid_values_are_rejected_like_resolve_basis(self):
        with self.assertRaises(ValueError):
            scope.default_view({"basis": "bogus"})
        with self.assertRaises(ValueError):
            scope.capability({"basis": "cash", "accrual_capability_version": 2})


if __name__ == "__main__":
    unittest.main()
