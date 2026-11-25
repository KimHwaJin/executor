"""Shared worksheet formatting for the deliverable workbooks."""

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.properties import PageSetupProperties

HEAD_FILL = PatternFill("solid", fgColor="D9E1F2")
THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def write_grid(sheet, top: int, header: list[str], body: list[list]) -> int:
    """Write a bordered table starting at row `top`; return the next free row."""
    for offset, values in enumerate([header, *body]):
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=top + offset, column=column, value=value)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if offset == 0:
                cell.font, cell.fill = Font(bold=True), HEAD_FILL
    return top + len(body) + 2


def lay_out(sheet, widths: tuple[int, ...]) -> None:
    """Set column widths and print the sheet one landscape page wide."""
    for index, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + index)].width = width
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
