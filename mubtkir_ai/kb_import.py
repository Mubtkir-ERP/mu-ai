# -*- coding: utf-8 -*-
"""Import documentation files into the AI knowledge base.

Any markdown/text file (e.g. a custom app's user guide) becomes an
`AI Knowledge Source` and is indexed for hybrid retrieval — this is how the
assistant learns about apps installed on the site.

Usage:
    bench --site <site> execute mubtkir_ai.kb_import.import_file \
        --kwargs "{'path': '/path/to/guide.md', 'title': 'دليل ...'}"

    # Convenience shortcut for the PRI Contracting guide:
    bench --site <site> execute mubtkir_ai.kb_import.import_pri_guide
"""

import os

import frappe

PRI_GUIDE_PATH = "/home/frappe/bench-2/apps/pri_contracting/دليل_المستخدم.md"


def import_file(path, title=None):
	"""Create/refresh a knowledge source from a file and index it synchronously."""
	if not os.path.exists(path):
		frappe.throw(f"الملف غير موجود: {path}")

	with open(path, encoding="utf-8") as f:
		content = f.read()

	title = title or os.path.splitext(os.path.basename(path))[0]

	frappe.flags.mubtkir_skip_auto_index = True
	name = frappe.db.get_value("AI Knowledge Source", {"title": title})
	if name:
		doc = frappe.get_doc("AI Knowledge Source", name)
		doc.content = content
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{"doctype": "AI Knowledge Source", "title": title, "content": content}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	from mubtkir_ai import rag

	rag.index_source(doc.name)
	return {
		"source": doc.name,
		"title": title,
		"chars": len(content),
		"chunks": frappe.db.count("AI Embedding", {"source": doc.name}),
	}


def import_pri_guide():
	"""Import the PRI Contracting user guide (covers subcontractor claims etc.)."""
	return import_file(PRI_GUIDE_PATH, "دليل نظام المقاولات (PRI Contracting)")
