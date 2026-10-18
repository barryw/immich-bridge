from datetime import date

from immich_bridge.fs_service import _day_names_in_range


def test_month_boundaries_exclude_outside_dates() -> None:
    days = [
        date(2023, 12, 31),
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 1, 15),
        date(2024, 2, 1),
    ]

    result = _day_names_in_range(days, date(2024, 1, 1), date(2024, 2, 1))

    assert result == ["2024-01-15", "2024-01-01"]


def test_leap_year_boundary() -> None:
    days = [date(2024, 2, 29), date(2024, 3, 1)]

    result = _day_names_in_range(days, date(2024, 2, 1), date(2024, 3, 1))

    assert result == ["2024-02-29"]


def test_empty_date_bucket() -> None:
    result = _day_names_in_range([], date(2026, 1, 1), date(2026, 2, 1))

    assert result == []
