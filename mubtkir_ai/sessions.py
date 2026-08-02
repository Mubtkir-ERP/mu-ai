# -*- coding: utf-8 -*-
"""Chat session persistence and the training-data seed pipeline.

Every chat turn is appended server-side to an `AI Session` document — the
canonical transcript (never trust the client DOM for training data). When a
session goes idle it is closed, scored, and — if good enough — turned into
an `AI Training Candidate` for weekly human review and JSONL export.

Quality heuristic (mirrors the project spec):
    base 0.5, +0.3 if never escalated to a ticket, +0.2 if the user's last
    message signals satisfaction. Candidates require >= QUALITY_THRESHOLD.
"""

import json

import frappe
from frappe.utils import add_to_date, now_datetime

IDLE_MINUTES = 30
QUALITY_THRESHOLD = 0.75

# Lightweight satisfaction signals in the user's final message
_THANKS_WORDS = ("شكرا", "شكرًا", "تمام", "ممتاز", "حلّت", "انحلت", "thanks", "thank you", "solved")


# ----------------------------------------------------------------- persistence


def append_turn(session_id, question, reply, tokens=0, escalated=False):
	"""Append one user/assistant exchange to the session (create on first use).

	Persistence must never break the chat — failures are logged and swallowed.
	"""
	try:
		name = frappe.db.get_value("AI Session", {"session_id": session_id})
		doc = (
			frappe.get_doc("AI Session", name)
			if name
			else frappe.get_doc(
				{
					"doctype": "AI Session",
					"session_id": session_id,
					"user": frappe.session.user,
					"status": "Active",
					"started_at": now_datetime(),
					"messages": "[]",
				}
			)
		)

		ts = str(now_datetime())
		messages = json.loads(doc.messages or "[]")
		messages.append({"role": "user", "content": question, "ts": ts})
		messages.append({"role": "assistant", "content": reply, "ts": ts})

		doc.messages = json.dumps(messages, ensure_ascii=False)
		doc.message_count = len(messages)
		doc.tokens_total = (doc.tokens_total or 0) + (tokens or 0)
		doc.last_activity = now_datetime()
		if escalated:
			doc.escalated = 1

		doc.save(ignore_permissions=True) if name else doc.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Mubtkir session persist error", message=frappe.get_traceback())


# ------------------------------------------------------------ quality scoring


def compute_quality(doc):
	"""Score a closed session in [0, 1]; higher means better training data."""
	score = 0.5
	if not doc.escalated:
		score += 0.3

	try:
		messages = json.loads(doc.messages or "[]")
	except ValueError:
		messages = []
	last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
	if any(word in last_user.lower() for word in _THANKS_WORDS):
		score += 0.2

	return min(round(score, 2), 1.0)


def close_session(name):
	"""Close one session, score it, and create a training candidate if it
	clears the quality bar. Idempotent for already-closed sessions."""
	doc = frappe.get_doc("AI Session", name)
	if doc.status == "Closed":
		return doc

	doc.db_set("status", "Closed", update_modified=False)
	score = compute_quality(doc)
	doc.db_set("quality_score", score, update_modified=False)

	if score >= QUALITY_THRESHOLD and not frappe.db.exists(
		"AI Training Candidate", {"session": name}
	):
		frappe.get_doc(
			{
				"doctype": "AI Training Candidate",
				"session": name,
				"status": "Pending Review",
				"quality_score": score,
			}
		).insert(ignore_permissions=True)

	return doc


def close_idle_sessions():
	"""Scheduled task: close sessions with no activity for IDLE_MINUTES."""
	cutoff = add_to_date(now_datetime(), minutes=-IDLE_MINUTES)
	for row in frappe.get_all(
		"AI Session",
		filters={"status": "Active", "last_activity": ["<", cutoff]},
		pluck="name",
	):
		close_session(row)
	frappe.db.commit()
