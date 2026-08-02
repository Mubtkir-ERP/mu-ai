# -*- coding: utf-8 -*-
import frappe
from frappe.model.document import Document


class AIAccountingUpload(Document):
	def validate(self):
		if not self.company:
			self.company = frappe.defaults.get_global_default("company")

	def on_update(self):
		"""Process in the background on save; re-saving retries a failed run.

		``frappe.flags.mubtkir_skip_auto_index`` switches to synchronous
		processing (used by tests).
		"""
		if self.journal_entry or self.status == "Processing":
			return
		if frappe.flags.mubtkir_skip_auto_index:
			return
		self.db_set("status", "Processing", update_modified=False)
		frappe.enqueue(
			"mubtkir_ai.accountant.process_upload",
			queue="short",
			upload_name=self.name,
			enqueue_after_commit=True,
		)
