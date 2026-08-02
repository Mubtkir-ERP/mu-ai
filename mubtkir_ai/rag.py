# -*- coding: utf-8 -*-
"""RAG engine backed exclusively by MariaDB — no external vector database.

Design:
- Embeddings are stored L2-normalized as float32 in a LONGBLOB column,
  so cosine similarity reduces to a single numpy dot product.
- Keyword retrieval uses a FULLTEXT index over Arabic-normalized text.
- Both result lists are fused with Reciprocal Rank Fusion (RRF), which
  measurably beats either method alone and catches exact tokens
  (error codes, names) that embeddings blur.
"""

import re

import frappe
from frappe.utils import now_datetime

EMB_TABLE = "tabAI Embedding"
RRF_K = 60
CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP = 150

# Arabic diacritics (tashkeel) + tatweel
_AR_DIACRITICS = re.compile(r"[ً-ْٰـ]")


# -------------------------------------------------------- text normalization


def normalize_ar(text):
	"""Normalize Arabic for keyword search: strip diacritics and unify
	alef/ya/ta-marbuta variants. Highly inflected Arabic fragments FULLTEXT
	matching without this step."""
	if not text:
		return ""
	t = _AR_DIACRITICS.sub("", text)
	t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
	t = t.replace("ى", "ي").replace("ة", "ه")
	return re.sub(r"\s+", " ", t).strip().lower()


def chunk_text(text, max_chars=CHUNK_MAX_CHARS, overlap=CHUNK_OVERLAP):
	"""Split text into chunks on paragraph boundaries with a small overlap."""
	paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()]
	chunks = []
	current = ""
	for p in paragraphs:
		if len(current) + len(p) + 1 <= max_chars:
			current = (current + "\n" + p).strip()
			continue
		if current:
			chunks.append(current)
		while len(p) > max_chars:  # hard-split oversized paragraphs
			chunks.append(p[:max_chars])
			p = p[max_chars - overlap:]
		current = p
	if current:
		chunks.append(current)
	return chunks


# ------------------------------------------------------------------ embedding


def embed_texts(texts):
	"""Embed texts via the gateway settings; returns unit-norm float32 vectors."""
	import litellm
	import numpy as np

	from mubtkir_ai import ai_gateway

	s = ai_gateway.get_settings()
	model = s.get("embedding_model") or "text-embedding-3-small"

	resp = litellm.embedding(model=model, input=texts, api_key=s.get_password("api_key"))
	ai_gateway.log_usage("embedding", model, [getattr(resp, "usage", None)], responses=[resp])

	vectors = []
	for item in resp.data:
		v = np.asarray(item["embedding"], dtype=np.float32)
		norm = np.linalg.norm(v)
		vectors.append(v / norm if norm > 0 else v)
	return vectors


# ------------------------------------------------------------------- indexing


def index_source(source_name):
	"""(Re)index one knowledge source: wipe old chunks, chunk, embed, store."""
	doc = frappe.get_doc("AI Knowledge Source", source_name)
	try:
		frappe.db.delete("AI Embedding", {"source": source_name})

		chunks = chunk_text(doc.content or "")
		if chunks:
			vectors = embed_texts(chunks)
			for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
				emb = frappe.get_doc(
					{
						"doctype": "AI Embedding",
						"source": source_name,
						"source_title": doc.title,
						"chunk_index": i,
						"chunk_text": chunk,
						"chunk_text_norm": normalize_ar(chunk),
					}
				).insert(ignore_permissions=True)
				# The BLOB column is outside the ORM (no binary field type),
				# hence the direct UPDATE right after insert.
				frappe.db.sql(
					f"UPDATE `{EMB_TABLE}` SET embedding=%s WHERE name=%s",
					(vec.tobytes(), emb.name),
				)

		doc.db_set("status", "Indexed", update_modified=False)
		doc.db_set("chunks_count", len(chunks), update_modified=False)
		doc.db_set("indexed_at", now_datetime(), update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		doc.db_set("status", "Failed", update_modified=False)
		frappe.db.commit()
		frappe.log_error(title="Mubtkir RAG index error", message=frappe.get_traceback())
		raise


# -------------------------------------------------------------- hybrid search


def _semantic_ranks(query, limit=20):
	"""Rank chunk names by cosine similarity (dense retrieval).

	The whole corpus is loaded into one numpy matrix — brute force stays
	in the low milliseconds for tens of thousands of chunks, which is far
	beyond a support KB's size. Revisit (MariaDB 11.8 native VECTOR) only
	if the corpus outgrows this.
	"""
	import numpy as np

	rows = frappe.db.sql(
		f"SELECT name, embedding FROM `{EMB_TABLE}` WHERE embedding IS NOT NULL",
		as_dict=True,
	)
	if not rows:
		return []

	qv = embed_texts([query])[0]
	matrix = np.vstack(
		[np.frombuffer(r["embedding"], dtype=np.float32, count=qv.shape[0]) for r in rows]
	)
	order = np.argsort(-(matrix @ qv))[:limit]
	names = [r["name"] for r in rows]
	return [names[i] for i in order]


def _keyword_ranks(query, limit=20):
	"""Rank chunk names by FULLTEXT relevance over normalized text."""
	q = normalize_ar(query)
	if not q:
		return []
	rows = frappe.db.sql(
		f"""SELECT name FROM `{EMB_TABLE}`
			WHERE MATCH(chunk_text_norm) AGAINST (%s)
			ORDER BY MATCH(chunk_text_norm) AGAINST (%s) DESC
			LIMIT {int(limit)}""",
		(q, q),
		as_dict=True,
	)
	return [r["name"] for r in rows]


def search(query, top_k=4):
	"""Hybrid retrieval fused with RRF; returns chunk dicts with provenance.

	Either retrieval arm may fail independently (e.g. embeddings API down);
	the other still serves results.
	"""
	ranks = []
	for fn, label in ((_semantic_ranks, "semantic"), (_keyword_ranks, "keyword")):
		try:
			ranks.append(fn(query))
		except Exception:
			frappe.log_error(
				title=f"Mubtkir RAG {label} error", message=frappe.get_traceback()
			)
			ranks.append([])

	scores = {}
	for rank_list in ranks:
		for rank, name in enumerate(rank_list):
			scores[name] = scores.get(name, 0.0) + 1.0 / (RRF_K + rank + 1)
	if not scores:
		return []

	top = sorted(scores, key=scores.get, reverse=True)[:top_k]
	rows = frappe.db.sql(
		f"""SELECT name, source, source_title, chunk_text
			FROM `{EMB_TABLE}` WHERE name IN %s""",
		(tuple(top),),
		as_dict=True,
	)
	by_name = {r["name"]: r for r in rows}
	return [by_name[n] for n in top if n in by_name]
