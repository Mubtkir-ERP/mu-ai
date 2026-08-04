# -*- coding: utf-8 -*-
"""Live ERPNext data tools exposed to the AI assistant via function calling.

Security model (borrowed from Raven): every read goes through Frappe APIs
that enforce the *current user's* permissions (`frappe.get_list`,
`frappe.client.get`). No raw SQL, no permission bypass — whatever the user
cannot see in the Desk, the assistant cannot see either.

Adding a tool:
    1. Append an OpenAI-format schema to `TOOL_SCHEMAS` with a prescriptive
       "call this when..." description (improves should-call accuracy).
    2. Implement the executor and register it in `_EXECUTORS`.
"""

import json
import re

import frappe

MAX_ROWS = 50
MAX_RESULT_CHARS = 6000

_FIELD_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_ORDER_BY_RE = re.compile(r"^[a-zA-Z0-9_]+( (asc|desc))?$", re.I)

# Set on frappe.local during a request so the chat layer can surface
# side effects (e.g. the created ticket) back to the UI.
_ARTIFACTS_KEY = "mubtkir_tool_artifacts"


# --------------------------------------------------------------------- schemas

TOOL_SCHEMAS = [
	{
		"type": "function",
		"function": {
			"name": "count_documents",
			"description": (
				"Count ERPNext documents the current user is permitted to see. "
				"Call whenever the user asks 'how many' about business data "
				"(invoices, customers, items, orders, tickets...). "
				"Common doctypes: Sales Invoice, Purchase Invoice, Customer, "
				"Supplier, Item, Sales Order, Payment Entry, Employee, Project."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string", "description": "ERPNext DocType, e.g. 'Sales Invoice'"},
					"filters": {
						"type": "object",
						"description": (
							"Optional filters: {field: value} or {field: [operator, value]}. "
							"Operators: =, !=, >, <, >=, <=, like, in, between. "
							'Example: {"posting_date": [">=", "2026-07-01"]}'
						),
					},
				},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "list_documents",
			"description": (
				"List ERPNext documents the current user is permitted to see. "
				"Call when the user asks to view records ('اعرض آخر الفواتير'). "
				"Returns at most 50 rows."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"filters": {"type": "object"},
					"fields": {
						"type": "array",
						"items": {"type": "string"},
						"description": 'Fields to return, e.g. ["name","customer","grand_total","status"]',
					},
					"order_by": {"type": "string", "description": "e.g. 'modified desc'"},
					"limit": {"type": "integer", "description": "Max rows (default 10, max 50)"},
				},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "get_document",
			"description": (
				"Fetch one ERPNext document by ID with full details "
				"(e.g. invoice ACC-SINV-2026-00001). Permission-checked."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"name": {"type": "string", "description": "Document ID"},
				},
				"required": ["doctype", "name"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "sum_field",
			"description": (
				"Sum a numeric field over permitted documents. "
				"Call for totals: 'كم إجمالي مبيعات هذا الشهر' -> "
				"doctype='Sales Invoice', field='grand_total', posting_date filter. "
				"Tax total field on invoices is 'total_taxes_and_charges'. "
				"Submittable doctypes default to SUBMITTED docs only (docstatus=1); "
				"pass {'docstatus': 0} explicitly for drafts. "
				"If the result contains numeric_fields, retry with one of them. "
				"Mention in your answer that totals cover submitted documents. "
				"Do NOT invent filters (especially date ranges) the user did not "
				"explicitly ask for — 'كامل/الكل' means NO date filter at all."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"field": {"type": "string", "description": "Numeric field, e.g. grand_total"},
					"filters": {"type": "object"},
				},
				"required": ["doctype", "field"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "aggregate_documents",
			"description": (
				"Group and aggregate documents: 'which X appears most', totals per "
				"customer/item/status, top-N breakdowns. "
				"For items inside invoices use the CHILD table: "
				"doctype='Sales Invoice Item', group_by='item_code', "
				"parent_doctype='Sales Invoice' (items are NOT fields on the "
				"invoice itself). Same pattern: Purchase Invoice Item, "
				"Sales Order Item. aggregate='count' for frequency, 'sum' with "
				"aggregate_field (e.g. qty or amount) for totals per group. "
				"Only submitted parent documents are counted. "
				"Do NOT invent filters the user did not ask for."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string", "description": "DocType or child table, e.g. 'Sales Invoice Item'"},
					"group_by": {"type": "string", "description": "Field to group by, e.g. 'item_code'"},
					"aggregate": {"type": "string", "enum": ["count", "sum"], "description": "Default count"},
					"aggregate_field": {"type": "string", "description": "Numeric field to sum (required for sum)"},
					"parent_doctype": {"type": "string", "description": "Required for child tables, e.g. 'Sales Invoice'"},
					"filters": {"type": "object"},
					"limit": {"type": "integer", "description": "Top N groups (default 10, max 50)"},
				},
				"required": ["doctype", "group_by"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "generate_report",
			"description": (
				"Build a tabular report EXACTLY as the user requested it. "
				"Call when the user asks for a report ('اشتي تقرير...') and names "
				"the data and columns. Pass columns as TECHNICAL ENGLISH fieldnames "
				"in the user's order (اسم العميل -> customer_name, التاريخ -> "
				"posting_date, الإجمالي -> grand_total, الحالة -> status, "
				"رقم الفاتورة -> name); Arabic names are tolerated but fieldnames "
				"are preferred. If the result contains available_fields, retry with "
				"fieldnames from it. The UI renders the full table and a CSV "
				"download automatically — do NOT repeat rows in your reply; confirm "
				"briefly with the row count and any dropped columns. Do NOT add "
				"filters or a limit the user did not explicitly ask for."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"title": {"type": "string", "description": "Report title in the user's language"},
					"doctype": {"type": "string"},
					"columns": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Fieldnames in the exact order the user asked for",
					},
					"filters": {"type": "object"},
					"order_by": {"type": "string"},
					"limit": {"type": "integer", "description": "Max rows (default 100, max 500)"},
				},
				"required": ["title", "doctype", "columns"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "create_document",
			"description": (
				"Create a NEW ERPNext document as a DRAFT (never submitted), on the "
				"user's explicit request ('أنشئ لي صنف/فاتورة/عميل...'). "
				"BEFORE calling: make sure you have the essential fields — if any "
				"are missing, ASK the user short questions in Arabic instead of "
				"guessing values. Verify referenced records (customer, item...) "
				"exist using list_documents first when unsure. "
				"Field cheatsheet — Item: item_code, item_group, stock_uom. "
				"Customer: customer_name, customer_type. "
				"Sales Invoice: customer + items:[{item_code, qty, rate}]. "
				"If the result contains missing_fields, ask the user for them. "
				"After success tell the user the document number (the UI shows a "
				"link automatically)."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string", "description": "ERPNext DocType, e.g. 'Sales Invoice'"},
					"values": {
						"type": "object",
						"description": (
							"Field values. Child tables as arrays of objects, "
							'e.g. {"customer": "X", "items": [{"item_code": "A", "qty": 2, "rate": 100}]}'
						),
					},
				},
				"required": ["doctype", "values"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "create_support_ticket",
			"description": (
				"Create a support ticket (HD Ticket) for the human support team. "
				"Call ONLY when: you cannot resolve the user's problem, the user "
				"explicitly asks for human support, or the user reports a system "
				"error/bug you cannot fix. Do NOT call for questions you can answer. "
				"After calling, tell the user their ticket number and that the "
				"conversation context was attached."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"subject": {"type": "string", "description": "Short ticket subject in the user's language"},
					"description": {
						"type": "string",
						"description": "Summary of the problem and what was already tried",
					},
				},
				"required": ["subject", "description"],
			},
		},
	},
]


# ------------------------------------------------------------------- internals


def _check_doctype(doctype):
	"""Early, explicit validation; get_list/client.get re-check anyway."""
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(f"DocType غير موجود: {doctype}")
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(f"لا تملك صلاحية قراءة {doctype}")


def _safe_fields(fields):
	valid = [f for f in fields or [] if isinstance(f, str) and _FIELD_RE.match(f)]
	return valid or ["name"]


def _normalize_filters(filters):
	"""Accept both Frappe's list-form and the Mongo-style object-form.

	Models frequently emit {"posting_date": {"between": [a, b]}} instead of
	{"posting_date": ["between", [a, b]]}; the former crashes db_query, so
	single-operator dicts are converted rather than rejected.
	"""
	out = {}
	for field, cond in (filters or {}).items():
		if isinstance(cond, dict) and len(cond) == 1:
			op, value = next(iter(cond.items()))
			out[field] = [op, value]
		else:
			out[field] = cond
	return out


def _with_default_filters(doctype, filters):
	"""Normalize filters, then default to SUBMITTED docs for submittable
	doctypes.

	Financial questions ("كم مجموع الفواتير") must reflect the books —
	drafts and cancelled documents silently inflating totals is a
	correctness bug. The model can still pass an explicit docstatus
	filter to inspect drafts (documented in the tool descriptions).
	"""
	filters = _normalize_filters(filters)
	if "docstatus" not in filters and frappe.get_meta(doctype).is_submittable:
		filters["docstatus"] = 1
	return filters


def _numeric_field_catalog(doctype, limit=20):
	"""{fieldname, label} for numeric fields — lets the model self-correct."""
	catalog = []
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in ("Currency", "Float", "Int"):
			catalog.append({"fieldname": df.fieldname, "label": df.label or df.fieldname})
			if len(catalog) >= limit:
				break
	return catalog


def _serialize(data):
	text = json.dumps(data, ensure_ascii=False, default=str)
	if len(text) > MAX_RESULT_CHARS:
		text = text[:MAX_RESULT_CHARS] + "… (truncated)"
	return text


def _artifacts():
	if not hasattr(frappe.local, _ARTIFACTS_KEY):
		setattr(frappe.local, _ARTIFACTS_KEY, {})
	return getattr(frappe.local, _ARTIFACTS_KEY)


def get_artifacts():
	"""Side effects produced by tools during the current request."""
	return getattr(frappe.local, _ARTIFACTS_KEY, {})


def reset_artifacts():
	"""Clear per-message state. Must run at the start of every chat turn so
	artifacts (created docs, tickets, reports) never leak between messages."""
	setattr(frappe.local, _ARTIFACTS_KEY, {})


# ------------------------------------------------------------------- executors


def count_documents(doctype, filters=None):
	_check_doctype(doctype)
	filters = _with_default_filters(doctype, filters)
	# get_list applies user permissions; capped so huge tables stay cheap.
	rows = frappe.get_list(doctype, filters=filters, fields=["name"], limit_page_length=1001)
	n = len(rows)
	return {"doctype": doctype, "count": n if n <= 1000 else "1000+", "filters_used": filters}


def list_documents(doctype, filters=None, fields=None, order_by=None, limit=10):
	_check_doctype(doctype)
	if order_by and not _ORDER_BY_RE.match(order_by):
		order_by = None
	filters = _with_default_filters(doctype, filters)
	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=_safe_fields(fields),
		order_by=order_by or "modified desc",
		limit_page_length=min(int(limit or 10), MAX_ROWS),
	)
	return {"doctype": doctype, "rows": rows, "returned": len(rows), "filters_used": filters}


def get_document(doctype, name):
	_check_doctype(doctype)
	from frappe.client import get as client_get

	# client.get enforces document + field-level read permissions.
	doc = client_get(doctype, name=name)
	return {k: v for k, v in doc.items() if not k.startswith("_") and k not in ("docstatus", "idx")}


def sum_field(doctype, field, filters=None):
	_check_doctype(doctype)
	meta = frappe.get_meta(doctype)
	df = meta.get_field(field or "")
	if not _FIELD_RE.match(field or "") or not df or df.fieldtype not in _NUMERIC_FIELDTYPES:
		# Recoverable: hand the model the real numeric fields to retry with.
		return {
			"error": f"الحقل {field} غير موجود أو ليس رقميًا في {doctype}",
			"numeric_fields": _numeric_field_catalog(doctype),
			"hint": "Call sum_field again with a fieldname from numeric_fields.",
		}

	filters = _with_default_filters(doctype, filters)
	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=[f"sum(`{field}`) as total", "count(name) as rows_counted"],
	)
	row = rows[0] if rows else {}
	return {
		"doctype": doctype,
		"field": field,
		"total": row.get("total") or 0,
		"rows_counted": row.get("rows_counted") or 0,
		"filters_used": filters,
	}


MAX_REPORT_ROWS = 500
DEFAULT_REPORT_ROWS = 100
MAX_GROUPS = 50
_NUMERIC_FIELDTYPES = ("Currency", "Float", "Int")


def _validated_field(meta, fieldname, numeric=False):
	"""A fieldname that provably exists on the doctype (SQL-injection gate)."""
	if not (isinstance(fieldname, str) and _FIELD_RE.match(fieldname)):
		return None
	df = meta.get_field(fieldname)
	if not df and fieldname != "name":
		return None
	if numeric and (not df or df.fieldtype not in _NUMERIC_FIELDTYPES):
		return None
	return fieldname


def aggregate_documents(
	doctype, group_by, aggregate="count", aggregate_field=None, parent_doctype=None, filters=None, limit=10
):
	"""GROUP BY aggregation over a doctype or a child table.

	Child tables carry no permissions of their own, so access is gated on
	READ permission of the parent doctype, and rows are joined to SUBMITTED
	parents only. All identifiers are validated against meta before they
	reach SQL; values are always parameterized.
	"""
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(f"DocType غير موجود: {doctype}")
	meta = frappe.get_meta(doctype)
	limit = min(int(limit or 10), MAX_GROUPS)

	group_field = _validated_field(meta, group_by)
	if not group_field:
		return {"error": f"حقل التجميع {group_by} غير موجود في {doctype}"}

	sum_field_name = None
	if aggregate == "sum":
		sum_field_name = _validated_field(meta, aggregate_field, numeric=True)
		if not sum_field_name:
			return {
				"error": f"حقل الجمع {aggregate_field} غير موجود أو ليس رقميًا",
				"numeric_fields": _numeric_field_catalog(doctype),
			}

	select_agg = f"SUM(c.`{sum_field_name}`)" if sum_field_name else "COUNT(*)"

	if meta.istable:
		# Child table: permission comes from the parent doctype.
		if not parent_doctype or not frappe.db.exists("DocType", parent_doctype):
			return {
				"error": "الجداول الفرعية تتطلب parent_doctype صحيحًا",
				"hint": "e.g. doctype='Sales Invoice Item' -> parent_doctype='Sales Invoice'",
			}
		if not frappe.has_permission(parent_doctype, "read"):
			frappe.throw(f"لا تملك صلاحية قراءة {parent_doctype}")

		parent_meta = frappe.get_meta(parent_doctype)
		docstatus_cond = "AND p.docstatus = 1" if parent_meta.is_submittable else ""
		rows = frappe.db.sql(
			f"""SELECT c.`{group_field}` AS value, {select_agg} AS total
				FROM `tab{doctype}` c
				JOIN `tab{parent_doctype}` p ON c.parent = p.name AND c.parenttype = %s
				WHERE 1=1 {docstatus_cond}
				GROUP BY c.`{group_field}`
				ORDER BY total DESC
				LIMIT {limit}""",
			(parent_doctype,),
			as_dict=True,
		)
		scope_note = f"submitted {parent_doctype} documents only"
	else:
		if not frappe.has_permission(doctype, "read"):
			frappe.throw(f"لا تملك صلاحية قراءة {doctype}")
		normalized = _with_default_filters(doctype, filters)
		agg_alias = f"sum(`{sum_field_name}`) as total" if sum_field_name else "count(name) as total"
		rows = frappe.get_list(
			doctype,
			filters=normalized,
			fields=[f"`{group_field}` as value", agg_alias],
			group_by=f"`tab{doctype}`.`{group_field}`",
			order_by="total desc",
			limit_page_length=limit,
		)
		scope_note = f"filters: {normalized}"

	return {
		"doctype": doctype,
		"group_by": group_field,
		"aggregate": "sum:" + sum_field_name if sum_field_name else "count",
		"groups": rows,
		"scope": scope_note,
	}

# Frequent Arabic column names -> canonical fieldnames. Checked before the
# meta-label match so the most common requests resolve deterministically.
_COMMON_COLUMN_ALIASES = {
	"رقم الفاتوره": "name",
	"رقم المستند": "name",
	"الرقم": "name",
	"اسم العميل": "customer_name",
	"العميل": "customer",
	"اسم المورد": "supplier_name",
	"المورد": "supplier",
	"التاريخ": "posting_date",
	"تاريخ الفاتوره": "posting_date",
	"الاجمالي": "grand_total",
	"المبلغ": "grand_total",
	"الحاله": "status",
	"الاسم": "name",
}


def _resolve_column(meta, requested):
	"""Resolve a requested column to a real fieldname.

	Accepts technical fieldnames as-is, then falls back to matching the
	user's wording (Arabic-normalized) against a common-alias map, field
	labels, and their translations. Returns None when nothing matches.
	"""
	from mubtkir_ai.rag import normalize_ar

	if not isinstance(requested, str) or not requested.strip():
		return None
	requested = requested.strip()

	# 1) Already a valid fieldname
	if _FIELD_RE.match(requested) and (requested == "name" or meta.get_field(requested)):
		return requested

	normalized = normalize_ar(requested)

	# 2) Common Arabic aliases (keys stored pre-normalized)
	alias = _COMMON_COLUMN_ALIASES.get(normalized)
	if alias and (alias == "name" or meta.get_field(alias)):
		return alias

	# 3) Field label or its translation matches the user's wording
	for df in meta.fields:
		label = df.label or ""
		if normalized in (normalize_ar(label), normalize_ar(frappe._(label))):
			return df.fieldname
	return None


def _field_catalog(meta, limit=30):
	"""Compact {fieldname, label} sample the model can use to self-correct."""
	catalog = [{"fieldname": "name", "label": "ID"}]
	for df in meta.fields:
		if df.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML"):
			continue
		catalog.append({"fieldname": df.fieldname, "label": df.label or df.fieldname})
		if len(catalog) >= limit:
			break
	return catalog


def generate_report(title, doctype, columns, filters=None, order_by=None, limit=None):
	"""Build a user-specified tabular report.

	The user dictates the columns and their order; Arabic column names are
	resolved to real fieldnames (see `_resolve_column`). The full table is
	handed to the UI through the request artifacts — the model only
	receives a compact summary to keep token usage low.
	"""
	_check_doctype(doctype)
	meta = frappe.get_meta(doctype)

	valid, unknown = [], []
	for col in columns or []:
		resolved = _resolve_column(meta, col)
		if resolved and resolved not in valid:
			valid.append(resolved)
		elif not resolved:
			unknown.append(col)

	if not valid:
		# Recoverable error: give the model the real fields so it can
		# retry with correct fieldnames in the next tool round.
		return {
			"error": "لم يُتعرَّف على أي عمود من المطلوبة",
			"requested_columns": columns,
			"available_fields": _field_catalog(meta),
			"hint": "Call generate_report again using 'fieldname' values from available_fields.",
		}

	if order_by and not _ORDER_BY_RE.match(order_by):
		order_by = None

	filters = _with_default_filters(doctype, filters)
	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=valid,
		order_by=order_by or "modified desc",
		limit_page_length=min(int(limit or DEFAULT_REPORT_ROWS), MAX_REPORT_ROWS),
	)

	def column_meta(fieldname):
		df = meta.get_field(fieldname)
		return {
			"fieldname": fieldname,
			"label": (df and df.label) or fieldname,
			"numeric": bool(df and df.fieldtype in _NUMERIC_FIELDTYPES),
		}

	report_columns = [column_meta(c) for c in valid]
	totals = {
		c["fieldname"]: sum(frappe.utils.flt(r.get(c["fieldname"])) for r in rows)
		for c in report_columns
		if c["numeric"]
	}

	_artifacts()["report"] = {
		"title": title,
		"doctype": doctype,
		"columns": report_columns,
		"rows": rows,
		"totals": totals,
		"row_count": len(rows),
	}
	return {
		"title": title,
		"row_count": len(rows),
		"columns": [c["fieldname"] for c in report_columns],
		"unknown_columns": unknown,
		"preview": rows[:5],
		"note": "Full table + CSV download already shown to the user.",
	}


# Doctypes the assistant must never create, even with write actions enabled —
# security/config surfaces where a hallucinated insert could be dangerous.
WRITE_BLACKLIST = {
	"User", "Role", "Role Profile", "User Permission", "DocType", "DocField",
	"Custom Field", "Property Setter", "Server Script", "Client Script",
	"System Settings", "Data Import", "Webhook", "API Key", "OAuth Client",
	"Mubtkir AI Settings",
}


def _write_enabled():
	from mubtkir_ai.ai_gateway import get_settings

	return bool(get_settings().get("enable_write_actions"))


def get_tool_schemas():
	"""Tool schemas for the current request; write tools appear only when
	the site setting allows them, so a disabled feature is invisible to the
	model instead of a runtime refusal."""
	if _write_enabled():
		return TOOL_SCHEMAS
	return [t for t in TOOL_SCHEMAS if t["function"]["name"] != "create_document"]


def create_document(doctype, values):
	"""Create a draft document with the full Frappe permission engine active.

	No ignore_permissions: whatever the current user cannot create in the
	Desk, the assistant cannot create either. Mandatory-field errors are
	returned as recoverable data so the model asks the user instead of
	failing the whole request.
	"""
	if not _write_enabled():
		return {"error": "ميزة الإنشاء عبر المساعد معطلة — فعِّلها من إعدادات مُبتكِر"}
	if doctype in WRITE_BLACKLIST:
		return {"error": f"إنشاء {doctype} غير مسموح عبر المساعد لأسباب أمنية"}
	_check_doctype(doctype)
	if not frappe.has_permission(doctype, "create"):
		return {"error": f"لا تملك صلاحية إنشاء {doctype}"}
	if not isinstance(values, dict) or not values:
		return {"error": "أرسل قيم الحقول في values"}

	# Internal fields are never model-controlled; drafts only.
	clean = {k: v for k, v in values.items() if not k.startswith("_")}
	for forbidden in ("docstatus", "owner", "name"):
		clean.pop(forbidden, None)

	try:
		doc = frappe.get_doc({"doctype": doctype, **clean})
		doc.insert()  # permission-checked insert; stays a draft
	except frappe.MandatoryError as e:
		frappe.db.rollback()
		return {
			"missing_fields": str(e).split(":")[-1].strip(),
			"hint": "Ask the user for these fields, then call create_document again.",
		}
	except frappe.LinkValidationError as e:
		frappe.db.rollback()
		return {
			"invalid_link": str(e)[:300],
			"hint": (
				"A linked value does not exist on this site. Call list_documents "
				"on that DocType to find valid values (they may be in Arabic), "
				"pick the right one, then call create_document again."
			),
		}
	except frappe.ValidationError as e:
		frappe.db.rollback()
		return {
			"error": str(e)[:300],
			"hint": "Fix the values (verify links via list_documents) and retry, or ask the user.",
		}

	_artifacts()["created_doc"] = {"doctype": doctype, "name": doc.name}
	return {
		"created": doc.name,
		"doctype": doctype,
		"status": "Draft",
		"message": "أُنشئ المستند كمسودة. أبلغ المستخدم برقمه.",
	}


# Preferred first; falls back so escalation works on any site regardless
# of which support app is installed (helpdesk's HD Ticket vs ERPNext's Issue).
SUPPORT_DOCTYPES = ("HD Ticket", "Issue")


def _resolve_support_doctype():
	for doctype in SUPPORT_DOCTYPES:
		if frappe.db.exists("DocType", doctype):
			return doctype
	frappe.throw("لا يوجد نظام تذاكر مثبّت على هذا الموقع")


def create_support_ticket(subject, description):
	"""Create a support ticket on behalf of the current user, attaching the
	conversation transcript stashed by the chat layer (if any)."""
	transcript = get_artifacts().get("transcript")
	if transcript:
		description += "\n\n---\nسياق المحادثة:\n" + transcript

	doctype = _resolve_support_doctype()
	ticket = frappe.get_doc(
		{
			"doctype": doctype,
			"subject": subject[:140],
			"description": description.replace("\n", "<br>"),
			"raised_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)

	_artifacts()["ticket"] = ticket.name
	_artifacts()["ticket_doctype"] = doctype
	return {
		"ticket": ticket.name,
		"status": ticket.status,
		"message": "أُنشئت التذكرة وأُرفق سياق المحادثة. أبلغ المستخدم برقمها.",
	}


_EXECUTORS = {
	"count_documents": count_documents,
	"list_documents": list_documents,
	"get_document": get_document,
	"sum_field": sum_field,
	"aggregate_documents": aggregate_documents,
	"generate_report": generate_report,
	"create_document": create_document,
	"create_support_ticket": create_support_ticket,
}


def execute_tool(name, arguments):
	"""Execute a tool by name; always return a JSON string the model can read.

	Errors are returned as data (not raised) so the model can recover
	gracefully instead of the whole request failing.
	"""
	fn = _EXECUTORS.get(name)
	if not fn:
		return _serialize({"error": f"unknown tool: {name}"})
	try:
		args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
		return _serialize(fn(**args))
	except Exception as e:
		frappe.db.rollback()
		# Log server-side too — errors returned only to the model are
		# invisible when diagnosing production failures.
		frappe.log_error(
			title=f"Mubtkir tool error: {name}",
			message=f"arguments: {arguments}\n\n{frappe.get_traceback()}",
		)
		return _serialize({"error": str(e)[:400]})
