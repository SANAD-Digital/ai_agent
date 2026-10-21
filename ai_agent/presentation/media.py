"""Small inline thumbnails for cards.

A sandboxed MCP App cannot fetch the site's files (no network origins, no session cookie), so the server reads
the file the user is already allowed to see (via the document they can read) and inlines a small picture.
"""

import base64
import io

import frappe

MAX_BYTES = 90_000


def thumbnail_data_uri(file_url, max_side=240):
    """data: URI of a reduced copy of a File, or None. Never raises: a missing picture must not break a card."""
    if not file_url or not isinstance(file_url, str) or not file_url.startswith(("/files/", "/private/files/")):
        return None
    try:
        from PIL import Image

        name = frappe.db.get_value("File", {"file_url": file_url}, "name")
        if not name:
            return None
        content = frappe.get_doc("File", name).get_content()
        image = Image.open(io.BytesIO(content))
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        has_alpha = image.mode in ("RGBA", "LA", "P")
        if has_alpha:
            image.convert("RGBA").save(buffer, "PNG", optimize=True)
            mime = "image/png"
        else:
            image.convert("RGB").save(buffer, "JPEG", quality=80, optimize=True)
            mime = "image/jpeg"
        data = buffer.getvalue()
        if len(data) > MAX_BYTES:
            return None
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:
        # Cosmetic: a missing picture must not break a card (see docstring).
        return None
