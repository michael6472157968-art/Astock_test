import copy
import unittest
from unittest.mock import patch

from app.services.strategy_v2.storage import StrategyBacktestRepository, _normalize_backtest_result
from app.utils.db_postgres import PostgresCursor


class _BulkCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, query, rows):
        self.calls.append((query, list(rows)))


class _RawCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, query, rows):
        self.calls.append((query, list(rows)))
        return "bulk-ok"


class StrategyV2StorageCompatibilityTests(unittest.TestCase):
    def test_backtest_details_are_persisted_in_complete_batches(self):
        cursor = _BulkCursor()
        result = {
            "closedTrades": [{
                "exit_time": "2026-01-02T00:00:00Z",
                "side": "long",
                "exit_price": 102,
                "quantity": 2,
                "profit": 4,
                "balance": 10004,
                "close_reason": "grid_exit",
            }],
            "equityCurve": [
                {"time": "2026-01-01T00:00:00Z", "value": 10000},
                {"time": "2026-01-02T00:00:00Z", "value": 10004},
            ],
        }

        StrategyBacktestRepository._persist_details(cursor, 8, 7, 6, result)

        self.assertEqual(len(cursor.calls), 2)
        self.assertEqual(len(cursor.calls[0][1]), 1)
        self.assertEqual(len(cursor.calls[1][1]), 2)
        self.assertEqual(cursor.calls[1][1][-1], (8, 2, "2026-01-02T00:00:00Z", 10004.0))

    def test_postgres_cursor_bulk_path_converts_placeholders_without_returning_ids(self):
        raw = _RawCursor()
        cursor = PostgresCursor(raw)
        rows = [(1, 2), (3, 4)]

        with (
            patch("app.utils.db_postgres.HAS_PSYCOPG2", True),
            patch(
                "app.utils.db_postgres.execute_batch",
                return_value="bulk-ok",
                create=True,
            ) as bulk,
        ):
            result = cursor.executemany(
                "INSERT INTO sample (a, b) VALUES (?, ?)",
                rows,
            )

        self.assertEqual(result, "bulk-ok")
        bulk.assert_called_once_with(
            raw,
            "INSERT INTO sample (a, b) VALUES (%s, %s)",
            rows,
            page_size=1000,
        )
        self.assertEqual(raw.calls, [])

    def test_legacy_backtest_result_restores_overview_fields_from_executions(self):
        legacy = {
            "equityCurve": [
                {"time": "2025-01-01 00:00:00", "value": 9995.0},
                {"time": "2025-01-02 00:00:00", "value": 10089.9},
            ],
            "rawTrades": [
                {
                    "time": "2025-01-01 00:00:00",
                    "side": "buy",
                    "symbol": "Crypto:BTC/USDT@spot",
                    "quantity": 50,
                    "price": 100,
                    "commission": 5,
                },
                {
                    "time": "2025-01-02 00:00:00",
                    "side": "sell",
                    "symbol": "Crypto:BTC/USDT@spot",
                    "quantity": 50,
                    "price": 102,
                    "commission": 5.1,
                },
            ],
        }

        restored = _normalize_backtest_result(legacy, {
            "initial_capital": 10000,
            "start_date": "2025-01-01",
            "end_date": "2025-01-02",
            "leverage": 5,
            "commission": 0.001,
            "slippage": 0.002,
        })

        first, last = restored["equityCurve"]
        self.assertAlmostEqual(first["cash"], 4995)
        self.assertAlmostEqual(first["netExposure"], 5000 / 9995)
        self.assertAlmostEqual(first["grossExposure"], 5000 / 9995)
        self.assertAlmostEqual(last["cash"], 10089.9)
        self.assertAlmostEqual(last["netExposure"], 0)
        self.assertAlmostEqual(restored["attribution"]["feeDrag"], 10.1 / 10000)
        self.assertEqual(restored["attribution"]["orderStatus"], {
            "filled": 2,
            "partial": 0,
            "deferred": 0,
            "rejected": 0,
        })
        self.assertEqual(len(restored["orderLedger"]), 2)
        self.assertEqual(restored["executionAssumptions"], {
            "initialCapital": 10000,
            "startDate": "2025-01-01",
            "endDate": "2025-01-02",
            "leverageEnabled": True,
            "leverage": 5,
            "commission": 0.001,
            "slippage": 0.002,
        })
        self.assertTrue(restored["compatibility"]["legacyBackfill"])

    def test_current_backtest_result_keeps_saved_detail_values(self):
        current = {
            "initialCapital": 10000,
            "executionAssumptions": {
                "initialCapital": 10000,
                "startDate": "2025-01-01",
                "endDate": "2025-01-02",
                "leverageEnabled": False,
                "leverage": 1,
                "commission": 0.0005,
                "slippage": 0.0005,
            },
            "equityCurve": [{
                "time": "2025-01-01T00:00:00Z",
                "value": 10100,
                "cash": 2200,
                "grossExposure": 0.8,
                "netExposure": 0.6,
            }],
            "orderLedger": [{"orderId": "order-1", "status": "partial"}],
            "attribution": {
                "feeDrag": 0.0123,
                "orderStatus": {"filled": 0, "partial": 1, "deferred": 0, "rejected": 0},
            },
        }
        expected = copy.deepcopy(current)

        restored = _normalize_backtest_result(current, {
            "initial_capital": 5000,
            "start_date": "ignored",
            "end_date": "ignored",
            "leverage": 2,
            "commission": 0.1,
            "slippage": 0.1,
        })

        self.assertEqual(restored, expected)
        self.assertNotIn("compatibility", restored)

    def test_legacy_negative_equity_continuation_is_flagged_for_rerun(self):
        restored = _normalize_backtest_result({
            "equityCurve": [
                {"time": "2025-01-01", "value": 10000},
                {"time": "2025-01-02", "value": -100},
                {"time": "2025-01-03", "value": -500},
            ],
        }, {
            "initial_capital": 10000,
            "leverage": 5,
        })

        self.assertTrue(restored["legacyInsolventContinuation"])
        self.assertIn(
            "legacyInsolventContinuation",
            restored["compatibility"]["backfilledFields"],
        )


if __name__ == "__main__":
    unittest.main()
