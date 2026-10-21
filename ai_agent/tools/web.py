"""Web browsing for the agent: search the internet and read public pages.

Safety: only http(s) to public internet addresses. Every hop (including each
redirect) is resolved and refused if it points at a private, loopback,
link-local or reserved address, so the agent cannot reach this server, the
local network or cloud metadata endpoints. No cookies or credentials are sent,
responses are size- and time-limited, and page text reaches the model as
untrusted data (the engine wraps every tool result).
"""

import ipaddress
import re
import socket
from contextlib import contextmanager
from urllib.parse import parse_qs, urljoin, urlparse

import frappe
import requests
from frappe.utils import cint

USER_AGENT = "Mozilla/5.0 (compatible; SanadAI/1.0; +https://sanad.digital)"
TIMEOUT = (5, 15)
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
DEFAULT_CHARS = 8000
MAX_CHARS = 20000
MAX_LINKS = 25
DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "form", "nav", "footer", "header", "aside")


class WebError(Exception):
	pass


def _settings():
	return frappe.get_cached_doc("AI Settings")


def enabled():
	return bool(cint(_settings().get("enable_web_browsing")))


def _check_public(url):
	parts = urlparse(url)
	if parts.scheme not in ("http", "https") or not parts.hostname:
		raise WebError("Only http and https web addresses can be opened.")
	if parts.username or parts.password:
		raise WebError("Addresses with embedded credentials are not allowed.")
	port = parts.port or (443 if parts.scheme == "https" else 80)
	try:
		infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
	except socket.gaierror:
		raise WebError(f"The site {parts.hostname} could not be found.")
	for info in infos:
		ip = ipaddress.ip_address(info[4][0])
		if not ip.is_global or ip.is_multicast:
			raise WebError("This address points to a private or internal network and cannot be opened.")


@contextmanager
def _public_connections_only():
	"""Check the address actually connected to, not only the one resolved by _check_public: a host whose DNS
	answers a public IP to the check and 127.0.0.1 a moment later (DNS rebinding) is refused at connect time."""
	import urllib3.util.connection as u3

	original = u3.create_connection

	def guarded(address, *args, **kwargs):
		host, port = address
		infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
		for info in infos:
			ip = ipaddress.ip_address(info[4][0])
			if not ip.is_global or ip.is_multicast:
				raise WebError("This address points to a private or internal network and cannot be opened.")
		# connect to the address that was just checked (TLS still verifies the certificate for the host name)
		return original((infos[0][4][0], port), *args, **kwargs)

	u3.create_connection = guarded
	try:
		yield
	finally:
		u3.create_connection = original


def _get(url, *, params=None, headers=None, method="GET", json_body=None):
	"""GET/POST a public URL, following redirects by hand so each hop is checked."""
	with _public_connections_only():
		return _get_checked(url, params=params, headers=headers, method=method, json_body=json_body)


def _get_checked(url, *, params=None, headers=None, method="GET", json_body=None):
	session = requests.Session()
	session.trust_env = False   # no proxy/netrc credentials from the server environment
	headers = {"User-Agent": USER_AGENT, "Accept-Language": "ar,en;q=0.8", **(headers or {})}
	for _ in range(MAX_REDIRECTS + 1):
		_check_public(url)
		response = session.request(method, url, params=params, headers=headers, json=json_body,
			timeout=TIMEOUT, allow_redirects=False, stream=True)
		if response.is_redirect or response.is_permanent_redirect:
			url = urljoin(url, response.headers.get("Location", ""))
			params, method, json_body = None, "GET", None
			response.close()
			continue
		body = b""
		for chunk in response.iter_content(65536):
			body += chunk
			if len(body) > MAX_BYTES:
				break
		response.close()
		return response, body[:MAX_BYTES], url
	raise WebError("Too many redirects.")


def _clean(text):
	text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
	text = re.sub(r"\n\s*\n+", "\n\n", text)
	return text.strip()


def _html_to_text(html, base_url, with_links):
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(html, "html.parser")
	title = _clean(soup.title.get_text()) if soup.title else ""
	for tag in soup(DROP_TAGS):
		tag.decompose()
	main = soup.find("main") or soup.find("article") or soup.body or soup
	for br in main.find_all(["br"]):
		br.replace_with("\n")
	for block in main.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "table"]):
		block.insert_after("\n")
	links = []
	if with_links:
		seen = set()
		for a in main.find_all("a", href=True):
			href = urljoin(base_url, a["href"])
			label = _clean(a.get_text())
			if href.startswith("http") and label and href not in seen:
				seen.add(href)
				links.append({"text": label[:80], "url": href})
			if len(links) >= MAX_LINKS:
				break
	return title, _clean(main.get_text()), links


def _pdf_to_text(body):
	import fitz

	with fitz.open(stream=body, filetype="pdf") as doc:
		return _clean("\n".join(page.get_text() for page in doc.pages(0, min(doc.page_count, 30))))


# A URL the model builds itself can carry what it has read (a prompt-injected "fetch https://x/?d=<balances>").
# Such URLs open only when they came from outside the model: a search result, a link on a page already
# fetched, or the user's own message. A plain address (no query, short path) stays open.
KNOWN_URLS_TTL = 86400
TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_%-]{40,}")


def _known_key(run_name):
	return f"ai_agent:web_known_urls:{run_name}"


def remember_urls(run_name, urls):
	if not run_name:
		return
	urls = [u for u in urls if u]
	if urls:
		frappe.cache.sadd(_known_key(run_name), *urls)
		frappe.cache.expire(frappe.cache.make_key(_known_key(run_name)), KNOWN_URLS_TTL)


def _plain_address(url):
	parts = urlparse(url)
	return not parts.query and not parts.fragment and len(parts.path) <= 120 and not TOKEN_RE.search(parts.path)


def _url_allowed(url, permission_context):
	if _plain_address(url):
		return True
	run_name = getattr(permission_context, "run_name", None)
	if run_name and frappe.cache.sismember(_known_key(run_name), url):
		return True
	session = frappe.db.get_value("AI Run", run_name, "chat_session") if run_name else None
	if session:
		said = frappe.get_all("AI Chat Message", filters={"session": session, "role": "user", "text": ("like", f"%{url[:400]}%")}, limit=1)
		if said:
			return True
	return False


def handle_fetch(arguments, permission_context):
	"""web.fetch: the readable text of one public web page (HTML, PDF or text)."""
	if not enabled():
		return {"error": "Web browsing is turned off in AI Settings."}
	url = str(arguments.get("url") or "").strip()
	if url and not re.match(r"^https?://", url, re.I):
		url = "https://" + url
	if url and not _url_allowed(url, permission_context):
		return {"error": "This address has a query or a long path and did not come from a search result, a fetched page or "
			"the user: it is refused (it could carry data out). Open the page from a search result, or ask the user for the link.",
			"url": url}
	limit = max(500, min(cint(arguments.get("max_chars")) or DEFAULT_CHARS, MAX_CHARS))
	try:
		response, body, final_url = _get(url)
	except WebError as exc:
		return {"error": str(exc), "url": url}
	except requests.RequestException as exc:
		return {"error": f"The page could not be opened: {exc.__class__.__name__}.", "url": url}
	if response.status_code >= 400:
		return {"error": f"The site answered with status {response.status_code}.", "url": final_url}

	ctype = (response.headers.get("Content-Type") or "").lower()
	title, links = "", []
	if "pdf" in ctype or body[:5] == b"%PDF-":
		text = _pdf_to_text(body)
	elif "html" in ctype or body.lstrip()[:1] == b"<":
		html = body.decode(response.encoding or "utf-8", errors="replace")
		title, text, links = _html_to_text(html, final_url, arguments.get("include_links", True))
	elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
		text = _clean(body.decode(response.encoding or "utf-8", errors="replace"))
	else:
		return {"error": f"This kind of file ({ctype or 'unknown'}) cannot be read.", "url": final_url}

	result = {"url": final_url, "title": title, "content": text[:limit], "truncated": len(text) > limit, "length": len(text)}
	if links:
		result["links"] = links
		remember_urls(getattr(permission_context, "run_name", None), [link["url"] for link in links])
	if not text:
		result["note"] = "The page has no readable text; it may need JavaScript or a login."
	return result


# -- search providers --------------------------------------------------------------

def _ok(response):
	if response.status_code >= 400:
		raise WebError(f"The search engine refused the request (status {response.status_code}). Check the search settings and key.")
	return response


def _duckduckgo(query, count):
	from bs4 import BeautifulSoup

	response, body, _ = _get("https://html.duckduckgo.com/html/", params={"q": query})
	_ok(response)
	soup = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")
	results = []
	for item in soup.select(".result"):
		a = item.select_one("a.result__a")
		if not a or "result--ad" in (item.get("class") or []):
			continue
		href = a.get("href", "")
		href = "https:" + href if href.startswith("//") else href
		target = parse_qs(urlparse(href).query).get("uddg", [href])[0]   # DuckDuckGo wraps links in a redirect
		snippet = item.select_one(".result__snippet")
		results.append({"title": _clean(a.get_text()), "url": target, "snippet": _clean(snippet.get_text()) if snippet else ""})
		if len(results) >= count:
			break
	return results


def _brave(query, count, key):
	response, body, _ = _get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": count},
		headers={"X-Subscription-Token": key, "Accept": "application/json"})
	_ok(response)
	data = frappe.parse_json(body.decode())
	return [{"title": r.get("title"), "url": r.get("url"), "snippet": _clean(re.sub("<[^>]+>", "", r.get("description") or ""))}
		for r in ((data.get("web") or {}).get("results") or [])[:count]]


def _tavily(query, count, key):
	response, body, _ = _get("https://api.tavily.com/search", method="POST", json_body={"query": query, "max_results": count},
		headers={"Authorization": f"Bearer {key}"})
	_ok(response)
	data = frappe.parse_json(body.decode())
	return [{"title": r.get("title"), "url": r.get("url"), "snippet": _clean(r.get("content") or "")[:400]} for r in (data.get("results") or [])[:count]]


def _searxng(query, count, base):
	response, body, _ = _get(base.rstrip("/") + "/search", params={"q": query, "format": "json"})
	_ok(response)
	data = frappe.parse_json(body.decode())
	return [{"title": r.get("title"), "url": r.get("url"), "snippet": _clean(r.get("content") or "")[:400]} for r in (data.get("results") or [])[:count]]


def handle_search(arguments, permission_context):
	"""web.search: titles, links and snippets from the configured search engine."""
	if not enabled():
		return {"error": "Web browsing is turned off in AI Settings."}
	query = _clean(str(arguments.get("query") or ""))[:300]
	if not query:
		return {"error": "query is required"}
	count = max(1, min(cint(arguments.get("max_results")) or 6, 10))
	settings = _settings()
	provider = settings.get("web_search_provider") or "DuckDuckGo"
	try:
		if provider == "Brave Search":
			results = _brave(query, count, settings.get_password("web_search_api_key", raise_exception=False) or "")
		elif provider == "Tavily":
			results = _tavily(query, count, settings.get_password("web_search_api_key", raise_exception=False) or "")
		elif provider == "SearXNG":
			results = _searxng(query, count, settings.get("web_search_url") or "")
		else:
			results = _duckduckgo(query, count)
	except WebError as exc:
		return {"error": str(exc), "provider": provider}
	except (requests.RequestException, ValueError) as exc:
		return {"error": f"The search engine did not answer ({exc.__class__.__name__}).", "provider": provider}
	remember_urls(getattr(permission_context, "run_name", None), [r.get("url") for r in results if isinstance(r, dict)])
	return {"query": query, "provider": provider, "results": results,
		"note": "Open the most relevant results with web.fetch before answering; cite the page URL."}
