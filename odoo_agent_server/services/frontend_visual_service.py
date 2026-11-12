from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from ..schemas import FrontendVisualImplementRequest, OdooCodeFileChange


@dataclass(frozen=True)
class VisualVariant:
    name: str
    role: str
    palette: str
    file_name: str


class FrontendVisualService:
    """Deterministic frontend visual asset generator for Odoo modules.

    This service intentionally focuses on repeatable implementation assets
    (SVG/CSS/SCSS helpers) rather than free-form design guessing.  The caller can
    use workflow.frontend_visual_implement to create premium borders, pipes,
    backgrounds, logos, icons, or decorative dashboard/POS elements and then run
    the normal Odoo code-change/deploy pipeline.
    """

    _SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

    def build_writes(self, payload: FrontendVisualImplementRequest, module_name: str) -> tuple[list[OdooCodeFileChange], dict[str, Any]]:
        component_type = self._normalise(payload.component_type or "auto")
        design_text = " ".join([
            payload.design_goal or "",
            payload.target_area or "",
            component_type,
            " ".join(payload.allowed_methods or []),
        ]).lower()
        strategy = self._select_strategy(payload, design_text)
        asset_name = self._safe_name(payload.asset_name or self._default_asset_name(component_type, design_text))
        css_file, existing_css = self._choose_css_target(payload, module_name, asset_name, design_text)

        if strategy == "svg_asset":
            variants = self._variants(payload, asset_name, design_text)
            writes: list[OdooCodeFileChange] = []
            for variant in variants:
                svg_rel = self._module_rel(module_name, f"{module_name}/static/src/img/{variant.file_name}")
                writes.append(OdooCodeFileChange(relative_path=svg_rel, content=self._svg_for_variant(variant, payload)))
            generated_css = self._css_for_variants(module_name, payload, variants)
            writes.append(OdooCodeFileChange(relative_path=css_file, content=self._merge_generated_css(existing_css, asset_name, generated_css)))
            plan = {
                "status": "generated",
                "implementation_type": "svg_asset",
                "component_type": component_type,
                "asset_name": asset_name,
                "css_file": css_file,
                "assets": [f"{module_name}/static/src/img/{variant.file_name}" for variant in variants],
                "reason": "SVG asset selected for exact visual shapes, logos, backgrounds, decorative borders, and pipe-like curves that CSS borders approximate poorly.",
                "manifest_note": "Static SVGs in static/src/img are served by Odoo directly. The generated SCSS still needs to be included in the relevant asset bundle, or its content can be merged into an existing bundled SCSS file.",
            }
            return writes, plan

        generated_css = self._scss_only(payload, asset_name)
        writes = [OdooCodeFileChange(relative_path=css_file, content=self._merge_generated_css(existing_css, asset_name, generated_css))]
        plan = {
            "status": "generated",
            "implementation_type": "scss",
            "component_type": component_type,
            "asset_name": asset_name,
            "css_file": css_file,
            "reason": "SCSS selected at runtime because the visual is layout-driven or must follow live component dimensions; fixed SVG geometry is not required.",
        }
        return writes, plan


    def _choose_css_target(
        self,
        payload: FrontendVisualImplementRequest,
        module_name: str,
        asset_name: str,
        design_text: str,
    ) -> tuple[str, str]:
        """Resolve one module-owned stylesheet and preserve its current content."""
        requested = payload.css_file_path or f"{module_name}/static/src/css/{asset_name}.scss"
        relative_path = self._module_rel(module_name, requested)
        existing = ""
        if payload.project_path:
            target = (Path(payload.project_path).resolve() / relative_path).resolve()
            project = Path(payload.project_path).resolve()
            try:
                target.relative_to(project)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Visual CSS target escapes project checkout") from exc
            if target.exists():
                if not target.is_file():
                    raise HTTPException(status_code=400, detail="Visual CSS target is not a file")
                existing = target.read_text(encoding="utf-8")
        return relative_path, existing

    def _merge_generated_css(self, existing: str, asset_name: str, generated: str) -> str:
        """Replace this generator's previous block without duplicating rules."""
        start = f"/* BEGIN frontend_visual:{asset_name} */"
        end = f"/* END frontend_visual:{asset_name} */"
        block = f"{start}\n{generated.rstrip()}\n{end}"
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        if pattern.search(existing):
            return pattern.sub(block, existing, count=1).rstrip() + "\n"
        prefix = existing.rstrip()
        return f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"

    def _select_strategy(self, payload: FrontendVisualImplementRequest, design_text: str) -> str:
        """Choose the implementation at runtime from behavior, not keywords alone.

        ``allowed_methods`` is a strict allow-list. Dynamic or expandable visuals
        always prefer SCSS because their geometry must follow the live DOM size.
        SVG is reserved for fixed artwork and is never selected merely because
        the request contains words such as ``pipe`` or ``border``.
        """
        allowed = {self._normalise(item) for item in (payload.allowed_methods or []) if item}
        if allowed:
            supports_scss = bool({"scss", "css"} & allowed)
            supports_svg = bool({"svg", "svg_asset"} & allowed)
        else:
            supports_scss = supports_svg = True

        dynamic_tokens = (
            "dynamic", "expandable", "expandable", "responsive", "auto height",
            "variable height", "variable width", "follow height", "follow width",
            "stretch", "grows", "shrinks", "runtime size", "content height",
        )
        fixed_art_tokens = (
            "fixed artwork", "logo", "illustration", "static icon",
            "fixed decorative shape", "exact vector", "non-scaling artwork",
        )
        is_dynamic = any(token in design_text for token in dynamic_tokens)
        is_fixed_art = any(token in design_text for token in fixed_art_tokens)

        if is_dynamic:
            if not supports_scss:
                raise HTTPException(
                    status_code=400,
                    detail="Dynamic/expandable visual requires SCSS, but SCSS is not allowed by allowed_methods.",
                )
            return "scss"
        if is_fixed_art and supports_svg:
            return "svg_asset"
        if supports_scss:
            return "scss"
        if supports_svg:
            return "svg_asset"
        raise HTTPException(status_code=400, detail="No supported frontend visual implementation method is allowed.")

    def _default_asset_name(self, component_type: str, design_text: str) -> str:
        if "pipe" in design_text or "elbow" in design_text or "trolley" in design_text:
            return "trolley_pipe"
        if component_type and component_type != "auto":
            return component_type
        return "visual_component"

    def _safe_name(self, value: str) -> str:
        value = self._SAFE_NAME_RE.sub("_", str(value or "").strip()).strip("_").lower()
        if not value:
            value = "visual_component"
        return value[:80]

    def _normalise(self, value: str) -> str:
        return self._safe_name(value).replace("-", "_")

    def _module_rel(self, module_name: str, relative_path: str) -> str:
        value = str(relative_path or "").replace("\\", "/").lstrip("/")
        if "\x00" in value or ".." in value.split("/"):
            raise HTTPException(status_code=400, detail="Unsafe visual asset path")
        if not value.startswith(f"{module_name}/"):
            value = f"{module_name}/{value}"
        return value

    def _variants(self, payload: FrontendVisualImplementRequest, asset_name: str, design_text: str) -> list[VisualVariant]:
        if payload.variants:
            variants: list[VisualVariant] = []
            for raw in payload.variants[:12]:
                name = self._safe_name(str(raw.get("name") or raw.get("role") or "variant"))
                role = self._normalise(str(raw.get("role") or name))
                palette = self._normalise(str(raw.get("palette") or raw.get("color") or role or "gold"))
                file_name = self._safe_name(str(raw.get("file_name") or f"{asset_name}_{name}.svg"))
                if not file_name.endswith(".svg"):
                    file_name = f"{file_name}.svg"
                variants.append(VisualVariant(name=name, role=role, palette=palette, file_name=file_name))
            return variants or [VisualVariant("default", "default", "gold", f"{asset_name}.svg")]

        if "pipe" in design_text or "elbow" in design_text or "trolley" in design_text:
            return [
                VisualVariant("active", "active", "gold", f"{asset_name}_active.svg"),
                VisualVariant("purple", "packed_purple", "purple", f"{asset_name}_purple.svg"),
                VisualVariant("silver", "packed_silver", "silver", f"{asset_name}_silver.svg"),
            ]
        return [VisualVariant("default", "default", "gold", f"{asset_name}.svg")]

    def _palette(self, palette: str) -> dict[str, str]:
        palettes = {
            "gold": {"dark": "#8a5a1f", "mid": "#c79239", "light": "#fff2c6", "accent": "#f1d391", "shadow": "#4c2a08"},
            "purple": {"dark": "#6f3d6f", "mid": "#b16aa5", "light": "#f6d8ef", "accent": "#dca6d2", "shadow": "#3f2440"},
            "silver": {"dark": "#717178", "mid": "#bfc0c5", "light": "#ffffff", "accent": "#e2e3e7", "shadow": "#45464c"},
            "plum": {"dark": "#4d1d3b", "mid": "#7b315f", "light": "#f4d8ea", "accent": "#b46a9e", "shadow": "#2b1422"},
        }
        return palettes.get(palette, palettes["gold"])

    def _svg_for_variant(self, variant: VisualVariant, payload: FrontendVisualImplementRequest) -> str:
        pal = self._palette(variant.palette)
        title = (payload.design_goal or variant.name).replace("&", "and").replace("<", "").replace(">", "")[:120]
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 72 360" role="img" aria-label="{title}">
  <defs>
    <linearGradient id="pipe" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0" stop-color="{pal['dark']}"/>
      <stop offset="0.18" stop-color="{pal['mid']}"/>
      <stop offset="0.42" stop-color="{pal['light']}"/>
      <stop offset="0.58" stop-color="{pal['mid']}"/>
      <stop offset="0.82" stop-color="{pal['accent']}"/>
      <stop offset="1" stop-color="{pal['dark']}"/>
    </linearGradient>
    <filter id="softShadow" x="-80%" y="-10%" width="260%" height="130%">
      <feDropShadow dx="0" dy="5" stdDeviation="4" flood-color="{pal['shadow']}" flood-opacity="0.24"/>
    </filter>
  </defs>
  <path d="M58 24 H34 C22 24 16 30 16 42 V318 C16 330 22 336 34 336 H58"
        fill="none" stroke="url(#pipe)" stroke-width="18" stroke-linecap="round" stroke-linejoin="round" filter="url(#softShadow)"/>
  <path d="M58 24 H34 C22 24 16 30 16 42 V318 C16 330 22 336 34 336 H58"
        fill="none" stroke="#ffffff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" opacity="0.36" transform="translate(-4 0)"/>
  <rect x="7" y="118" width="34" height="74" rx="17" fill="url(#pipe)" filter="url(#softShadow)"/>
  <rect x="12" y="124" width="9" height="62" rx="4.5" fill="#ffffff" opacity="0.30"/>
</svg>
'''

    def _css_for_variants(self, module_name: str, payload: FrontendVisualImplementRequest, variants: list[VisualVariant]) -> str:
        selector = str(payload.css_selector or "").strip()
        component_type = self._normalise(payload.component_type or "auto")
        base_comment = (
            "/* Generated by workflow.frontend_visual_implement.\n"
            "   Safe SVG-backed visual helpers for Odoo frontend/POS/OWL UI.\n"
            "   Include this SCSS file in the correct Odoo asset bundle, or copy the rules into an existing bundled SCSS file. */\n"
        )
        url = lambda variant: f"/{module_name}/static/src/img/{variant.file_name}"
        if selector:
            lines = [base_comment]
            for idx, variant in enumerate(variants):
                suffix = "" if idx == 0 else f".is-{variant.name}"
                lines.append(f"{selector}{suffix} {{\n    position: relative;\n    overflow: visible;\n}}\n")
                lines.append(f"{selector}{suffix}::before {{\n    content: \"\";\n    position: absolute;\n    left: -1.15rem;\n    top: .25rem;\n    bottom: .25rem;\n    width: 2.2rem;\n    background-image: url(\"{url(variant)}\");\n    background-repeat: no-repeat;\n    background-size: 100% 100%;\n    background-position: left center;\n    pointer-events: none;\n    z-index: 2;\n}}\n")
            return "\n".join(lines)

        if component_type in {"border", "auto"} and any("pipe" in variant.file_name for variant in variants):
            by_role = {variant.role: variant for variant in variants}
            active = by_role.get("active", variants[0])
            purple = by_role.get("packed_purple", variants[min(1, len(variants)-1)])
            silver = by_role.get("packed_silver", variants[min(2, len(variants)-1)])
            return base_comment + f'''
.pos-theme-custom .pos-theme-trolley-active,
.pos-theme-custom .pos-theme-trolley-packed {{
    position: relative;
    overflow: visible !important;
}}

.pos-theme-custom .pos-theme-trolley-active::before {{
    content: "";
    position: absolute;
    left: -1.18rem;
    top: .16rem;
    bottom: .16rem;
    width: 2.28rem;
    background-image: url("{url(active)}");
    background-repeat: no-repeat;
    background-size: 100% 100%;
    background-position: left center;
    pointer-events: none;
    z-index: 2;
}}

.pos-theme-custom .pos-theme-trolley-packed::before {{
    content: "";
    position: absolute;
    left: -.92rem;
    top: .38rem;
    bottom: .38rem;
    width: 1.7rem;
    background-image: url("{url(purple)}");
    background-repeat: no-repeat;
    background-size: 100% 100%;
    background-position: left center;
    pointer-events: none;
    z-index: 2;
}}

.pos-theme-custom .pos-theme-trolley-packed:nth-child(3n + 3)::before {{
    background-image: url("{url(silver)}");
}}
'''
        variant = variants[0]
        return base_comment + f'''
.o_frontend_visual_{self._safe_name(payload.asset_name or 'component')} {{
    background-image: url("{url(variant)}");
    background-repeat: no-repeat;
    background-size: contain;
    background-position: center;
}}
'''

    def _scss_only(self, payload: FrontendVisualImplementRequest, asset_name: str) -> str:
        selector = str(payload.css_selector or f".o_frontend_visual_{asset_name}").strip()
        return f'''/* Generated by workflow.frontend_visual_implement. */
{selector} {{
    position: relative;
    border-radius: 18px;
    border: 1px solid rgba(199, 150, 79, .28);
    background: linear-gradient(180deg, rgba(255,253,248,.98), rgba(251,247,240,.95));
    box-shadow: 0 14px 34px rgba(77, 29, 59, .10);
}}
'''
