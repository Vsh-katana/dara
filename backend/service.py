"""REST service. Every response and mutation is backed by SQLite."""
from __future__ import annotations

import base64
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
import zipfile

from . import database, importers, ledger
from .database import connect, now_iso, transaction, audit, ROOT
from .jalali import parse_local_date
from .money import decimal_value, money_text, MoneyError


class ApiError(Exception):
    def __init__(self, status: int, message: str, details=None):
        self.status, self.message, self.details = status, message, details
        super().__init__(message)


class ResponseData:
    def __init__(self, body: bytes, content_type: str, filename: str | None = None, status: int = 200):
        self.body, self.content_type, self.filename, self.status = body, content_type, filename, status


def _err(status, message, details=None):
    raise ApiError(status, message, details)


def _int(value, default=None, minimum=None, maximum=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None: n = max(minimum, n)
    if maximum is not None: n = min(maximum, n)
    return n


def _bool(value, default=False):
    if value is None: return default
    if isinstance(value, bool): return value
    return str(value).lower() in ("1", "true", "yes", "on")


def _required_text(body, key, max_length=180):
    value = str(body.get(key) or "").strip()
    if not value: _err(422, f"{key} is required.")
    if len(value) > max_length: _err(422, f"{key} is too long.")
    return value


def _date(value, calendar="gregorian", required=True):
    if not value and not required:
        return None
    try:
        return parse_local_date(str(value), calendar).isoformat()
    except (ValueError, TypeError) as exc:
        _err(422, str(exc))


def _amount(value, positive=False):
    try:
        number = decimal_value(value, allow_negative=not positive)
    except MoneyError as exc:
        _err(422, str(exc))
    if positive and number <= 0:
        _err(422, "Amount must be greater than zero.")
    return money_text(number)


def _time(value):
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", raw):
        _err(422, "Time must use 24-hour HH:MM format.")
    return raw if len(raw) == 8 else raw+":00"


def _row_dict(row):
    return dict(row) if row is not None else None


def _setting(conn, key, fallback=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else fallback


def _selected_account(conn, account_id):
    if account_id in (None, ""):
        row = conn.execute("SELECT id FROM accounts WHERE archived=0 ORDER BY id LIMIT 1").fetchone()
        return row["id"] if row else None
    row = conn.execute("SELECT id FROM accounts WHERE id=? AND archived=0", (int(account_id),)).fetchone()
    if not row: _err(422, "The selected account is unavailable.")
    return row["id"]


def _selected_category(conn, kind, category_id=None, category_name=None):
    if category_id not in (None, ""):
        row = conn.execute("SELECT * FROM categories WHERE id=? AND kind=? AND enabled=1", (int(category_id), kind)).fetchone()
        if not row: _err(422, "The selected category is unavailable for new transactions.")
        return row["id"], row["name"]
    name = str(category_name or "").strip()
    if not name: _err(422, "Category is required.")
    row = conn.execute("SELECT * FROM categories WHERE kind=? AND name=? AND enabled=1 ORDER BY id LIMIT 1", (kind, name)).fetchone()
    if not row:
        stamp = now_iso()
        cat_id = conn.execute("INSERT INTO categories(kind,name,created_at,updated_at) VALUES(?,?,?,?)", (kind,name,stamp,stamp)).lastrowid
        return cat_id, name
    return row["id"], row["name"]


def _insert_transaction(conn, data, *, audit_action="created"):
    kind = data.get("kind", data.get("type"))
    if kind not in ("income", "expense"):
        _err(422, "Transaction type must be income or expense.")
    title = _required_text(data, "title")
    original = decimal_value(_amount(data.get("amount"), positive=True))
    signed = original if kind == "income" else -original
    tx_date = _date(data.get("date"), data.get("calendar") or "gregorian")
    tx_time = _time(data.get("time"))
    cat_id, cat_name = _selected_category(conn, kind, data.get("category_id"), data.get("category"))
    account_id = _selected_account(conn, data.get("account_id"))
    note = str(data.get("note") or "")[:2000]
    subcategory = str(data.get("subcategory") or "")[:120]
    tags = data.get("tags", [])
    if isinstance(tags, str): tags = [x.strip() for x in tags.split(",") if x.strip()]
    if not isinstance(tags, list) or len(tags) > 30 or any(len(str(x)) > 40 for x in tags):
        _err(422, "Tags must be a list of up to 30 short values.")
    inclusion = data.get("inclusion") or {"all": True, "report": True, "budget": True, "forecast": True, "operational": True}
    if not isinstance(inclusion, dict): _err(422, "Inclusion settings are invalid.")
    uid, stamp = str(uuid.uuid4()), now_iso()
    attachment = _store_attachment(data.get("attachment")) if data.get("attachment") else None
    cursor = conn.execute(
        """INSERT INTO transactions(uid,kind,amount,raw_amount,date,time,title,category_id,category_text,subcategory,note,
        account_id,tags_json,inclusion_json,attachment_name,attachment_path,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid,kind,money_text(signed),money_text(signed),tx_date,tx_time,title,cat_id,cat_name,subcategory,note,
         account_id,json.dumps(tags,ensure_ascii=False),json.dumps(inclusion,ensure_ascii=False),
         attachment["name"] if attachment else None, attachment["path"] if attachment else None,stamp,stamp),
    )
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (cursor.lastrowid,)).fetchone()
    audit(conn,"transaction",cursor.lastrowid,audit_action,after={"kind":kind,"amount":money_text(signed),"date":tx_date,"title":title})
    return row


def _store_attachment(attachment):
    if not isinstance(attachment, dict): _err(422, "Attachment data is invalid.")
    name = Path(str(attachment.get("name") or "file")).name[:180]
    ext = Path(name).suffix.lower()
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
    if ext not in allowed: _err(422, "Attachment must be a PDF or an image.")
    try: content = base64.b64decode(attachment.get("content_base64") or "", validate=True)
    except Exception: _err(422, "Attachment data is invalid.")
    if not content or len(content) > 5*1024*1024: _err(422, "Attachment must be between 1 byte and 5 MB.")
    file_id = uuid.uuid4().hex+ext
    path = database.data_dir()/"attachments"/file_id
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(content)
    return {"name":name,"path":f"attachments/{file_id}"}


def _serialize_transactions(rows):
    return rows


def _tx_view(conn, tx_id):
    row=conn.execute("SELECT date FROM transactions WHERE id=?",(int(tx_id),)).fetchone()
    if not row:return None
    day=row["date"]
    return next((item for item in ledger.transaction_rows(conn,start=day,end=day) if item["id"]==int(tx_id)),None)


def _transactions(conn, params):
    start = params.get("from") or params.get("date_from")
    end = params.get("to") or params.get("date_to")
    filters = {"start":start,"end":end,"kind":params.get("kind") or params.get("type"),"q":params.get("q"),
               "category":params.get("category"),"account_id":params.get("account_id"),"min_amount":params.get("min_amount"),
               "max_amount":params.get("max_amount"),"included":params.get("included"),"tags":params.get("tags")}
    rows = ledger.transaction_rows(conn, **filters)
    sort_by = params.get("sort") or "date"
    if sort_by not in {"date","amount","category","kind","title"}: sort_by="date"
    reverse = (params.get("dir") or "desc").lower() != "asc"
    if sort_by == "amount": rows.sort(key=lambda r: abs(decimal_value(r["amount"])), reverse=reverse)
    elif sort_by == "category": rows.sort(key=lambda r: r["display_category"].casefold(), reverse=reverse)
    elif sort_by == "kind": rows.sort(key=lambda r: r["kind"], reverse=reverse)
    elif sort_by == "title": rows.sort(key=lambda r: r["title"].casefold(), reverse=reverse)
    page = _int(params.get("page"),1,1,100000)
    limit = _int(params.get("limit"),50,1,250)
    count = len(rows)
    return {"items":rows[(page-1)*limit:page*limit],"page":page,"limit":limit,"total":count,"pages":max(1,(count+limit-1)//limit)}


def _month_bounds(raw_month, calendar):
    if not raw_month: return None
    raw = str(raw_month)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", raw)
    if not match: _err(422,"Month must use YYYY-MM format.")
    y,m=map(int,match.groups())
    if calendar == "jalali":
        start = parse_local_date(f"{y:04d}/{m:02d}/01","jalali")
        ny,nm=(y+1,1) if m==12 else (y,m+1)
        end = parse_local_date(f"{ny:04d}/{nm:02d}/01","jalali")-timedelta(days=1)
    else:
        start=date(y,m,1)
        end=date(y+(m==12),m%12+1,1)-timedelta(days=1)
    return start,end


def _create_transfer(conn, data):
    amount = _amount(data.get("amount"),positive=True)
    tx_date = _date(data.get("date"), data.get("calendar") or "gregorian")
    tx_time = _time(data.get("time"))
    from_id, to_id = data.get("from_account_id"), data.get("to_account_id")
    if not from_id or not to_id or int(from_id)==int(to_id): _err(422,"Choose two different accounts for a transfer.")
    _selected_account(conn,from_id); _selected_account(conn,to_id)
    stamp=now_iso()
    cur=conn.execute("INSERT INTO transfers(amount,date,time,from_account_id,to_account_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                     (amount,tx_date,tx_time,int(from_id),int(to_id),str(data.get("note") or "")[:2000],stamp))
    audit(conn,"transfer",cur.lastrowid,"created",after={"amount":amount,"from_account_id":from_id,"to_account_id":to_id,"date":tx_date})
    return _row_dict(conn.execute("SELECT * FROM transfers WHERE id=?",(cur.lastrowid,)).fetchone())


def _category_item(row, conn):
    item=dict(row)
    rows=conn.execute("SELECT amount FROM transactions WHERE category_id=? AND deleted_at IS NULL",(item["id"],)).fetchall()
    item["transaction_count"]=len(rows)
    item["transaction_total"]=money_text(sum((abs(decimal_value(r["amount"])) for r in rows),Decimal(0)))
    return item


def _category_delete(conn, cat_id, target_id=None):
    row=conn.execute("SELECT * FROM categories WHERE id=?",(cat_id,)).fetchone()
    if not row: _err(404,"Category not found.")
    count=conn.execute("SELECT COUNT(*) n FROM transactions WHERE category_id=? AND deleted_at IS NULL",(cat_id,)).fetchone()["n"]
    target=None
    if target_id:
        target=conn.execute("SELECT * FROM categories WHERE id=? AND kind=? AND enabled=1",(int(target_id),row["kind"])).fetchone()
        if not target: _err(422,"Replacement category is unavailable.")
    before=dict(row)
    if count and not target_id:
        conn.execute("UPDATE categories SET enabled=0,updated_at=? WHERE id=?",(now_iso(),cat_id))
        audit(conn,"category",cat_id,"disabled_with_history",before=before,after={"transaction_count":count,"enabled":False})
        return {"deleted":False,"disabled":True,"history_count":count}
    if target:
        conn.execute("UPDATE transactions SET category_id=?,updated_at=? WHERE category_id=?",(target["id"],now_iso(),cat_id))
    # Imported rows retain their original category text; clearing this optional link never changes it.
    conn.execute("UPDATE transactions SET category_id=NULL,updated_at=? WHERE category_id=?",(now_iso(),cat_id))
    conn.execute("UPDATE categories SET parent_id=NULL WHERE parent_id=?",(cat_id,))
    conn.execute("DELETE FROM categories WHERE id=?",(cat_id,))
    audit(conn,"category",cat_id,"deleted",before=before,after={"moved_to":target_id,"history_count":count})
    return {"deleted":True,"history_count":count}


def _debt_item(conn,row):
    item=dict(row)
    paid=sum((decimal_value(r["amount"]) for r in conn.execute("SELECT amount FROM debt_payments WHERE debt_id=?",(item["id"],))),Decimal(0))
    original=decimal_value(item["original_amount"])
    item["paid"]=money_text(paid); item["remaining"]=money_text(max(original-paid,Decimal(0)))
    item["overdue"]=bool(item["due_date"] and item["due_date"] < date.today().isoformat() and paid<original)
    return item


def _goal_item(conn,row):
    item=dict(row)
    contributions=sum((decimal_value(r["amount"]) for r in conn.execute("SELECT amount FROM goal_movements WHERE goal_id=? AND action='contribution'",(item["id"],))),Decimal(0))
    withdrawals=sum((decimal_value(r["amount"]) for r in conn.execute("SELECT amount FROM goal_movements WHERE goal_id=? AND action='withdrawal'",(item["id"],))),Decimal(0))
    saved=contributions-withdrawals; target=decimal_value(item["target_amount"])
    item["saved"]=money_text(saved);item["remaining"]=money_text(max(target-saved,Decimal(0)))
    item["percentage"]=money_text(saved*100/target if target else Decimal(0))
    return item


def _advance_due(value: date, cadence: str, interval: int) -> date:
    if cadence=="daily":return value+timedelta(days=interval)
    if cadence=="weekly":return value+timedelta(days=7*interval)
    if cadence=="custom":return value+timedelta(days=interval)
    if cadence=="yearly":
        try:return value.replace(year=value.year+interval)
        except ValueError:return value.replace(year=value.year+interval,day=28)
    month=value.month-1+interval
    year=value.year+month//12;month=month%12+1
    next_first=date(year,month,1)
    end=date(year+(month==12),month%12+1,1)-timedelta(days=1)
    return min(next_first+timedelta(days=value.day-1),end)


def _run_due(conn, through: date | None=None):
    through=through or date.today()
    generated=[]; stamp=now_iso()
    for rec in conn.execute("SELECT * FROM recurring WHERE active=1 AND next_date<=? ORDER BY next_date,id",(through.isoformat(),)).fetchall():
        due=date.fromisoformat(rec["next_date"])
        rule_generated=False
        while due<=through and (not rec["end_date"] or due.isoformat()<=rec["end_date"]):
            found=conn.execute("SELECT transaction_id FROM recurring_runs WHERE recurring_id=? AND due_date=?",(rec["id"],due.isoformat())).fetchone()
            if not found:
                uid=str(uuid.uuid5(uuid.NAMESPACE_URL,f"finance-recurring:{rec['id']}:{due.isoformat()}"))
                signed=decimal_value(rec["amount"])*(1 if rec["kind"]=="income" else -1)
                cur=conn.execute("INSERT OR IGNORE INTO transactions(uid,kind,amount,raw_amount,date,time,title,category_id,category_text,account_id,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (uid,rec["kind"],money_text(signed),money_text(signed),due.isoformat(),rec["time"],rec["title"],rec["category_id"],rec["category_name"],rec["account_id"],rec["note"],stamp,stamp))
                txid=cur.lastrowid or conn.execute("SELECT id FROM transactions WHERE uid=?",(uid,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO recurring_runs(recurring_id,due_date,transaction_id) VALUES(?,?,?)",(rec["id"],due.isoformat(),txid))
                audit(conn,"transaction",txid,"recurring_generated",after={"recurring_id":rec["id"],"due_date":due.isoformat()})
                generated.append(txid)
                rule_generated=True
            due=_advance_due(due,rec["cadence"],rec["interval_n"])
        conn.execute("UPDATE recurring SET next_date=?,last_generated_date=?,updated_at=? WHERE id=?",(due.isoformat(),through.isoformat() if rule_generated else rec["last_generated_date"],stamp,rec["id"]))
    return generated


def _make_backup(conn):
    tables=[r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    data={name:[dict(row) for row in conn.execute(f'SELECT * FROM "{name}"')] for name in tables}
    payload={"product":"DaraFinance","schema_version":1,"created_at":now_iso(),"tables":data}
    canonical=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    payload["sha256"]=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    audit(conn,"backup","all","created",after={"tables":len(tables)})
    return payload


def _restore(conn, payload):
    if not isinstance(payload,dict) or payload.get("product")!="DaraFinance" or not isinstance(payload.get("tables"),dict):
        _err(422,"This is not a supported Dara backup.")
    digest=payload.get("sha256")
    canonical={k:v for k,v in payload.items() if k!="sha256"}
    expected=hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
    if not digest or digest!=expected: _err(422,"Backup integrity check failed.")
    tables=payload["tables"]
    known={r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    unknown=set(tables)-known
    if unknown: _err(422,"Backup contains unsupported tables.",sorted(unknown))
    with transaction(conn):
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for name in sorted(known,reverse=True):
            if name in tables:conn.execute(f'DELETE FROM "{name}"')
        for name in sorted(tables):
            rows=tables[name]
            if not isinstance(rows,list):_err(422,f"Backup table {name} is malformed.")
            for row in rows:
                if not isinstance(row,dict):_err(422,f"Backup table {name} contains invalid rows.")
                cols=list(row)
                if any(c not in {x[1] for x in conn.execute(f'PRAGMA table_info("{name}")')} for c in cols):_err(422,f"Backup table {name} contains unknown fields.")
                if cols:
                    conn.execute(f'INSERT INTO "{name}" ({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({",".join("?" for _ in cols)})',[row[c] for c in cols])
        audit(conn,"backup","all","restored",after={"tables":len(tables)})
    return {"restored_tables":len(tables)}


def _xlsx_export(rows, headings):
    def col(n):
        result=""
        while n:
            n,rem=divmod(n-1,26);result=chr(65+rem)+result
        return result
    xml_rows=[]
    all_rows=[headings]+[[row.get(key,"") for key in headings.keys()] for row in rows]
    for ri,row in enumerate(all_rows,1):
        cells=[]
        for ci,val in enumerate(row.values(),1):
            text=xml_escape(str(val if val is not None else ""))
            cells.append(f'<c r="{col(ci)}{ri}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        xml_rows.append(f'<row r="{ri}">'+"".join(cells)+"</row>")
    sheet=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+"".join(xml_rows)+"</sheetData></worksheet>")
    files={"[Content_Types].xml":'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
           "_rels/.rels":'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
           "xl/workbook.xml":'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Transactions" sheetId="1" r:id="rId1"/></sheets></workbook>',
           "xl/_rels/workbook.xml.rels":'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
           "xl/worksheets/sheet1.xml":sheet}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    return buffer.getvalue()


def xml_escape(text):
    return text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")


def _import_preview(conn, body):
    filename,data=importers.decode_upload(body)
    tabular=importers.read_tabular(filename,data)
    if not tabular:_err(422,"The file contains no rows.")
    mapping=body.get("mapping") or {}
    calendar=body.get("calendar") or "jalali"
    first=tabular[0]
    has_header=_bool(body.get("has_header"),True)
    headers=[str(x).strip() for x in first] if has_header else [f"Column {i+1}" for i in range(len(first))]
    records=tabular[1:] if has_header else tabular
    def val(row,key,default=""):
        index=_int(mapping.get(key),None,0,200)
        return str(row[index]).strip() if index is not None and index<len(row) else default
    previews=[]
    for i,row in enumerate(records,2 if has_header else 1):
        if not any(str(x).strip() for x in row):continue
        issues=[]
        raw_date=val(row,"date")
        try:
            if re.fullmatch(r"\d+(?:\.\d+)?",raw_date): parsed_date=importers.excel_serial_date(raw_date)
            else:parsed_date=_date(raw_date,calendar)
        except Exception:parsed_date=None;issues.append("تاریخ معتبر نیست")
        raw_amount=val(row,"amount")
        try:
            amount=decimal_value(raw_amount.replace(",",""),allow_negative=True)
            if amount==0:issues.append("مبلغ صفر است")
        except MoneyError:amount=Decimal(0);issues.append("مبلغ معتبر نیست")
        raw_kind=val(row,"kind").lower()
        kind="income" if raw_kind in {"income","in","درآمد","واریز"} else "expense" if raw_kind in {"expense","out","هزینه","برداشت"} else ""
        if not kind:issues.append("نوع تراکنش را در نگاشت ستون تعیین کنید")
        category=val(row,"category")
        if not category:issues.append("دسته‌بندی خالی است")
        title=val(row,"title") or category or "واردشده از فایل"
        note=val(row,"note")
        duplicate=False
        if parsed_date and kind:
            signed=money_text(abs(amount)*(1 if kind=="income" else -1))
            duplicate=conn.execute("SELECT 1 FROM transactions WHERE date=? AND kind=? AND amount=? AND category_text=? AND note=? AND deleted_at IS NULL LIMIT 1",(parsed_date,kind,signed,category,note)).fetchone() is not None
        previews.append({"row":i,"date":parsed_date,"time":val(row,"time") or None,"amount":money_text(abs(amount)),"kind":kind,
                         "category":category,"title":title,"note":note,"issues":issues,"duplicate":duplicate})
    return {"headers":headers,"rows":previews,"total":len(previews),"valid":sum(not r["issues"] and not r["duplicate"] for r in previews),
            "duplicates":sum(r["duplicate"] for r in previews),"errors":sum(bool(r["issues"]) for r in previews)}


def _import_commit(conn, body):
    rows=body.get("rows")
    if not isinstance(rows,list) or not rows or len(rows)>10000:_err(422,"Choose between 1 and 10,000 validated rows to import.")
    imported=duplicates=skipped=0
    with transaction(conn):
        for row in rows:
            if row.get("duplicate"):
                duplicates+=1;continue
            if row.get("issues"):
                skipped+=1;continue
            try:
                _insert_transaction(conn,{**row,"calendar":"gregorian"},audit_action="imported")
                imported+=1
            except ApiError:
                skipped+=1
        audit(conn,"import","batch","completed",after={"imported":imported,"duplicates":duplicates,"skipped":skipped})
    return {"imported":imported,"duplicates":duplicates,"skipped":skipped}


def dispatch(method: str, path: str, params: dict | None=None, body: dict | None=None):
    """Return JSON-compatible data or a ResponseData. Caller maps ApiError to HTTP."""
    params,body=params or {},body or {}
    database.init_database()
    conn=connect()
    try:
        if method=="GET" and path=="/api/health":
            return {"ok":True,"database":"sqlite","version":1}
        if method=="GET" and path=="/api/bootstrap":
            with transaction(conn):generated=_run_due(conn)
            return {"settings":{r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM settings")},
                    "accounts":ledger.account_balances(conn),"categories":[_category_item(r,conn) for r in conn.execute("SELECT * FROM categories ORDER BY kind,sort_order,name")],
                    "counts":{"income":conn.execute("SELECT COUNT(*) n FROM transactions WHERE kind='income' AND deleted_at IS NULL").fetchone()["n"],
                              "expense":conn.execute("SELECT COUNT(*) n FROM transactions WHERE kind='expense' AND deleted_at IS NULL").fetchone()["n"]},
                    "generated_recurring":generated}
        if method=="GET" and path=="/api/dashboard":
            with transaction(conn):_run_due(conn)
            return ledger.dashboard(conn,calendar=params.get("calendar") or _setting(conn,"calendar","jalali"))
        if method=="GET" and path=="/api/transactions":return _transactions(conn,params)
        if method=="GET" and path=="/api/transactions/deleted":
            rows=ledger.transaction_rows(conn,include_deleted=True)
            deleted=[r for r in rows if r["deleted_at"]]
            page=_int(params.get("page"),1,1,100000);limit=_int(params.get("limit"),50,1,250)
            return {"items":deleted[(page-1)*limit:page*limit],"page":page,"limit":limit,"total":len(deleted),"pages":max(1,(len(deleted)+limit-1)//limit)}
        if method=="POST" and path=="/api/transactions":
            with transaction(conn):row=_insert_transaction(conn,body)
            return _tx_view(conn,row["id"])
        if method=="POST" and path=="/api/transactions/bulk-delete":
            ids=body.get("ids",[])
            if not isinstance(ids,list) or len(ids)>1000:_err(422,"Invalid bulk selection.")
            deleted=skipped=0
            with transaction(conn):
                for txid in ids:
                    row=conn.execute("SELECT * FROM transactions WHERE id=? AND deleted_at IS NULL",(int(txid),)).fetchone()
                    if not row:skipped+=1;continue
                    conn.execute("UPDATE transactions SET deleted_at=?,updated_at=? WHERE id=?",(now_iso(),now_iso(),txid));audit(conn,"transaction",txid,"soft_deleted",before=dict(row));deleted+=1
            return {"deleted":deleted,"skipped":skipped}
        if path.startswith("/api/transactions/"):
            match=re.fullmatch(r"/api/transactions/(\d+)(?:/(duplicate|inclusion|restore|attachment))?",path)
            if not match:_err(404,"Route not found.")
            txid,action=int(match.group(1)),match.group(2)
            row=conn.execute("SELECT * FROM transactions WHERE id=?",(txid,)).fetchone()
            if not row:_err(404,"Transaction not found.")
            if method=="GET" and not action:return _tx_view(conn,txid)
            if method=="POST" and action=="duplicate":
                data={"kind":row["kind"],"amount":money_text(abs(decimal_value(row["amount"]))),"date":row["date"],"time":row["time"],
                      "title":row["title"]+" · نسخه", "category_id":row["category_id"],"category":row["category_text"],"note":row["note"],
                      "subcategory":row["subcategory"],"account_id":row["account_id"],"tags":json.loads(row["tags_json"] or "[]")}
                with transaction(conn):newrow=_insert_transaction(conn,data)
                return _tx_view(conn,newrow["id"])
            if method=="PATCH" and action=="inclusion":
                before=json.loads(row["inclusion_json"] or "{}")
                values={"all":True,"report":True,"budget":True,"forecast":True,"operational":True,**before}
                flags=body.get("inclusion") or {}
                allowed={"all","report","budget","forecast","operational"}
                if not isinstance(flags,dict) or set(flags)-allowed:_err(422,"Inclusion settings are invalid.")
                values.update({k:_bool(v) for k,v in flags.items()})
                with transaction(conn):
                    conn.execute("UPDATE transactions SET inclusion_json=?,updated_at=? WHERE id=?",(json.dumps(values),now_iso(),txid))
                    audit(conn,"transaction",txid,"inclusion_changed",before=before,after=values)
                return {"id":txid,"inclusion":values}
            if method=="GET" and action=="attachment":
                if not row["attachment_path"]:_err(404,"No attachment is available.")
                file_path=(database.data_dir()/row["attachment_path"]).resolve()
                if not file_path.is_relative_to(database.data_dir().resolve()) or not file_path.exists():_err(404,"Attachment file not found.")
                mime=mimetypes.guess_type(row["attachment_name"] or "")[0] or "application/octet-stream"
                return ResponseData(file_path.read_bytes(),mime,row["attachment_name"])
            if method=="PATCH" and not action:
                before=dict(row)
                updated={"kind":body.get("kind",row["kind"]),"title":body.get("title",row["title"]),"amount":body.get("amount",abs(decimal_value(row["amount"]))),
                         "date":body.get("date",row["date"]),"time":body.get("time",row["time"]),"category_id":body.get("category_id",row["category_id"]),
                         "category":body.get("category",row["category_text"]),"account_id":body.get("account_id",row["account_id"]),"note":body.get("note",row["note"]),
                         "subcategory":body.get("subcategory",row["subcategory"]),"tags":body.get("tags",json.loads(row["tags_json"] or "[]")),"calendar":body.get("calendar","gregorian")}
                kind=updated["kind"]
                if kind not in ("income","expense"):_err(422,"Transaction type must be income or expense.")
                amount=decimal_value(_amount(updated["amount"],positive=True));signed=amount if kind=="income" else -amount
                tx_date=_date(updated["date"],updated["calendar"]);tx_time=_time(updated["time"])
                cat_id,cat_name=_selected_category(conn,kind,updated["category_id"],updated["category"])
                account_id=_selected_account(conn,updated["account_id"])
                tags=updated["tags"] if isinstance(updated["tags"],list) else [x.strip() for x in str(updated["tags"]).split(",") if x.strip()]
                stamp=now_iso()
                with transaction(conn):
                    conn.execute("UPDATE transactions SET kind=?,amount=?,date=?,time=?,title=?,category_id=?,category_text=?,subcategory=?,note=?,account_id=?,tags_json=?,updated_at=? WHERE id=?",
                                 (kind,money_text(signed),tx_date,tx_time,str(updated["title"])[:180],cat_id,cat_name,str(updated["subcategory"])[:120],str(updated["note"])[:2000],account_id,json.dumps(tags,ensure_ascii=False),stamp,txid))
                    audit(conn,"transaction",txid,"updated",before=before,after=updated)
                return _tx_view(conn,txid)
            if method=="DELETE" and not action:
                with transaction(conn):
                    conn.execute("UPDATE transactions SET deleted_at=?,updated_at=? WHERE id=?",(now_iso(),now_iso(),txid));audit(conn,"transaction",txid,"soft_deleted",before=dict(row))
                return {"deleted":True,"recoverable":True}
            if method=="POST" and action=="restore":
                if not row["deleted_at"]:_err(409,"Transaction is not in the deleted list.")
                with transaction(conn):
                    conn.execute("UPDATE transactions SET deleted_at=NULL,updated_at=? WHERE id=?",(now_iso(),txid));audit(conn,"transaction",txid,"restored",before={"deleted_at":row["deleted_at"]})
                return {"restored":True}
        if method=="GET" and path=="/api/transfers":
            return [dict(r) for r in conn.execute("SELECT t.*,a.name from_account,b.name to_account FROM transfers t JOIN accounts a ON a.id=t.from_account_id JOIN accounts b ON b.id=t.to_account_id WHERE t.deleted_at IS NULL ORDER BY t.date DESC,t.id DESC")]
        if method=="POST" and path=="/api/transfers":
            with transaction(conn):return _create_transfer(conn,body)
        if method=="GET" and path=="/api/categories":
            rows=conn.execute("SELECT * FROM categories ORDER BY kind,sort_order,name").fetchall()
            return [_category_item(r,conn) for r in rows]
        if method=="POST" and path=="/api/categories":
            kind=body.get("kind")
            if kind not in ("income","expense"):_err(422,"Category type must be income or expense.")
            name=_required_text(body,"name",100);parent=body.get("parent_id")
            if parent and not conn.execute("SELECT 1 FROM categories WHERE id=? AND kind=?",(int(parent),kind)).fetchone():_err(422,"Parent category type does not match.")
            stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO categories(kind,name,parent_id,color,icon,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (kind,name,parent,str(body.get("color") or "#f59e0b"),str(body.get("icon") or "circle"),_int(body.get("sort_order"),0,-10000,10000),stamp,stamp))
                audit(conn,"category",cur.lastrowid,"created",after=body)
            return _category_item(conn.execute("SELECT * FROM categories WHERE id=?",(cur.lastrowid,)).fetchone(),conn)
        if path.startswith("/api/categories/"):
            match=re.fullmatch(r"/api/categories/(\d+)",path)
            if not match:_err(404,"Route not found.")
            cat_id=int(match.group(1));row=conn.execute("SELECT * FROM categories WHERE id=?",(cat_id,)).fetchone()
            if not row:_err(404,"Category not found.")
            if method=="PATCH":
                before=dict(row);name=str(body.get("name",row["name"])).strip();parent=body.get("parent_id",row["parent_id"])
                if not name or len(name)>100:_err(422,"Category name must be between 1 and 100 characters.")
                if parent and (int(parent)==cat_id or not conn.execute("SELECT 1 FROM categories WHERE id=? AND kind=?",(int(parent),row["kind"])).fetchone()):_err(422,"Invalid parent category.")
                enabled=_bool(body.get("enabled"),bool(row["enabled"]));color=str(body.get("color",row["color"]))
                if not re.fullmatch(r"#[0-9a-fA-F]{6}",color):_err(422,"Color must be a six-digit hexadecimal value.")
                with transaction(conn):
                    conn.execute("UPDATE categories SET name=?,parent_id=?,color=?,icon=?,enabled=?,sort_order=?,updated_at=? WHERE id=?",
                                 (name,parent,color,str(body.get("icon",row["icon"]))[:40],int(enabled),_int(body.get("sort_order"),row["sort_order"],-10000,10000),now_iso(),cat_id))
                    audit(conn,"category",cat_id,"updated",before=before,after=body)
                return _category_item(conn.execute("SELECT * FROM categories WHERE id=?",(cat_id,)).fetchone(),conn)
            if method=="DELETE":
                with transaction(conn):return _category_delete(conn,cat_id,body.get("move_to") or params.get("move_to"))
        if method=="GET" and path=="/api/accounts":return ledger.account_balances(conn)
        if method=="POST" and path=="/api/accounts":
            name=_required_text(body,"name",100);kind=body.get("kind") or "cash"
            if kind not in ("cash","bank","card","wallet","other"):_err(422,"Account type is invalid.")
            opening=_amount(body.get("opening_balance","0"));stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO accounts(name,kind,currency,opening_balance,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                 (name,kind,str(body.get("currency") or "")[:16],opening,str(body.get("note") or "")[:300],stamp,stamp))
                audit(conn,"account",cur.lastrowid,"created",after=body)
            return next(a for a in ledger.account_balances(conn) if a["id"]==cur.lastrowid)
        if path.startswith("/api/accounts/"):
            match=re.fullmatch(r"/api/accounts/(\d+)",path)
            if not match:_err(404,"Route not found.")
            aid=int(match.group(1));row=conn.execute("SELECT * FROM accounts WHERE id=?",(aid,)).fetchone()
            if not row:_err(404,"Account not found.")
            if method=="PATCH":
                before=dict(row);name=str(body.get("name",row["name"])).strip()
                if not name:_err(422,"Account name is required.")
                archived=_bool(body.get("archived"),bool(row["archived"]))
                with transaction(conn):
                    conn.execute("UPDATE accounts SET name=?,kind=?,currency=?,opening_balance=?,archived=?,note=?,updated_at=? WHERE id=?",
                                 (name,body.get("kind",row["kind"]),body.get("currency",row["currency"]),_amount(body.get("opening_balance",row["opening_balance"])),int(archived),str(body.get("note",row["note"]))[:300],now_iso(),aid))
                    audit(conn,"account",aid,"updated",before=before,after=body)
                return next(a for a in ledger.account_balances(conn) if a["id"]==aid)
            if method=="DELETE":
                count=conn.execute("SELECT COUNT(*) n FROM transactions WHERE account_id=?",(aid,)).fetchone()["n"]+conn.execute("SELECT COUNT(*) n FROM transfers WHERE from_account_id=? OR to_account_id=?",(aid,aid)).fetchone()["n"]
                if count:_err(409,"This account has financial history. Archive it to preserve the audit trail.")
                with transaction(conn):conn.execute("DELETE FROM accounts WHERE id=?",(aid,));audit(conn,"account",aid,"deleted",before=dict(row))
                return {"deleted":True}
        if method=="GET" and path=="/api/reports":
            start=params.get("from");end=params.get("to");kind=params.get("kind")
            if kind not in (None,"income","expense"): _err(422,"Report type is invalid.")
            if start:start=_date(start,params.get("calendar") or _setting(conn,"calendar","jalali"))
            if end:end=_date(end,params.get("calendar") or _setting(conn,"calendar","jalali"))
            return ledger.report(conn,start=start,end=end,kind=kind,calendar=params.get("calendar") or _setting(conn,"calendar","jalali"),group=params.get("group","month"),category=params.get("category"))
        if method=="GET" and path=="/api/forecast":return ledger.forecast(conn,calendar=params.get("calendar") or _setting(conn,"calendar","jalali"),history_months=_int(params.get("history_months"),6,1,24))
        if method=="GET" and path=="/api/compare":
            start=_date(params.get("from"),params.get("calendar") or _setting(conn,"calendar","jalali"));end=_date(params.get("to"),params.get("calendar") or _setting(conn,"calendar","jalali"))
            return ledger.compare(conn,date.fromisoformat(start),date.fromisoformat(end),periods=_int(params.get("periods"),3,1,12),unit=params.get("unit","custom"),calendar=params.get("calendar") or _setting(conn,"calendar","jalali"))
        if method=="GET" and path=="/api/cashflow":
            start=params.get("from");end=params.get("to")
            rows=ledger.transaction_rows(conn,start=start,end=end,report_context="report")
            transfer_where=["deleted_at IS NULL"];transfer_params=[]
            if start:transfer_where.append("date>=?");transfer_params.append(start)
            if end:transfer_where.append("date<=?");transfer_params.append(end)
            transfers=[dict(r) for r in conn.execute("SELECT * FROM transfers WHERE "+" AND ".join(transfer_where),transfer_params)]
            debt_where=[];debt_params=[]
            if start:debt_where.append("dp.date>=?");debt_params.append(start)
            if end:debt_where.append("dp.date<=?");debt_params.append(end)
            debt_sql="SELECT dp.*,d.kind debt_kind FROM debt_payments dp JOIN debts d ON d.id=dp.debt_id"
            if debt_where:debt_sql+=" WHERE "+" AND ".join(debt_where)
            debt_payments=[dict(r) for r in conn.execute(debt_sql,debt_params)]
            sums=ledger._period_sums(rows)
            transfer_total=sum((decimal_value(t["amount"]) for t in transfers),Decimal(0))
            return {"inflows":money_text(sums["income"]+sum((decimal_value(p["amount"]) for p in debt_payments if p["debt_kind"]=="receivable"),Decimal(0))),
                    "outflows":money_text(sums["expenses"]+sum((decimal_value(p["amount"]) for p in debt_payments if p["debt_kind"]=="payable"),Decimal(0))),
                    "net":money_text(sums["net"]+sum((decimal_value(p["amount"])*(1 if p["debt_kind"]=="receivable" else -1) for p in debt_payments),Decimal(0))),
                    "internal_transfers":money_text(transfer_total),"accounts":ledger.account_balances(conn),"series":ledger._group_series(rows,params.get("group","month"),params.get("calendar","jalali"))}
        if method=="GET" and path=="/api/budgets":
            return ledger.budget_status(conn)["items"]
        if method=="POST" and path=="/api/budgets":
            amount=_amount(body.get("amount"),positive=True);start=_date(body.get("start_date") or body.get("month"),body.get("calendar","jalali"))
            end=_date(body.get("end_date"),body.get("calendar","jalali"),required=False)
            if not end:end=(date.fromisoformat(start)+timedelta(days=30)).isoformat()
            if end<start:_err(422,"Budget end date must not be before its start.")
            category_id=body.get("category_id");cat_name=""
            if category_id:
                cat=conn.execute("SELECT * FROM categories WHERE id=? AND kind='expense'",(int(category_id),)).fetchone()
                if not cat:_err(422,"Budget category must be an expense category.")
                cat_name=cat["name"]
            thresholds=body.get("thresholds",[70,90,100])
            if not isinstance(thresholds,list) or any(_int(x,None,1,100) is None for x in thresholds):_err(422,"Budget thresholds must be between 1 and 100.")
            stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO budgets(name,category_id,category_name,amount,start_date,end_date,threshold_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (str(body.get("name") or ""),category_id,cat_name,amount,start,end,json.dumps(sorted(set(map(int,thresholds)))),stamp,stamp))
                audit(conn,"budget",cur.lastrowid,"created",after=body)
            return next((item for item in ledger.budget_status(conn)["items"] if item["id"]==cur.lastrowid),None)
        if path.startswith("/api/budgets/"):
            match=re.fullmatch(r"/api/budgets/(\d+)",path)
            if not match:_err(404,"Route not found.")
            bid=int(match.group(1));row=conn.execute("SELECT * FROM budgets WHERE id=?",(bid,)).fetchone()
            if not row:_err(404,"Budget not found.")
            if method=="PATCH":
                before=dict(row);amount=_amount(body.get("amount",row["amount"]),positive=True)
                start=_date(body.get("start_date",row["start_date"]),body.get("calendar","gregorian"));end=_date(body.get("end_date",row["end_date"]),body.get("calendar","gregorian"))
                if end<start:_err(422,"Budget end date must not be before its start.")
                cat_id=body.get("category_id",row["category_id"]);cat_name=row["category_name"]
                if cat_id:
                    cat=conn.execute("SELECT * FROM categories WHERE id=? AND kind='expense'",(int(cat_id),)).fetchone()
                    if not cat:_err(422,"Budget category must be an expense category.")
                    cat_name=cat["name"]
                thresholds=body.get("thresholds",json.loads(row["threshold_json"]))
                with transaction(conn):
                    conn.execute("UPDATE budgets SET name=?,category_id=?,category_name=?,amount=?,start_date=?,end_date=?,threshold_json=?,enabled=?,updated_at=? WHERE id=?",
                                 (str(body.get("name",row["name"])),cat_id,cat_name,amount,start,end,json.dumps(thresholds),int(_bool(body.get("enabled"),bool(row["enabled"]))),now_iso(),bid))
                    audit(conn,"budget",bid,"updated",before=before,after=body)
                return next(x for x in ledger.budget_status(conn)["items"] if x["id"]==bid)
            if method=="DELETE":
                with transaction(conn):conn.execute("DELETE FROM budgets WHERE id=?",(bid,));audit(conn,"budget",bid,"deleted",before=dict(row))
                return {"deleted":True}
        if method=="GET" and path=="/api/recurring":
            items=[]
            for row in conn.execute("SELECT * FROM recurring ORDER BY active DESC,next_date"):
                item=dict(row);item["expected"]=item["next_date"]>date.today().isoformat();items.append(item)
            return items
        if method=="POST" and path=="/api/recurring":
            kind=body.get("kind")
            if kind not in ("income","expense"):_err(422,"Recurring type must be income or expense.")
            title=_required_text(body,"title");amount=_amount(body.get("amount"),positive=True);cadence=body.get("cadence")
            if cadence not in ("daily","weekly","monthly","yearly","custom"):_err(422,"Recurrence cadence is invalid.")
            interval=_int(body.get("interval_n"),1,1,366);start=_date(body.get("start_date"),body.get("calendar","jalali"));nxt=_date(body.get("next_date") or start,body.get("calendar","jalali"));end=_date(body.get("end_date"),body.get("calendar","jalali"),required=False)
            if end and end<start:_err(422,"Recurrence end date must follow its start date.")
            cat_id,cat_name=_selected_category(conn,kind,body.get("category_id"),body.get("category"));account=_selected_account(conn,body.get("account_id"));stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO recurring(title,kind,amount,category_id,category_name,account_id,cadence,interval_n,start_date,next_date,end_date,time,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (title,kind,amount,cat_id,cat_name,account,cadence,interval,start,nxt,end,_time(body.get("time")),str(body.get("note") or ""),stamp,stamp))
                audit(conn,"recurring",cur.lastrowid,"created",after=body)
            return dict(conn.execute("SELECT * FROM recurring WHERE id=?",(cur.lastrowid,)).fetchone())
        if method=="POST" and path=="/api/recurring/run-due":
            with transaction(conn):ids=_run_due(conn)
            return {"generated":len(ids),"transaction_ids":ids}
        if path.startswith("/api/recurring/"):
            match=re.fullmatch(r"/api/recurring/(\d+)",path)
            if not match:_err(404,"Route not found.")
            rid=int(match.group(1));row=conn.execute("SELECT * FROM recurring WHERE id=?",(rid,)).fetchone()
            if not row:_err(404,"Recurring rule not found.")
            if method=="PATCH":
                before=dict(row);active=_bool(body.get("active"),bool(row["active"]))
                with transaction(conn):
                    conn.execute("UPDATE recurring SET active=?,end_date=?,updated_at=? WHERE id=?",(int(active),body.get("end_date",row["end_date"]),now_iso(),rid));audit(conn,"recurring",rid,"updated",before=before,after=body)
                return dict(conn.execute("SELECT * FROM recurring WHERE id=?",(rid,)).fetchone())
            if method=="DELETE":
                with transaction(conn):conn.execute("UPDATE recurring SET active=0,updated_at=? WHERE id=?",(now_iso(),rid));audit(conn,"recurring",rid,"paused",before=dict(row))
                return {"active":False}
        if method=="GET" and path=="/api/debts":return [_debt_item(conn,r) for r in conn.execute("SELECT * FROM debts WHERE status!='deleted' ORDER BY due_date,id")]
        if method=="POST" and path=="/api/debts":
            kind=body.get("kind")
            if kind not in ("payable","receivable"):_err(422,"Debt type must be payable or receivable.")
            party=_required_text(body,"party");amount=_amount(body.get("original_amount"),positive=True);due=_date(body.get("due_date"),body.get("calendar","jalali"),required=False);stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO debts(kind,party,title,original_amount,due_date,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (kind,party,str(body.get("title") or "")[:150],amount,due,str(body.get("notes") or "")[:2000],stamp,stamp))
                audit(conn,"debt",cur.lastrowid,"created",after=body)
            return _debt_item(conn,conn.execute("SELECT * FROM debts WHERE id=?",(cur.lastrowid,)).fetchone())
        if path.startswith("/api/debts/"):
            match=re.fullmatch(r"/api/debts/(\d+)(?:/payments)?",path)
            if not match:_err(404,"Route not found.")
            did=int(match.group(1));row=conn.execute("SELECT * FROM debts WHERE id=?",(did,)).fetchone()
            if not row:_err(404,"Debt record not found.")
            if method=="GET" and path.endswith("/payments"):
                return [dict(p) for p in conn.execute("SELECT dp.*,a.name account_name FROM debt_payments dp LEFT JOIN accounts a ON a.id=dp.account_id WHERE debt_id=? ORDER BY date DESC,id DESC",(did,))]
            if method=="POST" and path.endswith("/payments"):
                amount=_amount(body.get("amount"),positive=True);paid=sum((decimal_value(r["amount"]) for r in conn.execute("SELECT amount FROM debt_payments WHERE debt_id=?",(did,))),Decimal(0))
                if decimal_value(amount)>decimal_value(row["original_amount"])-paid:_err(422,"Payment exceeds the remaining balance.")
                tx_date=_date(body.get("date") or date.today().isoformat(),body.get("calendar","gregorian"));account=_selected_account(conn,body.get("account_id"));stamp=now_iso()
                with transaction(conn):
                    cur=conn.execute("INSERT INTO debt_payments(debt_id,amount,date,account_id,note,created_at) VALUES(?,?,?,?,?,?)",(did,amount,tx_date,account,str(body.get("note") or "")[:1000],stamp))
                    remaining=decimal_value(row["original_amount"])-paid-decimal_value(amount)
                    if remaining==0:conn.execute("UPDATE debts SET status='settled',updated_at=? WHERE id=?",(stamp,did))
                    audit(conn,"debt_payment",cur.lastrowid,"created",after={"debt_id":did,"amount":amount,"date":tx_date})
                return _debt_item(conn,conn.execute("SELECT * FROM debts WHERE id=?",(did,)).fetchone())
            if method=="PATCH":
                before=dict(row);party=str(body.get("party",row["party"])).strip()
                with transaction(conn):conn.execute("UPDATE debts SET party=?,title=?,due_date=?,notes=?,updated_at=? WHERE id=?",(party,str(body.get("title",row["title"])),_date(body.get("due_date",row["due_date"]),"gregorian",required=False),str(body.get("notes",row["notes"])),now_iso(),did));audit(conn,"debt",did,"updated",before=before,after=body)
                return _debt_item(conn,conn.execute("SELECT * FROM debts WHERE id=?",(did,)).fetchone())
            if method=="DELETE":
                with transaction(conn):conn.execute("UPDATE debts SET status='deleted',updated_at=? WHERE id=?",(now_iso(),did));audit(conn,"debt",did,"soft_deleted",before=dict(row))
                return {"deleted":True,"recoverable":True}
        if method=="GET" and path=="/api/goals":return [_goal_item(conn,r) for r in conn.execute("SELECT * FROM goals ORDER BY status='active' DESC,target_date")]
        if method=="POST" and path=="/api/goals":
            name=_required_text(body,"name");target=_amount(body.get("target_amount"),positive=True);target_date=_date(body.get("target_date"),body.get("calendar","jalali"),required=False);stamp=now_iso()
            with transaction(conn):
                cur=conn.execute("INSERT INTO goals(name,target_amount,target_date,color,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(name,target,target_date,str(body.get("color") or "#f59e0b"),str(body.get("notes") or "")[:1000],stamp,stamp));audit(conn,"goal",cur.lastrowid,"created",after=body)
            return _goal_item(conn,conn.execute("SELECT * FROM goals WHERE id=?",(cur.lastrowid,)).fetchone())
        if path.startswith("/api/goals/"):
            match=re.fullmatch(r"/api/goals/(\d+)(?:/movements)?",path)
            if not match:_err(404,"Route not found.")
            gid=int(match.group(1));row=conn.execute("SELECT * FROM goals WHERE id=?",(gid,)).fetchone()
            if not row:_err(404,"Goal not found.")
            if method=="GET" and path.endswith("/movements"):return [dict(r) for r in conn.execute("SELECT * FROM goal_movements WHERE goal_id=? ORDER BY date DESC,id DESC",(gid,))]
            if method=="POST" and path.endswith("/movements"):
                action=body.get("action")
                if action not in ("contribution","withdrawal"):_err(422,"Goal movement must be contribution or withdrawal.")
                amount=_amount(body.get("amount"),positive=True);current=_goal_item(conn,row)
                if action=="withdrawal" and decimal_value(amount)>decimal_value(current["saved"]):_err(422,"Withdrawal exceeds the goal's saved amount.")
                tx_date=_date(body.get("date") or date.today().isoformat(),body.get("calendar","gregorian"));account=_selected_account(conn,body.get("account_id"));stamp=now_iso()
                with transaction(conn):
                    cur=conn.execute("INSERT INTO goal_movements(goal_id,amount,date,action,account_id,note,created_at) VALUES(?,?,?,?,?,?,?)",(gid,amount,tx_date,action,account,str(body.get("note") or "")[:1000],stamp));audit(conn,"goal_movement",cur.lastrowid,"created",after=body)
                return _goal_item(conn,conn.execute("SELECT * FROM goals WHERE id=?",(gid,)).fetchone())
            if method=="PATCH":
                with transaction(conn):conn.execute("UPDATE goals SET status=?,updated_at=? WHERE id=?",(body.get("status",row["status"]),now_iso(),gid));audit(conn,"goal",gid,"updated",before=dict(row),after=body)
                return _goal_item(conn,conn.execute("SELECT * FROM goals WHERE id=?",(gid,)).fetchone())
            if method=="DELETE":
                with transaction(conn):conn.execute("UPDATE goals SET status='archived',updated_at=? WHERE id=?",(now_iso(),gid));audit(conn,"goal",gid,"archived",before=dict(row))
                return {"status":"archived"}
        if method=="GET" and path=="/api/calendar":
            calendar=params.get("calendar") or _setting(conn,"calendar","jalali");raw_month=params.get("month")
            bounds=_month_bounds(raw_month,calendar) if raw_month else ledger.period_dates(date.today(),"month",calendar)
            start,end=bounds
            rows=ledger.transaction_rows(conn,start=start.isoformat(),end=end.isoformat())
            days={}
            for row in rows:
                item=days.setdefault(row["date"],{"income":"0","expenses":"0","transactions":[]})
                key="income" if row["kind"]=="income" else "expenses"
                item[key]=money_text(decimal_value(item[key])+decimal_value(row["display_amount"]))
                item["transactions"].append(row)
            recurring=[dict(r) for r in conn.execute("SELECT * FROM recurring WHERE active=1 AND next_date<=? AND (end_date IS NULL OR end_date>=?)",(end.isoformat(),start.isoformat()))]
            obligations=[_debt_item(conn,r) for r in conn.execute("SELECT * FROM debts WHERE status='open' AND due_date BETWEEN ? AND ?",(start.isoformat(),end.isoformat()))]
            return {"month":raw_month,"from":start.isoformat(),"to":end.isoformat(),"days":days,"recurring":recurring,"obligations":obligations}
        if method=="GET" and path=="/api/filters":return [dict(r,filters=json.loads(r["filters_json"])) for r in conn.execute("SELECT * FROM saved_filters ORDER BY name")]
        if method=="POST" and path=="/api/filters":
            name=_required_text(body,"name",80);filters=body.get("filters") or {}
            if not isinstance(filters,dict):_err(422,"Saved filter data is invalid.")
            with transaction(conn):
                try:cur=conn.execute("INSERT INTO saved_filters(name,filters_json,created_at) VALUES(?,?,?)",(name,json.dumps(filters,ensure_ascii=False),now_iso()))
                except sqlite3.IntegrityError:_err(409,"A saved filter with this name already exists.")
                audit(conn,"saved_filter",cur.lastrowid,"created",after=body)
            return {"id":cur.lastrowid,"name":name,"filters":filters}
        if path.startswith("/api/filters/") and method=="DELETE":
            fid=_int(path.rsplit("/",1)[-1],None,1)
            row=conn.execute("SELECT * FROM saved_filters WHERE id=?",(fid,)).fetchone()
            if not row:_err(404,"Saved filter not found.")
            with transaction(conn):conn.execute("DELETE FROM saved_filters WHERE id=?",(fid,));audit(conn,"saved_filter",fid,"deleted",before=dict(row))
            return {"deleted":True}
        if method=="GET" and path=="/api/settings":
            return {"settings":{r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM settings")},
                    "notifications":[dict(r) for r in conn.execute("SELECT * FROM notification_settings")],"database":str(database.db_path())}
        if method=="PATCH" and path=="/api/settings":
            allowed={"language","calendar","currency_unit","theme","accent"};changed={k:v for k,v in body.items() if k in allowed}
            if "language" in changed and changed["language"] not in ("fa","en"):_err(422,"Language is invalid.")
            if "calendar" in changed and changed["calendar"] not in ("jalali","gregorian"):_err(422,"Calendar is invalid.")
            if "theme" in changed and changed["theme"] not in ("dark","light","auto"):_err(422,"Theme is invalid.")
            if "accent" in changed and changed["accent"] not in ("teal","violet","orange","pink","blue","amber"):_err(422,"Accent is invalid.")
            with transaction(conn):
                for key,value in changed.items():conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),now_iso()))
                audit(conn,"settings","app","updated",after=changed)
            return {"settings":{r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM settings")}}
        if method=="PATCH" and path=="/api/notifications/settings":
            values=body.get("items",{})
            if not isinstance(values,dict):_err(422,"Notification preferences are invalid.")
            with transaction(conn):
                for key,item in values.items():
                    if not conn.execute("SELECT 1 FROM notification_settings WHERE key=?",(key,)).fetchone():continue
                    conn.execute("UPDATE notification_settings SET enabled=?,threshold=? WHERE key=?",(int(_bool(item.get("enabled"),True)),_int(item.get("threshold"),None,1,100),key))
                audit(conn,"notification_settings","all","updated",after=values)
            return [dict(r) for r in conn.execute("SELECT * FROM notification_settings")]
        if method=="GET" and path=="/api/notifications":
            items=ledger.insights(conn,calendar=_setting(conn,"calendar","jalali"));stored=[dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50")]
            return {"insights":items,"history":stored,"unread":sum(1 for r in stored if not r["read_at"])}
        if method=="POST" and path=="/api/notifications/read":
            nid=body.get("id")
            with transaction(conn):
                if nid:conn.execute("UPDATE notifications SET read_at=? WHERE id=?",(now_iso(),int(nid)))
                else:conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL",(now_iso(),))
            return {"read":True}
        if method=="GET" and path=="/api/audit":
            limit=_int(params.get("limit"),100,1,500)
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",(limit,))]
        if method=="GET" and path=="/api/backup":
            with transaction(conn):backup=_make_backup(conn)
            return ResponseData(json.dumps(backup,ensure_ascii=False,indent=2).encode("utf-8"),"application/json","dara-backup.json")
        if method=="POST" and path=="/api/restore":return _restore(conn,body)
        if method=="POST" and path=="/api/import/preview":return _import_preview(conn,body)
        if method=="POST" and path=="/api/import/commit":return _import_commit(conn,body)
        if method=="GET" and path in ("/api/exports/transactions.csv","/api/exports/transactions.xlsx"):
            rows=ledger.transaction_rows(conn,start=params.get("from"),end=params.get("to"),kind=params.get("kind"),q=params.get("q"),category=params.get("category"),account_id=params.get("account_id"),min_amount=params.get("min_amount"),max_amount=params.get("max_amount"),included=params.get("included"),tags=params.get("tags"))
            exported=[{"id":r["id"],"date":r["date"],"time":r["time"] or "","type":r["kind"],"title":r["title"],"amount":r["amount"],"category":r["display_category"],"subcategory":r["subcategory"],"note":r["note"],"account":r["account_name"] or "","tags":", ".join(r["tags"]),"source":r["source_sheet"] or "","source_row":r["source_row"] or "","locked":r["locked"]} for r in rows]
            headers={"id":"ID","date":"Date","time":"Time","type":"Type","title":"Title","amount":"Amount","category":"Category","subcategory":"Subcategory","note":"Note","account":"Account","tags":"Tags","source":"Source","source_row":"Source row","locked":"Imported"}
            if path.endswith(".csv"):
                text=io.StringIO();writer=csv.DictWriter(text,fieldnames=list(headers),extrasaction="ignore");writer.writerow(headers);writer.writerows(exported)
                return ResponseData(text.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8","transactions.csv")
            return ResponseData(_xlsx_export(exported,headers),"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","transactions.xlsx")
        if method=="GET" and path=="/api/date/convert":
            value=params.get("value");calendar=params.get("calendar","jalali");day=_date(value,calendar)
            g=date.fromisoformat(day)
            from .jalali import gregorian_to_jalali
            return {"iso":day,"jalali":"%04d-%02d-%02d"%gregorian_to_jalali(g),"gregorian":day}
        _err(404,"Route not found.")
    except ApiError:
        raise
    except (sqlite3.IntegrityError,sqlite3.OperationalError) as exc:
        raise ApiError(409,"Database operation failed.",str(exc)) from None
    except (ValueError,TypeError,MoneyError) as exc:
        raise ApiError(422,str(exc)) from None
    finally:
        conn.close()
