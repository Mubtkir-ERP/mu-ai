# -*- coding: utf-8 -*-
"""Data provider for the Mubtkir admin dashboard (usage KPIs and health)."""

import frappe
from frappe.utils import add_days, flt, nowdate


@frappe.whitelist()
def get_data():
	frappe.only_for("System Manager")

	today = nowdate()
	month_start = today[:8] + "01"
	window_start = add_days(today, -13)

	def usage_aggregate(condition, params):
		row = frappe.db.sql(
			f"""SELECT COUNT(*) calls,
					COALESCE(SUM(total_tokens),0) tokens,
					COALESCE(SUM(cached_tokens),0) cached,
					COALESCE(SUM(cost_usd),0) cost
				FROM `tabAI Usage Log` WHERE {condition}""",
			params,
			as_dict=True,
		)
		return {k: flt(v) for k, v in (row[0] if row else {}).items()}

	# Daily token series for the last 14 days (gaps filled with zeros)
	series_rows = frappe.db.sql(
		"""SELECT DATE(creation) d, COALESCE(SUM(total_tokens),0) tokens
			FROM `tabAI Usage Log`
			WHERE DATE(creation) >= %s GROUP BY DATE(creation)""",
		(window_start,),
		as_dict=True,
	)
	tokens_by_day = {str(r["d"]): int(r["tokens"]) for r in series_rows}
	series = [
		{"date": str(day), "tokens": tokens_by_day.get(str(day), 0)}
		for day in (add_days(window_start, i) for i in range(14))
	]

	recent = frappe.get_all(
		"AI Usage Log",
		fields=[
			"name", "user", "feature", "model",
			"total_tokens", "cached_tokens", "cost_usd", "creation",
		],
		order_by="creation desc",
		limit_page_length=10,
	)
	for r in recent:
		r["creation"] = str(r["creation"])[:16]

	from mubtkir_ai import ai_gateway

	settings = ai_gateway.get_settings()

	return {
		"today": usage_aggregate("DATE(creation)=%s", (today,)),
		"month": usage_aggregate("DATE(creation)>=%s", (month_start,)),
		"total": usage_aggregate("1=1", ()),
		"series": series,
		"recent": recent,
		"health": {
			"configured": ai_gateway.is_configured(),
			"provider": settings.provider,
			"model": settings.model,
			"kb_sources": frappe.db.count("AI Knowledge Source"),
			"kb_chunks": frappe.db.count("AI Embedding"),
		},
	}
