"""Data coordinator for EDC sharing."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdcApiClient, EdcApiError, EdcAuthenticationError
from .calculation import (
    DailySharing,
    EanInfo,
    HourlySharing,
    IncompleteProfileLayoutError,
    SharingStatistics,
    TargetDailySharing,
    calculate_statistics,
    extract_eans,
    one_calendar_year_ago,
    parse_daily_profile,
    parse_daily_target_profiles,
    parse_hourly_profile,
    parse_hourly_target_profiles,
    TargetHourlySharing,
    profile_date_ranges,
    profile_date_ranges_backwards,
    two_calendar_month_start,
)
from .const import (
    CONF_SALE_PRICE,
    CONF_SSE_ID,
    CONF_SSE_NAME,
    DEFAULT_SALE_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .ean_settings import ean_name, target_sale_price
from .energy import energy_targets
from .history import (
    async_import_daily_history,
    async_import_energy_history,
    async_import_hourly_history,
    async_import_target_hourly_history,
)

_LOGGER = logging.getLogger(__name__)

_HISTORY_STORE_VERSION = 1
_BACKFILL_REQUEST_DELAY = 0.25


def _stored_date(value: object) -> date | None:
    """Parse a stored ISO date without failing integration setup."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _stored_datetime(value: object) -> datetime | None:
    """Parse a stored ISO timestamp without failing integration setup."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _stored_non_negative_int(value: object) -> int:
    """Return a safe non-negative counter loaded from storage."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


_DAILY_DECIMAL_FIELDS = (
    "consumption",
    "grid_purchase",
    "shared",
    "producer_overflow",
    "used_overflow",
    "unused_overflow",
    "coverage",
    "consistency_difference",
)

_TARGET_DAILY_DECIMAL_FIELDS = (
    "consumption",
    "grid_purchase",
    "shared",
    "coverage",
)


def _serialize_daily_row(row: DailySharing) -> dict[str, str]:
    """Serialize one aggregate without losing Decimal precision."""
    return {
        "day": row.day.isoformat(),
        **{field: str(getattr(row, field)) for field in _DAILY_DECIMAL_FIELDS},
    }


def _stored_daily_rows(value: object) -> dict[date, DailySharing]:
    """Restore valid cached daily aggregates and ignore malformed entries."""
    if not isinstance(value, list):
        return {}
    restored: dict[date, DailySharing] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        day = _stored_date(item.get("day"))
        if day is None:
            continue
        try:
            values = {
                field: Decimal(str(item[field])) for field in _DAILY_DECIMAL_FIELDS
            }
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        if not all(number.is_finite() for number in values.values()):
            continue
        restored[day] = DailySharing(day=day, **values)
    return restored


def _serialize_target_daily_row(row: TargetDailySharing) -> dict[str, str]:
    """Serialize one target EAN aggregate without losing Decimal precision."""
    return {
        "ean": row.ean,
        "day": row.day.isoformat(),
        **{
            field: str(getattr(row, field))
            for field in _TARGET_DAILY_DECIMAL_FIELDS
        },
    }


def _stored_target_daily_rows(
    value: object,
) -> dict[str, dict[date, TargetDailySharing]]:
    """Restore safe per-target daily cache entries from storage."""
    if not isinstance(value, list):
        return {}
    restored: dict[str, dict[date, TargetDailySharing]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        ean = item.get("ean")
        day = _stored_date(item.get("day"))
        if (
            not isinstance(ean, str)
            or not ean.strip()
            or len(ean) > 32
            or day is None
        ):
            continue
        ean = ean.strip()
        try:
            values = {
                field: Decimal(str(item[field]))
                for field in _TARGET_DAILY_DECIMAL_FIELDS
            }
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        if not all(number.is_finite() for number in values.values()):
            continue
        row = TargetDailySharing(ean=ean, day=day, **values)
        restored.setdefault(ean, {})[day] = row
    return restored


def _hour_start_utc(value: datetime, local_tz: tzinfo) -> datetime:
    """Return an hourly profile timestamp as an aware UTC datetime."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=local_tz)
    return dt_util.as_utc(value)


def _hour_local_date(value: datetime, local_tz: tzinfo) -> date:
    """Return the local calendar date represented by an hourly timestamp."""
    return _hour_start_utc(value, local_tz).astimezone(local_tz).date()


class EdcSharingCoordinator(DataUpdateCoordinator[SharingStatistics]):
    """Fetch and calculate EDC statistics."""

    config_entry: ConfigEntry
    _target_hours: dict[str, dict[datetime, TargetHourlySharing]] = {}

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: EdcApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.eans: tuple[EanInfo, ...] = ()
        self._days: dict[date, DailySharing] = {}
        self._history_days: dict[date, DailySharing] = {}
        self._target_days: dict[str, dict[date, TargetDailySharing]] = {}
        self._history_target_days: dict[str, dict[date, TargetDailySharing]] = {}
        self._hours: dict[datetime, HourlySharing] = {}
        self._target_hours: dict[str, dict[datetime, TargetHourlySharing]] = {}
        self._history_refresh_date: date | None = None
        self._history_import_enabled = False
        self._history_import_signature: tuple[object, ...] | None = None
        self._energy_import_signature: tuple[object, ...] | None = None
        self.last_attempt_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.next_attempt_at: datetime | None = None
        self.last_attempt_result = "never"
        self.last_attempt_error: str | None = None
        self.history_backfill_status = "not_started"
        self.history_backfill_started_at: datetime | None = None
        self.history_backfill_completed_at: datetime | None = None
        self.history_backfill_scan_start: date | None = None
        self.history_backfill_cursor: date | None = None
        self.history_earliest_date: date | None = None
        self.history_backfill_processed_chunks = 0
        self.history_backfill_total_chunks = 0
        self.history_backfill_imported_days = 0
        self.history_backfill_imported_hours = 0
        self.history_backfill_error: str | None = None
        self._history_backfill_task: asyncio.Task[None] | None = None
        self._history_store: Store[dict[str, Any]] = Store(
            hass,
            _HISTORY_STORE_VERSION,
            (
                f"{DOMAIN}.history_backfill.{entry.entry_id}."
                f"{entry.data[CONF_SSE_ID]}"
            ),
        )

    async def async_initialize(self) -> None:
        """Restore persisted history-backfill progress."""
        stored = await self._history_store.async_load()
        if not isinstance(stored, dict):
            return
        stored_status = str(stored.get("status") or "not_started")
        self.history_backfill_status = (
            stored_status
            if stored_status
            in {"not_started", "running", "paused", "failed", "completed"}
            else "not_started"
        )
        self.history_backfill_started_at = _stored_datetime(stored.get("started_at"))
        self.history_backfill_completed_at = _stored_datetime(
            stored.get("completed_at")
        )
        self.history_backfill_scan_start = _stored_date(stored.get("scan_start"))
        self.history_backfill_cursor = _stored_date(stored.get("cursor"))
        self.history_earliest_date = _stored_date(stored.get("earliest_date"))
        self.history_backfill_processed_chunks = _stored_non_negative_int(
            stored.get("processed_chunks")
        )
        self.history_backfill_total_chunks = _stored_non_negative_int(
            stored.get("total_chunks")
        )
        self.history_backfill_imported_days = _stored_non_negative_int(
            stored.get("imported_days")
        )
        self.history_backfill_imported_hours = _stored_non_negative_int(
            stored.get("imported_hours")
        )
        error = stored.get("error")
        self.history_backfill_error = str(error) if error else None
        self._history_days = _stored_daily_rows(stored.get("daily_rows"))
        self._history_target_days = _stored_target_daily_rows(
            stored.get("target_daily_rows")
        )

    def _target_prices(self, default_price: Decimal) -> dict[str, Decimal]:
        """Return safe per-target prices while preserving the group default."""
        return {
            ean: target_sale_price(ean, self.config_entry.options, default_price)
            for ean in self._history_target_days
        }

    def _calculate_statistics(
        self, sale_price: Decimal, today: date
    ) -> SharingStatistics:
        """Calculate group and optional individual-target values from one cache."""
        return calculate_statistics(
            tuple(self._history_days.values()),
            sale_price,
            today,
            target_days={
                ean: tuple(rows.values())
                for ean, rows in self._history_target_days.items()
            },
            target_prices=self._target_prices(sale_price),
        )

    @callback
    def target_days_for_range(
        self, date_from: date, date_to: date
    ) -> dict[str, tuple[TargetDailySharing, ...]]:
        """Return cached target rows for a half-open range without an API call."""
        return {
            ean: tuple(
                row
                for day, row in sorted(rows.items())
                if date_from <= day < date_to
            )
            for ean, rows in self._history_target_days.items()
        }

    async def _async_update_data(self) -> SharingStatistics:
        now = dt_util.now()
        self.last_attempt_at = now
        self.next_attempt_at = None
        self.last_attempt_result = "running"
        self.last_attempt_error = None
        today = now.date()
        local_tz = now.tzinfo or dt_util.get_default_time_zone()
        full_history_refresh = self._history_refresh_date != today
        date_from = (
            two_calendar_month_start(today)
            if full_history_refresh
            else today.replace(day=1)
        )
        date_to = today + timedelta(days=1)
        try:
            fetched: dict[date, DailySharing] = {}
            fetched_target_days: dict[str, dict[date, TargetDailySharing]] = {}
            fetched_hours: dict[datetime, HourlySharing] = {}
            fetched_target_hours: dict[str, dict[datetime, TargetHourlySharing]] = {}
            fetched_eans: set[EanInfo] = set()
            for chunk_from, chunk_to in profile_date_ranges(date_from, date_to):
                local_from = datetime.combine(chunk_from, time.min, tzinfo=local_tz)
                local_to = datetime.combine(chunk_to, time.min, tzinfo=local_tz)
                raw = await self.api.async_get_daily_profile(
                    int(self.config_entry.data[CONF_SSE_ID]),
                    dt_util.as_utc(local_from).isoformat().replace("+00:00", "Z"),
                    dt_util.as_utc(local_to).isoformat().replace("+00:00", "Z"),
                )
                try:
                    fetched.update(
                        {
                            row.day: row
                            for row in parse_daily_profile(raw)
                            if date_from <= row.day < date_to
                        }
                    )
                    for row in parse_daily_target_profiles(raw):
                        if date_from <= row.day < date_to:
                            fetched_target_days.setdefault(row.ean, {})[row.day] = row
                    fetched_hours.update(
                        {
                            row.start: row
                            for row in parse_hourly_profile(raw, local_tz=local_tz)
                            if date_from
                            <= _hour_local_date(row.start, local_tz)
                            < date_to
                        }
                    )
                    for row in parse_hourly_target_profiles(raw, local_tz=local_tz):
                        if (
                            date_from
                            <= _hour_local_date(row.start, local_tz)
                            < date_to
                        ):
                            fetched_target_hours.setdefault(row.ean, {})[row.start] = row
                    fetched_eans.update(extract_eans(raw))
                except IncompleteProfileLayoutError as err:
                    # Before both EAN roles joined the sharing group, EDC can
                    # return a valid profile containing only one side. This
                    # historical block must not prevent the integration from
                    # starting with the complete, more recent data.
                    _LOGGER.debug(
                        "Skipping incomplete EDC coordinator block %s to %s: %s",
                        chunk_from,
                        chunk_to,
                        err,
                    )

            if full_history_refresh:
                self._days = {
                    day: row for day, row in self._days.items() if day >= date_from
                }
                self._hours = {
                    start: row
                    for start, row in self._hours.items()
                    if _hour_local_date(start, local_tz) >= date_from
                }
                self._target_days = {
                    ean: {
                        day: row for day, row in rows.items() if day >= date_from
                    }
                    for ean, rows in self._target_days.items()
                    if any(day >= date_from for day in rows)
                }
                self._target_hours = {
                    ean: {
                        start: row
                        for start, row in rows.items()
                        if _hour_local_date(start, local_tz) >= date_from
                    }
                    for ean, rows in self._target_hours.items()
                    if any(_hour_local_date(start, local_tz) >= date_from for start in rows)
                }
                self._history_refresh_date = today
            self._days.update(fetched)
            self._hours.update(fetched_hours)
            for ean, rows in fetched_target_days.items():
                self._target_days.setdefault(ean, {}).update(rows)
            for ean, rows in fetched_target_hours.items():
                self._target_hours.setdefault(ean, {}).update(rows)
            history_changed = any(
                self._history_days.get(day) != row for day, row in fetched.items()
            )
            history_changed = history_changed or any(
                self._history_target_days.get(ean, {}).get(day) != row
                for ean, rows in fetched_target_days.items()
                for day, row in rows.items()
            )
            self._history_days.update(fetched)
            for ean, rows in fetched_target_days.items():
                self._history_target_days.setdefault(ean, {}).update(rows)
            if self._history_days:
                earliest_known = min(self._history_days)
                if (
                    self.history_earliest_date is None
                    or earliest_known < self.history_earliest_date
                ):
                    self.history_earliest_date = earliest_known
            if fetched_eans:
                self.eans = tuple(
                    sorted(fetched_eans, key=lambda item: (item.role, item.ean))
                )
            price = Decimal(str(self.config_entry.options.get(
                CONF_SALE_PRICE,
                self.config_entry.data.get(CONF_SALE_PRICE, DEFAULT_SALE_PRICE),
            )))
            result = self._calculate_statistics(price, today)
            if self._history_import_enabled:
                self._async_import_energy_history(now)
                self._async_import_history_if_changed(
                    result, tuple(self._hours.values()), now
                )
            completed_at = dt_util.now()
            self.last_success_at = completed_at
            self.next_attempt_at = completed_at + DEFAULT_SCAN_INTERVAL
            self.last_attempt_result = "success"
            if history_changed:
                await self._async_save_history_backfill_state()
            return result
        except EdcAuthenticationError as err:
            self.last_attempt_result = "authentication_failed"
            self.last_attempt_error = "Přihlášení k EDC již není platné."
            self.next_attempt_at = None
            raise ConfigEntryAuthFailed from err
        except (EdcApiError, ValueError, KeyError) as err:
            self.last_attempt_result = "failed"
            self.last_attempt_error = str(err)
            self.next_attempt_at = dt_util.now() + DEFAULT_SCAN_INTERVAL
            raise UpdateFailed(str(err)) from err

    @callback
    def async_start_history_backfill(self, *, resume_only: bool = False) -> bool:
        """Start or resume a background scan of all possible EDC history."""
        if (
            self._history_backfill_task is not None
            and not self._history_backfill_task.done()
        ):
            return False

        can_resume = (
            self.history_backfill_status in {"running", "paused", "failed"}
            and self.history_backfill_cursor is not None
        )
        if resume_only and not can_resume:
            return False

        now = dt_util.now()
        if not can_resume:
            scan_end = two_calendar_month_start(now.date())
            self.history_backfill_scan_start = one_calendar_year_ago(now.date())
            ranges = profile_date_ranges_backwards(
                self.history_backfill_scan_start,
                scan_end,
            )
            self.history_backfill_cursor = scan_end
            self.history_backfill_started_at = now
            self.history_backfill_completed_at = None
            self.history_earliest_date = (
                min(self._history_days) if self._history_days else None
            )
            self.history_backfill_processed_chunks = 0
            self.history_backfill_total_chunks = len(ranges)
            self.history_backfill_imported_days = 0
            self.history_backfill_imported_hours = 0
        else:
            scan_start = self.history_backfill_scan_start or one_calendar_year_ago(
                now.date()
            )
            self.history_backfill_scan_start = scan_start
            remaining = len(
                profile_date_ranges_backwards(
                    scan_start,
                    self.history_backfill_cursor,
                )
            )
            self.history_backfill_total_chunks = max(
                self.history_backfill_total_chunks,
                self.history_backfill_processed_chunks + remaining,
            )

        self.history_backfill_status = "running"
        self.history_backfill_error = None
        self.async_update_listeners()
        task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_backfill_history(),
            f"{DOMAIN} history backfill {self.config_entry.entry_id}",
        )
        self._history_backfill_task = task
        task.add_done_callback(self._history_backfill_done)
        return True

    @callback
    def _history_backfill_done(self, task: asyncio.Task[None]) -> None:
        """Release the completed task reference."""
        if self._history_backfill_task is task:
            self._history_backfill_task = None

    async def _async_backfill_history(self) -> None:
        """Scan backwards and import every available EDC history block."""
        try:
            await self._async_save_history_backfill_state()
            cursor = self.history_backfill_cursor
            scan_start = self.history_backfill_scan_start
            if scan_start is None:
                scan_start = one_calendar_year_ago(dt_util.now().date())
                self.history_backfill_scan_start = scan_start
            if cursor is None or cursor <= scan_start:
                await self._async_finish_history_backfill()
                return

            ranges = profile_date_ranges_backwards(
                scan_start,
                cursor,
            )
            local_tz = dt_util.now().tzinfo or dt_util.get_default_time_zone()
            price = Decimal(
                str(
                    self.config_entry.options.get(
                        CONF_SALE_PRICE,
                        self.config_entry.data.get(
                            CONF_SALE_PRICE, DEFAULT_SALE_PRICE
                        ),
                    )
                )
            )
            for chunk_from, chunk_to in ranges:
                local_from = datetime.combine(chunk_from, time.min, tzinfo=local_tz)
                local_to = datetime.combine(chunk_to, time.min, tzinfo=local_tz)
                raw = await self.api.async_get_daily_profile(
                    int(self.config_entry.data[CONF_SSE_ID]),
                    dt_util.as_utc(local_from).isoformat().replace("+00:00", "Z"),
                    dt_util.as_utc(local_to).isoformat().replace("+00:00", "Z"),
                )
                try:
                    days = tuple(
                        row
                        for row in parse_daily_profile(raw)
                        if chunk_from <= row.day < chunk_to
                    )
                    hours = tuple(
                        row
                        for row in parse_hourly_profile(raw, local_tz=local_tz)
                        if chunk_from
                        <= _hour_local_date(row.start, local_tz)
                        < chunk_to
                    )
                    target_days = tuple(
                        row
                        for row in parse_daily_target_profiles(raw)
                        if chunk_from <= row.day < chunk_to
                    )
                    target_hours = tuple(
                        row
                        for row in parse_hourly_target_profiles(raw, local_tz=local_tz)
                        if chunk_from
                        <= _hour_local_date(row.start, local_tz)
                        < chunk_to
                    )
                except IncompleteProfileLayoutError as err:
                    # Before both EAN roles joined the sharing group, EDC can
                    # return a valid profile containing only one side. Such a
                    # block is outside this integration's usable history and
                    # must not stop the backwards scan.
                    _LOGGER.debug(
                        "Skipping incomplete EDC history block %s to %s: %s",
                        chunk_from,
                        chunk_to,
                        err,
                    )
                    days = ()
                    hours = ()
                    target_days = ()
                    target_hours = ()
                now = dt_util.now()
                self.history_backfill_imported_days += async_import_daily_history(
                    self.hass,
                    sse_id=int(self.config_entry.data[CONF_SSE_ID]),
                    sse_name=str(self.config_entry.data[CONF_SSE_NAME]),
                    days=days,
                    sale_price=price,
                    today=now.date(),
                    local_tz=local_tz,
                )
                self.history_backfill_imported_hours += async_import_hourly_history(
                    self.hass,
                    sse_id=int(self.config_entry.data[CONF_SSE_ID]),
                    sse_name=str(self.config_entry.data[CONF_SSE_NAME]),
                    hours=hours,
                    sale_price=price,
                    now=now,
                    local_tz=local_tz,
                )
                for ean in {row.ean for row in target_hours}:
                    target_name = ean_name(ean, self.config_entry.options)
                    ean_hours = tuple(row for row in target_hours if row.ean == ean)
                    async_import_target_hourly_history(
                        self.hass,
                        ean=ean,
                        target_name=target_name,
                        hours=ean_hours,
                        now=now,
                        local_tz=local_tz,
                    )
                if days:
                    self._history_days.update({row.day: row for row in days})
                    earliest = min(row.day for row in days)
                    if (
                        self.history_earliest_date is None
                        or earliest < self.history_earliest_date
                    ):
                        self.history_earliest_date = earliest
                for row in target_days:
                    self._history_target_days.setdefault(row.ean, {})[row.day] = row
                if self._history_import_enabled:
                    self._async_import_energy_history(now)
                self.history_backfill_cursor = chunk_from
                self.history_backfill_processed_chunks += 1
                await self._async_save_history_backfill_state()
                self.async_set_updated_data(self._calculate_statistics(price, now.date()))
                await asyncio.sleep(_BACKFILL_REQUEST_DELAY)

            await self._async_finish_history_backfill()
        except asyncio.CancelledError:
            self.history_backfill_status = "paused"
            await self._async_save_history_backfill_state()
            self.async_update_listeners()
            raise
        except EdcAuthenticationError:
            await self._async_fail_history_backfill(
                "Přihlášení k EDC již není platné."
            )
        except (EdcApiError, HomeAssistantError, ValueError, KeyError) as err:
            await self._async_fail_history_backfill(str(err))
        except Exception as err:  # noqa: BLE001
            await self._async_fail_history_backfill(
                f"Neočekávaná chyba při doplňování historie: {err}"
            )

    async def _async_finish_history_backfill(self) -> None:
        """Persist successful completion and notify diagnostic entities."""
        self.history_backfill_status = "completed"
        self.history_backfill_cursor = self.history_backfill_scan_start
        self.history_backfill_processed_chunks = self.history_backfill_total_chunks
        self.history_backfill_completed_at = dt_util.now()
        self.history_backfill_error = None
        await self._async_save_history_backfill_state()
        self.async_update_listeners()

    async def _async_fail_history_backfill(self, error: str) -> None:
        """Persist a recoverable backfill failure."""
        self.history_backfill_status = "failed"
        self.history_backfill_error = error
        await self._async_save_history_backfill_state()
        self.async_update_listeners()
        _LOGGER.warning("Could not backfill all EDC history: %s", error)

    async def _async_save_history_backfill_state(self) -> None:
        """Persist progress so an interrupted scan can resume."""
        await self._history_store.async_save(
            {
                "status": self.history_backfill_status,
                "started_at": self.history_backfill_started_at.isoformat()
                if self.history_backfill_started_at is not None
                else None,
                "completed_at": self.history_backfill_completed_at.isoformat()
                if self.history_backfill_completed_at is not None
                else None,
                "scan_start": self.history_backfill_scan_start.isoformat()
                if self.history_backfill_scan_start is not None
                else None,
                "cursor": self.history_backfill_cursor.isoformat()
                if self.history_backfill_cursor is not None
                else None,
                "earliest_date": self.history_earliest_date.isoformat()
                if self.history_earliest_date is not None
                else None,
                "processed_chunks": self.history_backfill_processed_chunks,
                "total_chunks": self.history_backfill_total_chunks,
                "imported_days": self.history_backfill_imported_days,
                "imported_hours": self.history_backfill_imported_hours,
                "error": self.history_backfill_error,
                "daily_rows": [
                    _serialize_daily_row(row)
                    for row in sorted(
                        self._history_days.values(), key=lambda item: item.day
                    )
                ],
                "target_daily_rows": [
                    _serialize_target_daily_row(row)
                    for ean in sorted(self._history_target_days)
                    for row in sorted(
                        self._history_target_days[ean].values(),
                        key=lambda item: item.day,
                    )
                ],
            }
        )

    @callback
    def async_enable_history_import(self) -> None:
        """Enable imports after platforms have registered their entities."""
        self._history_import_enabled = True
        self._async_import_energy_history(dt_util.now())
        self._async_import_history_if_changed(
            self.data, tuple(self._hours.values()), dt_util.now()
        )

    @callback
    def _async_import_energy_history(self, now: datetime) -> None:
        """Import selected income using the full cache, including older backfill."""
        selected = energy_targets(self.config_entry.options)
        if not selected:
            return
        price = Decimal(str(self.config_entry.options.get(
            CONF_SALE_PRICE, self.config_entry.data.get(CONF_SALE_PRICE, DEFAULT_SALE_PRICE)
        )))
        signature = (
            selected, now.date(), price,
            tuple((ean, target_sale_price(ean, self.config_entry.options, price),
                   ean_name(ean, self.config_entry.options),
                   tuple(sorted(self._history_target_days.get(ean, {}).items())))
                  for ean in selected),
        )
        if signature == self._energy_import_signature:
            return
        try:
            async_import_energy_history(
                self.hass,
                sse_id=int(self.config_entry.data[CONF_SSE_ID]),
                sse_name=str(self.config_entry.data[CONF_SSE_NAME]),
                target_days=self._history_target_days,
                options=self.config_entry.options,
                sale_price=price,
                today=now.date(),
                local_tz=now.tzinfo or dt_util.get_default_time_zone(),
            )
        except HomeAssistantError:
            _LOGGER.warning("Could not queue EDC Energy statistics")
            return
        self._energy_import_signature = signature

    def _async_import_history_if_changed(
        self,
        statistics: SharingStatistics,
        hours: tuple[HourlySharing, ...],
        now: datetime,
    ) -> None:
        finalized = tuple(row for row in statistics.days if row.day < now.date())
        local_tz = now.tzinfo or dt_util.get_default_time_zone()
        current_hour = dt_util.as_utc(
            now.replace(minute=0, second=0, microsecond=0)
        )
        finalized_hours = tuple(
            sorted(
                (
                    row
                    for row in hours
                    if _hour_start_utc(row.start, local_tz) < current_hour
                ),
                key=lambda row: row.start,
            )
        )
        target_finalized_hours = tuple(
            row
            for ean in sorted(self._target_hours)
            for row in sorted(self._target_hours[ean].values(), key=lambda r: r.start)
            if _hour_start_utc(row.start, local_tz) < current_hour
        )
        signature: tuple[object, ...] = (
            *finalized,
            *finalized_hours,
            *target_finalized_hours,
            statistics.sale_price,
        )
        if signature == self._history_import_signature:
            return
        try:
            imported_days = async_import_daily_history(
                self.hass,
                sse_id=int(self.config_entry.data[CONF_SSE_ID]),
                sse_name=str(self.config_entry.data[CONF_SSE_NAME]),
                days=statistics.days,
                sale_price=statistics.sale_price,
                today=now.date(),
                local_tz=now.tzinfo,
            )
            imported_hours = async_import_hourly_history(
                self.hass,
                sse_id=int(self.config_entry.data[CONF_SSE_ID]),
                sse_name=str(self.config_entry.data[CONF_SSE_NAME]),
                hours=hours,
                sale_price=statistics.sale_price,
                now=now,
                local_tz=now.tzinfo,
            )
            imported_target_hours = 0
            for ean, ean_hours in self._target_hours.items():
                target_name = ean_name(ean, self.config_entry.options)
                imported_target_hours += async_import_target_hourly_history(
                    self.hass,
                    ean=ean,
                    target_name=target_name,
                    hours=tuple(ean_hours.values()),
                    now=now,
                    local_tz=now.tzinfo,
                )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not import EDC history: %s", err)
            return
        self._history_import_signature = signature
        if imported_days:
            _LOGGER.debug(
                "Queued %s finalized EDC days for long-term statistics",
                imported_days,
            )
        if imported_hours:
            _LOGGER.debug(
                "Queued %s finalized EDC hours for long-term statistics",
                imported_hours,
            )
        if imported_target_hours:
            _LOGGER.debug(
                "Queued %s finalized EDC target hours for long-term statistics",
                imported_target_hours,
            )
