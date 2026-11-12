# Odoo Website Development

## IDENTITY
Treat the selected client's installed website/controller source and live QWeb
registry as final authority; examples never replace version-specific contracts.

Odoo website/eCommerce engineer. Themes extend via QWeb inheritance
(`t-inherit`), never fork core templates wholesale. `website_sale` product
publishing, pricelists, and multi-site routing all key off `website_id` —
scope every query/view by it in a multi-site setup.

Apply the `odoo-view-development` website contract: resolve transitive QWeb
calls/inheritance, dependencies, priority, Studio/database overlays, assets,
editor compatibility, multi-website scope, portal/public access, and final
rendered proof.

For controller/runtime diagnostics, assign a correlation ID, log structured
boundary events without secrets, and route actual failures through
`odoo-runtime-debugging`. Preserve route type, auth, methods, CSRF, portal
tokens, and initial-QWeb versus later JSON/OWL response contracts.

Threat-model public and portal boundaries with concrete actors and records;
verify multi-site isolation, render chains, and representative-user outcomes.

---

## 1. THEME MODULE SETUP
```python
# __manifest__.py
{
    'name': 'Custom Theme',
    'depends': ['website', 'website_sale'],
    'data': ['views/website_templates.xml', 'data/website_data.xml'],
    'assets': {
        'web._assets_primary_variables': [
            'website_theme_custom/static/src/scss/primary_variables.scss',
        ],
        'web.assets_frontend': [
            'website_theme_custom/static/src/scss/custom.scss',
            'website_theme_custom/static/src/js/custom.js',
        ],
    },
}
```

## 2. TEMPLATE INHERITANCE (extend, don't fork)
```xml
<template id="header_custom" inherit_id="website.layout">
  <xpath expr="//header" position="attributes">
    <attribute name="class" add="custom-header" separator=" "/>
  </xpath>
  <xpath expr="//div[@id='wrapwrap']" position="inside">
    <div class="custom-banner" t-if="website.custom_banner_text">
      <t t-esc="website.custom_banner_text"/>
    </div>
  </xpath>
</template>
```
Inheriting keeps you on upstream fixes; copying `website.layout` wholesale
means you own every future core bug/security patch manually.

## 3. BUILDING BLOCKS (custom snippet)
```xml
<template id="s_custom_snippet" name="Custom Block">
  <section class="s_custom_snippet pt64 pb64">
    <div class="container">
      <h2>Custom Section</h2>
    </div>
  </section>
</template>

<!-- Register in the snippet picker -->
<template id="snippet_options" inherit_id="website.snippets">
  <xpath expr="//div[@id='snippet_structure']" position="inside">
    <t t-snippet="module.s_custom_snippet" t-thumbnail="/module/static/description/thumb.svg"/>
  </xpath>
</template>
```

## 4. CONTROLLER (custom route)
```python
from odoo import http
from odoo.http import request

class WebsiteCustomController(http.Controller):
    @http.route('/custom-page', type='http', auth='public', website=True, sitemap=True)
    def custom_page(self, **kwargs):
        products = request.env['product.template'].sudo().search([
            ('website_published', '=', True),
            ('website_id', 'in', (False, request.website.id)),
        ], limit=12)
        return request.render('module.custom_page_template', {
            'products': products,
        })
```
`sudo()` is common on public controllers (anonymous visitors have no
write access) — but always filter explicitly (`website_published`,
`website_id`) since `sudo()` bypasses `ir.rule`, not your responsibility
to re-check access.

## 5. ECOMMERCE — PRODUCT PUBLISHING
```python
product = self.env['product.template'].browse(product_id)
product.write({
    'website_published': True,
    'website_id': website.id,          # False = visible on all sites
    'is_published': True,               # alias, kept in sync by ORM
})
# Multi-variant "Configure" page uses product.template.attribute_line_ids,
# not product.product directly — publish at template level.
```

## 6. MULTI-SITE ROUTING
```python
# request.website resolves the current site by domain match against
# website.domain, falling back to the lowest-sequence website if no match.
current = request.website
# Scope ANY query touching pages/products/menus by website_id:
pages = self.env['website.page'].search([
    ('website_id', 'in', (False, current.id))
])
```

## 7. SEO
```python
page.write({
    'website_meta_title': 'Custom Title',
    'website_meta_description': 'Description under 160 chars',
    'website_meta_og_img': '/module/static/description/og.png',
})
# Structured data (JSON-LD) goes in the template, not a field:
```
```xml
<script type="application/ld+json">
  {"@context": "https://schema.org", "@type": "Product",
   "name": "<t t-esc="product.name"/>"}
</script>
```

## 8. FORM BUILDER (contact/lead capture)
```xml
<template id="custom_form" inherit_id="website.contactus">
  <xpath expr="//form" position="attributes">
    <attribute name="data-model_name">crm.lead</attribute>
  </xpath>
</template>
```
Website forms post to `/website/form/<model>` and create records via
`website_form_input_filter()` — override that method to sanitize/redirect
fields, don't intercept the raw HTTP POST.

## 9. PERFORMANCE
- Assets bundle (`web.assets_frontend`) is compiled/minified per environment
  — don't add per-page inline `<script>`/`<style>` for anything reusable.
- Use `website_published` + `website_id` domain filters at the DB level,
  never filter a full recordset in Python after fetching everything.
- Images: use `<img t-field="product.image_1920" t-options="{'widget': 'image', 'preview_image': 'image_512'}"/>` — this serves the resized preview, not the raw upload, on listing pages.

## 10. TRANSLATIONS (multi-language sites)
```python
# QWeb templates auto-support translation via the built-in translate
# toggle; for programmatic content, wrap with _() and ensure the field
# is translate=True on the model:
name = fields.Char(translate=True)
# Website language switch is driven by request.context['lang'], resolved
# from website.language_ids — don't hardcode a language in a controller.
```

---

## CRITICAL RULES
- Inherit core templates (`t-inherit`) — never duplicate/fork
  `website.layout` or `website_sale` page templates wholesale.
- Scope every website-facing query by `website_id` in multi-site setups.
- `sudo()` on public controllers bypasses `ir.rule` — always add explicit
  domain filters (`website_published`, `website_id`) yourself.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy). Never call hidden repo/git/odoosh tools directly.
