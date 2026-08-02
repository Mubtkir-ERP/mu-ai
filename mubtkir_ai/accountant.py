# -*- coding: utf-8 -*-
"""AI accountant: bank statement -> classified draft Journal Entry.

Safety model (research-backed — unguided LLM entries are rarely correct):
- A DETERMINISTIC parser reads dates/amounts from CSV/XLSX. The LLM never
  touches numbers.
- The LLM only SUGGESTS a contra account per transaction, chosen strictly
  from the company's real chart of accounts.
- Code builds the Journal Entry, asserts debits == credits, and saves it
  as a DRAFT. Submission is always a human decision.
"""

import io
import json
import re

import frappe
from frappe.utils import flt, getdate, nowdate

MAX_TRANSACTIONS = 50
MAX_ACCOUNTS_IN_PROMPT = 60

# Header synonyms for column detection (normalized, lowercase)
HEADERS = {
	"date": ("date", "التاريخ", "تاريخ", "posting date"),
	"description": ("description", "البيان", "الوصف", "التفاصيل", "narration", "details"),
	"debit": ("debit", "مدين", "سحب", "withdrawal", "خصم"),
	"credit": ("credit", "دائن", "إيداع", "ايداع", "deposit"),
	"amount": ("amount", "المبلغ", "القيمة"),
}


# ------------------------------------------------------------ deterministic parse


def _normalize_header(value):
	return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _detect_columns(header_row):
	"""Map semantic roles to column indexes; None when a role is absent."""
	normalized = [_normalize_header(h) for h in header_row]
	mapping = {}
	for role, aliases in HEADERS.items():
		mapping[role] = next(
			(i for i, h in enumerate(normalized) if h in aliases), None
		)
	if mapping["date"] is None or mapping["description"] is None:
		frappe.throw("لم يُتعرف على أعمدة الكشف — يلزم عمودا التاريخ والبيان على الأقل")
	if mapping["amount"] is None and mapping["debit"] is None and mapping["credit"] is None:
		frappe.throw("لم يُعثر على أعمدة المبالغ (مدين/دائن أو المبلغ)")
	return mapping


def _to_amount(value):
	"""Parse a money cell defensively (commas, blanks, Arabic digits)."""
	s = str(value if value is not None else "").strip()
	if not s:
		return 0.0
	s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫،", "0123456789.,"))
	s = s.replace(",", "").replace("SAR", "").replace("ر.س", "").strip()
	try:
		return flt(s)
	except Exception:
		return 0.0


def _rows_from_file(file_url):
	"""Yield raw rows from a CSV or XLSX attachment."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	content = file_doc.get_content()
	name = (file_doc.file_name or file_url).lower()

	if name.endswith((".xlsx", ".xls")):
		import openpyxl

		wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
		sheet = wb.active
		return [[c for c in row] for row in sheet.iter_rows(values_only=True)]

	if isinstance(content, bytes):
		content = content.decode("utf-8-sig", errors="replace")
	import csv

	return list(csv.reader(io.StringIO(content)))


def parse_statement(file_url):
	"""File -> normalized transactions: [{date, description, in, out}]."""
	rows = [r for r in _rows_from_file(file_url) if any(str(c or "").strip() for c in r)]
	if len(rows) < 2:
		frappe.throw("الملف فارغ أو بلا صفوف بيانات")

	cols = _detect_columns(rows[0])
	transactions = []
	for row in rows[1 : MAX_TRANSACTIONS + 1]:
		def cell(role):
			idx = cols[role]
			return row[idx] if idx is not None and idx < len(row) else None

		try:
			date = str(getdate(cell("date")))
		except Exception:
			continue  # skip non-data rows (footers, totals)

		if cols["amount"] is not None:
			amount = _to_amount(cell("amount"))
			money_in, money_out = (amount, 0.0) if amount >= 0 else (0.0, -amount)
		else:
			money_out = _to_amount(cell("debit"))
			money_in = _to_amount(cell("credit"))

		if not money_in and not money_out:
			continue
		transactions.append(
			{
				"date": date,
				"description": str(cell("description") or "").strip()[:200],
				"in": money_in,
				"out": money_out,
			}
		)

	if not transactions:
		frappe.throw("لم تُستخرج أي حركة صالحة من الملف")
	return transactions


# ------------------------------------------------------------- AI classification


def _candidate_accounts(company):
	"""Leaf income/expense accounts the model may choose from."""
	return frappe.get_all(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"root_type": ["in", ("Income", "Expense")],
			"disabled": 0,
		},
		fields=["name", "account_name", "root_type"],
		limit_page_length=MAX_ACCOUNTS_IN_PROMPT,
	)


def _fallback_account(accounts, root_type):
	return next((a["name"] for a in accounts if a["root_type"] == root_type), None)


def classify_transactions(transactions, company):
	"""Ask the model to pick a contra account per transaction (names only).

	Returns the transactions with a `suggested_account` each. Any invalid or
	missing suggestion falls back to a sane default instead of failing.
	"""
	from mubtkir_ai import ai_gateway
	from mubtkir_ai.rag import normalize_ar

	accounts = _candidate_accounts(company)
	if not accounts:
		frappe.throw(f"لا توجد حسابات مصروفات/إيرادات في شركة {company}")

	# Models often echo the bare account_name without the company suffix
	# ("- P"), so resolve suggestions leniently: full name or bare name,
	# Arabic-normalized on both sides.
	resolver = {}
	for a in accounts:
		resolver[normalize_ar(a["name"])] = a["name"]
		resolver[normalize_ar(a["account_name"])] = a["name"]

	prompt = (
		"صنّف حركات كشف الحساب البنكي التالية. لكل حركة اختر حسابًا واحدًا "
		"من قائمة الحسابات فقط (بالاسم الكامل حرفيًا). الحركات الصادرة (out) "
		"غالبًا مصروفات، والواردة (in) غالبًا إيرادات.\n\n"
		"الحسابات المتاحة:\n"
		+ "\n".join(f"- {a['name']} ({a['root_type']})" for a in accounts)
		+ "\n\nالحركات:\n"
		+ "\n".join(
			f"{i}: {t['description']} | in={t['in']} out={t['out']}"
			for i, t in enumerate(transactions)
		)
		+ '\n\nأجب بـ JSON فقط بالشكل: {"0": "اسم الحساب", "1": "..."}'
	)

	mapping = {}
	try:
		text, _usage, _tools = ai_gateway.complete(prompt, session_id="accountant")
		match = re.search(r"\{.*\}", text, re.S)
		if match:
			mapping = json.loads(match.group(0))
	except Exception:
		frappe.log_error(title="Mubtkir accountant classify error", message=frappe.get_traceback())

	for i, txn in enumerate(transactions):
		suggested = resolver.get(normalize_ar(str(mapping.get(str(i)) or "")))
		if suggested:
			txn["classified_by"] = "ai"
		else:
			root = "Expense" if txn["out"] else "Income"
			suggested = _fallback_account(accounts, root)
			txn["classified_by"] = "fallback"
		txn["suggested_account"] = suggested
	return transactions


# ----------------------------------------------------------------- JE builder


def build_draft_journal_entry(transactions, bank_account, company):
	"""Deterministically build a balanced DRAFT Journal Entry.

	Per transaction: money out -> debit suggested account / credit bank;
	money in -> debit bank / credit suggested account. The balance assertion
	is a hard gate — an unbalanced entry never reaches the accountant.
	"""
	rows = []
	for txn in transactions:
		amount = txn["out"] or txn["in"]
		bank_side = {"account": bank_account, "user_remark": txn["description"]}
		contra_side = {"account": txn["suggested_account"], "user_remark": txn["description"]}
		if txn["out"]:
			contra_side["debit_in_account_currency"] = amount
			bank_side["credit_in_account_currency"] = amount
		else:
			bank_side["debit_in_account_currency"] = amount
			contra_side["credit_in_account_currency"] = amount
		rows.extend([contra_side, bank_side])

	total_debit = sum(flt(r.get("debit_in_account_currency")) for r in rows)
	total_credit = sum(flt(r.get("credit_in_account_currency")) for r in rows)
	if abs(total_debit - total_credit) > 0.005:
		frappe.throw(
			f"القيد غير متوازن (مدين {total_debit} ≠ دائن {total_credit}) — أُلغي الإنشاء"
		)

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": company,
			"posting_date": transactions[-1]["date"] or nowdate(),
			"user_remark": "قيد مقترح من المحاسب الذكي — بانتظار مراجعة المحاسب",
			"accounts": rows,
		}
	)
	je.insert()  # permission-checked; stays a draft
	return je


# ------------------------------------------------------------------ orchestration


def process_upload(upload_name):
	"""Full pipeline for one upload; safe to re-run after failures."""
	doc = frappe.get_doc("AI Accounting Upload", upload_name)
	try:
		doc.db_set("status", "Processing", update_modified=False)

		transactions = parse_statement(doc.file)
		transactions = classify_transactions(transactions, doc.company)
		je = build_draft_journal_entry(transactions, doc.bank_account, doc.company)

		doc.db_set("journal_entry", je.name, update_modified=False)
		doc.db_set("txn_count", len(transactions), update_modified=False)
		doc.db_set("total_in", sum(t["in"] for t in transactions), update_modified=False)
		doc.db_set("total_out", sum(t["out"] for t in transactions), update_modified=False)
		doc.db_set(
			"transactions_preview",
			json.dumps(transactions, ensure_ascii=False, indent=1),
			update_modified=False,
		)
		doc.db_set("status", "Processed", update_modified=False)
		doc.db_set("error_message", "", update_modified=False)
		frappe.db.commit()
		return {"journal_entry": je.name, "transactions": len(transactions)}
	except Exception as e:
		frappe.db.rollback()
		doc.db_set("status", "Failed", update_modified=False)
		doc.db_set("error_message", str(e)[:500], update_modified=False)
		frappe.db.commit()
		frappe.log_error(title="Mubtkir accountant error", message=frappe.get_traceback())
		raise
