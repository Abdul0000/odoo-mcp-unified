from odoo_agent_server.schemas import FrontendPatternPreflightRequest, OdooCodeChangeRequest, SmartToolRouteRequest
from odoo_agent_server.routers.workflow import smart_tool_router
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.routers.tools import (
    _adaptive_workflow_profile,
    _focused_scope_file_paths,
    _looks_like_frontend_code_change,
    _material_clarification,
    _scope_focus_identifiers,
)


def test_product_word_does_not_trigger_pos():
    req = FrontendPatternPreflightRequest(
        task_description="Update backend controller behavior",
        target_module_name="sample_website_module",
        custom_file_paths=["/repo/sample_website_module/controllers/main.py"],
        planned_file_paths=["sample_website_module/controllers/main.py"],
    )
    assert FrontendPatternService._detect_area(req) == "website"


def test_python_website_controller_skips_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_website_module",
        task_description="Update website controller logic",
        task_type="controller",
        target_files=["sample_website_module/controllers/main.py"],
    )
    assert not _looks_like_frontend_code_change(payload, payload.target_files)


def test_adaptive_workflow_profile_scales_without_agent_file_limit_guessing():
    small = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Add one configuration field",
        target_files=["sample_module/models/config.py"],
    )
    large = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Build the complete module across all requested layers",
        create_module=True,
    )

    assert _adaptive_workflow_profile(small, {"required_layers": ["models"]})["tier"] == "small"
    large_profile = _adaptive_workflow_profile(
        large,
        {"full_module_requested": True, "required_layers": ["models", "security", "views", "data", "tests", "controllers"]},
    )
    assert large_profile["tier"] == "large"
    assert large_profile["file_budget"] == 50
    assert large_profile["deployment_policy"] == "one_commit_one_deploy_after_contract_closure"


def test_material_clarification_only_blocks_explicit_product_choice():
    ambiguous = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Allow either all users or maybe only managers to see the approval button",
    )
    clear = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Only sales managers can see the approval button",
    )

    assert _material_clarification(ambiguous)["required"] is True
    assert _material_clarification(clear) is None
    assert _material_clarification(
        ambiguous.model_copy(update={"clarification_answer": "Only sales managers."})
    ) is None


def test_real_frontend_file_still_triggers_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_website_module",
        task_description="Update public page behavior",
        task_type="controller",
        target_files=["sample_website_module/static/src/js/page_widget.js"],
    )
    assert _looks_like_frontend_code_change(payload, payload.target_files)

from odoo_agent_server.routers.tools import _task_profile


def test_task_profiles_are_proportional():
    cases = {
        "python_model": (False, "registry_smoke_test"),
        "controller": (False, "endpoint_smoke"),
        "xml_view": (False, "backend_view_open"),
        "qweb_template": (True, "report_or_page_render"),
        "report": (False, "report_render"),
        "js_report": (True, "report_ui_smoke"),
        "js_owl": (True, "owl_ui_smoke"),
        "pos_owl": (True, "pos_flow"),
        "scss_css": (True, "page_render"),
        "manifest": (False, "module_upgrade"),
        "data_xml": (False, "module_upgrade"),
        "init": (False, "module_import"),
        "security_csv": (False, "access_smoke"),
        "cron": (False, "method_smoke"),
        "payment": (False, "payment_route_smoke"),
        "payment_frontend": (True, "payment_ui_smoke"),
    }
    for task_type, (frontend, verifier) in cases.items():
        payload = OdooCodeChangeRequest(
            module_name="sample_module",
            task_description="targeted change",
            task_type=task_type,
        )
        profile = _task_profile(payload, [])
        assert profile["frontend"] is frontend
        assert profile["live_verifier"] == verifier


def test_mixed_module_routes_from_changed_file_not_module_name():
    payload = OdooCodeChangeRequest(
        module_name="website_pos_payment_mix",
        task_description="fix backend controller",
        task_type="controller",
    )
    assert not _looks_like_frontend_code_change(payload, ["website_pos_payment_mix/controllers/main.py"])
    assert _looks_like_frontend_code_change(payload, ["website_pos_payment_mix/static/src/js/payment_form.js"])


def test_backend_task_ignores_incidental_static_search_matches():
    payload = OdooCodeChangeRequest(
        module_name="aba_recon_v2",
        task_description="Fix invoice_id on aba.recon.match.line without changing payment behavior",
        task_type="python_model",
        target_files=["aba_recon_v2/models/recon_session.py"],
    )

    assert not _looks_like_frontend_code_change(payload, [
        "aba_recon_v2/models/recon_session.py",
        "aba_recon_v2/static/src/js/recon_session_autorun.js",
        "aba_recon_v2/static/src/xml/ReconGrid.xml",
    ])


def test_scope_uses_explicit_task_files_not_broad_search_evidence(tmp_path):
    project = tmp_path / "repo"
    module = project / "aba_recon_v2"
    model = module / "models" / "recon_session.py"
    view = module / "views" / "recon_match_line_views.xml"
    manifest = module / "__manifest__.py"
    static = module / "static" / "src" / "js" / "unrelated.js"
    for path in (model, view, manifest, static):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")

    selected, focused = _focused_scope_file_paths(
        project=project,
        module_name="aba_recon_v2",
        module_root_rel="aba_recon_v2",
        custom_file_paths=[str(path) for path in (manifest, model, view, static)],
        target_files=["models/recon_session.py", "views/recon_match_line_views.xml"],
        writes=[],
    )

    assert focused is True
    assert selected == [str(model.resolve()), str(view.resolve())]


def test_missing_explicit_target_keeps_scope_on_manifest_only(tmp_path):
    project = tmp_path / "repo"
    module = project / "aba_recon_v2"
    manifest = module / "__manifest__.py"
    unrelated = module / "models" / "unrelated.py"
    unrelated.parent.mkdir(parents=True)
    manifest.write_text("{'name': 'ABA'}\n", encoding="utf-8")
    unrelated.write_text("VALUE = 1\n", encoding="utf-8")

    selected, focused = _focused_scope_file_paths(
        project=project,
        module_name="aba_recon_v2",
        module_root_rel="aba_recon_v2",
        custom_file_paths=[str(manifest), str(unrelated)],
        target_files=["models/new_target.py"],
        writes=[],
    )

    assert focused is True
    assert selected == [str(manifest)]


def test_scope_focus_prefers_exact_field_inside_descriptive_target_hint():
    payload = OdooCodeChangeRequest(
        module_name="aba_recon_v2",
        task_description="Recover the existing Bill link safely",
        task_type="python_model",
        target_hint="aba.recon.match.line invoice_id / Bill linkage for existing created invoices",
    )

    assert _scope_focus_identifiers(payload)[:2] == [
        "aba.recon.match.line",
        "invoice_id",
    ]


def test_module_wide_request_does_not_narrow_scope_to_incidental_symbols():
    payload = OdooCodeChangeRequest(
        module_name="aba_recon_v2",
        task_description="Complete module-wide redesign including invoice_id",
        task_type="python_model",
    )

    assert _scope_focus_identifiers(payload) == []


def test_backend_xml_view_does_not_trigger_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update backend form view",
        task_type="xml_view",
        target_files=["sample_module/views/sample_views.xml"],
    )
    assert not _looks_like_frontend_code_change(payload, payload.target_files)


def test_static_qweb_xml_still_triggers_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update frontend template",
        task_type="qweb_template",
        target_files=["sample_module/static/src/xml/sample_template.xml"],
    )
    assert _looks_like_frontend_code_change(payload, payload.target_files)


def test_product_path_does_not_trigger_pos_profile():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update product backend logic",
        task_type="python_model",
        target_files=["sample_module/models/product_template.py"],
    )
    profile = _task_profile(payload, payload.target_files)
    assert profile["scope"] == "model"
    assert profile["live_verifier"] == "registry_smoke_test"


def test_smart_router_distinguishes_live_data_from_source_reads():
    data = smart_tool_router(SmartToolRouteRequest(task_description="Show unpaid invoices"))
    source = smart_tool_router(SmartToolRouteRequest(task_description="Show the existing Python method"))

    assert data.task_type == "odoo_data_query"
    assert data.recommended_write_tool == "none"
    assert source.task_type == "source_read"
    assert source.recommended_write_tool == "none"
    assert source.steps[-1].tool == "repo.read"


def test_smart_router_does_not_treat_module_as_a_write_signal():
    result = smart_tool_router(SmartToolRouteRequest(task_description="Get module installation status"))

    assert result.task_type == "odoo_data_query"
    assert result.recommended_write_tool == "none"


def test_smart_router_uses_safe_discovery_for_ambiguous_requests():
    result = smart_tool_router(SmartToolRouteRequest(task_description="Customer portal improvements"))

    assert result.task_type == "ambiguous"
    assert result.confidence == "low"
    assert result.clarification_required is True
    assert result.recommended_write_tool == "none"


def test_smart_router_explicit_write_and_file_evidence_win():
    result = smart_tool_router(SmartToolRouteRequest(
        task_description="Adjust the behavior",
        operation="write",
        planned_file_paths=["sample/static/src/js/widget.js"],
    ))

    assert result.task_type == "frontend_code"
    assert result.frontend_required is True
    assert result.confidence == "high"
    assert result.clarification_required is False
