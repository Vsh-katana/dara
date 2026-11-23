"""Small dependency-free CSV/XLSX readers for the import preview workflow."""
from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta
import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "p": "http://schemas.openxmlformats.org/package/2006/relationships"}


def decode_upload(payload: dict) -> tuple[str, bytes]:
    filename = str(payload.get("filename") or "upload.csv")
    encoded = payload.get("content_base64") or ""
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("The uploaded file data is invalid.") from None
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Import files must be 20 MB or smaller.")
    return filename, data


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref.upper())
    n = 0
    for char in letters.group(0) if letters else "A":
        n = n*26 + ord(char)-64
    return n-1


def _xlsx_rows(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.findall(".//m:t", NS)) for si in root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        first = workbook.find("m:sheets/m:sheet", NS)
        if first is None:
            return []
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        target = rel_targets[first.attrib[f"{{{NS['r']}}}id"]].lstrip("/")
        sheet_path = target if target.startswith("xl/") else "xl/"+target
        sheet = ET.fromstring(archive.read(sheet_path))
        table = []
        for row in sheet.findall(".//m:sheetData/m:row", NS):
            cells = {}
            for cell in row.findall("m:c", NS):
                raw = cell.find("m:v", NS)
                value = raw.text if raw is not None else ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                cells[_col_index(cell.attrib.get("r", "A1"))] = value
            if cells:
                table.append([cells.get(i, "") for i in range(max(cells)+1)])
        return table


def _csv_rows(data: bytes) -> list[list[str]]:
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV encoding is not supported.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect) if row]


def read_tabular(filename: str, data: bytes) -> list[list[str]]:
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "xlsx":
        return _xlsx_rows(data)
    if ext == "csv":
        return _csv_rows(data)
    raise ValueError("Only .xlsx and .csv files are supported.")


def excel_serial_date(raw: str) -> str:
    value = float(raw)
    return (datetime(1899, 12, 30) + timedelta(days=value)).date().isoformat()
