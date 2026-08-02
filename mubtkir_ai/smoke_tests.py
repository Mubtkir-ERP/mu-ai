# -*- coding: utf-8 -*-
"""Smoke tests for the Mubtkir AI stack. Run against a configured site:

    bench --site <site> execute mubtkir_ai.smoke_tests.run_all

Each check exercises a full user-visible path (RAG, live-data tools,
ticket escalation) and returns raw results for manual inspection.
These hit the real LLM provider and consume a few thousand tokens.
"""

import frappe

KB_TITLE = "سياسة الدعم الفني"
KB_CONTENT = """سياسة الدعم الفني في منصة مُبتكِر

أوقات عمل فريق الدعم الفني في مبتكر: من الأحد إلى الخميس، من الساعة 8 صباحًا حتى 6 مساءً بتوقيت الرياض.

زمن الاستجابة المضمون للتذاكر: التذاكر الحرجة خلال 30 دقيقة، والعادية خلال 4 ساعات عمل.

رقم الدعم الموحد هو 920033445، والبريد support@mubtkir.net.

سياسة التصعيد: إذا لم تُحل التذكرة خلال 24 ساعة تُصعَّد تلقائيًا إلى مدير الدعم أحمد الشهري."""


def _upsert_kb_source():
	"""Create or refresh the demo knowledge article, indexing synchronously
	(the flag prevents racing the background indexer on tabSeries locks)."""
	frappe.flags.mubtkir_skip_auto_index = True

	name = frappe.db.get_value("AI Knowledge Source", {"title": KB_TITLE})
	if name:
		doc = frappe.get_doc("AI Knowledge Source", name)
		doc.content = KB_CONTENT
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "AI Knowledge Source", "title": KB_TITLE, "content": KB_CONTENT}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	from mubtkir_ai import rag

	rag.index_source(doc.name)
	return doc.name


def run_rag():
	"""Index a KB article, then verify retrieval and a grounded answer."""
	from mubtkir_ai import rag
	from mubtkir_ai.api import chat

	source = _upsert_kb_source()
	results = rag.search("كم زمن الاستجابة للتذاكر الحرجة؟", top_k=3)
	answer = chat.get_reply("smoke-rag", "ما هو رقم الدعم الموحد في مبتكر؟")

	return {
		"chunks": frappe.db.count("AI Embedding", {"source": source}),
		"search_hit": bool(results),
		"answer": answer,
	}


def run_tools():
	"""Verify live-data answers match direct DB counts."""
	from mubtkir_ai.api import chat

	return {
		"db_truth": {
			"sales_invoices": frappe.db.count("Sales Invoice"),
			"customers": frappe.db.count("Customer"),
		},
		"count_answer": chat.get_reply("smoke-tools", "كم عدد فواتير المبيعات؟ أجب برقم."),
		"list_answer": chat.get_reply("smoke-tools", "اعرض آخر 3 عملاء بأسمائهم فقط."),
	}


def run_escalation():
	"""Drive an unresolvable-problem conversation and expect an HD Ticket."""
	from mubtkir_ai.api import chat

	history = [
		{"role": "user", "content": "يطلع لي خطأ غريب عند ترحيل فاتورة المبيعات ACC-SINV-0001"},
		{"role": "assistant", "content": "جرّب التأكد من تحديد حساب الضريبة في صف الضرائب ثم أعد الترحيل."},
	]
	answer = chat.get_reply(
		"smoke-escalation",
		"جربت اقتراحك وما زال الخطأ يظهر. ما عاد أعرف أحل المشكلة — صعّدها لفريق الدعم.",
		history=history,
	)

	from mubtkir_ai import ai_tools

	ticket = answer.get("ticket")
	ticket_doctype = ai_tools.get_artifacts().get("ticket_doctype")
	ticket_doc = frappe.get_doc(ticket_doctype, ticket).as_dict() if ticket else None
	return {
		"answer": answer,
		"ticket_created": bool(ticket),
		"ticket_subject": ticket_doc and ticket_doc.get("subject"),
		"transcript_attached": bool(
			ticket_doc and "سياق المحادثة" in (ticket_doc.get("description") or "")
		),
	}


def run_sessions():
	"""Full training-seed pipeline: persist -> close -> score -> candidate -> export."""
	import json

	from mubtkir_ai import sessions, training
	from mubtkir_ai.api import chat

	session_id = "smoke-session-pipeline"

	# Clean slate so the test is repeatable
	old = frappe.db.get_value("AI Session", {"session_id": session_id})
	if old:
		frappe.db.delete("AI Training Candidate", {"session": old})
		frappe.db.delete("AI Session", {"name": old})
		frappe.db.commit()

	# Turn 1 exercises the real chat path (persists server-side)
	chat.get_reply(session_id, "كيف أضيف عميلًا جديدًا في النظام؟")
	# Turn 2 appended directly: a satisfied closing message (cheap, no LLM)
	sessions.append_turn(session_id, "تمام شكرًا انحلت المشكلة", "على الرحب والسعة!", tokens=0)

	name = frappe.db.get_value("AI Session", {"session_id": session_id})
	closed = sessions.close_session(name)
	candidate = frappe.db.get_value(
		"AI Training Candidate", {"session": name}, ["name", "quality_score"], as_dict=True
	)

	# Approve and export to validate the JSONL end of the pipeline
	export = None
	if candidate:
		frappe.db.set_value("AI Training Candidate", candidate["name"], "status", "Approved")
		export = training.export_approved()

	messages = json.loads(closed.messages or "[]")
	return {
		"persisted_turns": len(messages),
		"quality_score": closed.quality_score,
		"candidate_created": bool(candidate),
		"export": export,
	}


def run_report():
	"""User asks for a report with explicit columns -> table artifact returned."""
	from mubtkir_ai.api import chat

	answer = chat.get_reply(
		"smoke-report",
		"اشتي تقرير بكل فواتير المبيعات، يكون فيه الأعمدة: رقم الفاتورة، اسم العميل، التاريخ، الإجمالي، الحالة",
	)
	report = answer.get("report") or {}
	return {
		"has_report": bool(report),
		"title": report.get("title"),
		"columns": [c["fieldname"] for c in report.get("columns", [])],
		"row_count": report.get("row_count"),
		"db_truth": frappe.db.count("Sales Invoice"),
		"totals": report.get("totals"),
		"reply": (answer.get("reply") or "")[:200],
	}


def run_accountant():
	"""Full accountant pipeline: CSV -> parse -> classify -> balanced draft JE."""
	from mubtkir_ai import accountant

	frappe.flags.mubtkir_skip_auto_index = True
	company = frappe.defaults.get_global_default("company")
	bank_account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": ["in", ("Bank", "Cash")], "is_group": 0},
	)
	if not bank_account:
		return {"skipped": "لا يوجد حساب بنك/صندوق في الشركة"}

	csv_content = "\n".join(
		[
			"التاريخ,البيان,مدين,دائن",
			"2026-07-01,رواتب موظفين شهر يونيو,25000,",
			"2026-07-03,فاتورة كهرباء المكتب,1200,",
			"2026-07-05,تحصيل نقدي من عميل,,18000",
			"2026-07-10,اشتراك انترنت,,",  # empty row must be skipped
			"2026-07-12,ايجار المستودع,7500,",
		]
	)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": "smoke-bank-statement.csv",
			"content": csv_content,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)

	upload = frappe.get_doc(
		{
			"doctype": "AI Accounting Upload",
			"file": file_doc.file_url,
			"bank_account": bank_account,
			"company": company,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	result = accountant.process_upload(upload.name)
	upload.reload()

	je = frappe.get_doc("Journal Entry", upload.journal_entry)
	total_debit = sum(r.debit_in_account_currency for r in je.accounts)
	total_credit = sum(r.credit_in_account_currency for r in je.accounts)

	import json as _json

	transactions = _json.loads(upload.transactions_preview)
	return {
		"status": upload.status,
		"journal_entry": upload.journal_entry,
		"is_draft": je.docstatus == 0,
		"balanced": abs(total_debit - total_credit) < 0.005,
		"total_debit": total_debit,
		"txn_count": upload.txn_count,
		"expected_txns": 4,  # the empty-amount row must be skipped
		"totals": {"in": upload.total_in, "out": upload.total_out},
		"classified_by_ai": sum(1 for t in transactions if t.get("classified_by") == "ai"),
		"sample": [
			{"desc": t["description"], "account": t["suggested_account"]}
			for t in transactions[:2]
		],
	}


def run_all():
	return {
		"rag": run_rag(),
		"tools": run_tools(),
		"escalation": run_escalation(),
		"sessions": run_sessions(),
		"report": run_report(),
		"accountant": run_accountant(),
	}
