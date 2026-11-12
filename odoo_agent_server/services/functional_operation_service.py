from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import (
    OdooFlowTestRequest,
    OdooMutationAuditRequest,
    OdooRecordActionRequest,
    OdooRecordCreateRequest,
    OdooRecordWriteRequest,
    OdooResult,
)
from .approval_service import ApprovalService
from .client_resolver_service import ClientResolverService, ResolvedClient
from .odoo_jsonrpc_service import OdooJsonRpcService
from .store_service import JsonStore


_FORBIDDEN_WRITE_FIELDS = {
    "id", "state", "create_uid", "create_date", "write_uid", "write_date", "__last_update",
}
_SAFE_CONTEXT_KEYS = {
    "lang", "tz", "company_id", "allowed_company_ids", "active_id", "active_ids",
    "active_model", "tracking_disable", "mail_notrack", "mail_create_nosubscribe",
    "mail_auto_subscribe_no_notify",
}
_BLOCKED_ACTION_METHODS = {
    "create", "write", "unlink", "read", "search", "search_read", "read_group",
    "fields_get", "default_get", "check_access_rights", "check_access_rule",
    "sudo", "with_context", "with_user", "execute_kw",
}
_SENSITIVE_ACTION_TOKENS = {
    "post", "validate", "done", "complete", "cancel", "delete", "unlink",
    "payment", "pay", "reconcile", "refund", "return", "scrap", "close",
}
_NON_SENSITIVE_ACTIONS = {"action_return"}
_BLOCKED_MODELS = {
    "ir.config_parameter", "ir.model", "ir.model.fields", "ir.model.access",
    "ir.rule", "ir.cron", "ir.actions.server", "base.automation",
    "res.users", "res.groups",
}
_ACTION_EXPECTED_STATES: dict[tuple[str, str], set[str]] = {
    ("sale.order", "action_confirm"): {"sale", "done"},
    ("sale.order", "action_cancel"): {"cancel"},
    ("purchase.order", "button_confirm"): {"purchase", "done"},
    ("purchase.order", "button_cancel"): {"cancel"},
    ("stock.picking", "action_confirm"): {"confirmed", "waiting", "assigned"},
    ("stock.picking", "action_assign"): {"assigned"},
    ("stock.picking", "button_validate"): {"done"},
    ("mrp.production", "action_confirm"): {"confirmed", "progress", "to_close"},
    ("mrp.production", "action_assign"): {"confirmed", "progress", "to_close"},
    ("mrp.production", "button_mark_done"): {"done"},
    ("account.move", "action_post"): {"posted"},
    ("account.move", "button_cancel"): {"cancel"},
}
_FLOW_RECIPES = {
    "sale_order": {
        "model": "sale.order",
        "marker": "client_order_ref",
        "actions": {"confirm": "action_confirm", "cancel": "action_cancel"},
        "verify": ["id", "name", "state", "partner_id", "amount_total"],
    },
    "purchase_order": {
        "model": "purchase.order",
        "marker": "partner_ref",
        "actions": {"confirm": "button_confirm", "cancel": "button_cancel"},
        "verify": ["id", "name", "state", "partner_id", "amount_total"],
    },
    "inventory_transfer": {
        "model": "stock.picking",
        "marker": "origin",
        "actions": {"confirm": "action_confirm", "reserve": "action_assign", "validate": "button_validate"},
        "verify": ["id", "name", "state", "picking_type_id", "origin"],
    },
    "inventory_return": {
        "model": "stock.return.picking",
        "actions": {"create_return": "create_returns"},
        "verify": ["id", "picking_id", "product_return_moves"],
    },
    "manufacturing_order": {
        "model": "mrp.production",
        "marker": "origin",
        "actions": {"confirm": "action_confirm", "reserve": "action_assign", "complete": "button_mark_done"},
        "verify": ["id", "name", "state", "product_id", "product_qty"],
    },
    "customer_invoice": {
        "model": "account.move",
        "marker": "ref",
        "defaults": {"move_type": "out_invoice"},
        "actions": {"post": "action_post", "cancel": "button_cancel"},
        "verify": ["id", "name", "state", "move_type", "partner_id", "amount_total", "payment_state"],
    },
    "vendor_bill": {
        "model": "account.move",
        "marker": "ref",
        "defaults": {"move_type": "in_invoice"},
        "actions": {"post": "action_post", "cancel": "button_cancel"},
        "verify": ["id", "name", "state", "move_type", "partner_id", "amount_total", "payment_state"],
    },
}


class FunctionalOperationService:
    """Execute permission-aware live Odoo mutations outside the code pipeline."""

    def __init__(self) -> None:
        self.store = JsonStore()
        self.approvals = ApprovalService()

    def create(self, request: OdooRecordCreateRequest) -> dict[str, Any]:
        client, rpc = self._runtime(request.client_name)
        payload = request.model_dump(mode="json", exclude={"approval_id"})
        replay = self._replay(request.request_id, self._client_key(client), "record_create", payload)
        if replay:
            return replay
        self._ensure_model(request.model)
        self._ensure_batch(len(request.records))
        fields = self._metadata(rpc, request.model)
        self._ensure_access(rpc, request.model, "create")
        for values in request.records:
            self._validate_values(fields, values, creating=True)
        context = self._safe_context(request.context)
        result = rpc.create_records(request.model, request.records, context)
        ids = self._created_ids(result)
        if not ids:
            return self._failed(request, client, "record_create", payload, result.error or "Odoo create returned no record IDs.")
        verify_fields = self._verify_fields(fields, request.verify_fields, request.records)
        verified_records = self._read_ok(rpc, request.model, ids, verify_fields, context)
        response = {
            "status": "ok",
            "operation": "record_create",
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "model": request.model,
            "record_ids": ids,
            "records": verified_records,
            "verified": len(verified_records) == len(ids),
            "success_allowed": len(verified_records) == len(ids),
        }
        return self._save(request, client, "record_create", payload, response)

    def write(self, request: OdooRecordWriteRequest) -> dict[str, Any]:
        client, rpc = self._runtime(request.client_name)
        payload = request.model_dump(mode="json", exclude={"approval_id"})
        replay = self._replay(request.request_id, self._client_key(client), "record_write", payload)
        if replay:
            return replay
        self._ensure_model(request.model)
        self._ensure_batch(len(request.ids))
        fields = self._metadata(rpc, request.model)
        self._ensure_access(rpc, request.model, "write")
        self._validate_values(fields, request.values, creating=False)
        context = self._safe_context(request.context)
        tracked = sorted(set(request.values) | set(request.expected_values) | set(request.verify_fields) | {"id", "display_name"})
        tracked = self._verify_fields(fields, tracked, [request.values])
        before = self._read_ok(rpc, request.model, request.ids, tracked, context)
        if len(before) != len(request.ids):
            raise HTTPException(status_code=404, detail="One or more records are missing or blocked by record rules.")
        mismatches = [
            {"id": row.get("id"), "field": key, "expected": expected, "actual": row.get(key)}
            for row in before
            for key, expected in request.expected_values.items()
            if row.get(key) != expected
        ]
        if mismatches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": "Record changed since inspection; write was not executed.", "mismatches": mismatches[:20]},
            )
        result = rpc.write_records(request.model, request.ids, request.values, context)
        if result.status != "ok" or result.result is not True:
            return self._failed(request, client, "record_write", payload, result.error or "Odoo write failed.")
        after = self._read_ok(rpc, request.model, request.ids, tracked, context)
        verified = len(after) == len(request.ids) and all(
            self._value_matches(row.get(key), value)
            for row in after
            for key, value in request.values.items()
            if not isinstance(value, (dict, list))
        )
        unverifiable_fields = sorted(
            key for key, value in request.values.items() if isinstance(value, (dict, list))
        )
        response = {
            "status": "ok",
            "operation": "record_write",
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "model": request.model,
            "record_ids": request.ids,
            "before": self._redact(before),
            "records": self._redact(after),
            "verified": verified,
            "unverifiable_fields": unverifiable_fields,
        }
        response["success_allowed"] = verified and not unverifiable_fields
        return self._save(request, client, "record_write", payload, response)

    def action(self, request: OdooRecordActionRequest) -> dict[str, Any]:
        client, rpc = self._runtime(request.client_name)
        payload = request.model_dump(mode="json", exclude={"approval_id"})
        replay = self._replay(request.request_id, self._client_key(client), "record_action", payload)
        if replay:
            return replay
        self._ensure_model(request.model)
        self._ensure_batch(len(request.ids))
        sensitive = self._action_sensitive(request.model, request.action)
        fields = self._metadata(rpc, request.model)
        self._ensure_access(rpc, request.model, "write")
        context = self._safe_context(request.context)
        verify_fields = self._verify_fields(
            fields,
            request.verify_fields or ["id", "display_name", "name", "state"],
            [],
        )
        before = self._read_ok(rpc, request.model, request.ids, verify_fields, context)
        if len(before) != len(request.ids):
            raise HTTPException(status_code=404, detail="One or more action records are missing or blocked by record rules.")
        approval_payload = self._redact(payload)
        try:
            self.approvals.ensure_allowed(
                f"odoo.record_action.{request.model}.{request.action}",
                request.approval_id,
                approval_payload,
                force=sensitive,
                bind_payload=sensitive,
            )
        except HTTPException as exc:
            detail = exc.detail
            if exc.status_code == status.HTTP_409_CONFLICT and isinstance(detail, dict) and detail.get("approval_required"):
                # Keep the mutation resumable. The approval is deliberately not
                # written to the mutation ledger, so the exact same request_id
                # can be retried after a human approves the payload-bound gate.
                return {
                    "status": "awaiting_approval",
                    "operation": "record_action",
                    "request_id": request.request_id,
                    "client_name": self._client_key(client),
                    "model": request.model,
                    "action": request.action,
                    "record_ids": request.ids,
                    "approval_id": detail.get("approval_id"),
                    "approval_action": detail.get("action"),
                    "verified": False,
                    "success_allowed": False,
                    "next_action": "Obtain explicit user approval, approve this exact approval_id, then retry the identical action with the same request_id and approval_id.",
                }
            raise
        result = rpc.run_action(
            request.model,
            request.ids,
            request.action,
            request.arguments,
            request.keyword_arguments,
            context,
        )
        if result.status != "ok":
            self.approvals.mark_failed(request.approval_id, result.error)
            return self._failed(request, client, "record_action", payload, result.error or "Odoo action failed.")
        after = self._read_ok(rpc, request.model, request.ids, verify_fields, context)
        self.approvals.mark_consumed(request.approval_id)
        expected_states = _ACTION_EXPECTED_STATES.get((request.model, request.action))
        states = [str(row.get("state") or "") for row in after]
        if expected_states:
            verified = len(after) == len(request.ids) and all(state in expected_states for state in states)
            verification_mode = "expected_state"
        else:
            verified = len(after) == len(request.ids) and result.result is not False
            verification_mode = "rpc_result_and_read_back"
        response = {
            "status": "ok",
            "operation": "record_action",
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "model": request.model,
            "action": request.action,
            "record_ids": request.ids,
            "before": self._redact(before),
            "records": self._redact(after),
            "action_result": self._redact(result.result),
            "expected_states": sorted(expected_states or []),
            "verification_mode": verification_mode,
            "verified": verified,
            "success_allowed": verified,
        }
        return self._save(request, client, "record_action", payload, response)

    def flow_test(self, request: OdooFlowTestRequest) -> dict[str, Any]:
        if request.steps:
            return self._dynamic_flow_test(request)
        if request.flow not in _FLOW_RECIPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown recipe {request.flow!r}. Provide ordered steps for a dynamic flow, "
                    f"or use one of: {sorted(_FLOW_RECIPES)}"
                ),
            )
        client, rpc = self._runtime(request.client_name)
        recipe = _FLOW_RECIPES[request.flow]
        model = str(recipe["model"])
        fields = self._metadata(rpc, model)
        self._ensure_model(model)
        self._ensure_access(rpc, model, "create")
        stages = request.stages or []
        unknown = [stage for stage in stages if stage not in recipe["actions"]]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported stages for {request.flow}: {unknown}. Allowed: {sorted(recipe['actions'])}",
            )
        values = {**recipe.get("defaults", {}), **request.values}
        marker = str(recipe.get("marker") or "")
        if request.mode == "persistent_test" and marker in fields and marker not in values:
            values[marker] = f"MCP-TEST-{request.request_id}"
        self._validate_values(fields, values, creating=True)
        plan = {
            "flow": request.flow,
            "model": model,
            "mode": request.mode,
            "stages": stages,
            "actions": [recipe["actions"][stage] for stage in stages],
            "sensitive_stages": [
                stage for stage in stages if self._action_sensitive(model, recipe["actions"][stage])
            ],
            "notifications_suppressed": True,
        }
        if request.mode == "preview":
            return {
                "status": "preview",
                "request_id": request.request_id,
                "client_name": self._client_key(client),
                "plan": plan,
                "write_executed": False,
                "success_allowed": False,
                "next_action": "Review the plan, then retry with persistent_test or live mode.",
            }
        create_request = OdooRecordCreateRequest(
            client_name=request.client_name,
            request_id=f"{request.request_id}:create",
            context=request.context,
            model=model,
            records=[values],
            verify_fields=request.verify_fields or recipe["verify"],
        )
        created = self.create(create_request)
        if created.get("status") != "ok" or not created.get("record_ids"):
            return {
                "status": "failed",
                "request_id": request.request_id,
                "plan": plan,
                "created": created,
                "success_allowed": False,
            }
        record_ids = list(created["record_ids"])
        stage_results: list[dict[str, Any]] = []
        for index, stage in enumerate(stages):
            action = str(recipe["actions"][stage])
            action_request = OdooRecordActionRequest(
                client_name=request.client_name,
                request_id=f"{request.request_id}:stage:{index}:{stage}",
                context=request.context,
                approval_id=request.approval_id,
                model=model,
                ids=record_ids,
                action=action,
                verify_fields=request.verify_fields or recipe["verify"],
            )
            result = self.action(action_request)
            stage_results.append({"stage": stage, **result})
            if result.get("status") != "ok" or result.get("verified") is not True:
                break
        pending = next((item for item in stage_results if item.get("status") == "awaiting_approval"), None)
        complete = len(stage_results) == len(stages) and all(
            item.get("status") == "ok" and item.get("verified") is True for item in stage_results
        )
        response = {
            "status": "awaiting_approval" if pending else ("ok" if complete else "failed"),
            "operation": "flow_test",
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "plan": plan,
            "record_ids": record_ids,
            "created": created,
            "stage_results": stage_results,
            "verified": complete,
            "success_allowed": complete,
            "cleanup": "Only draft/cancelled test records may be removed manually; no automatic unlink is exposed.",
        }
        if pending:
            response["approval"] = {
                "approval_id": pending.get("approval_id"),
                "action": pending.get("approval_action"),
                "model": pending.get("model"),
                "record_ids": pending.get("record_ids"),
            }
            response["next_action"] = "After explicit user approval, call approvals.approve and retry this exact flow with the same request_id and approval_id. Completed prior steps are idempotently replayed."
            return response
        payload = request.model_dump(mode="json", exclude={"approval_id"})
        return self._save(request, client, "flow_test", payload, response)

    def _dynamic_flow_test(self, request: OdooFlowTestRequest) -> dict[str, Any]:
        client, rpc = self._runtime(request.client_name)
        names = [step.name for step in request.steps]
        if len(names) != len(set(names)):
            raise HTTPException(status_code=400, detail="Dynamic flow step names must be unique.")

        plan_steps: list[dict[str, Any]] = []
        for step in request.steps:
            self._ensure_model(step.model)
            fields = self._metadata(rpc, step.model)
            access = "create" if step.operation == "create" else ("read" if step.operation == "verify" else "write")
            self._ensure_access(rpc, step.model, access)
            if step.operation == "create":
                for values in step.records:
                    self._validate_values(fields, values, creating=True)
            elif step.operation == "write":
                self._validate_values(fields, step.values, creating=False)
            sensitive = bool(step.action and self._action_sensitive(step.model, step.action))
            plan_steps.append(
                {
                    "name": step.name,
                    "operation": step.operation,
                    "model": step.model,
                    "action": step.action,
                    "ids": step.ids,
                    "ids_from": step.ids_from,
                    "sensitive": sensitive,
                }
            )

        plan = {
            "flow": request.flow,
            "mode": request.mode,
            "dynamic": True,
            "steps": plan_steps,
            "notifications_suppressed": True,
        }
        if request.mode == "preview":
            return {
                "status": "preview",
                "request_id": request.request_id,
                "client_name": self._client_key(client),
                "plan": plan,
                "write_executed": False,
                "success_allowed": False,
                "next_action": "Review the dynamic plan, then retry with persistent_test or live mode.",
            }

        outputs: dict[str, dict[str, Any]] = {}
        stage_results: list[dict[str, Any]] = []
        for index, step in enumerate(request.steps):
            records = self._resolve_refs(step.records, outputs)
            values = self._resolve_refs(step.values, outputs)
            arguments = self._resolve_refs(step.arguments, outputs)
            keyword_arguments = self._resolve_refs(step.keyword_arguments, outputs)
            expected_values = self._resolve_refs(step.expected_values, outputs)
            ids = [] if step.operation == "create" else self._step_ids(step.ids, step.ids_from, outputs)
            step_request_id = f"{request.request_id}:step:{index}:{step.name}"

            if step.operation == "create":
                result = self.create(
                    OdooRecordCreateRequest(
                        client_name=request.client_name,
                        request_id=step_request_id,
                        context=request.context,
                        model=step.model,
                        records=records,
                        verify_fields=step.verify_fields,
                    )
                )
            elif step.operation == "write":
                result = self.write(
                    OdooRecordWriteRequest(
                        client_name=request.client_name,
                        request_id=step_request_id,
                        context=request.context,
                        model=step.model,
                        ids=ids,
                        values=values,
                        expected_values=expected_values,
                        verify_fields=step.verify_fields,
                    )
                )
            elif step.operation == "action":
                result = self.action(
                    OdooRecordActionRequest(
                        client_name=request.client_name,
                        request_id=step_request_id,
                        context=request.context,
                        approval_id=step.approval_id or request.approval_id,
                        model=step.model,
                        ids=ids,
                        action=str(step.action),
                        arguments=arguments,
                        keyword_arguments=keyword_arguments,
                        verify_fields=step.verify_fields,
                    )
                )
            else:
                result = self._verify_dynamic_step(
                    rpc,
                    step.model,
                    ids,
                    expected_values,
                    step.verify_fields,
                    request.context,
                )

            output = {"step": step.name, **result}
            outputs[step.name] = output
            stage_results.append(output)
            if result.get("status") not in {"ok", "preview"} or result.get("verified") is not True:
                break

        pending = next((item for item in stage_results if item.get("status") == "awaiting_approval"), None)
        complete = len(stage_results) == len(request.steps) and all(
            item.get("status") == "ok" and item.get("verified") is True
            for item in stage_results
        )
        response = {
            "status": "awaiting_approval" if pending else ("ok" if complete else "failed"),
            "operation": "flow_test",
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "plan": plan,
            "stage_results": stage_results,
            "outputs": outputs,
            "verified": complete,
            "success_allowed": complete,
        }
        if pending:
            response["approval"] = {
                "approval_id": pending.get("approval_id"),
                "action": pending.get("approval_action"),
                "model": pending.get("model"),
                "record_ids": pending.get("record_ids"),
            }
            response["next_action"] = "After explicit user approval, call approvals.approve and retry this exact flow with the same request_id and approval_id. Completed prior steps are idempotently replayed."
            return response
        payload = request.model_dump(mode="json", exclude={"approval_id"})
        return self._save(request, client, "flow_test", payload, response)

    def _verify_dynamic_step(
        self,
        rpc: OdooJsonRpcService,
        model: str,
        ids: list[int],
        expected_values: dict[str, Any],
        verify_fields: list[str],
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        fields = self._metadata(rpc, model)
        selected = self._verify_fields(
            fields,
            [*verify_fields, *expected_values],
            [],
        )
        records = self._read_ok(rpc, model, ids, selected, self._safe_context(context))
        verified = len(records) == len(ids) and all(
            self._value_matches(row.get(key), value)
            for row in records
            for key, value in expected_values.items()
        )
        return {
            "status": "ok",
            "model": model,
            "record_ids": ids,
            "records": self._redact(records),
            "verified": verified,
            "success_allowed": verified,
        }

    def audit(self, request: OdooMutationAuditRequest) -> dict[str, Any]:
        client = ClientResolverService().resolve_client(request.client_name, require_complete=False)
        client_name = self._client_key(client)
        return {
            "status": "ok",
            "client_name": client_name,
            "items": self.store.list_mutations(request.limit, request.request_id, client_name),
        }

    def _runtime(self, client_name: str | None) -> tuple[ResolvedClient | None, OdooJsonRpcService]:
        if not settings.functional_operations_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Functional record operations are disabled. Set "
                    "ODOO_AGENT_FUNCTIONAL_OPERATIONS_ENABLED=true after test-database validation."
                ),
            )
        client = ClientResolverService().resolve_client(client_name, require_complete=False)
        rpc = OdooJsonRpcService(client)
        if not rpc.configured():
            raise HTTPException(
                status_code=400,
                detail="Authenticated Odoo URL, database, login, and password are required; SSH superuser fallback is prohibited.",
            )
        return client, rpc

    def _ensure_model(self, model: str) -> None:
        if model in _BLOCKED_MODELS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Security/system model {model!r} cannot be mutated through functional operations.",
            )

    def _ensure_batch(self, count: int) -> None:
        if count > settings.functional_max_batch_records:
            raise HTTPException(
                status_code=400,
                detail=f"Batch has {count} records; configured maximum is {settings.functional_max_batch_records}.",
            )

    @staticmethod
    def _safe_context(context: dict[str, Any] | None) -> dict[str, Any]:
        supplied = context or {}
        unknown = sorted(set(supplied) - _SAFE_CONTEXT_KEYS)
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unsupported Odoo context keys: {unknown}")
        return {
            **supplied,
            "tracking_disable": True,
            "mail_notrack": True,
            "mail_create_nosubscribe": True,
            "mail_auto_subscribe_no_notify": True,
        }

    @staticmethod
    def _metadata(rpc: OdooJsonRpcService, model: str) -> dict[str, dict[str, Any]]:
        result = rpc.fields_get(model)
        if result.status != "ok" or not isinstance(result.result, dict):
            raise HTTPException(status_code=400, detail=result.error or f"Cannot inspect fields for {model}.")
        return result.result

    @staticmethod
    def _ensure_access(rpc: OdooJsonRpcService, model: str, operation: str) -> None:
        result = rpc.check_access_rights(model, operation)
        if result.status != "ok" or result.result is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=result.error or f"Configured Odoo user lacks {operation} access on {model}.",
            )

    @staticmethod
    def _validate_values(fields: dict[str, dict[str, Any]], values: dict[str, Any], *, creating: bool) -> None:
        unknown = sorted(set(values) - set(fields))
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown or inaccessible fields: {unknown}")
        forbidden = sorted(set(values) & _FORBIDDEN_WRITE_FIELDS)
        if forbidden:
            raise HTTPException(
                status_code=400,
                detail=f"Direct writes to workflow/system fields are prohibited: {forbidden}",
            )
        readonly = sorted(
            name for name in values if fields.get(name, {}).get("readonly") and not creating
        )
        if readonly:
            raise HTTPException(status_code=400, detail=f"Readonly fields cannot be updated: {readonly}")

    @staticmethod
    def _verify_fields(
        fields: dict[str, dict[str, Any]],
        requested: list[str],
        values: list[dict[str, Any]],
    ) -> list[str]:
        candidates = ["id", "display_name", *requested]
        for row in values:
            candidates.extend(row)
        result: list[str] = []
        for name in candidates:
            if name in fields and name not in result:
                result.append(name)
        return result[:30]

    @staticmethod
    def _read_ok(
        rpc: OdooJsonRpcService,
        model: str,
        ids: list[int],
        fields: list[str],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = rpc.read_records(model, ids, fields, context)
        if result.status != "ok" or not isinstance(result.result, list):
            raise HTTPException(status_code=400, detail=result.error or f"Read-back verification failed for {model}.")
        return result.result

    @staticmethod
    def _created_ids(result: OdooResult) -> list[int]:
        if result.status != "ok":
            return []
        raw = result.result
        if isinstance(raw, int):
            return [raw]
        if isinstance(raw, list) and all(isinstance(item, int) for item in raw):
            return raw
        return []

    @staticmethod
    def _value_matches(actual: Any, expected: Any) -> bool:
        if isinstance(expected, int) and isinstance(actual, (list, tuple)) and actual:
            return actual[0] == expected
        return actual == expected

    def _step_ids(
        self,
        explicit_ids: list[int],
        ids_from: str | None,
        outputs: dict[str, dict[str, Any]],
    ) -> list[int]:
        raw: Any = explicit_ids
        if ids_from:
            raw = self._reference_value(ids_from, outputs)
        if isinstance(raw, int):
            raw = [raw]
        if not isinstance(raw, list) or not raw or any(not isinstance(item, int) or item <= 0 for item in raw):
            raise HTTPException(status_code=400, detail=f"Dynamic step resolved invalid record IDs from {ids_from!r}.")
        self._ensure_batch(len(raw))
        return raw

    def _resolve_refs(self, value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
        if isinstance(value, list):
            return [self._resolve_refs(item, outputs) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"$ref"}:
            reference = value.get("$ref")
            if not isinstance(reference, str):
                raise HTTPException(status_code=400, detail="Dynamic $ref must be a string.")
            return self._reference_value(reference, outputs)
        return {key: self._resolve_refs(item, outputs) for key, item in value.items()}

    @staticmethod
    def _reference_value(reference: str, outputs: dict[str, dict[str, Any]]) -> Any:
        parts = reference.split(".")
        if not parts or parts[0] not in outputs:
            raise HTTPException(status_code=400, detail=f"Dynamic reference {reference!r} does not target a completed step.")
        value: Any = outputs[parts[0]]
        for part in parts[1:]:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                raise HTTPException(status_code=400, detail=f"Dynamic reference path {reference!r} was not found.")
        return value

    @staticmethod
    def _action_sensitive(model: str, action: str) -> bool:
        if action.startswith("_") or action in _BLOCKED_ACTION_METHODS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Private/system method {model}.{action} cannot be called through odoo.record_action.",
            )
        lowered = action.lower()
        if lowered in _NON_SENSITIVE_ACTIONS:
            return False
        return any(token in lowered for token in _SENSITIVE_ACTION_TOKENS)

    @staticmethod
    def _client_key(client: ResolvedClient | None) -> str:
        return client.name if client else "default"

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _replay(
        self,
        request_id: str,
        client_name: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        existing = self.store.get_mutation(request_id, client_name)
        if not existing:
            return None
        if existing.get("operation") != operation or existing.get("payload_hash") != self._payload_hash(payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request_id was already used for a different mutation payload.",
            )
        response = dict(existing.get("response") or {})
        response["idempotent_replay"] = True
        return response

    def _save(
        self,
        request: Any,
        client: ResolvedClient | None,
        operation: str,
        payload: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "operation": operation,
            "payload_hash": self._payload_hash(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response": self._redact(response),
        }
        self.store.save_mutation(record)
        self.store.log_audit(
            f"odoo.{operation}",
            str(response.get("status") or "unknown"),
            f"{operation} {response.get('model') or response.get('plan', {}).get('model') or ''}".strip(),
            {
                "request_id": request.request_id,
                "client_name": self._client_key(client),
                "record_ids": response.get("record_ids") or [],
                "verified": response.get("verified"),
            },
        )
        return response

    def _failed(
        self,
        request: Any,
        client: ResolvedClient | None,
        operation: str,
        payload: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        response = {
            "status": "failed",
            "operation": operation,
            "request_id": request.request_id,
            "client_name": self._client_key(client),
            "error": error,
            "verified": False,
            "success_allowed": False,
        }
        return self._save(request, client, operation, payload, response)

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: (
                "[REDACTED]"
                if any(token in str(key).lower() for token in ("password", "secret", "token", "api_key"))
                else cls._redact(item)
            )
            for key, item in value.items()
        }
