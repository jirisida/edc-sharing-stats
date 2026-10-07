"""Tests for standalone EDC calculation logic."""

from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).parents[1]
if not (REPOSITORY_ROOT / "custom_components").is_dir():
    REPOSITORY_ROOT = Path(__file__).parents[2] / "home_assistant"
MODULE_PATH = REPOSITORY_ROOT / "custom_components" / "edc_sharing" / "calculation.py"
SPEC = importlib.util.spec_from_file_location("edc_calculation", MODULE_PATH)
calculation = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = calculation
SPEC.loader.exec_module(calculation)


class _Prague2026Timezone(tzinfo):
    """Small deterministic CET/CEST timezone used without external tzdata."""

    _SPRING_UTC = datetime(2026, 3, 29, 1)
    _AUTUMN_UTC = datetime(2026, 10, 25, 1)

    def utcoffset(self, value: datetime | None) -> timedelta | None:
        if value is None:
            return None
        wall = value.replace(tzinfo=None)
        if datetime(2026, 10, 25, 2) <= wall < datetime(2026, 10, 25, 3):
            return timedelta(hours=1 if value.fold else 2)
        if datetime(2026, 3, 29, 2) <= wall < datetime(2026, 3, 29, 3):
            return timedelta(hours=2 if value.fold else 1)
        if datetime(2026, 3, 29, 3) <= wall < datetime(2026, 10, 25, 3):
            return timedelta(hours=2)
        return timedelta(hours=1)

    def dst(self, value: datetime | None) -> timedelta | None:
        offset = self.utcoffset(value)
        return None if offset is None else offset - timedelta(hours=1)

    def tzname(self, value: datetime | None) -> str | None:
        return "CEST" if self.utcoffset(value) == timedelta(hours=2) else "CET"

    def fromutc(self, value: datetime) -> datetime:
        utc = value.replace(tzinfo=None)
        if self._SPRING_UTC <= utc < self._AUTUMN_UTC:
            return (utc + timedelta(hours=2)).replace(tzinfo=self, fold=0)
        fold = int(self._AUTUMN_UTC <= utc < self._AUTUMN_UTC + timedelta(hours=1))
        return (utc + timedelta(hours=1)).replace(tzinfo=self, fold=fold)


PRAGUE_2026 = _Prague2026Timezone()


def _dst_profile(day: str, starts: tuple[str, ...]) -> dict:
    return {
        "valueColumns": [
            {"ean": "producer", "type": "D", "dir": "IN"},
            {"ean": "producer", "type": "D", "dir": "OUT"},
            {"ean": "consumer", "type": "O", "dir": "IN"},
            {"ean": "consumer", "type": "O", "dir": "OUT"},
        ],
        "content": [
            {
                "date": day,
                "start": start,
                "values": [{"v": index + 1}, {"v": 0}, {"v": -(index + 1)}, {"v": 0}],
            }
            for index, start in enumerate(starts)
        ],
    }


class CalculationTests(unittest.TestCase):
    @staticmethod
    def _target_daily_row(
        ean: str,
        day: date,
        *,
        consumption: str,
        grid_purchase: str,
    ):
        consumption_value = Decimal(consumption)
        grid_value = Decimal(grid_purchase)
        shared = consumption_value - grid_value
        return calculation.TargetDailySharing(
            ean=ean,
            day=day,
            consumption=consumption_value,
            grid_purchase=grid_value,
            shared=shared,
            coverage=(shared / consumption_value * Decimal("100"))
            if consumption_value
            else Decimal("0"),
        )

    def test_target_eans_are_calculated_individually_and_sum_to_group(self) -> None:
        response = {
            "valueColumns": [
                {"ean": "producer", "type": "D", "dir": "IN"},
                {"ean": "producer", "type": "D", "dir": "OUT"},
                {"ean": "target-a", "type": "O", "dir": "IN"},
                {"ean": "target-a", "type": "O", "dir": "OUT"},
                {"ean": "target-b", "type": "O", "dir": "IN"},
                {"ean": "target-b", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {
                    "date": "2026-09-01",
                    "values": [
                        {"v": 14}, {"v": 4}, {"v": -8},
                        {"v": -2}, {"v": -6}, {"v": -2},
                    ],
                }
            ],
        }

        group = calculation.parse_daily_profile(response)
        targets = calculation.parse_daily_target_profiles(response)

        self.assertEqual(len(targets), 2)
        target_a, target_b = targets
        self.assertEqual((target_a.ean, target_a.shared), ("target-a", Decimal("6")))
        self.assertEqual((target_b.ean, target_b.shared), ("target-b", Decimal("4")))
        self.assertEqual(target_a.coverage, Decimal("75"))
        self.assertEqual(target_b.coverage, Decimal("66.66666666666666666666666667"))
        self.assertEqual(sum((row.shared for row in targets), Decimal("0")), group[0].shared)

    def test_target_statistics_honor_each_target_price_and_period(self) -> None:
        target_a = (
            self._target_daily_row(
                "target-a", date(2026, 8, 31), consumption="10", grid_purchase="4"
            ),
            self._target_daily_row(
                "target-a", date(2026, 9, 1), consumption="8", grid_purchase="2"
            ),
            self._target_daily_row(
                "target-a", date(2026, 9, 2), consumption="4", grid_purchase="1"
            ),
        )
        result = calculation.calculate_statistics(
            (),
            Decimal("2"),
            date(2026, 9, 2),
            target_days={"target-a": target_a},
            target_prices={"target-a": Decimal("3.5")},
        )
        target = result.target_statistics[0]

        self.assertEqual(target.sale_price, Decimal("3.5"))
        self.assertEqual(target.latest.shared, Decimal("3"))
        self.assertEqual(target.latest.revenue, Decimal("10.5"))
        self.assertEqual(target.week.shared, Decimal("15"))
        self.assertEqual(target.month.shared, Decimal("9"))
        self.assertEqual(target.year.shared, Decimal("15"))
        self.assertEqual(target.total.revenue, Decimal("52.5"))

    @staticmethod
    def _daily_row(
        day: date,
        *,
        shared: str,
        production_surplus: str,
        unused_surplus: str = "0",
        consumption: str = "0",
    ):
        consumption_value = Decimal(consumption)
        shared_value = Decimal(shared)
        return calculation.DailySharing(
            day=day,
            consumption=consumption_value,
            grid_purchase=Decimal("0"),
            shared=shared_value,
            producer_overflow=Decimal(production_surplus),
            used_overflow=shared_value,
            unused_overflow=Decimal(unused_surplus),
            coverage=(
                shared_value / consumption_value * Decimal("100")
                if consumption_value > 0
                else Decimal("0")
            ),
            consistency_difference=Decimal("0"),
        )

    def test_surplus_utilization_reference_value(self) -> None:
        result = calculation.calculate_surplus_utilization(
            (
                self._daily_row(
                    date(2026, 9, 1),
                    shared="6.74",
                    production_surplus="10.96",
                    unused_surplus="4.22",
                    consumption="13.74",
                ),
            )
        )

        self.assertEqual(round(result.value, 1), Decimal("61.5"))
        self.assertEqual(result.shared, Decimal("6.74"))
        self.assertEqual(result.production_surplus, Decimal("10.96"))
        self.assertEqual(result.unused_surplus, Decimal("4.22"))
        self.assertNotEqual(
            result.value,
            Decimal("6.74") / Decimal("13.74") * Decimal("100"),
        )

    def test_surplus_utilization_zero_and_invalid_denominators(self) -> None:
        zero_shared = calculation.calculate_surplus_utilization(
            (
                self._daily_row(
                    date(2026, 9, 1), shared="0", production_surplus="10"
                ),
            )
        )
        zero_surplus = calculation.calculate_surplus_utilization(
            (
                self._daily_row(
                    date(2026, 9, 1), shared="0", production_surplus="0"
                ),
            )
        )
        negative_values = calculation.calculate_surplus_utilization(
            (
                self._daily_row(
                    date(2026, 9, 1), shared="-1", production_surplus="-10"
                ),
            )
        )
        no_history = calculation.calculate_surplus_utilization(())

        self.assertEqual(zero_shared.value, Decimal("0"))
        self.assertEqual(zero_surplus.value, Decimal("0"))
        self.assertEqual(negative_values.value, Decimal("0"))
        self.assertEqual(no_history.value, Decimal("0"))
        self.assertIsNone(no_history.data_start)
        self.assertEqual(no_history.available_days, 0)

    def test_surplus_utilization_periods_use_the_correct_rows(self) -> None:
        result = calculation.calculate_statistics(
            (
                self._daily_row(
                    date(2025, 12, 31), shared="1", production_surplus="10"
                ),
                self._daily_row(
                    date(2026, 8, 31), shared="8", production_surplus="10"
                ),
                self._daily_row(
                    date(2026, 9, 1), shared="2", production_surplus="10"
                ),
                self._daily_row(
                    date(2026, 9, 2), shared="3", production_surplus="10"
                ),
                self._daily_row(
                    date(2026, 9, 3), shared="4", production_surplus="10"
                ),
                self._daily_row(
                    date(2026, 9, 4), shared="10", production_surplus="10"
                ),
            ),
            Decimal("2"),
            date(2026, 9, 3),
        )

        self.assertEqual(
            result.surplus_utilization_latest_available_day.value, Decimal("40")
        )
        self.assertEqual(
            result.surplus_utilization_this_week.value, Decimal("42.5")
        )
        self.assertEqual(
            result.surplus_utilization_this_month.value, Decimal("30")
        )
        self.assertEqual(
            result.surplus_utilization_this_year.value, Decimal("42.5")
        )
        self.assertEqual(
            result.surplus_utilization_total.value, Decimal("36")
        )
        self.assertEqual(result.surplus_utilization_total.available_days, 5)

    def test_spring_dst_hours_have_distinct_utc_timestamps(self) -> None:
        hours = calculation.parse_hourly_profile(
            _dst_profile("2026-03-29", ("01:00:00", "03:00:00")),
            local_tz=PRAGUE_2026,
        )

        self.assertEqual(
            tuple(row.start.isoformat() for row in hours),
            ("2026-03-29T00:00:00+00:00", "2026-03-29T01:00:00+00:00"),
        )

    def test_nonexistent_spring_dst_hour_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "neexistující lokální čas"):
            calculation.parse_hourly_profile(
                _dst_profile("2026-03-29", ("02:00:00",)),
                local_tz=PRAGUE_2026,
            )

    def test_autumn_dst_repeated_hour_is_not_merged(self) -> None:
        hours = calculation.parse_hourly_profile(
            _dst_profile("2026-10-25", ("02:00:00", "02:00:00")),
            local_tz=PRAGUE_2026,
        )

        self.assertEqual(len(hours), 2)
        self.assertEqual(
            tuple(row.start.isoformat() for row in hours),
            ("2026-10-25T00:00:00+00:00", "2026-10-25T01:00:00+00:00"),
        )
        self.assertEqual(tuple(row.shared for row in hours), (Decimal("1"), Decimal("2")))

    def test_local_midnight_keeps_its_calendar_date_after_utc_conversion(self) -> None:
        hours = calculation.parse_hourly_profile(
            _dst_profile("2026-08-04", ("00:00:00",)),
            local_tz=PRAGUE_2026,
        )

        self.assertEqual(hours[0].start.isoformat(), "2026-08-03T22:00:00+00:00")
        self.assertEqual(hours[0].start.astimezone(PRAGUE_2026).date(), date(2026, 8, 4))

    def test_two_month_range_is_split_at_31_days(self) -> None:
        self.assertEqual(
            calculation.two_calendar_month_start(date(2026, 9, 2)),
            date(2026, 8, 1),
        )
        self.assertEqual(
            calculation.profile_date_ranges(date(2026, 8, 1), date(2026, 9, 3)),
            (
                (date(2026, 8, 1), date(2026, 9, 1)),
                (date(2026, 9, 1), date(2026, 9, 3)),
            ),
        )

    def test_empty_profile_is_a_valid_partial_result(self) -> None:
        self.assertEqual(calculation.parse_daily_profile({"content": []}), ())

    def test_non_finite_edc_values_do_not_poison_totals(self) -> None:
        response = {
            "valueColumns": [
                {"ean": "producer", "type": "D", "dir": "IN"},
                {"ean": "producer", "type": "D", "dir": "OUT"},
                {"ean": "consumer", "type": "O", "dir": "IN"},
                {"ean": "consumer", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {
                    "date": "2026-01-01",
                    "values": [
                        {"v": "Infinity"},
                        {"v": "-Infinity"},
                        {"v": "NaN"},
                        {"v": float("nan")},
                    ],
                },
                {
                    "date": "2026-08-01",
                    "values": [{"v": 10}, {"v": 4}, {"v": -8}, {"v": -2}],
                },
            ],
        }

        days = calculation.parse_daily_profile(response)
        summary = calculation.calculate_period_summary(days, Decimal("2"))
        values = (
            summary.consumption,
            summary.grid_purchase,
            summary.shared,
            summary.producer_overflow,
            summary.unused_overflow,
            summary.coverage,
            summary.revenue,
        )

        self.assertTrue(all(value.is_finite() for value in values))
        self.assertEqual(summary.consumption, Decimal("8"))
        self.assertEqual(summary.shared, Decimal("6"))
        self.assertEqual(summary.coverage, Decimal("75"))

    def test_malformed_numeric_value_is_rejected(self) -> None:
        response = _dst_profile("2026-08-01", ("00:00:00",))
        response["content"][0]["values"][0]["v"] = "not-a-number"

        with self.assertRaisesRegex(ValueError, "neplatnou číselnou hodnotu"):
            calculation.parse_daily_profile(response)

    def test_extracts_multiple_sharing_and_target_eans(self) -> None:
        response = {
            "valueColumns": [
                {"ean": "producer-1", "type": "D", "dir": "IN"},
                {"ean": "producer-1", "type": "D", "dir": "OUT"},
                {"ean": "producer-2", "type": "D", "dir": "IN"},
                {"ean": "consumer-1", "type": "O", "dir": "IN"},
                {"ean": "consumer-2", "type": "O", "dir": "OUT"},
            ]
        }

        self.assertEqual(
            calculation.extract_eans(response),
            (
                calculation.EanInfo("producer-1", "sharing"),
                calculation.EanInfo("producer-2", "sharing"),
                calculation.EanInfo("consumer-1", "target"),
                calculation.EanInfo("consumer-2", "target"),
            ),
        )

    def test_one_year_history_ranges_cover_every_day_backwards(self) -> None:
        today = date(2026, 9, 3)
        date_from = calculation.one_calendar_year_ago(today)
        ranges = calculation.profile_date_ranges_backwards(
            date_from,
            date(2026, 8, 1),
        )

        self.assertEqual(date_from, date(2025, 9, 3))
        self.assertEqual(ranges[0], (date(2026, 7, 1), date(2026, 8, 1)))
        self.assertEqual(ranges[-1][0], date_from)
        self.assertTrue(
            all((chunk_to - chunk_from).days <= 31 for chunk_from, chunk_to in ranges)
        )
        self.assertTrue(
            all(older[1] == newer[0] for newer, older in zip(ranges, ranges[1:]))
        )

    def test_one_calendar_year_ago_handles_leap_day(self) -> None:
        self.assertEqual(
            calculation.one_calendar_year_ago(date(2028, 2, 29)),
            date(2027, 2, 28),
        )

    def test_report_date_ranges(self) -> None:
        today = date(2026, 9, 2)
        self.assertEqual(
            calculation.report_date_range("weekly", today),
            (date(2026, 8, 24), date(2026, 8, 31)),
        )
        self.assertEqual(
            calculation.report_date_range("monthly", today),
            (date(2026, 8, 1), date(2026, 9, 1)),
        )
        self.assertEqual(
            calculation.report_date_range("yearly", today),
            (date(2026, 1, 1), date(2026, 9, 3)),
        )

    def test_daily_monthly_and_profit(self) -> None:
        response = {
            "valueColumns": [
                {"ean": "111", "type": "D", "dir": "IN"},
                {"ean": "111", "type": "D", "dir": "OUT"},
                {"ean": "222", "type": "O", "dir": "IN"},
                {"ean": "222", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {"date": "2026-09-01", "values": [{"v": 10}, {"v": 4}, {"v": -8}, {"v": -2}]},
                {"date": "2026-08-31", "values": [{"v": 5}, {"v": 2}, {"v": -4}, {"v": -1}]},
            ],
        }
        result = calculation.calculate_profile(response, Decimal("2.20"), date(2026, 9, 1))
        self.assertEqual(result.today.shared, Decimal("6"))
        self.assertEqual(result.latest.shared, Decimal("6"))
        self.assertEqual(result.latest_day, date(2026, 9, 1))
        self.assertEqual(result.today.coverage, Decimal("75"))
        self.assertEqual(result.month_shared, Decimal("6"))
        self.assertEqual(result.month_revenue, Decimal("13.20"))
        self.assertEqual(result.month_unused, Decimal("4"))

    def test_missing_roles_is_rejected(self) -> None:
        with self.assertRaises(calculation.IncompleteProfileLayoutError):
            calculation.calculate_profile(
                {"valueColumns": [{"ean": "111", "type": "D", "dir": "IN"}], "content": [{"date": "2026-09-01", "values": [{"v": 1}]}]},
                Decimal("2"),
                date(2026, 9, 1),
            )

    def test_period_summary_sums_arbitrary_period(self) -> None:
        days = (
            calculation.DailySharing(
                date(2026, 8, 1),
                Decimal("10"), Decimal("6"), Decimal("4"), Decimal("9"),
                Decimal("4"), Decimal("5"), Decimal("40"), Decimal("0"),
            ),
            calculation.DailySharing(
                date(2026, 8, 2),
                Decimal("20"), Decimal("12"), Decimal("8"), Decimal("15"),
                Decimal("8"), Decimal("7"), Decimal("40"), Decimal("0"),
            ),
        )

        summary = calculation.calculate_period_summary(days, Decimal("2.50"))

        self.assertEqual(summary.consumption, Decimal("30"))
        self.assertEqual(summary.shared, Decimal("12"))
        self.assertEqual(summary.grid_purchase, Decimal("18"))
        self.assertEqual(summary.producer_overflow, Decimal("24"))
        self.assertEqual(summary.unused_overflow, Decimal("12"))
        self.assertEqual(summary.coverage, Decimal("40"))
        self.assertEqual(summary.revenue, Decimal("30.00"))

    def test_real_edc_sign_convention(self) -> None:
        """Producer values are positive and consumer values negative in EDC."""
        response = {
            "valueColumns": [
                {"ean": "test-producer-ean", "type": "D", "dir": "IN"},
                {"ean": "test-producer-ean", "type": "D", "dir": "OUT"},
                {"ean": "test-consumer-ean", "type": "O", "dir": "IN"},
                {"ean": "test-consumer-ean", "type": "O", "dir": "OUT"},
            ],
            "content": [{
                "date": "2026-08-03",
                "values": [{"v": 12.22}, {"v": 7.43}, {"v": -12.43}, {"v": -7.64}],
            }],
        }
        result = calculation.calculate_profile(response, Decimal("2.20"), date(2026, 8, 3))
        self.assertEqual(result.today.shared, Decimal("4.79"))
        self.assertEqual(result.today.grid_purchase, Decimal("7.64"))
        self.assertEqual(result.today.unused_overflow, Decimal("7.43"))
        self.assertEqual(result.today.consistency_difference, Decimal("0.00"))
        self.assertEqual(result.today_revenue, Decimal("10.5380"))

    def test_quarter_hour_rows_are_aggregated_into_one_day(self) -> None:
        """The EDC overview may return multiple intervals for one DAILY request."""
        response = {
            "valueColumns": [
                {"ean": "producer", "type": "D", "dir": "IN"},
                {"ean": "producer", "type": "D", "dir": "OUT"},
                {"ean": "consumer", "type": "O", "dir": "IN"},
                {"ean": "consumer", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {
                    "date": "2026-08-04T00:00:00",
                    "start": "00:00:00",
                    "values": [{"v": 0.01}, {"v": 0}, {"v": -0.04}, {"v": -0.03}],
                },
                {
                    "date": "2026-08-04T00:00:00",
                    "start": "00:15:00",
                    "values": [{"v": 0.01}, {"v": 0}, {"v": -0.05}, {"v": -0.04}],
                },
                {
                    "date": "2026-08-04T00:00:00",
                    "start": "00:45:00",
                    "values": [{"v": 0.02}, {"v": 0.01}, {"v": -0.01}, {"v": 0}],
                },
                {
                    "date": "2026-08-04T00:00:00",
                    "start": "01:00:00",
                    "values": [{"v": 0.02}, {"v": 0}, {"v": -0.04}, {"v": -0.02}],
                },
            ],
        }

        rows = calculation.parse_daily_profile(response)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].producer_overflow, Decimal("0.06"))
        self.assertEqual(rows[0].unused_overflow, Decimal("0.01"))
        self.assertEqual(rows[0].consumption, Decimal("0.14"))
        self.assertEqual(rows[0].grid_purchase, Decimal("0.09"))
        self.assertEqual(rows[0].shared, Decimal("0.05"))
        self.assertEqual(
            rows[0].coverage, Decimal("0.05") / Decimal("0.14") * Decimal("100")
        )
        self.assertEqual(rows[0].consistency_difference, Decimal("0.00"))

        hours = calculation.parse_hourly_profile(response)

        self.assertEqual(len(hours), 2)
        self.assertEqual(hours[0].start.isoformat(), "2026-08-04T00:00:00")
        self.assertEqual(hours[0].shared, Decimal("0.03"))
        self.assertEqual(hours[0].consumption, Decimal("0.10"))
        self.assertEqual(hours[0].grid_purchase, Decimal("0.07"))
        self.assertEqual(hours[1].start.isoformat(), "2026-08-04T01:00:00")
        self.assertEqual(hours[1].shared, Decimal("0.02"))
        self.assertEqual(hours[1].consumption, Decimal("0.04"))
        self.assertEqual(hours[1].grid_purchase, Decimal("0.02"))

        target_hours = calculation.parse_hourly_target_profiles(response)
        self.assertEqual(len(target_hours), 2)
        self.assertEqual(target_hours[0].ean, "consumer")
        self.assertEqual(target_hours[0].start.isoformat(), "2026-08-04T00:00:00")
        self.assertEqual(target_hours[0].shared, Decimal("0.03"))
        self.assertEqual(target_hours[0].consumption, Decimal("0.10"))
        self.assertEqual(target_hours[0].grid_purchase, Decimal("0.07"))
        self.assertEqual(target_hours[1].ean, "consumer")
        self.assertEqual(target_hours[1].start.isoformat(), "2026-08-04T01:00:00")
        self.assertEqual(target_hours[1].shared, Decimal("0.02"))
        self.assertEqual(target_hours[1].consumption, Decimal("0.04"))
        self.assertEqual(target_hours[1].grid_purchase, Decimal("0.02"))

    def test_latest_available_day_is_used_when_today_is_delayed(self) -> None:
        response = {
            "valueColumns": [
                {"ean": "111", "type": "D", "dir": "IN"},
                {"ean": "111", "type": "D", "dir": "OUT"},
                {"ean": "222", "type": "O", "dir": "IN"},
                {"ean": "222", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {"date": "2026-09-01", "values": [{"v": 10}, {"v": 4}, {"v": -8}, {"v": -2}]},
            ],
        }

        result = calculation.calculate_profile(response, Decimal("2"), date(2026, 9, 2))

        self.assertEqual(result.today.shared, Decimal("0"))
        self.assertEqual(result.latest.shared, Decimal("6"))
        self.assertEqual(result.latest_day, date(2026, 9, 1))


if __name__ == "__main__":
    unittest.main()
