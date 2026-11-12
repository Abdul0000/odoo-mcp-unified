import pytest

from odoo_agent_server.routers.tools import (
    _resolve_force_override_authorization,
    _verification_override_decision,
)
from odoo_agent_server.schemas import OdooCodeChangeRequest, SyntaxVerifyResult, VerifyFinding


def _finding(code: str) -> VerifyFinding:
    return VerifyFinding(
        file="/tmp/custom/views.xml",
        severity="CRITICAL",
        check=code,
        message="static verifier finding",
        fix_hint="inspect source",
    )


def _verify(*codes: str) -> SyntaxVerifyResult:
    findings = [_finding(code) for code in codes]
    return SyntaxVerifyResult(
        push_blocked=bool(findings),
        status="blocked" if findings else "passed",
        findings=findings,
        critical_count=len(findings),
    )


def test_force_override_requires_exact_confirmation():
    with pytest.raises(ValueError, match="I AUTHORIZE FORCE PUSH AND DEPLOY"):
        OdooCodeChangeRequest(
            module_name="custom_sale",
            task_description="Deploy this verified change",
            verification_override="force",
            verification_override_reason="User explicitly accepts verifier risk.",
            verification_override_confirmation="force it",
        )


def test_allow_unverified_accepts_overrideable_static_findings():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="allow_unverified",
        verification_override_reason="Custom multi-model XML causes known static ambiguity.",
    )
    decision = _verification_override_decision(
        payload,
        _verify("view_field_not_in_core_model", "related_field_exists"),
    )
    assert decision["accepted"] is True
    assert decision["critical_count"] == 2


def test_allow_unverified_rejects_non_overrideable_findings():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="allow_unverified",
        verification_override_reason="Attempt narrow override.",
    )
    decision = _verification_override_decision(payload, _verify("unknown_structural_failure"))
    assert decision["accepted"] is False
    assert decision["rejected_codes"] == ["unknown_structural_failure"]


def test_force_override_accepts_verifier_findings_with_audit_data():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="force",
        verification_override_reason="User explicitly requested force push and deploy after reviewing findings.",
        verification_override_confirmation="I AUTHORIZE FORCE PUSH AND DEPLOY",
    )
    decision = _verification_override_decision(payload, _verify("xpath_match_count"))
    assert decision["accepted"] is True
    assert decision["mode"] == "force"
    assert decision["reason"]


def test_legacy_aliases_are_canonicalized():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="irrelevant_only",
        verification_override_reason="Legacy client compatibility.",
    )
    decision = _verification_override_decision(payload, _verify("xpath_match_count"))
    assert decision["accepted"] is True
    assert decision["mode"] == "allow_unverified"
    assert decision["requested_mode"] == "irrelevant_only"


def test_strict_is_default_and_does_not_override():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
    )
    decision = _verification_override_decision(payload, _verify("view_field_not_in_core_model"))
    assert decision["requested"] is False
    assert decision["accepted"] is False
    assert decision["mode"] == "strict"


def test_force_override_request_allows_missing_confirmation_for_persisted_grant_lookup():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Continue the same authorized deployment",
        verification_override="force",
        verification_override_reason="User already authorized this exact case.",
    )
    assert payload.verification_override_confirmation is None


def test_force_override_decision_requires_resolved_case_authorization():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="force",
        verification_override_reason="User accepts verifier risk.",
    )
    denied = _verification_override_decision(payload, _verify("xpath_match_count"))
    assert denied["accepted"] is False
    assert denied["confirmation_required"] is True

    accepted = _verification_override_decision(
        payload,
        _verify("xpath_match_count"),
        force_authorization={
            "authorized": True,
            "source": "persisted_case_grant",
            "case_id": "forcecase-test",
        },
    )
    assert accepted["accepted"] is True
    assert accepted["authorization_source"] == "persisted_case_grant"


class _MemoryStore:
    def __init__(self):
        self.state = {}

    def load_session_state(self, client_name=None):
        return dict(self.state)

    def save_session_state(self, updates, client_name=None):
        self.state.update(updates)
        return dict(self.state)


def test_force_confirmation_is_reused_only_for_the_same_diff_and_findings(tmp_path):
    store = _MemoryStore()
    confirmed = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="force",
        verification_override_reason="User accepts this exact case.",
        verification_override_confirmation="I AUTHORIZE FORCE PUSH AND DEPLOY",
    )
    first = _resolve_force_override_authorization(
        store,
        confirmed,
        client_name="Client A",
        project_path=str(tmp_path),
        module_name="custom_sale",
        git_diff="diff-a",
        verify=_verify("xpath_match_count"),
    )
    assert first["authorized"] is True
    assert first["source"] == "explicit_confirmation"

    continued = confirmed.model_copy(update={"verification_override_confirmation": None})
    reused = _resolve_force_override_authorization(
        store,
        continued,
        client_name="Client A",
        project_path=str(tmp_path),
        module_name="custom_sale",
        git_diff="diff-a",
        verify=_verify("xpath_match_count"),
    )
    assert reused["authorized"] is True
    assert reused["source"] == "persisted_case_grant"
    assert reused["case_id"] == first["case_id"]

    changed_diff = _resolve_force_override_authorization(
        store,
        continued,
        client_name="Client A",
        project_path=str(tmp_path),
        module_name="custom_sale",
        git_diff="diff-b",
        verify=_verify("xpath_match_count"),
    )
    assert changed_diff["authorized"] is False
    assert changed_diff["case_id"] != first["case_id"]
