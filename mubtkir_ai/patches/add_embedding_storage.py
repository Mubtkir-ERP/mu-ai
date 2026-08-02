# -*- coding: utf-8 -*-
"""Add the binary vector column and keyword index to `tabAI Embedding`.

Frappe's ORM has no binary/vector field type, so both are created with
direct DDL. The patch is idempotent — safe to re-run on every migrate.
"""

import frappe


def execute():
	table = "tabAI Embedding"
	if not frappe.db.table_exists("AI Embedding"):
		return

	columns = [c["Field"] for c in frappe.db.sql(f"SHOW COLUMNS FROM `{table}`", as_dict=True)]
	if "embedding" not in columns:
		frappe.db.sql_ddl(f"ALTER TABLE `{table}` ADD COLUMN `embedding` LONGBLOB")

	indexes = {i["Key_name"] for i in frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True)}
	if "ft_chunk_norm" not in indexes:
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` ADD FULLTEXT INDEX `ft_chunk_norm` (`chunk_text_norm`)"
		)
