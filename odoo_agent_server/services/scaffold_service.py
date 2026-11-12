"""Deterministic Odoo code generators.

The model sends a tiny spec; this module writes the boilerplate (model class, form/list views, action,
menu, access rules, view inheritance) so the agent writes ~10 lines instead of 100+. Output is a list of
workspace edit dicts, so everything still flows through ``ws_edit`` (lint, auto-wiring, journal).

Version profile (checked against the Odoo release notes I know; override with ``series``):
  * <= 16: ``attrs=`` is still valid, list views are ``<tree>``.
  * 17:    ``attrs``/``states`` removed in favour of direct ``invisible=``/``readonly=`` expressions; still ``<tree>``.
  * >= 18: list views are ``<list>`` and ``view_mode`` uses ``list``.
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

FIELD_TYPES = {"Char", "Text", "Html", "Integer", "Float", "Boolean", "Date", "Datetime", "Selection", "Many2one", "One2many", "Many2many", "Monetary", "Binary"}
_MODEL_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SpecError(ValueError):
    pass


def snake(model: str) -> str:
    return model.replace(".", "_")


def camel(model: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[._]", model))


def title(model: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[._]", model.split(".", 1)[-1] if "." in model else model))


def list_tag(series: int | None) -> str:
    return "list" if (series or 0) >= 18 else "tree"


def view_mode(series: int | None) -> str:
    return "list,form" if (series or 0) >= 18 else "tree,form"


def validate_model(model: str) -> None:
    if not _MODEL_RE.match(model or ""):
        raise SpecError(f"model '{model}' must look like 'my.model' (lowercase, dot separated)")


def validate_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for raw in fields:
        field = dict(raw)
        name, kind = field.get("name", ""), field.get("type", "Char")
        if not _FIELD_RE.match(name):
            raise SpecError(f"field name '{name}' must be lowercase snake_case")
        if name in seen:
            raise SpecError(f"duplicate field '{name}'")
        seen.add(name)
        if kind not in FIELD_TYPES:
            raise SpecError(f"field '{name}': unknown type '{kind}' (use one of {', '.join(sorted(FIELD_TYPES))})")
        if kind in {"Many2one", "Many2many"} and not field.get("comodel"):
            raise SpecError(f"field '{name}': {kind} needs comodel")
        if kind == "One2many" and not (field.get("comodel") and field.get("inverse")):
            raise SpecError(f"field '{name}': One2many needs comodel and inverse (the Many2one name on the other model)")
        if kind == "Selection" and not field.get("options"):
            raise SpecError(f"field '{name}': Selection needs options like ['draft:Draft','done:Done']")
        field["type"] = kind
        out.append(field)
    return out


def _py(value: str) -> str:
    return repr(value)


def field_line(field: dict[str, Any]) -> str:
    kind, args, kwargs = field["type"], [], []
    if kind in {"Many2one", "Many2many"}:
        args.append(_py(field["comodel"]))
    elif kind == "One2many":
        args += [_py(field["comodel"]), _py(field["inverse"])]
    elif kind == "Selection":
        pairs = []
        for opt in field["options"]:
            key, _, label = str(opt).partition(":")
            pairs.append(f"({_py(key.strip())}, {_py((label or key.replace('_', ' ').title()).strip())})")
        args.append("[" + ", ".join(pairs) + "]")
    if kind == "Monetary":
        kwargs.append(f"currency_field={_py(field.get('currency_field', 'currency_id'))}")
    if field.get("string"):
        kwargs.append(f"string={_py(field['string'])}")
    if field.get("required"):
        kwargs.append("required=True")
    if kind == "Selection" and field.get("default"):
        kwargs.append(f"default={_py(field['default'])}")
    return f"    {field['name']} = fields.{kind}({', '.join(args + kwargs)})"


def model_source(model: str, fields: list[dict[str, Any]], *, mail: bool, extend: bool, description: str | None = None) -> str:
    lines = ["from odoo import fields, models", "", "", f"class {camel(model)}{'Ext' if extend else ''}(models.Model):"]
    if extend:
        lines.append(f"    _inherit = {_py(model)}")
    else:
        lines.append(f"    _name = {_py(model)}")
        lines.append(f"    _description = {_py(description or title(model))}")
        if mail:
            lines.append("    _inherit = ['mail.thread', 'mail.activity.mixin']")
    lines.append("")
    lines.extend(field_line(f) for f in fields)
    return "\n".join(lines) + "\n"


def _field_xml(fields: list[dict[str, Any]], indent: str) -> str:
    return "\n".join(f'{indent}<field name="{f["name"]}"/>' for f in fields)


def views_source(model: str, fields: list[dict[str, Any]], *, module: str, series: int | None, mail: bool, menu: str | None, menu_parent: str | None) -> str:
    s = snake(model)
    tag = list_tag(series)
    scalar = [f for f in fields if f["type"] not in {"One2many", "Html", "Binary"}]
    chatter = (
        '\n            <div class="oe_chatter">\n                <field name="message_follower_ids"/>\n'
        '                <field name="activity_ids"/>\n                <field name="message_ids"/>\n            </div>'
        if mail else ""
    )
    parent_attr = f" parent={quoteattr(menu_parent)}" if menu_parent else ""
    menu_name = menu or title(model)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_{s}_form" model="ir.ui.view">
        <field name="name">{model}.form</field>
        <field name="model">{model}</field>
        <field name="arch" type="xml">
            <form>
                <sheet>
                    <group>
{_field_xml(fields, " " * 24)}
                    </group>
                </sheet>{chatter}
            </form>
        </field>
    </record>

    <record id="view_{s}_{tag}" model="ir.ui.view">
        <field name="name">{model}.{tag}</field>
        <field name="model">{model}</field>
        <field name="arch" type="xml">
            <{tag}>
{_field_xml(scalar[:8], " " * 16)}
            </{tag}>
        </field>
    </record>

    <record id="action_{s}" model="ir.actions.act_window">
        <field name="name">{escape(menu_name)}</field>
        <field name="res_model">{model}</field>
        <field name="view_mode">{view_mode(series)}</field>
    </record>

    <menuitem id="menu_{s}" name={quoteattr(menu_name)} action="action_{s}"{parent_attr} sequence="10"/>
</odoo>
'''


def inherit_view_source(model: str, *, module: str, view_ref: str, anchor: str, position: str, fields: list[dict[str, Any]], view_type: str, series: int | None) -> str:
    s = snake(model)
    vt = "list" if view_type in {"list", "tree"} and (series or 0) >= 18 else ("tree" if view_type in {"list", "tree"} else view_type)
    new_fields = _field_xml(fields, " " * 16)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_{s}_{vt}_inherit_{module}" model="ir.ui.view">
        <field name="name">{model}.{vt}.inherit.{module}</field>
        <field name="model">{model}</field>
        <field name="inherit_id" ref={quoteattr(view_ref)}/>
        <field name="arch" type="xml">
            <xpath expr="//field[@name='{anchor}']" position="{position}">
{new_fields}
            </xpath>
        </field>
    </record>
</odoo>
'''


ACCESS_HEADER = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"


def access_row(model: str, group: str = "base.group_user") -> str:
    s = snake(model)
    return f"access_{s}_user,{model} user,model_{s},{group},1,1,1,1\n"


def scaffold_edits(*, module: str, model: str, fields: list[dict[str, Any]], series: int | None, mail: bool, menu: str | None, menu_parent: str | None, with_views: bool, access_exists: bool, access_group: str, depends: list[str], description: str | None) -> list[dict[str, Any]]:
    validate_model(model)
    fields = validate_fields(fields)
    if not any(f["name"] == "name" for f in fields):
        fields = [{"name": "name", "type": "Char", "required": True}] + fields
    s = snake(model)
    edits: list[dict[str, Any]] = [{"path": f"models/{s}.py", "op": "write", "content": model_source(model, fields, mail=mail, extend=False, description=description)}]
    if with_views:
        edits.append({"path": f"views/{s}_views.xml", "op": "write", "content": views_source(model, fields, module=module, series=series, mail=mail, menu=menu, menu_parent=menu_parent)})
    if access_exists:
        edits.append({"path": "security/ir.model.access.csv", "op": "append", "content": access_row(model, access_group)})
    else:
        edits.append({"path": "security/ir.model.access.csv", "op": "write", "content": ACCESS_HEADER + access_row(model, access_group)})
    if mail and "mail" not in depends:
        edits.append({"path": "__manifest__.py", "op": "manifest_add", "key": "depends", "entry": "mail", "auto": True})
    return edits


def extend_edits(*, module: str, model: str, fields: list[dict[str, Any]], series: int | None, view_ref: str | None, anchor: str | None, position: str, view_type: str, file_name: str | None) -> list[dict[str, Any]]:
    validate_model(model)
    fields = validate_fields(fields)
    if not fields:
        raise SpecError("extend needs at least one field")
    stem = re.sub(r"[^a-z0-9_]", "_", (file_name or f"{snake(model)}_ext").lower())
    edits = [{"path": f"models/{stem}.py", "op": "write", "content": model_source(model, fields, mail=False, extend=True)}]
    if view_ref:
        if not anchor:
            raise SpecError("view_ref needs after_field (the existing field to place the new fields next to)")
        if position not in {"after", "before", "inside"}:
            raise SpecError("position must be after, before or inside")
        edits.append({"path": f"views/{stem}_views.xml", "op": "write", "content": inherit_view_source(model, module=module, view_ref=view_ref, anchor=anchor, position=position, fields=fields, view_type=view_type, series=series)})
    return edits
