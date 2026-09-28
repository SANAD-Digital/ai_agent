"""knowledge.search: short passages from the user guides the user may read.

Plain text search over AI Knowledge Chunk (no vector store): candidates by
LIKE on a normalized copy of the text, ranked with a BM25-style score. A
document restricted to roles is searched only for users holding one of them.
"""

import math
import re

import frappe

from ai_agent.knowledge.indexer import normalize

MAX_CANDIDATES = 300
SNIPPET_CHARS = 500
STOPWORDS = {
	"في", "من", "على", "الى", "عن", "ما", "كيف", "هل", "او", "ثم", "مع", "هذا", "هذه", "ذلك", "التي", "الذي",
	"لا", "ان", "كان", "انا", "اريد", "ابغى", "the", "a", "an", "of", "to", "in", "and", "or", "how", "is", "do", "i",
}


def handle_search(arguments, permission_context):
	query = str(arguments.get("query") or "").strip()
	if not query:
		return {"error": "query is required"}
	try:
		top = max(1, min(int(arguments.get("top") or 5), 10))
	except (TypeError, ValueError):
		top = 5
	if not _readable_documents(permission_context.user):
		return {
			"query": query,
			"results": [],
			"note": "No user guide documents are available to this user. Do not search again; answer from general ERPNext knowledge and say it is not from the guide.",
		}
	results = search(query, permission_context.user, top)
	if not results:
		return {"query": query, "results": [], "note": "No matching passage. Try at most one rephrased query."}
	return {"query": query, "results": results}


def search(query, user, top=5):
	documents = _readable_documents(user)
	if not documents:
		return []
	terms = [t for t in re.findall(r"\w+", normalize(query)) if len(t) > 1 and t not in STOPWORDS]
	if not terms:
		return []
	# Chunks are internal index rows; access is decided per document above.
	candidates = frappe.get_all(
		"AI Knowledge Chunk",
		filters={"document": ["in", list(documents)]},
		or_filters=[["search_text", "like", f"%{term}%"] for term in terms],
		fields=["name", "document", "page", "heading", "content", "search_text"],
		limit_page_length=MAX_CANDIDATES,
	)
	if not candidates:
		return []
	document_frequency = {term: sum(1 for c in candidates if term in c.search_text) for term in terms}
	average_length = sum(len(c.search_text) for c in candidates) / len(candidates)
	scored = []
	for chunk in candidates:
		score = 0.0
		for term in terms:
			tf = chunk.search_text.count(term)
			if not tf:
				continue
			idf = math.log(1 + (len(candidates) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
			norm = tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(chunk.search_text) / average_length))
			score += idf * norm
		if score:
			scored.append((score, chunk))
	scored.sort(key=lambda pair: pair[0], reverse=True)
	return [
		{
			"source": documents[chunk.document],
			"page": chunk.page,
			"section": chunk.heading or None,
			"snippet": _snippet(chunk.content, terms),
			"score": round(score, 2),
		}
		for score, chunk in scored[:top]
	]


def _readable_documents(user):
	"""{name: title} of enabled, indexed documents this user may search."""
	roles = set(frappe.get_roles(user))
	readable = {}
	for row in frappe.get_all(
		"AI Knowledge Document",
		filters={"enabled": 1, "indexing_status": "Indexed"},
		fields=["name", "title", "allowed_roles"],
	):
		allowed = {r.strip() for r in (row.allowed_roles or "").splitlines() if r.strip()}
		if not allowed or allowed & roles:
			readable[row.name] = row.title
	return readable


def _snippet(content, terms):
	text = " ".join(str(content or "").split())
	normalized = normalize(text)
	positions = [normalized.find(term) for term in terms if normalized.find(term) >= 0]
	start = max(min(positions) - SNIPPET_CHARS // 3, 0) if positions else 0
	snippet = text[start:start + SNIPPET_CHARS]
	return ("…" if start else "") + snippet + ("…" if start + SNIPPET_CHARS < len(text) else "")
