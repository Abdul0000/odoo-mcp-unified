"""Unit tests for SyntaxVerifierService._verify_js_syntax_real.

Every JS check that existed before this method was added
(js_odoo_module_header, js_no_mixed_legacy_modern, js_import_alias_known,
etc. in _verify_js_owl) is regex/substring matching — none of them actually
parse the file, so a genuinely broken file (unmatched brace, stray token)
passes clean as long as the right magic strings appear. This test proves
_verify_js_syntax_real catches that class of bug via a real `node --check`
parse, and that it degrades to an INFO finding instead of crashing when
Node isn't available.
"""
from __future__ import annotations

import shutil

import pytest

from odoo_agent_server.schemas import VerifySeverity
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService

VALID_OWL_JS = """/** @odoo-module **/
import { Component, useState } from "@odoo/owl";

export class Foo extends Component {
    setup() {
        this.state = useState({ count: 0 });
    }
}
"""

BROKEN_OWL_JS = """/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Foo extends Component {
    setup() {
        this.state = { count: 0 )
    }
}
"""


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed in this environment")
def test_valid_js_produces_no_critical_findings():
    findings = SyntaxVerifierService()._verify_js_syntax_real("fake/valid.js", VALID_OWL_JS)
    assert not any(f.severity == VerifySeverity.CRITICAL for f in findings)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed in this environment")
def test_broken_js_is_caught_with_line_number():
    """This is the exact class of bug the old regex-only checks could never
    catch: valid-looking magic strings (@odoo-module header, useState in
    setup()) around a plain syntax error (unmatched brace/paren).
    """
    findings = SyntaxVerifierService()._verify_js_syntax_real("fake/broken.js", BROKEN_OWL_JS)
    critical = [f for f in findings if f.severity == VerifySeverity.CRITICAL]
    assert critical, "Expected a CRITICAL finding for a file with a real syntax error"
    assert critical[0].check == "js_real_syntax_error"
    assert critical[0].line == 6


def test_missing_node_degrades_to_info_not_crash(monkeypatch):
    """If Node isn't installed, verify_against_core must not crash — it
    should report INFO and let the existing regex checks still run.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)
    findings = SyntaxVerifierService()._verify_js_syntax_real("fake/whatever.js", VALID_OWL_JS)
    assert len(findings) == 1
    assert findings[0].severity == VerifySeverity.INFO
    assert findings[0].check == "js_real_syntax_check_skipped"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed in this environment")
def test_verify_dispatches_real_check_for_js_files(tmp_path):
    """End-to-end through the public verify() entrypoint, not just the
    private method, to confirm the dispatch wiring in verify() actually
    calls _verify_js_syntax_real for .js files.
    """
    from odoo_agent_server.schemas import SyntaxVerifyRequest

    js_file = tmp_path / "broken_widget.js"
    js_file.write_text(BROKEN_OWL_JS, encoding="utf-8")

    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(js_file)]))
    codes = [f.code for f in result.findings]
    assert "js_real_syntax_error" in codes
    assert result.push_blocked is True
