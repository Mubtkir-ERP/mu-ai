# -*- coding: utf-8 -*-
import frappe
from frappe.model.document import Document


class AITrainingCandidate(Document):
	def validate(self):
		"""Record who approved, the moment status flips to Approved."""
		if self.status == "Approved" and not self.approved_by:
			self.approved_by = frappe.session.user
