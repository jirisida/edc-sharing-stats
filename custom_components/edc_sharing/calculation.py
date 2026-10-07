"""Pure calculation helpers for EDC profile data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any, Literal


ZERO = Decimal("0")
MAX_PROFILE_DAYS = 31


class IncompleteProfileLayoutError(ValueError):
    """EDC profile data does not contain both sides of electricity sharing."""


@dataclass(frozen=True, slots=True)
class EanInfo:
    """One EAN participating in an EDC sharing group."""

    ean: str
    role: Literal["sharing", "target"]


@dataclass(frozen=True, slots=True)
class DailySharing:
    """Calculated values for one day."""

    day: date
    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    producer_overflow: Decimal
    used_overflow: Decimal
    unused_overflow: Decimal
    coverage: Decimal
    consistency_difference: Decimal


@dataclass(frozen=True, slots=True)
class TargetDailySharing:
    """Calculated consumption values for one target EAN and calendar day."""

    ean: str
    day: date
    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    coverage: Decimal


@dataclass(frozen=True, slots=True)
class TargetHourlySharing:
    """Calculated hourly values for one target EAN."""

    ean: str
    start: datetime
    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    coverage: Decimal


@dataclass(frozen=True, slots=True)
class HourlySharing:
    """Calculated values for one clock hour."""

    start: datetime
    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    producer_overflow: Decimal
    used_overflow: Decimal
    unused_overflow: Decimal
    coverage: Decimal
    consistency_difference: Decimal


@dataclass(frozen=True, slots=True)
class SurplusUtilization:
    """Surplus utilization and the energy values used to calculate it."""

    value: Decimal
    shared: Decimal
    production_surplus: Decimal
    unused_surplus: Decimal
    data_start: date | None
    data_end: date | None
    available_days: int


@dataclass(frozen=True, slots=True)
class SharingStatistics:
    """Statistics exposed by the integration."""

    days: tuple[DailySharing, ...]
    today: DailySharing
    latest: DailySharing
    latest_day: date | None
    month_consumption: Decimal
    month_grid_purchase: Decimal
    month_shared: Decimal
    month_overflow: Decimal
    month_unused: Decimal
    month_coverage: Decimal
    month_revenue: Decimal
    today_revenue: Decimal
    sale_price: Decimal
    surplus_utilization_latest_available_day: SurplusUtilization
    surplus_utilization_this_week: SurplusUtilization
    surplus_utilization_this_month: SurplusUtilization
    surplus_utilization_this_year: SurplusUtilization
    surplus_utilization_total: SurplusUtilization
    target_statistics: tuple[TargetSharingStatistics, ...]


@dataclass(frozen=True, slots=True)
class PeriodSummary:
    """Aggregate sharing values for an arbitrary report period."""

    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    producer_overflow: Decimal
    unused_overflow: Decimal
    coverage: Decimal
    revenue: Decimal


@dataclass(frozen=True, slots=True)
class TargetPeriodSummary:
    """Aggregate values for one target EAN over an available period."""

    consumption: Decimal
    grid_purchase: Decimal
    shared: Decimal
    coverage: Decimal
    revenue: Decimal
    data_start: date | None
    data_end: date | None
    available_days: int


@dataclass(frozen=True, slots=True)
class TargetSharingStatistics:
    """Current and historical summaries for one target EAN."""

    ean: str
    latest_day: date | None
    latest: TargetPeriodSummary
    week: TargetPeriodSummary
    month: TargetPeriodSummary
    year: TargetPeriodSummary
    total: TargetPeriodSummary
    sale_price: Decimal


def _decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise ValueError("EDC vrátilo neplatnou číselnou hodnotu.") from err
    return parsed if parsed.is_finite() else ZERO


def calculate_surplus_utilization(
    days: tuple[DailySharing, ...],
) -> SurplusUtilization:
    """Calculate how much production surplus was used for sharing."""
    ordered = tuple(sorted(days, key=lambda row: row.day))
    shared = sum((row.shared for row in ordered), ZERO)
    production_surplus = sum((row.producer_overflow for row in ordered), ZERO)
    unused_surplus = sum((row.unused_overflow for row in ordered), ZERO)
    value = (
        shared / production_surplus * Decimal("100")
        if shared > ZERO and production_surplus > ZERO
        else ZERO
    )
    return SurplusUtilization(
        value=value,
        shared=shared,
        production_surplus=production_surplus,
        unused_surplus=unused_surplus,
        data_start=ordered[0].day if ordered else None,
        data_end=ordered[-1].day if ordered else None,
        available_days=len(ordered),
    )


def two_calendar_month_start(today: date) -> date:
    """Return the first day of the previous calendar month."""
    current_month = today.replace(day=1)
    return (current_month - timedelta(days=1)).replace(day=1)


def profile_date_ranges(
    date_from: date, date_to: date
) -> tuple[tuple[date, date], ...]:
    """Split a half-open date interval into EDC-compatible requests."""
    if date_to <= date_from:
        return ()
    ranges: list[tuple[date, date]] = []
    chunk_from = date_from
    while chunk_from < date_to:
        chunk_to = min(chunk_from + timedelta(days=MAX_PROFILE_DAYS), date_to)
        ranges.append((chunk_from, chunk_to))
        chunk_from = chunk_to
    return tuple(ranges)


def profile_date_ranges_backwards(
    date_from: date, date_to: date
) -> tuple[tuple[date, date], ...]:
    """Split an interval into newest-first EDC-compatible requests."""
    if date_to <= date_from:
        return ()
    ranges: list[tuple[date, date]] = []
    chunk_to = date_to
    while chunk_to > date_from:
        chunk_from = max(date_from, chunk_to - timedelta(days=MAX_PROFILE_DAYS))
        ranges.append((chunk_from, chunk_to))
        chunk_to = chunk_from
    return tuple(ranges)


def one_calendar_year_ago(today: date) -> date:
    """Return the same calendar date one year earlier."""
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return today.replace(year=today.year - 1, day=28)


def report_date_range(
    period: str, today: date
) -> tuple[date, date]:
    """Return the half-open range requested by an email report."""
    if period == "weekly":
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end
    if period == "monthly":
        end = today.replace(day=1)
        return (end - timedelta(days=1)).replace(day=1), end
    if period == "yearly":
        return date(today.year, 1, 1), today + timedelta(days=1)
    raise ValueError(f"Unsupported report period: {period}")


def _profile_layout(
    response: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """Return content and the producer/consumer column mappings."""
    columns = response.get("valueColumns") or []
    content = response.get("content") or []
    if not content:
        return columns, content, {}, {}
    if not columns:
        raise ValueError("EDC nevrátilo popis profilových dat.")

    producers: dict[str, dict[str, int]] = {}
    consumers: dict[str, dict[str, int]] = {}
    for index, column in enumerate(columns):
        ean = str(column.get("ean", ""))
        direction = str(column.get("dir", "")).upper()
        role = str(column.get("type", "")).upper()
        target = producers if role == "D" else consumers if role == "O" else None
        if target is not None and direction in ("IN", "OUT"):
            target.setdefault(ean, {})[direction] = index
    if not producers or not consumers:
        raise IncompleteProfileLayoutError(
            "V odpovědi EDC nebyl rozpoznán výrobní a odběrný EAN."
        )
    return columns, content, producers, consumers


def extract_eans(response: dict[str, Any]) -> tuple[EanInfo, ...]:
    """Return every distinct sharing and target EAN in an EDC profile."""
    found: set[EanInfo] = set()
    for column in response.get("valueColumns") or []:
        ean = str(column.get("ean") or "").strip()
        role = str(column.get("type") or "").upper()
        if not ean:
            continue
        if role == "D":
            found.add(EanInfo(ean, "sharing"))
        elif role == "O":
            found.add(EanInfo(ean, "target"))
    return tuple(sorted(found, key=lambda item: (item.role, item.ean)))


def _add_values(
    target: list[Decimal], values: list[Any], column_count: int
) -> None:
    """Add one EDC interval to an aggregate value vector."""
    for index, raw in enumerate(values[:column_count]):
        target[index] += _decimal(raw.get("v") if isinstance(raw, dict) else raw)


def _sharing_values(
    values: list[Decimal],
    producers: dict[str, dict[str, int]],
    consumers: dict[str, dict[str, int]],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Calculate sharing values from aggregated EDC value columns."""
    def value(index: int | None) -> Decimal:
        if index is None or index >= len(values):
            return ZERO
        return values[index]

    producer_in = sum((value(pair.get("IN")) for pair in producers.values()), ZERO)
    producer_out = sum((value(pair.get("OUT")) for pair in producers.values()), ZERO)
    consumer_in = sum((abs(value(pair.get("IN"))) for pair in consumers.values()), ZERO)
    consumer_out = sum((abs(value(pair.get("OUT"))) for pair in consumers.values()), ZERO)
    shared_consumer = consumer_in - consumer_out
    shared_producer = producer_in - producer_out
    coverage = shared_consumer / consumer_in * Decimal("100") if consumer_in else ZERO
    return (
        consumer_in,
        consumer_out,
        shared_consumer,
        producer_in,
        shared_producer,
        producer_out,
        coverage,
        abs(shared_consumer - shared_producer),
    )


def _target_values(
    values: list[Decimal], pair: dict[str, int]
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Calculate consumption and sharing values for one target EAN."""

    def value(index: int | None) -> Decimal:
        if index is None or index >= len(values):
            return ZERO
        return values[index]

    consumption = abs(value(pair.get("IN")))
    grid_purchase = abs(value(pair.get("OUT")))
    shared = consumption - grid_purchase
    coverage = shared / consumption * Decimal("100") if consumption else ZERO
    return consumption, grid_purchase, shared, coverage


def _daily_value_totals(
    columns: list[dict[str, Any]], content: list[dict[str, Any]]
) -> dict[date, list[Decimal]]:
    """Aggregate every EDC interval into its local calendar day."""
    values_by_day: dict[date, list[Decimal]] = {}
    for item in content:
        item_date = date.fromisoformat(str(item["date"])[:10])
        values = item.get("values") or []
        totals = values_by_day.setdefault(item_date, [ZERO] * len(columns))
        _add_values(totals, values, len(columns))
    return values_by_day


def parse_daily_profile(response: dict[str, Any]) -> tuple[DailySharing, ...]:
    """Aggregate standard profile rows into calendar days."""
    columns, content, producers, consumers = _profile_layout(response)
    if not content:
        return ()

    # The overview endpoint may return quarter-hour rows even when DAILY is
    # requested. Aggregate every value column by calendar day before deriving
    # sharing totals; otherwise the coordinator would retain only the final
    # interval of each day.
    values_by_day = _daily_value_totals(columns, content)

    daily: list[DailySharing] = []
    for item_date in sorted(values_by_day):
        values = _sharing_values(values_by_day[item_date], producers, consumers)
        daily.append(
            DailySharing(
                day=item_date,
                consumption=values[0],
                grid_purchase=values[1],
                shared=values[2],
                producer_overflow=values[3],
                used_overflow=values[4],
                unused_overflow=values[5],
                coverage=values[6],
                consistency_difference=values[7],
            )
        )

    return tuple(daily)


def parse_daily_target_profiles(
    response: dict[str, Any],
) -> tuple[TargetDailySharing, ...]:
    """Return daily values for every target EAN in an EDC profile response."""
    columns, content, _producers, consumers = _profile_layout(response)
    if not content:
        return ()

    values_by_day = _daily_value_totals(columns, content)
    daily: list[TargetDailySharing] = []
    for item_date in sorted(values_by_day):
        values = values_by_day[item_date]
        for ean, pair in sorted(consumers.items()):
            consumption, grid_purchase, shared, coverage = _target_values(values, pair)
            daily.append(
                TargetDailySharing(
                    ean=ean,
                    day=item_date,
                    consumption=consumption,
                    grid_purchase=grid_purchase,
                    shared=shared,
                    coverage=coverage,
                )
            )
    return tuple(daily)


def _local_hour_as_utc(
    local_hour: datetime,
    local_tz: tzinfo,
    occurrence: int,
) -> datetime:
    """Resolve one EDC wall-clock hour, including DST folds, to UTC."""
    first = local_hour.replace(tzinfo=local_tz, fold=0)
    second = local_hour.replace(tzinfo=local_tz, fold=1)
    first_exists = (
        first.astimezone(UTC).astimezone(local_tz).replace(tzinfo=None)
        == local_hour
    )
    second_exists = (
        second.astimezone(UTC).astimezone(local_tz).replace(tzinfo=None)
        == local_hour
    )
    if not first_exists and not second_exists:
        raise ValueError(
            f"EDC vrátilo neexistující lokální čas {local_hour.isoformat()}."
        )
    ambiguous = (
        first_exists
        and second_exists
        and first.utcoffset() != second.utcoffset()
    )
    resolved = second if ambiguous and occurrence else first
    return resolved.astimezone(UTC)


def parse_hourly_profile(
    response: dict[str, Any], *, local_tz: tzinfo | None = None
) -> tuple[HourlySharing, ...]:
    """Aggregate quarter-hour standard profile rows into clock hours."""
    columns, content, producers, consumers = _profile_layout(response)
    if not content:
        return ()

    values_by_hour: dict[datetime, list[Decimal]] = {}
    interval_occurrences: dict[tuple[date, str], int] = {}
    for item in content:
        start_text = str(item.get("start") or "")
        hour_text = start_text.partition(":")[0]
        if not hour_text.isdigit():
            continue
        hour = int(hour_text)
        if not 0 <= hour <= 23:
            continue
        item_date = date.fromisoformat(str(item["date"])[:10])
        interval_key = (item_date, start_text)
        occurrence = interval_occurrences.get(interval_key, 0)
        interval_occurrences[interval_key] = occurrence + 1
        local_hour = datetime.combine(item_date, datetime.min.time()).replace(hour=hour)
        hour_start = (
            _local_hour_as_utc(local_hour, local_tz, occurrence)
            if local_tz is not None
            else local_hour
        )
        totals = values_by_hour.setdefault(hour_start, [ZERO] * len(columns))
        _add_values(totals, item.get("values") or [], len(columns))

    hourly: list[HourlySharing] = []
    for hour_start in sorted(values_by_hour):
        values = _sharing_values(values_by_hour[hour_start], producers, consumers)
        hourly.append(
            HourlySharing(
                start=hour_start,
                consumption=values[0],
                grid_purchase=values[1],
                shared=values[2],
                producer_overflow=values[3],
                used_overflow=values[4],
                unused_overflow=values[5],
                coverage=values[6],
                consistency_difference=values[7],
            )
        )
    return tuple(hourly)


def parse_hourly_target_profiles(
    response: dict[str, Any], *, local_tz: tzinfo | None = None
) -> tuple[TargetHourlySharing, ...]:
    """Aggregate quarter-hour standard profile rows into clock hours for each target."""
    columns, content, _producers, consumers = _profile_layout(response)
    if not content:
        return ()

    values_by_hour: dict[datetime, list[Decimal]] = {}
    interval_occurrences: dict[tuple[date, str], int] = {}
    for item in content:
        start_text = str(item.get("start") or "")
        hour_text = start_text.partition(":")[0]
        if not hour_text.isdigit():
            continue
        hour = int(hour_text)
        if not 0 <= hour <= 23:
            continue
        item_date = date.fromisoformat(str(item["date"])[:10])
        interval_key = (item_date, start_text)
        occurrence = interval_occurrences.get(interval_key, 0)
        interval_occurrences[interval_key] = occurrence + 1
        local_hour = datetime.combine(item_date, datetime.min.time()).replace(hour=hour)
        hour_start = (
            _local_hour_as_utc(local_hour, local_tz, occurrence)
            if local_tz is not None
            else local_hour
        )
        totals = values_by_hour.setdefault(hour_start, [ZERO] * len(columns))
        _add_values(totals, item.get("values") or [], len(columns))

    hourly: list[TargetHourlySharing] = []
    for hour_start in sorted(values_by_hour):
        values = values_by_hour[hour_start]
        for ean, pair in sorted(consumers.items()):
            consumption, grid_purchase, shared, coverage = _target_values(values, pair)
            hourly.append(
                TargetHourlySharing(
                    ean=ean,
                    start=hour_start,
                    consumption=consumption,
                    grid_purchase=grid_purchase,
                    shared=shared,
                    coverage=coverage,
                )
            )
    return tuple(hourly)


def calculate_target_period_summary(
    days: tuple[TargetDailySharing, ...], sale_price: Decimal
) -> TargetPeriodSummary:
    """Sum available rows for one target EAN without rounding inputs."""
    ordered = tuple(sorted(days, key=lambda row: row.day))
    consumption = sum((row.consumption for row in ordered), ZERO)
    shared = sum((row.shared for row in ordered), ZERO)
    return TargetPeriodSummary(
        consumption=consumption,
        grid_purchase=sum((row.grid_purchase for row in ordered), ZERO),
        shared=shared,
        coverage=shared / consumption * Decimal("100") if consumption else ZERO,
        revenue=shared * sale_price,
        data_start=ordered[0].day if ordered else None,
        data_end=ordered[-1].day if ordered else None,
        available_days=len(ordered),
    )


def calculate_target_statistics(
    ean: str,
    days: tuple[TargetDailySharing, ...],
    sale_price: Decimal,
    today: date,
) -> TargetSharingStatistics:
    """Calculate latest and calendar summaries for one target EAN."""
    available = tuple(sorted((row for row in days if row.day <= today), key=lambda row: row.day))
    latest = calculate_target_period_summary(
        (available[-1],) if available else (), sale_price
    )
    week_start = today - timedelta(days=today.weekday())
    return TargetSharingStatistics(
        ean=ean,
        latest_day=latest.data_end,
        latest=latest,
        week=calculate_target_period_summary(
            tuple(row for row in available if row.day >= week_start), sale_price
        ),
        month=calculate_target_period_summary(
            tuple(
                row
                for row in available
                if row.day.year == today.year and row.day.month == today.month
            ),
            sale_price,
        ),
        year=calculate_target_period_summary(
            tuple(row for row in available if row.day.year == today.year), sale_price
        ),
        total=calculate_target_period_summary(available, sale_price),
        sale_price=sale_price,
    )


def calculate_statistics(
    days: tuple[DailySharing, ...],
    sale_price: Decimal,
    today: date,
    *,
    target_days: Mapping[str, tuple[TargetDailySharing, ...]] | None = None,
    target_prices: Mapping[str, Decimal] | None = None,
) -> SharingStatistics:
    """Calculate current values from already parsed daily rows."""
    daily = sorted(days, key=lambda row: row.day)
    empty_today = DailySharing(today, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO)
    today_row = next((row for row in daily if row.day == today), empty_today)
    available_rows = [row for row in daily if row.day <= today]
    latest_row = available_rows[-1] if available_rows else empty_today
    latest_day = latest_row.day if available_rows else None
    week_start = today - timedelta(days=today.weekday())
    week_rows = [row for row in available_rows if row.day >= week_start]
    month_rows = [
        row
        for row in available_rows
        if row.day.year == today.year and row.day.month == today.month
    ]
    year_rows = [row for row in available_rows if row.day.year == today.year]
    month_consumption = sum((row.consumption for row in month_rows), ZERO)
    month_shared = sum((row.shared for row in month_rows), ZERO)
    month_coverage = month_shared / month_consumption * Decimal("100") if month_consumption else ZERO
    targets = tuple(
        calculate_target_statistics(
            ean,
            rows,
            (target_prices or {}).get(ean, sale_price),
            today,
        )
        for ean, rows in sorted((target_days or {}).items())
    )
    return SharingStatistics(
        days=tuple(daily),
        today=today_row,
        latest=latest_row,
        latest_day=latest_day,
        month_consumption=month_consumption,
        month_grid_purchase=sum((row.grid_purchase for row in month_rows), ZERO),
        month_shared=month_shared,
        month_overflow=sum((row.producer_overflow for row in month_rows), ZERO),
        month_unused=sum((row.unused_overflow for row in month_rows), ZERO),
        month_coverage=month_coverage,
        month_revenue=month_shared * sale_price,
        today_revenue=today_row.shared * sale_price,
        sale_price=sale_price,
        surplus_utilization_latest_available_day=calculate_surplus_utilization(
            (latest_row,) if latest_day is not None else ()
        ),
        surplus_utilization_this_week=calculate_surplus_utilization(
            tuple(week_rows)
        ),
        surplus_utilization_this_month=calculate_surplus_utilization(
            tuple(month_rows)
        ),
        surplus_utilization_this_year=calculate_surplus_utilization(
            tuple(year_rows)
        ),
        surplus_utilization_total=calculate_surplus_utilization(
            tuple(available_rows)
        ),
        target_statistics=targets,
    )


def calculate_period_summary(
    days: tuple[DailySharing, ...], sale_price: Decimal
) -> PeriodSummary:
    """Sum daily rows for an arbitrary report period."""
    consumption = sum((row.consumption for row in days), ZERO)
    shared = sum((row.shared for row in days), ZERO)
    return PeriodSummary(
        consumption=consumption,
        grid_purchase=sum((row.grid_purchase for row in days), ZERO),
        shared=shared,
        producer_overflow=sum((row.producer_overflow for row in days), ZERO),
        unused_overflow=sum((row.unused_overflow for row in days), ZERO),
        coverage=shared / consumption * Decimal("100") if consumption else ZERO,
        revenue=shared * sale_price,
    )


def calculate_profile(response: dict[str, Any], sale_price: Decimal, today: date) -> SharingStatistics:
    """Calculate sharing from one standard profile overview response."""
    return calculate_statistics(parse_daily_profile(response), sale_price, today)
