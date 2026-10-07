"""Smoke tests executed with supported Home Assistant releases installed."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


HOME_ASSISTANT_INSTALLED = importlib.util.find_spec("homeassistant") is not None


@unittest.skipUnless(
    HOME_ASSISTANT_INSTALLED,
    "Home Assistant is installed only in the compatibility CI job",
)
class HomeAssistantCompatibilityTest(unittest.TestCase):
    """Catch removed or renamed Home Assistant APIs before publishing."""

    def test_all_integration_modules_import(self) -> None:
        modules = (
            "custom_components.edc_sharing",
            "custom_components.edc_sharing.api",
            "custom_components.edc_sharing.billing",
            "custom_components.edc_sharing.billing_api",
            "custom_components.edc_sharing.billing_document",
            "custom_components.edc_sharing.button",
            "custom_components.edc_sharing.calculation",
            "custom_components.edc_sharing.config_flow",
            "custom_components.edc_sharing.coordinator",
            "custom_components.edc_sharing.dashboard",
            "custom_components.edc_sharing.dashboard_api",
            "custom_components.edc_sharing.ean_settings",
            "custom_components.edc_sharing.energy",
            "custom_components.edc_sharing.history",
            "custom_components.edc_sharing.report",
            "custom_components.edc_sharing.profile_report",
            "custom_components.edc_sharing.profile_options",
            "custom_components.edc_sharing.profile_overview",
            "custom_components.edc_sharing.report_profiles",
            "custom_components.edc_sharing.sensor",
        )

        for module in modules:
            with self.subTest(module=module):
                importlib.import_module(module)

    def test_new_diagnostic_sensor_api_is_available(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass

        from custom_components.edc_sharing.sensor import (
            EdcHistoryBackfillStatusSensor,
            EdcHistoryEarliestDateSensor,
        )

        status_sensor = object.__new__(EdcHistoryBackfillStatusSensor)
        earliest_date_sensor = object.__new__(EdcHistoryEarliestDateSensor)
        self.assertEqual(
            status_sensor.device_class,
            SensorDeviceClass.ENUM,
        )
        self.assertEqual(
            earliest_date_sensor.device_class,
            SensorDeviceClass.DATE,
        )

    def test_surplus_utilization_sensor_metadata(self) -> None:
        from homeassistant.components.sensor import SensorStateClass
        from homeassistant.const import PERCENTAGE

        from custom_components.edc_sharing.sensor import SENSORS

        keys = {
            "surplus_utilization_latest_available_day",
            "surplus_utilization_this_week",
            "surplus_utilization_this_month",
            "surplus_utilization_this_year",
            "surplus_utilization_total",
        }
        descriptions = {
            description.key: description
            for description in SENSORS
            if description.key in keys
        }

        self.assertEqual(set(descriptions), keys)
        for description in descriptions.values():
            self.assertEqual(description.native_unit_of_measurement, PERCENTAGE)
            self.assertEqual(description.state_class, SensorStateClass.MEASUREMENT)
            self.assertEqual(description.suggested_display_precision, 1)
            self.assertEqual(description.icon, "mdi:solar-power-variant")

    def test_target_ean_sensor_metadata(self) -> None:
        from homeassistant.components.sensor import SensorStateClass
        from homeassistant.const import PERCENTAGE, UnitOfEnergy

        from custom_components.edc_sharing.sensor import TARGET_SENSORS

        descriptions = {description.key: description for description in TARGET_SENSORS}
        self.assertEqual(len(descriptions), 11)
        self.assertEqual(
            descriptions["shared_latest_available_day"].native_unit_of_measurement,
            UnitOfEnergy.KILO_WATT_HOUR,
        )
        self.assertEqual(
            descriptions["sharing_coverage_this_month"].native_unit_of_measurement,
            PERCENTAGE,
        )
        self.assertEqual(
            descriptions["sharing_coverage_this_month"].state_class,
            SensorStateClass.MEASUREMENT,
        )

    def test_target_ean_uses_supported_via_device_id(self) -> None:
        from custom_components.edc_sharing.sensor import (
            EdcTargetSensor,
            TARGET_SENSORS,
        )

        coordinator = Mock()
        entry = SimpleNamespace(
            data={"sse_id": "test"},
            options={},
            runtime_data=SimpleNamespace(coordinator=coordinator),
        )
        sensor = EdcTargetSensor(
            entry,
            "target-example",
            TARGET_SENSORS[0],
            "group-device-id",
        )

        self.assertEqual(sensor.device_info["via_device_id"], "group-device-id")
        self.assertNotIn("via_device", sensor.device_info)

    def test_coordinator_skips_incomplete_initial_history_block(self) -> None:
        """An older pre-sharing profile must not prevent initial setup."""
        from custom_components.edc_sharing.coordinator import EdcSharingCoordinator

        incomplete_profile = {
            "valueColumns": [{"ean": "producer", "type": "D", "dir": "IN"}],
            "content": [{"date": "2026-09-01", "values": [{"v": 1}]}],
        }
        api = SimpleNamespace(async_get_daily_profile=AsyncMock(return_value=incomplete_profile))
        entry = SimpleNamespace(
            data={"sse_id": "1", "sse_name": "Test group", "sale_price": 2},
            options={},
        )
        coordinator = object.__new__(EdcSharingCoordinator)
        coordinator.api = api
        coordinator.config_entry = entry
        coordinator.eans = ()
        coordinator._days = {}
        coordinator._history_days = {}
        coordinator._target_days = {}
        coordinator._history_target_days = {}
        coordinator._hours = {}
        coordinator._target_hours = {}
        coordinator._history_refresh_date = None
        coordinator._history_import_enabled = False
        coordinator.history_earliest_date = None
        coordinator._calculate_statistics = Mock(return_value=Mock())

        fixed_now = datetime(2026, 10, 3, 12, tzinfo=UTC)
        with (
            patch("custom_components.edc_sharing.coordinator.dt_util.now", return_value=fixed_now),
            patch(
                "custom_components.edc_sharing.coordinator.profile_date_ranges",
                return_value=((date(2026, 9, 1), date(2026, 9, 2)),),
            ),
        ):
            result = asyncio.run(coordinator._async_update_data())

        self.assertIsNotNone(result)
        self.assertEqual(coordinator.last_attempt_result, "success")
        self.assertEqual(coordinator._days, {})
        api.async_get_daily_profile.assert_awaited_once()

    def test_energy_export_passes_real_recorder_validation(self) -> None:
        from custom_components.edc_sharing.calculation import TargetDailySharing
        from custom_components.edc_sharing.history import async_import_energy_history

        day = date(2026, 10, 1)
        row = TargetDailySharing(
            "paid-example", day, Decimal(10), Decimal(6), Decimal(4), Decimal(40)
        )
        recorder = Mock()
        with patch(
            "homeassistant.components.recorder.statistics.get_instance", return_value=recorder
        ):
            async_import_energy_history(
                SimpleNamespace(config=SimpleNamespace(language="en")),
                sse_id=1,
                sse_name="Example group",
                target_days={"paid-example": {day: row}},
                options={"energy_targets": ["paid-example"]},
                sale_price=Decimal(2),
                today=date(2026, 10, 2),
                local_tz=UTC,
            )
        self.assertEqual(recorder.async_import_statistics.call_count, 2)
        revenue_metadata, points, _table = recorder.async_import_statistics.call_args_list[0].args
        self.assertTrue(revenue_metadata["has_sum"])
        self.assertEqual(revenue_metadata["unit_of_measurement"], "CZK")
        self.assertEqual(points[-1]["sum"], 8)

    def test_target_hourly_export_passes_real_recorder_validation(self) -> None:
        from custom_components.edc_sharing.calculation import TargetHourlySharing
        from custom_components.edc_sharing.history import async_import_target_hourly_history

        start = datetime(2026, 10, 1, 10, tzinfo=UTC)
        row = TargetHourlySharing(
            "target_example", start, Decimal(10), Decimal(6), Decimal(4), Decimal(40)
        )
        recorder = Mock()
        with patch(
            "homeassistant.components.recorder.statistics.get_instance", return_value=recorder
        ):
            async_import_target_hourly_history(
                SimpleNamespace(config=SimpleNamespace(language="en")),
                ean="target_example",
                target_name="Example target",
                hours=(row,),
                now=datetime(2026, 10, 1, 12, tzinfo=UTC),
                local_tz=UTC,
            )
        self.assertEqual(recorder.async_import_statistics.call_count, 4)
        metadata, points, _table = recorder.async_import_statistics.call_args_list[0].args
        self.assertFalse(metadata["has_sum"])
        self.assertEqual(metadata["statistic_id"], "edc_sharing:target_example_shared_hourly")
        self.assertEqual(points[0]["mean"], 4.0)

    def test_cached_daily_row_round_trip_preserves_precision(self) -> None:
        from custom_components.edc_sharing.calculation import (
            DailySharing,
            TargetDailySharing,
        )
        from custom_components.edc_sharing.coordinator import (
            _serialize_daily_row,
            _serialize_target_daily_row,
            _stored_daily_rows,
            _stored_target_daily_rows,
        )

        row = DailySharing(
            date(2026, 9, 1),
            Decimal("10.96"),
            Decimal("4.22"),
            Decimal("6.74"),
            Decimal("10.96"),
            Decimal("6.74"),
            Decimal("4.22"),
            Decimal("61.49635036496350364963503650"),
            Decimal("0.00"),
        )

        self.assertEqual(
            _stored_daily_rows([_serialize_daily_row(row)]), {row.day: row}
        )
        self.assertEqual(_stored_daily_rows([{"day": "invalid"}]), {})

        target_row = TargetDailySharing(
            "consumer-example",
            date(2026, 9, 1),
            Decimal("10.96"),
            Decimal("4.22"),
            Decimal("6.74"),
            Decimal("61.49635036496350364963503650"),
        )
        self.assertEqual(
            _stored_target_daily_rows([_serialize_target_daily_row(target_row)]),
            {target_row.ean: {target_row.day: target_row}},
        )
        self.assertEqual(_stored_target_daily_rows([{"ean": "", "day": "invalid"}]), {})

    def test_external_statistics_metadata_and_reimport_are_stable(self) -> None:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.const import UnitOfEnergy

        from custom_components.edc_sharing.calculation import HourlySharing
        from custom_components.edc_sharing.history import async_import_hourly_history

        def hour(start: datetime, shared: str) -> HourlySharing:
            value = Decimal(shared)
            return HourlySharing(
                start,
                value,
                Decimal("0"),
                value,
                value,
                value,
                Decimal("0"),
                Decimal("100"),
                Decimal("0"),
            )

        hours = (
            hour(datetime(2026, 10, 25, 0, tzinfo=UTC), "1"),
            hour(datetime(2026, 10, 25, 1, tzinfo=UTC), "2"),
        )
        hass = SimpleNamespace(config=SimpleNamespace(language="en"))

        with patch(
            "custom_components.edc_sharing.history.async_add_external_statistics"
        ) as add_statistics:
            for _ in range(2):
                async_import_hourly_history(
                    hass,
                    sse_id=1,
                    sse_name="Test group",
                    hours=hours,
                    sale_price=Decimal("2"),
                    now=datetime(2026, 10, 25, 3, tzinfo=UTC),
                    local_tz=UTC,
                )
            corrected_hours = (
                hour(datetime(2026, 10, 25, 0, tzinfo=UTC), "3"),
                hours[1],
            )
            async_import_hourly_history(
                hass,
                sse_id=1,
                sse_name="Test group",
                hours=corrected_hours,
                sale_price=Decimal("2"),
                now=datetime(2026, 10, 25, 3, tzinfo=UTC),
                local_tz=UTC,
            )

        self.assertEqual(add_statistics.call_count, 18)
        first_metadata = add_statistics.call_args_list[0].args[1]
        first_rows = add_statistics.call_args_list[0].args[2]
        repeated_rows = add_statistics.call_args_list[6].args[2]
        corrected_rows = add_statistics.call_args_list[12].args[2]
        self.assertEqual(first_metadata["source"], "edc_sharing")
        self.assertEqual(first_metadata["statistic_id"], "edc_sharing:1_shared_hourly")
        self.assertEqual(first_metadata["mean_type"], StatisticMeanType.ARITHMETIC)
        self.assertFalse(first_metadata["has_sum"])
        self.assertEqual(
            first_metadata["unit_of_measurement"], UnitOfEnergy.KILO_WATT_HOUR
        )
        self.assertEqual(
            tuple(row["start"] for row in first_rows),
            (
                datetime(2026, 10, 25, 0, tzinfo=UTC),
                datetime(2026, 10, 25, 1, tzinfo=UTC),
            ),
        )
        self.assertEqual(first_rows, repeated_rows)
        self.assertEqual(first_rows[0]["start"], corrected_rows[0]["start"])
        self.assertEqual(first_rows[0]["mean"], 1.0)
        self.assertEqual(corrected_rows[0]["mean"], 3.0)

    def test_dst_fold_with_home_assistant_timezone(self) -> None:
        from homeassistant.util import dt as dt_util

        from custom_components.edc_sharing.calculation import parse_hourly_profile

        local_tz = dt_util.get_time_zone("Europe/Prague")
        self.assertIsNotNone(local_tz)
        response = {
            "valueColumns": [
                {"ean": "producer", "type": "D", "dir": "IN"},
                {"ean": "producer", "type": "D", "dir": "OUT"},
                {"ean": "consumer", "type": "O", "dir": "IN"},
                {"ean": "consumer", "type": "O", "dir": "OUT"},
            ],
            "content": [
                {
                    "date": "2026-10-25",
                    "start": "02:00:00",
                    "values": [{"v": 1}, {"v": 0}, {"v": -1}, {"v": 0}],
                },
                {
                    "date": "2026-10-25",
                    "start": "02:00:00",
                    "values": [{"v": 2}, {"v": 0}, {"v": -2}, {"v": 0}],
                },
            ],
        }

        hours = parse_hourly_profile(response, local_tz=local_tz)

        self.assertEqual(
            tuple(row.start for row in hours),
            (
                datetime(2026, 10, 25, 0, tzinfo=UTC),
                datetime(2026, 10, 25, 1, tzinfo=UTC),
            ),
        )


@unittest.skipUnless(
    HOME_ASSISTANT_INSTALLED, "Requires Home Assistant compatibility CI"
)
class ReportProfileFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from custom_components.edc_sharing.config_flow import EdcSharingOptionsFlow

        self.entry = SimpleNamespace(
            options={
                "report_targets": ["notify.owner"],
                "daily_report": True,
                "sale_price": 2,
            },
            data={"sse_id": "test"},
        )
        self.flow = EdcSharingOptionsFlow(self.entry)
        self.flow.hass = SimpleNamespace(
            config=SimpleNamespace(language="cs"),
            states=SimpleNamespace(get=Mock(return_value=None)),
        )
        self.flow.async_show_form = lambda **kwargs: kwargs
        self.flow.async_show_menu = lambda **kwargs: kwargs
        self.flow.async_create_entry = lambda **kwargs: kwargs

    async def test_energy_selection_preserves_existing_options(self):
        self.entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(
            eans=[SimpleNamespace(role="target", ean="paid-example"),
                  SimpleNamespace(role="sharing", ean="producer-example")],
            _history_target_days={},
        ))
        result = await self.flow.async_step_energy_settings({"energy_targets": ["paid-example"]})
        self.assertEqual(result["data"]["energy_targets"], ["paid-example"])
        self.assertEqual(result["data"]["sale_price"], 2)
        self.assertEqual(result["data"]["report_targets"], ["notify.owner"])
        invalid = await self.flow.async_step_energy_settings({"energy_targets": ["producer-example"]})
        self.assertIn("base", invalid["errors"])

    async def test_profile_creation_validation_and_legacy_preservation(self):
        from custom_components.edc_sharing.report_profiles import default_profile

        menu = await self.flow.async_step_init()
        self.assertEqual(
            menu["menu_options"],
            ["general", "ean_settings", "energy_settings", "payment_settings", "profiles", "dashboard", "billing"],
        )
        form = await self.flow.async_step_profiles({"profile": "new"})
        values = default_profile() | {
            "name": "Accountant",
            "targets": ["notify.accountant"],
            "periods": ["monthly"],
            "frequency": "yearly",
        }
        values.pop("id")
        normalized = form["data_schema"](values)
        saved = await self.flow.async_step_profile_edit(normalized)
        self.assertEqual(saved["data"]["sale_price"], 2)
        self.assertEqual(saved["data"]["report_targets"], ["notify.owner"])
        self.assertEqual(len(saved["data"]["report_profiles"]), 2)
        self.assertEqual(saved["data"]["report_profiles"][0]["id"], "legacy_daily")
        self.assertEqual(
            saved["data"]["report_profiles"][1]["targets"], ["notify.accountant"]
        )

    async def test_ean_details_store_name_location_and_target_price(self):
        from custom_components.edc_sharing.calculation import EanInfo

        self.entry.runtime_data = SimpleNamespace(
            coordinator=SimpleNamespace(
                eans=(EanInfo("target-example", "target"),)
            )
        )
        chosen = await self.flow.async_step_ean_settings({"ean": "target-example"})
        values = chosen["data_schema"](
            {
                "name": "Flat 2",
                "location": "Prague",
                "use_group_price": False,
                "price": 3.5,
            }
        )
        saved = await self.flow.async_step_ean_edit(values)
        self.assertEqual(
            saved["data"]["ean_settings"]["target-example"],
            {"name": "Flat 2", "location": "Prague", "price": "3.5"},
        )

    async def test_payment_account_is_scoped_to_options_and_validated(self):
        form = await self.flow.async_step_payment_settings()
        values = form["data_schema"](
            {"payment_account_number": "12-34", "payment_bank_code": "9999"}
        )
        saved = await self.flow.async_step_payment_settings(values)
        self.assertEqual(saved["data"]["payment_account_number"], "12-34")
        self.assertEqual(saved["data"]["payment_bank_code"], "9999")

        invalid = await self.flow.async_step_payment_settings(
            {"payment_account_number": "bad", "payment_bank_code": "9999"}
        )
        self.assertEqual(invalid["errors"]["base"], "invalid_payment_settings")

    async def test_invalid_profile_stays_in_form(self):
        await self.flow.async_step_profiles({"profile": "new"})
        response = await self.flow.async_step_profile_edit({"name": "No recipients"})
        self.assertEqual(response["errors"], {"base": "invalid_profile"})

    async def test_duplicate_is_paused_and_has_new_identity(self):
        await self.flow.async_step_profiles({"profile": "legacy_daily"})
        await self.flow.async_step_profile_duplicate()
        self.assertNotEqual(self.flow._selected_profile["id"], "legacy_daily")
        self.assertFalse(self.flow._selected_profile["enabled"])

    async def test_delete_requires_confirmation_and_preserves_options(self):
        await self.flow.async_step_profiles({"profile": "legacy_daily"})
        canceled = await self.flow.async_step_profile_delete({"confirm": False})
        self.assertEqual(canceled["step_id"], "profile_manage")
        saved = await self.flow.async_step_profile_delete({"confirm": True})
        self.assertEqual(saved["data"]["report_profiles"], [])
        self.assertEqual(saved["data"]["sale_price"], 2)

    async def test_overview_reads_all_profiles_without_sending_or_saving(self):
        from copy import deepcopy
        from datetime import timedelta, timezone

        from custom_components.edc_sharing.report_profiles import default_profile

        active = default_profile("active") | {
            "name": "Owner",
            "enabled": True,
            "targets": ["notify.owner"],
            "periods": ["daily", "yearly"],
        }
        paused = default_profile("paused") | {
            "name": "Accountant",
            "targets": ["notify.missing"],
            "periods": ["monthly"],
            "combined": False,
            "frequency": "monthly",
        }
        self.entry.options["report_profiles"] = [active, paused]
        options_before = deepcopy(self.entry.options)
        self.flow.hass.states.get.side_effect = lambda entity_id: (
            SimpleNamespace(name="Owner recipient", state="unavailable")
            if entity_id == "notify.owner"
            else None
        )
        manager = Mock()
        manager.status.side_effect = lambda profile: {
            "result": "sent" if profile["enabled"] else "not_sent",
            "last_attempt": "2026-09-05T06:00:00+00:00",
            "last_success": "2026-09-05T06:01:00+00:00",
            "next_attempt": "2026-09-06T06:00:00+00:00" if profile["enabled"] else "–",
        }
        self.entry.runtime_data = SimpleNamespace(
            reporter=SimpleNamespace(profiles=manager)
        )
        with patch(
            "custom_components.edc_sharing.profile_options.dt_util.now",
            return_value=datetime(2026, 9, 5, 12, tzinfo=timezone(timedelta(hours=2))),
        ):
            result = await self.flow.async_step_profiles()
        overview = result["description_placeholders"]["overview"]
        self.assertIn("Owner — Zapnuto", overview)
        self.assertIn("Accountant — Pozastaveno", overview)
        self.assertIn("Owner recipient — nedostupné", overview)
        self.assertIn("entita nenalezena", overview)
        self.assertIn("05. 09. 2026 08:01:00", overview)
        self.assertIn("souhrn v jednom e-mailu", overview)
        self.assertIn("samostatné e-maily", overview)
        self.assertIn("**Příští pokus:** —", overview)
        self.assertEqual(self.entry.options, options_before)
        self.assertEqual([call[0] for call in manager.mock_calls], ["status", "status"])

    async def test_empty_or_unloaded_overview_still_allows_profile_management(self):
        self.entry.options = {"report_profiles": []}
        empty = await self.flow.async_step_profiles()
        self.assertIn("žádné profily", empty["description_placeholders"]["overview"])
        self.entry.options = {"daily_report": True, "report_targets": ["notify.owner"]}
        unloaded = await self.flow.async_step_profiles()
        self.assertIn(
            "Integrace není načtená", unloaded["description_placeholders"]["overview"]
        )
