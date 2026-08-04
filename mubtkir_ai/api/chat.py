# -*- coding: utf-8 -*-
"""Chat backend for the Mubtkir assistant.

Primary path: `get_reply` — a synchronous HTTP endpoint that returns the
full answer (reliable regardless of websocket/proxy setup). It combines:
- RAG retrieval from the MariaDB-backed knowledge base,
- live ERPNext data tools (permission-enforced),
- automatic escalation to an HD Ticket when the assistant cannot help.

A legacy realtime path (`send_message` + Socket.IO events) is kept for
environments where websocket delivery works.
"""

import json
import time

import frappe
from frappe.utils import nowdate

# Realtime event names shared with the chat page JS
EV_START = "mubtkir_chat_start"
EV_CHUNK = "mubtkir_chat_chunk"
EV_DONE = "mubtkir_chat_done"

MAX_HISTORY_TURNS = 8
TRANSCRIPT_TURN_CHARS = 400

NOT_CONFIGURED_SOURCE = "استجابة تجريبية — أضف مفتاح API في إعدادات مُبتكِر"
LIVE_DATA_SOURCE = "بيانات ERPNext الحية (ضمن صلاحياتك)"


# ------------------------------------------------------------------- helpers


def _parse_history(history):
	"""Accept the client-sent history (JSON string or list) and cap its size."""
	if isinstance(history, str):
		try:
			history = json.loads(history)
		except ValueError:
			history = []
	if not isinstance(history, list):
		history = []
	return history[-MAX_HISTORY_TURNS:]


def _build_transcript(history, current_text):
	"""Compact plain-text transcript attached to escalation tickets."""
	lines = []
	for turn in history:
		role = "المستخدم" if turn.get("role") == "user" else "المساعد"
		content = (turn.get("content") or "")[:TRANSCRIPT_TURN_CHARS]
		lines.append(f"{role}: {content}")
	lines.append(f"المستخدم: {current_text[:TRANSCRIPT_TURN_CHARS]}")
	return "\n".join(lines)


def _retrieve_context(text):
	"""Return (context, source_label) from the knowledge base, or (None, None)."""
	try:
		from mubtkir_ai import rag

		chunks = rag.search(text, top_k=4)
		if not chunks:
			return None, None
		context = "\n\n".join(f"[{c['source_title']}]\n{c['chunk_text']}" for c in chunks)
		titles = list(dict.fromkeys(c["source_title"] for c in chunks))
		return context, "قاعدة المعرفة: " + "، ".join(titles[:3])
	except Exception:
		frappe.log_error(title="Mubtkir chat RAG error", message=frappe.get_traceback())
		return None, None


def _placeholder_answer(text):
	"""Demo reply used until a provider API key is configured."""
	return (
		"هذه استجابة تجريبية من واجهة مُبتكِر للتأكد من عمل المسار. "
		"سؤالك كان: «" + text + "». "
		"بمجرد ربط بوابة النماذج ومفاتيح المزوّدين، سيأتي الرد الحقيقي من "
		"وثائق ERPNext ومن بياناتك الفعلية، وضمن صلاحياتك فقط."
	)


# ---------------------------------------------------------------- HTTP path


def _format_page_context(page_context):
	"""Turn the client-sent route info into one context line for the model.

	Untrusted client input: parsed defensively, whitelisted keys only,
	length-capped. Any document access it triggers still goes through the
	permission-checked tools.
	"""
	if isinstance(page_context, str):
		try:
			page_context = json.loads(page_context)
		except ValueError:
			return None
	if not isinstance(page_context, dict):
		return None

	doctype = str(page_context.get("doctype") or "")[:80]
	docname = str(page_context.get("docname") or "")[:140]
	route = str(page_context.get("route") or "")[:140]

	if doctype and docname:
		return (
			f"المستخدم يتصفح الآن مستند {doctype} رقم {docname} — "
			"إن قال «هذه الفاتورة/هذا المستند/هذه الصفحة» فهو يقصده، "
			"واستخدم get_document لقراءته عند الحاجة."
		)
	if doctype:
		return f"المستخدم يتصفح الآن قائمة {doctype}."
	if route:
		return f"المستخدم في الصفحة: {route}."
	return None


@frappe.whitelist()
def get_reply(session_id, text, history=None, page_context=None):
	"""Synchronous chat endpoint returning ``{reply, source, ticket}``."""
	text = (text or "").strip()
	if not text:
		frappe.throw("الرسالة فارغة")

	from mubtkir_ai import ai_gateway, ai_tools

	if not ai_gateway.is_configured():
		return {"reply": _placeholder_answer(text), "source": NOT_CONFIGURED_SOURCE, "ticket": None}

	history = _parse_history(history)

	try:
		context, kb_source = _retrieve_context(text)

		# Fresh per-message tool state, then stash the transcript for the
		# escalation tool. Without the reset, artifacts leak between turns.
		ai_tools.reset_artifacts()
		ai_tools._artifacts()["transcript"] = _build_transcript(history, text)

		# Volatile per-turn context (date, current page) lives in the user
		# turn so the cached system prefix stays stable. The date line carries
		# its own counter-instruction: models otherwise turn it into an
		# unrequested date filter on "total/all" questions.
		context_lines = [
			f"(تاريخ اليوم: {nowdate()} — للمرجعية فقط. "
			"لا تستخدم أي فلتر تواريخ في الأدوات إلا إذا طلب المستخدم فترة زمنية صراحة؛ "
			"«كامل/الكل/جميع» تعني بلا فلتر تاريخ إطلاقًا)"
		]
		page_line = _format_page_context(page_context)
		if page_line:
			context_lines.append(f"({page_line})")
		dated_text = "\n".join(context_lines) + "\n" + text

		reply, _usage, used_tools = ai_gateway.complete(
			dated_text,
			context=context,
			tools=ai_tools.get_tool_schemas(),
			history=history,
			session_id=session_id,
		)

		artifacts = ai_tools.get_artifacts()
		ticket = artifacts.get("ticket")

		# Server-side transcript is the canonical record (training data seed)
		from mubtkir_ai import sessions

		sessions.append_turn(
			session_id,
			question=text,
			reply=reply or "",
			tokens=getattr(_usage, "total_tokens", 0) or 0,
			escalated=bool(ticket),
		)
		created = artifacts.get("created_doc")

		return {
			"reply": reply or "لم يُرجِع النموذج أي نص. جرّب صياغة أخرى.",
			"source": _resolve_source_label(used_tools, kb_source, ticket),
			"ticket": ticket,
			"ticket_url": _doc_url(artifacts.get("ticket_doctype", "Issue"), ticket),
			"report": artifacts.get("report"),
			"created": created,
			"created_url": created and _doc_url(created["doctype"], created["name"]),
		}
	except Exception as e:
		frappe.log_error(title="Mubtkir chat get_reply error", message=frappe.get_traceback())
		return {
			"reply": "تعذّر الاتصال بالنموذج: " + str(e)[:300],
			"source": "خطأ",
			"ticket": None,
		}


def _doc_url(doctype, name):
	"""Desk form URL for a document, or None when name is empty."""
	if not name:
		return None
	return f"/app/{doctype.lower().replace(' ', '-')}/{name}"


def _resolve_source_label(used_tools, kb_source, ticket):
	"""Human-readable provenance line rendered under the reply bubble."""
	if ticket:
		return f"تذكرة دعم: {ticket}"
	parts = []
	if used_tools:
		parts.append(LIVE_DATA_SOURCE)
	if kb_source:
		parts.append(kb_source)
	if parts:
		return " + ".join(parts)
	from mubtkir_ai import ai_gateway

	return ai_gateway.get_settings().model


# ---------------------------------------------------- widget & history APIs


@frappe.whitelist()
def get_widget_config():
	"""Lightweight config the floating widget reads on every Desk load."""
	from mubtkir_ai import ai_gateway

	settings = ai_gateway.get_settings()
	return {
		"enabled": bool(settings.get("enable_floating_widget")),
		"configured": ai_gateway.is_configured(),
	}


@frappe.whitelist()
def get_sessions(limit=20):
	"""Current user's recent chat sessions for the history panel.

	The title shown is the first user message of each session.
	"""
	sessions = frappe.get_all(
		"AI Session",
		filters={"user": frappe.session.user},
		fields=["name", "session_id", "message_count", "last_activity", "messages"],
		order_by="last_activity desc",
		limit_page_length=min(int(limit or 20), 50),
	)

	out = []
	for s in sessions:
		try:
			messages = json.loads(s.messages or "[]")
		except ValueError:
			messages = []
		first_user = next((m["content"] for m in messages if m.get("role") == "user"), "")
		out.append(
			{
				"session_id": s.session_id,
				"title": (first_user or "محادثة")[:60],
				"message_count": s.message_count,
				"last_activity": str(s.last_activity or "")[:16],
			}
		)
	return out


@frappe.whitelist()
def get_session_messages(session_id):
	"""Full transcript of one session — owner only."""
	name = frappe.db.get_value(
		"AI Session", {"session_id": session_id, "user": frappe.session.user}
	)
	if not name:
		frappe.throw("الجلسة غير موجودة أو لا تخصك", frappe.PermissionError)

	messages = frappe.db.get_value("AI Session", name, "messages")
	try:
		return json.loads(messages or "[]")
	except ValueError:
		return []


# ------------------------------------------------------------- realtime path


@frappe.whitelist()
def send_message(session_id, text):
	"""Legacy realtime path: enqueue a streaming reply over Socket.IO."""
	text = (text or "").strip()
	if not text:
		frappe.throw("الرسالة فارغة")

	frappe.enqueue(
		"mubtkir_ai.api.chat.generate_reply",
		queue="short",
		session_id=session_id,
		text=text,
		user=frappe.session.user,
		enqueue_after_commit=True,
	)
	return {"ok": True, "session_id": session_id}


def generate_reply(session_id, text, user):
	"""Background worker: stream the reply over realtime events."""
	frappe.publish_realtime(EV_START, {"session_id": session_id}, user=user)

	from mubtkir_ai import ai_gateway

	def _publish(event, payload):
		payload["session_id"] = session_id
		frappe.publish_realtime(event, payload, user=user)

	if ai_gateway.is_configured():
		try:
			full = ai_gateway.stream_complete(text, lambda t: _publish(EV_CHUNK, {"text": t}))
			_publish(EV_DONE, {"text": full, "source": ai_gateway.get_settings().model})
			return
		except Exception as e:
			frappe.log_error(title="Mubtkir chat gateway error", message=frappe.get_traceback())
			_publish(EV_DONE, {"text": "تعذّر الاتصال بالنموذج: " + str(e)[:300], "source": "خطأ"})
			return

	# No provider configured: stream the demo reply word by word.
	reply = _placeholder_answer(text)
	buffer = ""
	for word in reply.split(" "):
		buffer += word + " "
		_publish(EV_CHUNK, {"text": buffer})
		time.sleep(0.045)
	_publish(EV_DONE, {"text": reply, "source": NOT_CONFIGURED_SOURCE})
