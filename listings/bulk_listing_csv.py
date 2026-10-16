"""Parse the UTF-8 CSV used by the graduation bulk-listing flow.

The parser deliberately has no Django form or database dependency.  It turns an
uploaded file into plain row-initial values and user-facing errors, so the view
can decide whether to render a preview or report file/row problems.  Row
validation is shared with the XLSX importer.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

BULK_LISTING_IMPORT_HEADERS = ('标题', '描述', '交易方式', '价格', '押金', '借用天数', '成色')
BULK_LISTING_IMPORT_MAX_ROWS = 20
BULK_LISTING_IMPORT_MAX_ERRORS = 50

# Historical public names kept for the first CSV-focused iteration.
BULK_LISTING_CSV_HEADERS = BULK_LISTING_IMPORT_HEADERS
BULK_LISTING_CSV_MAX_BYTES = 128 * 1024
BULK_LISTING_CSV_MAX_ROWS = BULK_LISTING_IMPORT_MAX_ROWS
BULK_LISTING_CSV_MAX_ERRORS = BULK_LISTING_IMPORT_MAX_ERRORS

TRADE_MODE_LABELS = {
    '出售': 'sale',
    '免费赠送': 'free',
    '限期借用': 'borrow',
}


@dataclass(frozen=True)
class BulkListingCsvRowError:
    """A row/column located import error that can be shown next to the upload form."""

    message: str
    row_number: int = 0
    column: str = ''

    @property
    def display(self) -> str:
        if not self.row_number:
            return self.message
        if self.column:
            return f'第 {self.row_number} 行「{self.column}」：{self.message}'
        return f'第 {self.row_number} 行：{self.message}'


@dataclass(frozen=True)
class BulkListingCsvParseResult:
    """Values suitable for ``BulkListingRowFormSet(initial=...)`` plus errors."""

    rows: list[dict[str, str]] = field(default_factory=list)
    row_errors: list[BulkListingCsvRowError] = field(default_factory=list)
    file_error: str = ''

    @property
    def can_preview(self) -> bool:
        return not self.file_error and not self.row_errors and bool(self.rows)


def _read_utf8(file) -> tuple[bytes | None, str]:
    """Read a bounded upload and decode strict UTF-8, with or without a BOM."""
    file.seek(0)
    content = file.read(BULK_LISTING_CSV_MAX_BYTES + 1)
    if len(content) > BULK_LISTING_CSV_MAX_BYTES:
        return None, 'CSV 文件不能超过 128 KB，请分批导入。'
    try:
        return content, content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None, 'CSV 必须使用 UTF-8 编码保存；Excel 请先另存为「CSV UTF-8」。'


def _valid_decimal(value: str, *, allow_blank: bool) -> tuple[Decimal | None, str | None]:
    value = value.strip()
    if not value:
        return (None, None) if allow_blank else (None, '这一列不能留空。')
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None, '请填写数字，最多两位小数。'
    if not number.is_finite() or number < 0:
        return None, '请填写不小于 0 的数字。'
    if -number.as_tuple().exponent > 2:
        return None, '金额最多保留两位小数。'
    if number >= Decimal('100000000'):
        return None, '金额不能超过 99,999,999.99。'
    return number, None


def parse_bulk_listing_row(
    row_number: int, values: dict[str, str]
) -> tuple[dict[str, str] | None, list[BulkListingCsvRowError]]:
    """Validate one format-neutral row; the final Django formset still revalidates."""
    errors: list[BulkListingCsvRowError] = []

    def add(column: str, message: str) -> None:
        if len(errors) < BULK_LISTING_IMPORT_MAX_ERRORS:
            errors.append(BulkListingCsvRowError(message, row_number, column))

    title = values['标题'].strip()
    if not title:
        add('标题', '这一列不能留空。')
    elif len(title) > 200:
        add('标题', '不能超过 200 个字符。')

    description = values['描述'].strip()
    if not description:
        add('描述', '这一列不能留空。')

    trade_label = values['交易方式'].strip()
    trade_mode = TRADE_MODE_LABELS.get(trade_label)
    if trade_label and trade_mode is None:
        add('交易方式', '只能填写出售、免费赠送或限期借用。')
    trade_mode = trade_mode or 'sale'

    price_text = values['价格'].strip()
    _, price_error = _valid_decimal(price_text, allow_blank=trade_mode != 'sale')
    if price_error:
        add('价格', price_error)

    deposit_text = values['押金'].strip()
    _, deposit_error = _valid_decimal(deposit_text, allow_blank=True)
    if deposit_error:
        add('押金', deposit_error)

    borrow_days_text = values['借用天数'].strip()
    if borrow_days_text:
        try:
            borrow_days = int(borrow_days_text)
        except ValueError:
            borrow_days = None
        if borrow_days is None or not 1 <= borrow_days <= 90:
            add('借用天数', '请填写 1 到 90 之间的整数。')

    condition = values['成色'].strip()
    if not condition:
        add('成色', '这一列不能留空。')
    elif len(condition) > 100:
        add('成色', '不能超过 100 个字符。')

    row = {
        'title': title,
        'description': description,
        'trade_mode': trade_mode,
        'price': price_text,
        'deposit_amount': deposit_text,
        'borrow_days': borrow_days_text,
        'condition': condition,
    }
    return row, errors


def finish_bulk_listing_result(
    *,
    rows: list[dict[str, str]],
    row_errors: list[BulkListingCsvRowError],
    file_error: str,
    format_name: str,
) -> BulkListingCsvParseResult:
    """Apply the shared empty-row and bounded-error rules used by both importers."""
    if not rows and not row_errors and not file_error:
        row_errors.append(BulkListingCsvRowError(f'{format_name} 中至少需要一行非空商品。'))
    if len(row_errors) > BULK_LISTING_IMPORT_MAX_ERRORS:
        row_errors = row_errors[:BULK_LISTING_IMPORT_MAX_ERRORS - 1]
        row_errors.append(BulkListingCsvRowError('错误过多，请先修正以上问题后再重新上传。'))
    return BulkListingCsvParseResult(
        rows=rows,
        row_errors=row_errors,
        file_error=file_error,
    )


def parse_bulk_listing_csv(file) -> BulkListingCsvParseResult:
    """Parse one uploaded CSV without touching the database."""
    if file is None:
        return BulkListingCsvParseResult(file_error='请选择要导入的 CSV 文件。')

    declared_size = getattr(file, 'size', None)
    if declared_size is not None and declared_size > BULK_LISTING_CSV_MAX_BYTES:
        return BulkListingCsvParseResult(file_error='CSV 文件不能超过 128 KB，请分批导入。')

    content, text = _read_utf8(file)
    if content is None:
        return BulkListingCsvParseResult(file_error=text)
    if '\x00' in text:
        return BulkListingCsvParseResult(file_error='CSV 中包含无效字符，请重新导出后再试。')

    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        header = next(reader, None)
    except csv.Error:
        return BulkListingCsvParseResult(file_error='CSV 格式无法解析，请检查引号是否成对。')

    if header is None:
        return BulkListingCsvParseResult(file_error='CSV 文件为空。')
    if tuple(cell.strip() for cell in header) != BULK_LISTING_IMPORT_HEADERS:
        expected = ','.join(BULK_LISTING_IMPORT_HEADERS)
        return BulkListingCsvParseResult(
            file_error=f'CSV 表头必须固定为：{expected}',
        )

    rows: list[dict[str, str]] = []
    row_errors: list[BulkListingCsvRowError] = []
    file_error = ''
    try:
        for row_number, raw_row in enumerate(reader, start=2):
            if len(raw_row) != len(BULK_LISTING_IMPORT_HEADERS):
                row_errors.append(BulkListingCsvRowError(
                    '这一行必须正好 7 列，请补齐或删除多余列。', row_number,
                ))
                continue
            values = dict(zip(BULK_LISTING_IMPORT_HEADERS, raw_row))
            if not any(value.strip() for value in values.values()):
                continue
            row, errors = parse_bulk_listing_row(row_number, values)
            if errors:
                row_errors.extend(errors)
            if len(rows) < BULK_LISTING_IMPORT_MAX_ROWS:
                rows.append(row)
            elif not file_error:
                file_error = f'CSV 商品行超过 {BULK_LISTING_IMPORT_MAX_ROWS} 行，请分批导入。'
    except csv.Error:
        return BulkListingCsvParseResult(
            rows=rows,
            row_errors=row_errors,
            file_error='CSV 格式无法解析，请检查引号是否成对。',
        )

    return finish_bulk_listing_result(
        rows=rows,
        row_errors=row_errors,
        file_error=file_error,
        format_name='CSV',
    )
