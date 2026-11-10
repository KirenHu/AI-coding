#!/usr/bin/env python3
"""Create the blank weekly WBS template from the supplied reference workbook."""
from __future__ import annotations

import copy
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS = ["周期", "分类", "事项", "负责人", "预计完成时间", "状态", "交付物/完成标准", "备注"]


def make_template(reference: Path, destination: Path | None = None) -> Path:
    if destination is None:
        destination = Path(__file__).resolve().parents[1] / "templates" / "周度计划表模板.xlsx"
    source = load_workbook(reference)
    source_ws = source.active
    # The reference's first row is the canonical header and its second row is
    # the canonical data-row style.  Do not copy any values from data rows.
    wb = Workbook()
    ws = wb.active
    ws.title = "周度计划表"
    for col in range(1, 9):
        src_header = source_ws.cell(1, col)
        src_data = source_ws.cell(2, col)
        dst = ws.cell(1, col, HEADERS[col - 1])
        # Copy individual style components: style-array ids belong to the
        # source workbook and cannot safely be reused in a new workbook.
        dst.font = copy.copy(src_header.font)
        dst.fill = copy.copy(src_header.fill)
        dst.border = copy.copy(src_header.border)
        dst.alignment = copy.copy(src_header.alignment)
        dst.protection = copy.copy(src_header.protection)
        dst.number_format = src_header.number_format
        letter = dst.column_letter
        src_dim = source_ws.column_dimensions.get(letter)
        if src_dim is not None:
            if src_dim.width is not None:
                ws.column_dimensions[letter].width = src_dim.width
            ws.column_dimensions[letter].hidden = src_dim.hidden
        else:
            # A style-only column must not acquire openpyxl's default 13-wide
            # custom width when the reference uses Excel's implicit width.
            ws.column_dimensions[letter].width = 0
            ws.column_dimensions[letter].min = col
            ws.column_dimensions[letter].max = col
        # Store the data-row appearance in each column dimension, without
        # creating a data row. Temporary cells let openpyxl register style
        # components in the destination workbook before they are removed.
        tmp = ws.cell(2, col)
        tmp.font = copy.copy(src_data.font)
        tmp.fill = copy.copy(src_data.fill)
        tmp.border = copy.copy(src_data.border)
        tmp.alignment = copy.copy(src_data.alignment)
        tmp.protection = copy.copy(src_data.protection)
        tmp.number_format = src_data.number_format
        ws.column_dimensions[letter]._style = copy.copy(tmp._style)
        del ws._cells[(2, col)]
    ws.row_dimensions[1].height = source_ws.row_dimensions[1].height
    ws.sheet_format.defaultRowHeight = source_ws.row_dimensions[2].height or source_ws.sheet_format.defaultRowHeight
    ws.sheet_view.showGridLines = source_ws.sheet_view.showGridLines

    # Copy the reference status validation exactly (including its allowed
    # values).  Its source includes a legacy "待确认" option; rendering still
    # validates the stricter four statuses requested by the interface.
    for src_dv in source_ws.data_validations.dataValidation:
        if src_dv.type != "list" or "F" not in str(src_dv.sqref):
            continue
        status_validation = copy.deepcopy(src_dv)
        status_validation.sqref = "F2:F1048576"
        ws.add_data_validation(status_validation)
        break
    destination.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destination)
    return destination


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("用法：python3 make_template.py <参考周度计划表.xlsx>")
    print(make_template(Path(sys.argv[1])))
