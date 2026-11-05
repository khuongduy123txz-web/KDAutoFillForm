"""File-backed UI loading, live edits and failure handling without touching the real UI."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import google_form_tool as tool


def page(text):
    return '<!doctype html><html><body>' + text + '<script>const TOKEN="__APP_TOKEN__";</script></body></html>'


class UiTemplateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.source = Path(folder.name) / "google_form_tool.py"
        self.template = self.source.with_name("web_ui.html")
        self.write_source("qa")
        self.template.write_text(page("Initial page"), encoding="utf-8")
        for name, value in (("__file__", str(self.source)),
                            ("_LIVE_SETTINGS_CACHE", {"stamp": None, "keys": None, "html": None})):
            mock = patch.object(tool, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def write_source(self, name):
        # The source is parsed, never executed. WEB_HTML is deliberately not a
        # literal: UI text must come exclusively from the sibling template file.
        self.source.write_text(
            "LEGACY_ACCESS_KEYS = " + repr({name: {"sha256": "a" * 64, "enabled": True}})
            + "\nWEB_HTML = object()\nraise RuntimeError('source must never execute')\n",
            encoding="utf-8")

    def test_cached_read_uses_template_and_never_evaluates_source_code(self):
        with patch.object(tool, "_read_web_template", wraps=tool._read_web_template) as read:
            first = tool.live_settings()
            second = tool.live_settings()
        self.assertIs(first, second)
        self.assertEqual(first["html"], page("Initial page"))
        self.assertEqual(set(first["keys"]), {"qa"})
        self.assertEqual(read.call_count, 1)

    def test_template_edit_invalidates_cache_without_source_change(self):
        first = tool.live_settings()
        source_stamp = self.source.stat().st_mtime_ns
        changed = page("A different, longer page")
        self.template.write_text(changed, encoding="utf-8")
        second = tool.live_settings()
        self.assertEqual(self.source.stat().st_mtime_ns, source_stamp)
        self.assertNotEqual(first["stamp"], second["stamp"])
        self.assertEqual(second["html"], changed)

    def test_legacy_key_source_edit_invalidates_cache_and_retains_template(self):
        first = tool.live_settings()
        self.write_source("new-qa-name")
        second = tool.live_settings()
        self.assertEqual(set(second["keys"]), {"new-qa-name"})
        self.assertEqual(second["html"], first["html"])
        self.assertNotEqual(first["stamp"], second["stamp"])

    def test_missing_template_after_cached_read_fails_instead_of_serving_stale_ui(self):
        tool.live_settings()
        self.template.unlink()
        with self.assertRaisesRegex(tool.FormError, "web_ui.html"):
            tool.live_settings()

    def test_missing_source_has_clear_error(self):
        self.source.unlink()
        with self.assertRaises(tool.FormError):
            tool.live_settings()

    def test_invalid_utf8_and_missing_session_marker_have_clear_template_error(self):
        for content in (b"\xff\xfe\xfa", b"", b"<html><body>No token</body></html>"):
            with self.subTest(content=content):
                self.template.write_bytes(content)
                with self.assertRaisesRegex(tool.FormError, "web_ui.html"):
                    tool.live_settings()

    def test_nonliteral_legacy_registry_is_rejected_without_execution(self):
        self.source.write_text("LEGACY_ACCESS_KEYS = dict()\n", encoding="utf-8")
        with self.assertRaises(tool.FormError):
            tool.live_settings()


if __name__ == "__main__":
    unittest.main()
