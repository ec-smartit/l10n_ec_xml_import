import base64

from bs4 import BeautifulSoup

from odoo import Command, api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    has_retention = fields.Boolean(
        string="¿Tiene Retención?",
        compute="_compute_has_retention",
        help="Check if the invoice has a retention",
    )

    sri_received_document_ids = fields.One2many(
        "sri.received.document", "move_id",
        string="Comprobante SRI",
    )
    l10n_ec_xml_amount_untaxed = fields.Float(
        string="Subtotal según XML (SRI)",
        compute="_compute_l10n_ec_xml_amounts",
        help="Valor sin impuestos que declara el XML autorizado del SRI para este comprobante.",
    )
    l10n_ec_xml_amount_tax = fields.Float(
        string="IVA según XML (SRI)",
        compute="_compute_l10n_ec_xml_amounts",
        help="IVA que declara el XML autorizado del SRI para este comprobante.",
    )
    l10n_ec_xml_amount_total = fields.Float(
        string="Total según XML (SRI)",
        compute="_compute_l10n_ec_xml_amounts",
        help="Importe total que declara el XML autorizado del SRI para este comprobante.",
    )
    l10n_ec_xml_amount_total_diff = fields.Float(
        string="Diferencia (Odoo - XML)",
        compute="_compute_l10n_ec_xml_amounts",
        help="Diferencia entre el Total registrado en Odoo y el Total del XML del SRI. "
             "Debe ser 0,00 si la factura se ingresó igual que el comprobante electrónico.",
    )

    @api.depends("name", "l10n_ec_withhold_ids")
    def _compute_has_retention(self):
        for rec in self:
            rec.has_retention = bool(rec.l10n_ec_withhold_ids)

    @api.depends(
        "amount_total",
        "sri_received_document_ids.valor_sin_impuestos_txt",
        "sri_received_document_ids.iva_txt",
        "sri_received_document_ids.importe_total_txt",
    )
    def _compute_l10n_ec_xml_amounts(self):
        for move in self:
            doc = move.sri_received_document_ids[:1]
            move.l10n_ec_xml_amount_untaxed = doc.valor_sin_impuestos_txt
            move.l10n_ec_xml_amount_tax = doc.iva_txt
            move.l10n_ec_xml_amount_total = doc.importe_total_txt
            move.l10n_ec_xml_amount_total_diff = (
                move.amount_total - doc.importe_total_txt if doc else 0.0
            )


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    l10n_ec_xml_supplier_code = fields.Char(
        string="Código Proveedor (XML)",
        help="Código principal que traía el XML del SRI para esta línea. "
             "Se usa para recordar automáticamente, la próxima vez que "
             "compres a este proveedor con este código, qué producto de "
             "Odoo asignarle.",
    )

    l10n_ec_xml_line = fields.Boolean(
        string="Línea importada del XML",
        help="Marca cualquier línea que vino de un XML del SRI, tenga código "
             "de producto o no.",
    )

    l10n_ec_xml_original_name = fields.Char(
        string="Descripción Original (XML)"
    )

    l10n_ec_xml_original_quantity = fields.Float(
        string="Cantidad Original (XML)"
    )

    l10n_ec_xml_original_price_unit = fields.Float(
        string="Precio Original (XML)"
    )

    @staticmethod
    def _sri_differs(a, b):
        try:
            return abs((a or 0.0) - (b or 0.0)) > 1e-6
        except TypeError:
            return a != b

    def _sri_recover_from_xml(self, line):
        """Recupera los valores originales directamente desde el XML."""

        attachment = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", line.move_id.id),
            ("mimetype", "in", ("application/xml", "text/xml")),
        ], limit=1)

        if not attachment or not attachment.datas:
            return False

        try:
            soup = BeautifulSoup(
                base64.b64decode(attachment.datas),
                "xml",
            )
        except Exception:
            return False

        for det in soup.find_all("detalle"):

            code_p = (
                (
                    det.codigoPrincipal.text
                    if det.codigoPrincipal
                    and det.codigoPrincipal.text
                    else False
                )
                or
                (
                    det.codigoInterno.text
                    if det.codigoInterno
                    and det.codigoInterno.text
                    else False
                )
            )

            code_a = (
                (
                    det.codigoAuxiliar.text
                    if det.codigoAuxiliar
                    and det.codigoAuxiliar.text
                    else False
                )
                or
                (
                    det.codigoAdicional.text
                    if det.codigoAdicional
                    and det.codigoAdicional.text
                    else False
                )
            )

            if line.l10n_ec_xml_supplier_code not in (code_p, code_a):
                continue

            try:
                return {
                    "l10n_ec_xml_original_name": (
                        det.descripcion.text
                        if det.descripcion
                        else False
                    ),

                    "l10n_ec_xml_original_quantity": (
                        float(det.cantidad.text)
                        if det.cantidad
                        else 0.0
                    ),

                    "l10n_ec_xml_original_price_unit": (
                        float(det.precioUnitario.text)
                        if det.precioUnitario
                        else 0.0
                    ),
                }

            except (TypeError, ValueError):
                return False

        return False

    def _sri_is_protected(self, line):
        """Determina si la línea fue importada desde XML."""

        return bool(
            line.l10n_ec_xml_line
            or line.l10n_ec_xml_supplier_code
            or line.l10n_ec_xml_original_name
        )

    def write(self, vals):
        touching_product = "product_id" in vals

        user_sets_taxes = "tax_ids" in vals

        if touching_product:

            protected = self.filtered(self._sri_is_protected)
            other = self - protected

            res = True

            if other:
                res = (
                    super(AccountMoveLine, other).write(vals)
                    and res
                )

            for line in protected:

                recovered = {}
                if not line.l10n_ec_xml_original_name:

                    recovered = (
                        self._sri_recover_from_xml(line)
                        if line.l10n_ec_xml_supplier_code
                        else False
                    )

                    if not recovered:
                        recovered = {
                            "l10n_ec_xml_original_name": (
                                line.l10n_ec_xml_original_name
                                or line.name
                            ),
                            "l10n_ec_xml_original_quantity": (
                                line.l10n_ec_xml_original_quantity
                                or line.quantity
                            ),
                            "l10n_ec_xml_original_price_unit": (
                                line.l10n_ec_xml_original_price_unit
                                or line.price_unit
                            ),
                        }

                original_name = recovered.get(
                    "l10n_ec_xml_original_name",
                    line.l10n_ec_xml_original_name,
                )
                original_quantity = (
                    recovered.get("l10n_ec_xml_original_quantity")
                    or line.l10n_ec_xml_original_quantity
                    or line.quantity
                )
                original_price_unit = (
                    recovered.get("l10n_ec_xml_original_price_unit")
                    or line.l10n_ec_xml_original_price_unit
                    or line.price_unit
                )
                original_tax_ids = line.tax_ids.ids

                combined_vals = dict(vals)
                combined_vals.update(recovered)
                combined_vals.update({
                    "name": original_name,
                    "quantity": original_quantity,
                    "price_unit": original_price_unit,
                })

                if not user_sets_taxes:
                    combined_vals["tax_ids"] = [
                        Command.set(original_tax_ids)
                    ]

                res = (
                    super(AccountMoveLine, line).write(
                        combined_vals
                    )
                    and res
                )

                if (
                    line.l10n_ec_xml_supplier_code
                    and line.move_id.partner_id
                    and line.product_id
                ):
                    self.env[
                        "sri.product.code.mapping"
                    ]._remember(
                        line.move_id.partner_id,
                        line.l10n_ec_xml_supplier_code,
                        line.l10n_ec_xml_original_name or line.name,
                        line.product_id,
                        self.env.company,
                    )

            return res

        res = super().write(vals)

        for line in self:

            if not self._sri_is_protected(line):
                continue

            fix = {}

            if (
                line.l10n_ec_xml_original_name
                and line.name
                != line.l10n_ec_xml_original_name
            ):
                fix["name"] = (
                    line.l10n_ec_xml_original_name
                )

            if (
                line.l10n_ec_xml_original_quantity
                and self._sri_differs(
                    line.quantity,
                    line.l10n_ec_xml_original_quantity,
                )
            ):
                fix["quantity"] = (
                    line.l10n_ec_xml_original_quantity
                )

            if (
                line.l10n_ec_xml_original_price_unit
                and self._sri_differs(
                    line.price_unit,
                    line.l10n_ec_xml_original_price_unit,
                )
            ):
                fix["price_unit"] = (
                    line.l10n_ec_xml_original_price_unit
                )

            if fix:
                super(AccountMoveLine, line).write(fix)

        return res
