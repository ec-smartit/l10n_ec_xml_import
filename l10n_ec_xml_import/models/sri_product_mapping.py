from odoo import api, fields, models


class SriProductCodeMapping(models.Model):
    _name = "sri.product.code.mapping"
    _description = "Homologación (código XML SRI -> producto Odoo)"
    _order = "product_id, partner_id"
    _rec_name = "supplier_code"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one("res.partner", string="Proveedor", required=True, index=True)
    supplier_code = fields.Char(string="Código Principal (XML)", required=True, index=True)
    supplier_description = fields.Char(string="Descripción del proveedor")
    product_id = fields.Many2one(
        "product.product", string="Producto en Odoo",
        help="Producto al que se debe cargar automáticamente cuando este proveedor use este código.")

    _sql_constraints = [
        ("partner_code_company_uniq", "unique(partner_id, supplier_code, company_id)",
         "Ya existe una homologación para este proveedor y este código."),
    ]

    @api.model
    def _get_or_create_pending(self, partner, code, description, company):
        """Usado durante la importación: si no existe la fila para este
        proveedor+código, la crea sin producto asignado, para que aparezca en
        la pantalla de Homologación."""
        if not partner or not code:
            return self.browse()
        existing = self.search([
            ("partner_id", "=", partner.commercial_partner_id.id),
            ("supplier_code", "=", code),
            ("company_id", "=", company.id),
        ], limit=1)
        if existing:
            return existing
        return self.create({
            "company_id": company.id,
            "partner_id": partner.commercial_partner_id.id,
            "supplier_code": code,
            "supplier_description": description,
        })

    @api.model
    def _remember(self, partner, code, description, product, company):
        """Se llama al guardar una línea de factura de compra donde el usuario asignó (o
        cambió) el producto de una línea que vino de un XML del SRI. Guarda/actualiza la
        homologación de forma silenciosa — si ya existía una distinta para este proveedor
        y código, la sobrescribe con el producto que se acaba de elegir."""
        if not partner or not code or not product:
            return self.browse()
        partner = partner.commercial_partner_id
        existing = self.search([
            ("partner_id", "=", partner.id),
            ("supplier_code", "=", code),
            ("company_id", "=", company.id),
        ], limit=1)
        if existing:
            existing.write({
                "product_id": product.id,
                "supplier_description": description or existing.supplier_description,
            })
            return existing
        return self.create({
            "company_id": company.id,
            "partner_id": partner.id,
            "supplier_code": code,
            "supplier_description": description,
            "product_id": product.id,
        })
