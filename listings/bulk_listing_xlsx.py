"""Parse the first worksheet of a small XLSX bulk-listing upload.

The importer is deliberately conservative: it accepts ordinary text and numbers,
rejects formulas, dates, booleans and error cells, and delegates all row rules
to the format-neutral validator shared with the CSV importer.
"""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.cell.rich_text import CellRichText
from openpyxl.utils.exceptions import InvalidFileException

from .bulk_listing_csv import (
    BULK_LISTING_IMPORT_HEADERS,
    BULK_LISTING_IMPORT_MAX_ERRORS,
    BULK_LISTING_IMPORT_MAX_ROWS,
    BulkListingCsvParseResult,
    BulkListingCsvRowError,
    finish_bulk_listing_result,
    parse_bulk_listing_row,
)

BULK_LISTING_XLSX_MAX_BYTES = 256 * 1024
BULK_LISTING_XLSX_MAX_EXPANDED_BYTES = 2 * 1024 * 1024
UNSUPPORTED_CELL_MESSAGE = '这一格必须是文本或数字，不能使用公式、日期、布尔或错误值。'


def _bounded_content(file) -> bytes | None:
    declared_size = getattr(file, 'size', None)
    if declared_size is not None and declared_size > BULK_LISTING_XLSX_MAX_BYTES:
        return b''
    file.seek(0)
    content = file.read(BULK_LISTING_XLSX_MAX_BYTES + 1)
    if len(content) > BULK_LISTING_XLSX_MAX_BYTES:
        return b''
    return content


def _expanded_size_is_bounded(content: bytes) -> bool:
    """Reject archives whose declared expansion can overwhelm the request parser."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            declared_size = sum(entry.file_size for entry in archive.infolist())
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False
    return 0 <= declared_size <= BULK_LISTING_XLSX_MAX_EXPANDED_BYTES


def _cell_text(cell) -> tuple[str, BulkListingCsvRowError | None]:
    value = cell.value
    if value is None:
        return '', None
    if getattr(cell, 'data_type', '') == 'f':
        return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')
    if isinstance(value, CellRichText):
        return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')
    if isinstance(value, str):
        return value.strip(), None
    if isinstance(value, bool):
        return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')
        if not number.is_finite():
            return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')
        return format(number, 'f'), None
    return '', BulkListingCsvRowError(UNSUPPORTED_CELL_MESSAGE, cell.row, '')


def _with_column(error: BulkListingCsvRowError, column: str) -> BulkListingCsvRowError:
    return BulkListingCsvRowError(error.message, error.row_number, column)


def parse_bulk_listing_xlsx(file) -> BulkListingCsvParseResult:
    """Parse the first worksheet without creating items or evaluating formulas."""
    if file is None:
        return BulkListingCsvParseResult(file_error='请选择要导入的 XLSX 文件。')

    content = _bounded_content(file)
    if not content:
        return BulkListingCsvParseResult(file_error='XLSX 文件不能超过 256 KB，请分批导入。')
    if not content.startswith(b'PK\x03\x04') or not _expanded_size_is_bounded(content):
        return BulkListingCsvParseResult(
            file_error='这不是可读取的 XLSX 工作簿，请重新保存为 .xlsx 后再试。',
        )

    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=False, data_only=False, keep_links=False, rich_text=True,
        )
    except (InvalidFileException, OSError, ValueError, KeyError, ParseError, zipfile.BadZipFile):
        return BulkListingCsvParseResult(
            file_error='XLSX 格式无法解析，请用 Excel 或 WPS 重新保存后再试。',
        )

    rows: list[dict[str, str]] = []
    row_errors: list[BulkListingCsvRowError] = []
    file_error = ''
    try:
        worksheet = workbook.worksheets[0]
        worksheet_rows = worksheet.iter_rows(values_only=False)
        header_cells = next(worksheet_rows, None)
        if header_cells is None:
            return BulkListingCsvParseResult(file_error='XLSX 第一个工作表为空。')

        header_values: list[str] = []
        for cell in header_cells:
            text, cell_error = _cell_text(cell)
            if cell_error is not None:
                header_values.append('')
            else:
                header_values.append(text)
        if tuple(value.strip() for value in header_values) != BULK_LISTING_IMPORT_HEADERS:
            expected = ','.join(BULK_LISTING_IMPORT_HEADERS)
            return BulkListingCsvParseResult(
                file_error=f'XLSX 第一个工作表表头必须固定为：{expected}',
            )

        non_empty_rows = 0
        for cells in worksheet_rows:
            if not any(cell.value is not None and cell.value != '' for cell in cells):
                continue
            non_empty_rows += 1
            row_number = cells[0].row if cells else non_empty_rows + 1
            if len(cells) != len(BULK_LISTING_IMPORT_HEADERS):
                if len(row_errors) < BULK_LISTING_IMPORT_MAX_ERRORS:
                    row_errors.append(BulkListingCsvRowError(
                        '这一行必须正好 7 列，请补齐或删除多余列。', row_number,
                    ))
                continue

            values: dict[str, str] = {}
            cell_errors: list[BulkListingCsvRowError] = []
            for column, cell in zip(BULK_LISTING_IMPORT_HEADERS, cells):
                text, cell_error = _cell_text(cell)
                if cell_error is None:
                    values[column] = text
                else:
                    cell_errors.append(_with_column(cell_error, column))
            if cell_errors:
                row_errors.extend(cell_errors)
                continue

            row, errors = parse_bulk_listing_row(row_number, values)
            if errors:
                row_errors.extend(errors)
            if len(rows) < BULK_LISTING_IMPORT_MAX_ROWS:
                rows.append(row)
            elif not file_error:
                file_error = (
                    f'XLSX 商品行超过 {BULK_LISTING_IMPORT_MAX_ROWS} 行，请分批导入。'
                )

            if non_empty_rows > BULK_LISTING_IMPORT_MAX_ROWS:
                break
    finally:
        workbook.close()

    return finish_bulk_listing_result(
        rows=rows,
        row_errors=row_errors,
        file_error=file_error,
        format_name='XLSX 第一个工作表',
    )
