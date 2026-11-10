#!/usr/bin/env python3
"""Render a weekly WBS workbook and its evidence markdown report."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

HEADERS = ["周期", "分类", "事项", "负责人", "预计完成时间", "状态", "交付物/完成标准", "备注"]
ALLOWED_STATUS = {"已完成", "进行中", "待开始", "暂停"}
HELP_STRUCTURE = {
    "name": "张三",
    "iso_week": "2026-W46",
    "updates": [{"match": {"周期": "Week11（11/02-11/06）", "事项": "原文"}, "set": {"状态": "已完成", "交付物/完成标准": "...", "备注": "..."}}],
    "new_rows": [{"周期": "...", "分类": "...", "事项": "...", "负责人": "...", "预计完成时间": "2026-11-13", "状态": "待开始", "交付物/完成标准": "...", "备注": "..."}],
    "md": {"pending": ["待确认事项..."], "gaps": ["来源缺口..."], "evidence": {"事项原文": ["E3", "E7"]}},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an 8-column weekly WBS workbook and markdown report.",
        epilog="rows.json structure (JSON):\n" + json.dumps(HELP_STRUCTURE, ensure_ascii=False, indent=2),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--rows", required=True, help="Path to rows.json")
    parser.add_argument("--base", default="", help="Base xlsx; empty/omitted uses ../templates/周度计划表模板.xlsx")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    return parser.parse_args()


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def parse_date(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            fail(f"预计完成时间不是有效日期 YYYY-MM-DD：{value!r}")
    if value in (None, ""):
        return value
    fail(f"预计完成时间必须是 YYYY-MM-DD 日期：{value!r}")


def find_header(ws: Worksheet) -> tuple[int, int]:
    for row in ws.iter_rows():
        values = [cell.value for cell in row]
        if "周期" in values and "事项" in values:
            start = values.index("周期")
            if values[start : start + len(HEADERS)] == HEADERS and len(values) == start + len(HEADERS):
                return row[0].row, start + 1
            fail(f"表头必须恰好8列且严格为：{','.join(HEADERS)}")
    fail("找不到同时含有“周期”和“事项”的表头行")


def copy_row_style(ws: Worksheet, source_row: int, destination_row: int, start_col: int = 1, use_column_defaults: bool = False) -> None:
    for col in range(start_col, start_col + 8):
        source = ws.cell(source_row, col)
        target = ws.cell(destination_row, col)
        if use_column_defaults and ws.column_dimensions[target.column_letter]._style is not None:
            # The template has no data row; its column dimension carries the
            # copied reference data-row style. Do not overwrite it with the
            # header cell's style below.
            target._style = copy.copy(ws.column_dimensions[target.column_letter]._style)
        else:
            if source.has_style:
                target._style = copy.copy(source._style)
            target.number_format = source.number_format
            target.alignment = copy.copy(source.alignment)
            target.font = copy.copy(source.font)
            target.fill = copy.copy(source.fill)
            target.border = copy.copy(source.border)
            target.protection = copy.copy(source.protection)
    if source_row in ws.row_dimensions and ws.row_dimensions[source_row].height is not None:
        ws.row_dimensions[destination_row].height = ws.row_dimensions[source_row].height
    elif use_column_defaults and ws.sheet_format.defaultRowHeight is not None:
        ws.row_dimensions[destination_row].height = ws.sheet_format.defaultRowHeight


def fit_row_height(ws: Worksheet, row: int, start_col: int) -> None:
    """按单元格内容估算行高，避免多行备注被截断（中文按 1 字宽 ≈ 2 个列宽单位估算）。"""
    from openpyxl.utils import get_column_letter
    lines = 1
    for i in range(8):
        col = start_col + i
        value = ws.cell(row, col).value
        if value is None:
            continue
        width = ws.column_dimensions[get_column_letter(col)].width or 10
        chars_per_line = max(int(width / 2), 1)
        count = sum(max(1, -(-len(part) // chars_per_line)) for part in str(value).split("\n"))
        lines = max(lines, count)
    base = ws.row_dimensions[row].height or 16
    ws.row_dimensions[row].height = max(base, lines * 16 + 4)


def choose_output_pair(xlsx_base: Path, md_base: Path) -> tuple[Path, Path]:
    """Reserve matching xlsx/markdown version suffixes without overwriting."""
    for index in range(1, 1000000):
        suffix = "" if index == 1 else f"-v{index}"
        xlsx = xlsx_base.with_name(xlsx_base.stem + suffix + xlsx_base.suffix)
        md = md_base.with_name(md_base.stem + suffix + md_base.suffix)
        if xlsx.exists() or md.exists():
            continue
        try:
            xfd = os.open(xlsx, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(xfd)
            try:
                mfd = os.open(md, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(mfd)
            except FileExistsError:
                # Preserve our reservation; another writer won the paired MD.
                # Never delete or overwrite a path after concurrent activity.
                continue
            return xlsx, md
        except FileExistsError:
            continue
    raise RuntimeError("无法为输出文件分配唯一版本")


def ensure_validation_for_new_rows(ws: Worksheet, first: int, last: int) -> None:
    if last < first:
        return
    # A template validation already spans the future rows. For a real base,
    # preserve its existing sqref and add only the appended range when needed.
    for dv in ws.data_validations.dataValidation:
        if dv.type == "list" and any(str(rng).startswith("F") for rng in dv.sqref.ranges):
            ranges = [str(rng) for rng in dv.sqref.ranges]
            if not any(str(rng) == f"F{first}:F{last}" for rng in dv.sqref.ranges):
                dv.add(f"F{first}:F{last}")
            return


def find_sheet_and_header(wb) -> tuple[Worksheet, int, int]:
    for sheet in wb.worksheets:
        try:
            header, start = find_header(sheet)
            return sheet, header, start
        except SystemExit:
            continue
    fail("所有工作表均找不到严格8列表头")


def choose_output(path: Path) -> Path:
    """Reserve a non-existing path without ever overwriting an earlier run."""
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path if index == 1 else path.with_name(f"{stem}-v{index}{suffix}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            index += 1


def row_dict(ws: Worksheet, row: int, start_col: int) -> dict[str, Any]:
    return {HEADERS[i]: ws.cell(row, start_col + i).value for i in range(8)}


def md_value(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def markdown_report(rows: dict[str, Any], ws: Worksheet, header_row: int, start_col: int, updated_rows: list[int]) -> str:
    md = rows.get("md") or {}
    pending = md.get("pending") or []
    gaps = md.get("gaps") or []
    evidence = md.get("evidence") or {}
    name = as_text(rows.get("name")) or "未命名"
    iso_week = as_text(rows.get("iso_week")) or "未知周次"
    out: list[str] = [f"# 周度计划表-{name}-{iso_week}", ""]
    out += ["## 待确认", ""]
    if pending:
        out += [f"- {item}" for item in pending]
    else:
        out.append("- 无")
    out += ["", "## 来源缺口", ""]
    if gaps:
        out += [f"- {item}" for item in gaps]
    else:
        out.append("- 无")

    def table(title: str, records: list[dict[str, Any]]) -> None:
        out.extend(["", f"## {title}", "", "|" + "|".join(HEADERS + ["证据"]) + "|", "|" + "|".join(["---"] * 9) + "|"])
        if not records:
            out.append("|" + "|".join([""] * 9) + "|")
            return
        for rec in records:
            item = as_text(rec.get("事项"))
            refs = evidence.get(item, [])
            refs_text = ", ".join(map(str, refs)) if isinstance(refs, list) else as_text(refs)
            out.append("|" + "|".join(md_value(rec.get(h)) for h in HEADERS + ["证据"][:-1]) + "|" + md_value(refs_text) + "|")

    update_records = [row_dict(ws, r, start_col) for r in updated_rows]
    new_records = rows.get("new_rows") or []
    if update_records:
        table("上周回顾", update_records)
    else:
        out.extend(["", "## 上周回顾", "", "首次生成，无上周行可回顾。"])
    table("本周规划", [dict((h, rec.get(h)) for h in HEADERS) for rec in new_records])
    return "\n".join(out) + "\n"


def render(payload: dict[str, Any], base_arg: str, out_dir: Path) -> tuple[Path, Path]:
    script_dir = Path(__file__).resolve().parent
    template = script_dir.parent / "templates" / "周度计划表模板.xlsx"
    base = Path(base_arg) if base_arg else template
    if not base.exists():
        fail(f"base 文件不存在：{base}")
    wb = load_workbook(base)
    ws, header_row, start_col = find_sheet_and_header(wb)
    if start_col != 1:
        fail("8列表头必须从第1列开始")
    if ws.max_column < 8:
        fail("工作表必须至少有8列")

    updates = payload.get("updates") or []
    new_rows = payload.get("new_rows") or []
    invalid_statuses: list[str] = []
    for idx, rec in enumerate(new_rows):
        status = rec.get("状态")
        if status not in ALLOWED_STATUS:
            invalid_statuses.append(f"new_rows[{idx}].状态={status!r}")
    for idx, update in enumerate(updates):
        status = (update.get("set") or {}).get("状态")
        if status is not None and status not in ALLOWED_STATUS:
            invalid_statuses.append(f"updates[{idx}].set.状态={status!r}")
    if invalid_statuses:
        fail("非法状态（仅允许已完成/进行中/待开始/暂停）：\n" + "\n".join(invalid_statuses))

    matched_rows: dict[tuple[str, str], int] = {}
    for row in range(header_row + 1, ws.max_row + 1):
        period = ws.cell(row, start_col).value
        item = ws.cell(row, start_col + 2).value
        if period is not None or item is not None:
            matched_rows[(as_text(period), as_text(item))] = row
    missing: list[str] = []
    updated_rows: list[int] = []
    for index, update in enumerate(updates):
        match = update.get("match") or {}
        key = (as_text(match.get("周期")), as_text(match.get("事项")))
        row = matched_rows.get(key)
        if row is None:
            missing.append(f"updates[{index}]: 周期={key[0]!r}, 事项={key[1]!r}")
            continue
        for field, value in (update.get("set") or {}).items():
            if field not in {"状态", "交付物/完成标准", "备注"}:
                fail(f"updates[{index}] 只能更新状态、交付物/完成标准、备注，不能更新：{field}")
            ws.cell(row, start_col + HEADERS.index(field)).value = value
        updated_rows.append(row)
    if missing:
        fail("未匹配到更新项：\n" + "\n".join(missing))

    data_rows = [r for r in range(header_row + 1, ws.max_row + 1) if any(ws.cell(r, c).value is not None for c in range(start_col, start_col + 8))]
    source_row = data_rows[-1] if data_rows else header_row
    use_column_defaults = not data_rows
    first_new = ws.max_row + 1 if ws.max_row > header_row else header_row + 1
    # Preserve formatting-only rows; copy the last populated data row's style.
    for offset, record in enumerate(new_rows):
        row = first_new + offset
        copy_row_style(ws, source_row, row, start_col, use_column_defaults=use_column_defaults)
        for i, field in enumerate(HEADERS):
            value = record.get(field)
            if field == "预计完成时间":
                value = parse_date(value)
            ws.cell(row, start_col + i).value = value
    # Preserve any base data validation exactly; do not rewrite its sqref.
    for row in updated_rows + list(range(first_new, first_new + len(new_rows))):
        fit_row_height(ws, row, start_col)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = as_text(payload.get("name")) or "未命名"
    week = as_text(payload.get("iso_week")) or "未知周次"
    ww = week.split("-W", 1)[1] if "-W" in week else week
    xlsx_path, md_path = choose_output_pair(
        out_dir / f"周度计划表-{name}-W{ww}.xlsx",
        out_dir / f"WBS-{name}-W{ww}.md",
    )
    try:
        wb.save(xlsx_path)
        md_path.write_text(markdown_report(payload, ws, header_row, start_col, updated_rows), encoding="utf-8")
    except Exception:
        # Keep partial files for diagnosis rather than deleting computed paths.
        print(f"输出失败，保留诊断文件：{xlsx_path} / {md_path}", file=sys.stderr)
        raise
    return xlsx_path, md_path


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(Path(args.rows).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            fail("rows.json 顶层必须是对象")
        xlsx, md = render(payload, args.base, Path(args.out_dir))
        print(json.dumps({"xlsx": str(xlsx), "md": str(md)}, ensure_ascii=False))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"render 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
