"""Strict Odoo frontend/OWL/POS/website preflight planner.

This service exists to stop the agent from writing Odoo JavaScript, OWL,
QWeb/XML, website snippet, payment, or POS code from memory.  It returns a
blocking read plan and exact framework rules based on the target area so the
agent must inspect real Odoo source first, then code with the same pattern.
"""
from __future__ import annotations

import hashlib
import json
import time
import re
from pathlib import Path
from typing import Iterable

from ..schemas import (
    FrontendPatternPreflightRequest,
    FrontendPatternPreflightResult,
    FrontendReadInstruction,
)
from ..config import settings
from .core_finder_service import CoreFinderService


_AREA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pos": (
        "pos", "point_of_sale", "point of sale", "payment screen", "receipt screen",
        "product screen", "ticket screen", "kitchen", "preparation screen",
    ),
    "website": (
        "website", "snippet", "builder", "editor", "website_form", "website_sale",
        "portal page", "frontend", "publicwidget", "public widget", "waitlist",
    ),
    "payment": (
        "payment", "provider", "checkout", "stripe", "paypal", "transaction",
        "website_payment", "payment form", "payment method",
    ),
    "web": (
        "owl", "component", "dashboard", "webclient", "web client", "registry",
        "kanban", "list view", "form controller", "control panel", "action service",
    ),
}


_AREA_PLANS: dict[str, list[tuple[str, str, str]]] = {
    "pos": [
        ("point_of_sale", "static/src/app", "POS OWL app entrypoints, store, services, screens, and components"),
        ("point_of_sale", "static/src/app/screens", "POS screen component patterns and template names"),
        ("point_of_sale", "static/src/app/store", "POS store/service access patterns"),
        ("point_of_sale", "static/src/app/components", "Reusable POS component patterns"),
        ("point_of_sale", "static/src/xml", "POS QWeb/OWL template extension patterns when present"),
        ("point_of_sale", "views", "POS asset bundle declaration patterns"),
        ("web", "static/src/core", "OWL hooks/services/registry base patterns"),
    ],
    "website": [
        ("website", "static/src", "Website frontend/editor JS and options patterns"),
        ("website", "views", "Website template/snippet/view inheritance patterns"),
        ("website_form", "static/src", "Website form field/snippet/widget patterns"),
        ("website_sale", "static/src", "Website sale frontend component patterns when shop/cart is involved"),
        ("web", "static/src/core", "OWL hooks/services/registry base patterns"),
    ],
    "payment": [
        ("payment", "static/src", "Payment provider frontend and form patterns"),
        ("payment", "views", "Payment provider backend/frontend view patterns"),
        ("website_payment", "static/src", "Website payment checkout JS patterns"),
        ("website_payment", "views", "Website payment QWeb/template inheritance patterns"),
        ("website_sale", "static/src", "Checkout/shop integration patterns"),
        ("web", "static/src/core", "OWL hooks/services/registry base patterns"),
    ],
    "web": [
        ("web", "static/src/core", "Core OWL hooks, services, registry, dialogs, notifications"),
        ("web", "static/src/views", "View/controller/renderer/model patterns"),
        ("web", "static/src/webclient", "Action/webclient service patterns"),
        ("web", "views", "Backend asset bundle declaration patterns"),
    ],
}

_AREA_ASSET_BUNDLES: dict[str, list[str]] = {
    "pos": ["point_of_sale._assets_pos"],
    "website": ["web.assets_frontend", "website.assets_editor", "web.assets_frontend_lazy"],
    "payment": ["web.assets_frontend", "website.assets_editor", "payment.assets_frontend", "web.assets_backend"],
    "web": ["web.assets_backend", "web.assets_web", "web.assets_qweb"],
}

_COMMON_RULES = [
    "Do not write JS/XML from memory. Read custom source first, then real Odoo source, then implement.",
    "Do not guess import aliases, registry category names, services, component props, template names, or XPath targets.",
    "Every modern JS/OWL file must use the syntax pattern found in the installed Odoo source for this version.",
    "Never mix legacy odoo.define/require code with ES-module /** @odoo-module **/ imports in one file.",
    "Manifest asset paths must exist on disk and must be placed in the correct bundle for the target area.",
    "QWeb/OWL templates must keep Component.template / static template and t-name values aligned.",
    "For t-inherit or xpath, read the inherited source template/view first and target only confirmed nodes/classes/names.",
]

_AREA_RULES: dict[str, list[str]] = {
    "pos": [
        "Read point_of_sale/static/src/app before any POS patch; POS service/store names differ by Odoo version.",
        "Use point_of_sale._assets_pos for POS JS/XML/SCSS unless the real manifest proves otherwise.",
        "Never define or override _load_pos_data_fields on pos.config; use the installed version's supported POS loader/session mechanism.",
        "Inspect existing POS screen/component extension style before patching ProductScreen, PaymentScreen, ReceiptScreen, or store logic.",
        "After deploy, verify exact POS URL/menu and capture console/network/Odoo dialog errors.",
    ],
    "website": [
        "Decide from source whether the feature is website frontend, website editor options, public widget, or OWL.",
        "Website editor option code belongs in website editor assets; public page behavior belongs in frontend assets.",
        "For snippets/forms, read website and website_form templates before writing t-inherit/xpath.",
        "After deploy, verify both normal public page behavior and editor save/edit behavior when the task touches builder/editor code.",
    ],
    "payment": [
        "Read payment and website_payment patterns before touching provider, checkout, tokenization, redirect, or iframe JS.",
        "Do not invent provider routes or JS contracts; inspect the exact Odoo payment form/provider implementation first.",
        "Verify portal/checkout paths after deploy with browser console/network capture.",
    ],
    "web": [
        "Read web/static/src/core and the specific web/static/src/views or webclient area before writing backend OWL.",
        "Confirm registry category, service names, hook usage, template naming, and patch style from installed source.",
        "For action/dashboard components, confirm action registry and asset bundle from real Odoo examples first.",
    ],
}


class FrontendPatternService:
    """Return mandatory preflight read plan for Odoo frontend work."""

    def preflight(self, request: FrontendPatternPreflightRequest) -> FrontendPatternPreflightResult:
        cached = self._load_cache(request)
        if cached is not None:
            return cached

        area = self._detect_area(request)
        is_frontend_task = area != "none"
        instructions: list[FrontendReadInstruction] = []
        explorer_calls: list[dict[str, str]] = []

        # Custom/user code is always first priority for an implementation task.
        for path in request.custom_file_paths:
            if path:
                instructions.append(FrontendReadInstruction(
                    source="custom",
                    module="(custom)",
                    subdir="",
                    reason="Custom source already related to the task — read before matching core patterns",
                    path=path,
                    mandatory=True,
                ))

        if is_frontend_task:
            plan = list(_AREA_PLANS.get(area, []))
            # If the caller gives a target module, browse its static/views too, before generic core examples.
            if request.target_module_name:
                plan.insert(0, (request.target_module_name, "static/src", "Target module frontend source and local conventions"))
                plan.insert(1, (request.target_module_name, "views", "Target module asset/template/view declaration conventions"))

            finder = CoreFinderService()
            for module, subdir, reason in plan:
                explorer_calls.append({"tool": "workflow.list_module_files", "module_name": module, "subdir": subdir})
                try:
                    result = finder.list_module_files(module, subdir, request.search_roots or None, client_name=request.client_name)
                except Exception:  # noqa: BLE001 - preflight must never crash the agent session
                    result = None

                selected = self._select_relevant_paths(result.files if result and result.found else [], area)
                if selected:
                    for path in selected:
                        instructions.append(FrontendReadInstruction(
                            source="odoo_source",
                            module=module,
                            subdir=subdir,
                            reason=reason,
                            path=path,
                            mandatory=True,
                        ))
                else:
                    instructions.append(FrontendReadInstruction(
                        source="odoo_source",
                        module=module,
                        subdir=subdir,
                        reason=f"{reason}; browse this module/subdir manually because no local candidate was found by preflight",
                        path=f"NOT_FOUND: {module}/{subdir}",
                        mandatory=False,
                    ))

        validation_steps = [
            "Run git.diff and inspect every JS/XML/manifest asset change before committing.",
            "Run qa.self_run for XML/Python/manifest checks.",
            "Run workflow.verify_against_core with all changed .py, .xml, .js, .scss, .css, .csv, and __manifest__.py files.",
            "If verify_against_core returns push_blocked=True, fix all CRITICAL findings and re-run before git.push.",
            "After upgrade, verify exact affected Odoo web/POS/website paths in browser with console/network capture.",
        ]

        result = FrontendPatternPreflightResult(
            target_area=area,
            is_frontend_task=is_frontend_task,
            push_blocked_until_done=is_frontend_task,
            mandatory_read_count=sum(1 for item in instructions if item.mandatory),
            instructions=instructions,
            explorer_calls=explorer_calls,
            required_asset_bundles=_AREA_ASSET_BUNDLES.get(area, []),
            strict_rules=_COMMON_RULES + _AREA_RULES.get(area, []),
            validation_steps=validation_steps,
            message=(
                "FRONTEND PREFLIGHT REQUIRED: execute mandatory reads before writing code. Result is cached for repeated similar tasks."
                if is_frontend_task else
                "No Odoo frontend/OWL/POS/website keywords detected. Use normal scope_advice + verify_against_core."
            ),
        )
        self._save_cache(request, result)
        return result

    def _cache_key(self, request: FrontendPatternPreflightRequest) -> str:
        payload = json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "fingerprint": self._source_fingerprint(request),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _source_fingerprint(request: FrontendPatternPreflightRequest) -> dict[str, object]:
        paths = list(request.custom_file_paths or []) + list(request.planned_file_paths or [])
        roots = request.search_roots or [
            "/home/odoo/src/odoo",
            "/home/odoo/src/enterprise",
            "/home/odoo/src/user",
        ]
        stats: dict[str, int | None] = {}
        for raw in paths + roots:
            try:
                path = Path(raw)
                stats[str(path)] = path.stat().st_mtime_ns
            except OSError:
                stats[str(raw)] = None
        return {
            "paths": stats,
            "core_sync_host": settings.odoosh_ssh_host or "",
        }

    def _cache_path(self, request: FrontendPatternPreflightRequest) -> Path:
        return settings.cache_path / "frontend_patterns" / f"{self._cache_key(request)}.json"

    def _load_cache(self, request: FrontendPatternPreflightRequest) -> FrontendPatternPreflightResult | None:
        if not settings.pattern_cache_enabled:
            return None
        path = self._cache_path(request)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            saved_at = float(raw.get("_saved_at", 0))
            if time.time() - saved_at > max(1, settings.pattern_cache_seconds):
                return None
            result = FrontendPatternPreflightResult.model_validate(raw.get("result") or {})
            result.message += " (served from pattern cache)"
            return result
        except Exception:
            return None

    def _save_cache(self, request: FrontendPatternPreflightRequest, result: FrontendPatternPreflightResult) -> None:
        if not settings.pattern_cache_enabled:
            return
        try:
            path = self._cache_path(request)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"_saved_at": time.time(), "result": result.model_dump(mode="json")}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _detect_area(request: FrontendPatternPreflightRequest) -> str:
        explicit = (request.target_area or "auto").lower()
        if explicit in {"pos", "website", "web", "payment"}:
            return explicit

        haystack_parts = [request.task_description or "", request.target_module_name or ""]
        haystack_parts.extend(request.custom_file_paths or [])
        haystack_parts.extend(request.planned_file_paths or [])
        haystack = " ".join(haystack_parts).lower().replace("-", "_")

        def has_term(term: str) -> bool:
            normalized = term.lower().replace("-", "_")
            # Short generic tokens such as "pos" must be matched as standalone
            # words. Substring matching classified words like "product" as POS.
            if len(normalized) <= 3 and normalized.replace("_", "").isalnum():
                return re.search(rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])", haystack) is not None
            return normalized in haystack

        # Strong module/path signals win over generic task words. A module named
        # website_* must not become POS merely because its text contains "product".
        strong_website = any(token in haystack for token in (
            "website_", "/website/", "/website_sale/", "web.assets_frontend",
            "publicwidget", "snippet",
        ))
        strong_pos = any(token in haystack for token in (
            "point_of_sale", "/pos/", "_assets_pos", "pos_store",
            "paymentscreen", "productscreen", "receiptscreen",
        ))
        if strong_pos:
            return "pos"
        if strong_website:
            return "website"

        # POS still precedes generic payment for real POS payment-screen tasks.
        for area in ("pos", "payment", "website", "web"):
            if any(has_term(term) for term in _AREA_KEYWORDS[area]):
                return area
        if any(ext in haystack for ext in (".js", ".scss", "static/src", "owl", "t-inherit", "t-name")):
            return "web"
        return "none"

    @staticmethod
    def _select_relevant_paths(files: Iterable[object], area: str, limit: int = 8) -> list[str]:
        """Choose a compact set of source examples that are worth reading."""
        scored: list[tuple[int, str]] = []
        for item in files:
            path = getattr(item, "path", "") or ""
            rel = (getattr(item, "relative_path", "") or path).lower()
            ext = Path(rel).suffix.lower()
            if ext not in {".js", ".xml", ".scss", ".py"} and "__manifest__.py" not in rel:
                continue
            score = 50
            if "__manifest__.py" in rel or "/views/" in rel or rel.startswith("views/"):
                score += 15
            if ext in {".js", ".xml"}:
                score += 15
            if area == "pos" and any(k in rel for k in ("screen", "store", "pos_store", "payment", "product", "receipt", "app/")):
                score += 25
            if area == "website" and any(k in rel for k in ("snippet", "options", "form", "editor", "website")):
                score += 25
            if area == "payment" and any(k in rel for k in ("payment", "checkout", "provider", "transaction", "form")):
                score += 25
            if area == "web" and any(k in rel for k in ("registry", "service", "dialog", "view", "controller", "renderer", "webclient")):
                score += 25
            if "test" in rel or "tour" in rel:
                score -= 20
            scored.append((score, path))
        scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        result: list[str] = []
        seen: set[str] = set()
        for _, path in scored:
            if path and path not in seen:
                seen.add(path)
                result.append(path)
            if len(result) >= limit:
                break
        return result
