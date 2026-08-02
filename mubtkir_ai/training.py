# -*- coding: utf-8 -*-
"""Export approved training candidates as an OpenAI fine-tuning JSONL file.

Run manually when enough candidates are approved:

    bench --site <site> execute mubtkir_ai.training.export_approved

The file is saved as a private File document; exported candidates are
flagged so re-runs only pick up new approvals.
"""

import json

import frappe
from frappe.utils import now_datetime


def _session_to_training_row(session_doc):
	"""One JSONL row: {"messages": [user/assistant turns...]}."""
	messages = json.loads(session_doc.messages or "[]")
	turns = [
		{"role": m["role"], "content": m["content"]}
		for m in messages
		if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
	]
	return {"messages": turns} if len(turns) >= 2 else None


@frappe.whitelist()
def export_approved():
	"""Build the JSONL from approved, unexported candidates."""
	frappe.only_for("System Manager")

	candidates = frappe.get_all(
		"AI Training Candidate",
		filters={"status": "Approved", "exported": 0},
		fields=["name", "session"],
	)
	if not candidates:
		return {"exported": 0, "file": None, "message": "لا مرشّحات معتمدة جديدة للتصدير"}

	lines = []
	used = []
	for c in candidates:
		session = frappe.get_doc("AI Session", c["session"])
		row = _session_to_training_row(session)
		if row:
			lines.append(json.dumps(row, ensure_ascii=False))
			used.append(c["name"])

	if not lines:
		return {"exported": 0, "file": None, "message": "المرشّحات المعتمدة لا تحتوي محادثات صالحة"}

	stamp = str(now_datetime())[:19].replace(" ", "_").replace(":", "-")
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"mubtkir_training_{stamp}.jsonl",
			"content": "\n".join(lines),
			"is_private": 1,
		}
	).insert(ignore_permissions=True)

	for name in used:
		frappe.db.set_value("AI Training Candidate", name, "exported", 1)
	frappe.db.commit()

	return {"exported": len(used), "file": file_doc.file_url}
