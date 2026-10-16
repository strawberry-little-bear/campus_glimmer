"""Summarize repeated bulk-listing import errors for the upload page."""

from __future__ import annotations

from dataclasses import dataclass

from .bulk_listing_csv import BulkListingCsvRowError

BULK_LISTING_IMPORT_SUMMARY_MAX_GROUPS = 6


@dataclass(frozen=True)
class BulkListingImportErrorGroup:
    """One repeated column/message problem and the rows that produced it."""

    column: str
    message: str
    row_numbers: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.row_numbers)

    @property
    def column_label(self) -> str:
        return self.column or '整行'

    @property
    def row_numbers_display(self) -> str:
        if not self.row_numbers:
            return '全局'
        if self.row_numbers == (0,):
            return '全局'
        return '、'.join(f'第 {row_number} 行' for row_number in self.row_numbers)


@dataclass(frozen=True)
class BulkListingImportErrorSummary:
    """A presentation-only roll-up; it never replaces row-level errors."""

    total_error_count: int
    affected_row_count: int
    group_count: int
    groups: tuple[BulkListingImportErrorGroup, ...]
    hidden_group_count: int


def build_bulk_listing_import_error_summary(
    errors: list[BulkListingCsvRowError],
) -> BulkListingImportErrorSummary | None:
    """Group bounded parser errors without changing their original meaning."""
    if not errors:
        return None

    unique_errors: list[BulkListingCsvRowError] = []
    seen_errors: set[tuple[int, str, str]] = set()
    rows_by_group: dict[tuple[str, str], list[int]] = {}

    for error in errors:
        error_key = (error.row_number, error.column, error.message)
        if error_key in seen_errors:
            continue
        seen_errors.add(error_key)
        unique_errors.append(error)
        rows_by_group.setdefault((error.column, error.message), []).append(
            error.row_number,
        )

    groups = tuple(
        BulkListingImportErrorGroup(column, message, tuple(row_numbers))
        for (column, message), row_numbers in rows_by_group.items()
    )
    groups = tuple(sorted(
        groups,
        key=lambda group: (
            -group.count,
            group.row_numbers[0] if group.row_numbers else 0,
            group.column,
            group.message,
        ),
    ))

    return BulkListingImportErrorSummary(
        total_error_count=len(unique_errors),
        affected_row_count=len({
            error.row_number for error in unique_errors if error.row_number > 0
        }),
        group_count=len(groups),
        groups=groups[:BULK_LISTING_IMPORT_SUMMARY_MAX_GROUPS],
        hidden_group_count=max(
            0, len(groups) - BULK_LISTING_IMPORT_SUMMARY_MAX_GROUPS,
        ),
    )
