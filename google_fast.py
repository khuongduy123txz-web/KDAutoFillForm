"""Batch ordinary Google Forms UI actions without submitting or editing entry data.

One browser evaluation handles a visible page. Unknown controls or values whose
real state cannot be verified are returned for the normal Playwright fallback.
The caller must still validate answers, traverse every required page and verify
the final server confirmation. No hidden input is assigned by this module.
"""
from __future__ import annotations

from pathlib import Path
import time


class FastFillError(ValueError):
    """The batch transport failed; callers may use the normal UI path."""


_FILL_PAGE = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


def fill_google_page(page, fields, answers, *, email=None):
    """Fill supported visible controls in one DOM call, never navigating/submitting.

    ``handled`` contains only verified fields. Callers must fill every ``fallback``
    field with the existing path and retain page/confirmation validation.
    The optional email and navigation snapshot share this evaluation; callers
    refresh navigation after a fallback or an unverified email control.
    """
    started = time.perf_counter()
    fields = list(fields)
    identifiers = [field["id"] for field in fields]
    requested = set(identifiers)
    if len(requested) != len(identifiers):
        raise FastFillError("Mã câu hỏi trùng trong trang đang điền.")
    try:
        result = page.evaluate(_FILL_PAGE, {"fields": fields, "answers": {identifier: answers[identifier]
                               for identifier in identifiers if identifier in answers}, "email": email})
        handled = result.get("handled") if isinstance(result, dict) else None
        if not isinstance(handled, list) or any(not isinstance(identifier, str) or identifier not in requested for identifier in handled):
            raise ValueError("Invalid batch result")
    except Exception as exc:
        raise FastFillError("Không dùng được bộ điền nhanh; cần chuyển sang cách điền thông thường.") from exc
    verified = set(handled)
    errors = result.get("errors", [])
    if not isinstance(errors, list):
        errors = []
    return {"handled": [identifier for identifier in identifiers if identifier in verified],
            "fallback": [identifier for identifier in identifiers if identifier not in verified],
            "errors": errors, "navigation": result.get("navigation"),
            "email_handled": result.get("email_handled", False), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
