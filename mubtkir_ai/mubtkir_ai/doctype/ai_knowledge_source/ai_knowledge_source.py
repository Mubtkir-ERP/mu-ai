# -*- coding: utf-8 -*-
import os

import frappe
from frappe.model.document import Document

TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".text")
MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB of text is far beyond any doc guide


class AIKnowledgeSource(Document):
	def before_validate(self):
		self._sync_content_from_file()

	def validate(self):
		if not (self.content or "").strip():
			frappe.throw("ارفع ملف توثيق أو اكتب المحتوى يدويًا")

	def on_update(self):
		"""Re-index in the background on every save (chunk + embed).

		Set ``frappe.flags.mubtkir_skip_auto_index`` to index synchronously
		instead (used by tests/importers to avoid racing the worker).
		"""
		if frappe.flags.mubtkir_skip_auto_index:
			return
		self.db_set("status", "Indexing", update_modified=False)
		frappe.enqueue(
			"mubtkir_ai.rag.index_source",
			queue="short",
			source_name=self.name,
			enqueue_after_commit=True,
		)

	def on_trash(self):
		"""Remove this source's chunks from the index."""
		frappe.db.delete("AI Embedding", {"source": self.name})

	# ------------------------------------------------------------- internals

	def _sync_content_from_file(self):
		"""Fill `content` (and a missing title) from a newly attached file.

		Runs only when the attachment actually changed, so manual edits to
		`content` are never clobbered by an old attachment.
		"""
		if not self.source_file or not self._file_changed():
			return

		file_name = self.source_file.rsplit("/", 1)[-1]
		stem, ext = os.path.splitext(file_name)
		if ext.lower() not in TEXT_EXTENSIONS:
			frappe.throw(
				"صيغة الملف غير مدعومة — ارفع ملفًا نصيًا: "
				+ " أو ".join(TEXT_EXTENSIONS)
			)

		file_doc = frappe.get_doc("File", {"file_url": self.source_file})
		raw = file_doc.get_content()
		if isinstance(raw, bytes):
			if len(raw) > MAX_FILE_BYTES:
				frappe.throw("الملف أكبر من الحد المسموح (2MB)")
			try:
				raw = raw.decode("utf-8")
			except UnicodeDecodeError:
				frappe.throw("تعذّرت قراءة الملف — تأكد أنه نص بترميز UTF-8")

		self.content = raw
		if not (self.title or "").strip():
			self.title = stem[:140]

	def _file_changed(self):
		if self.is_new():
			return True
		return self.source_file != frappe.db.get_value(self.doctype, self.name, "source_file")
