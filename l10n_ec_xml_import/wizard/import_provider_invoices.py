from datetime import datetime
from bs4 import BeautifulSoup

from odoo import Command, _, models, fields
from odoo.exceptions import UserError

CODDOC_MOVE_TYPE = {
    "01": "in_invoice",
    "03": "in_invoice",
    "04": "in_refund",
    "05": "in_invoice",
}


class ProviderInvoices(models.TransientModel):
    _name = 'import.provider.invoices'
    _description = 'Import Provider Invoices'

    def _process_xml_data(self, xml_string, filename):
        """Parse and validate individual XML string."""
        xml_string = xml_string.replace('\n', '').replace('\r', '').replace('\t', ' ').replace(' ', ' ')
        if not xml_string.startswith('<?xml'):
            raise UserError(_('The file %s is not a valid XML file.') % filename)
        try:
            return BeautifulSoup(xml_string, "xml")
        except Exception as e:
            raise UserError(_('Error parsing XML file %s: %s') % (filename, str(e)))

    def _find_original_invoice(self, xml_data, partner):
        """Para Notas de Crédito/Débito: busca la factura de compra original que modifican,
        usando <numDocModificado> del XML + el mismo proveedor. Enlazarla vía
        reversed_entry_id es lo que activa el botón inteligente nativo de Odoo hacia la
        factura original, y lo que exige el reporte ATS para poder generarse."""
        info = xml_data.find('infoNotaCredito') or xml_data.find('infoNotaDebito')
        if not info:
            return self.env["account.move"].browse(), False
        num_doc_tag = info.find('numDocModificado')
        if not num_doc_tag or not num_doc_tag.text:
            return self.env["account.move"].browse(), False
        document_number = num_doc_tag.text.strip()
        candidates = self.env["account.move"].search([
            ('name', 'like', document_number),
            ('move_type', '=', 'in_invoice'),
            ('company_id', '=', self.env.company.id),
        ])
        if partner:
            matched = candidates.filtered(
                lambda m: m.partner_id.commercial_partner_id.id == partner.commercial_partner_id.id)
            if matched:
                candidates = matched
        return candidates[:1], document_number

    def _prepare_headers_move(self, xml_data0, xml_data):
        try:
            ruc = xml_data.find('ruc').text
        except AttributeError:
            ruc = ""
        try:
            partner_name = xml_data.find('razonSocial').text
        except AttributeError:
            partner_name = ""
        try:
            partner_street = xml_data.find('dirEstablecimiento').text
        except AttributeError:
            partner_street = ""
        partner_id = self.env["res.partner"].search([('vat', '=', ruc)], limit=1)

        if not partner_id:
            partner_id = self.env["res.partner"].create({
                'name': partner_name,
                'vat': ruc,
                'company_id': self.env.company.id,
                'street': partner_street,
                'l10n_latam_identification_type_id': self.env.ref("l10n_ec.ec_ruc").id,
            })
        invoice_date = xml_data.find('fechaEmision').text or False
        if invoice_date:
            invoice_date = datetime.strptime(invoice_date, '%d/%m/%Y').date()
        journal_id = self.env["account.journal"].search([
            *self.env['account.journal']._check_company_domain(self.env.company),
            ('type', '=', 'purchase')], limit=1)
        document_number = '%s-%s-%s' % (
            xml_data.find('estab').text, xml_data.find('ptoEmi').text, xml_data.find('secuencial').text)
        l10n_latam_document_type_id = self.env["l10n_latam.document.type"].search(
            [('code', '=', xml_data.find('codDoc').text)], limit=1)

        cod_doc_tag = xml_data.find('codDoc')
        cod_doc = cod_doc_tag.text.strip() if cod_doc_tag and cod_doc_tag.text else False
        move_type = CODDOC_MOVE_TYPE.get(cod_doc, 'in_invoice')
        forma_pago = False
        for pago in xml_data.find_all('pago') or []:
            fp = pago.find('formaPago')
            if fp and fp.text:
                forma_pago = fp.text.strip()
                if forma_pago:
                    break
        l10n_ec_sri_payment_id = False
        if forma_pago:
            try:
                payment_model = self.env['l10n_ec.sri.payment']
                payment = payment_model.search([('code', '=', forma_pago)], limit=1)
                if not payment:
                    payment = payment_model.search([('name', 'ilike', forma_pago)], limit=1)
                l10n_ec_sri_payment_id = payment and payment.id or False
            except KeyError:
                l10n_ec_sri_payment_id = False

        vals = {
            'move_type': move_type,
            'partner_id': partner_id and partner_id.id or False,
            'invoice_date': invoice_date,
            'date': invoice_date,
            'ref': document_number,
            'journal_id': journal_id and journal_id.id or False,
            'l10n_latam_document_number': document_number,
            'l10n_ec_authorization_number': xml_data0.find('numeroAutorizacion').text,
            'l10n_latam_document_type_id': l10n_latam_document_type_id and l10n_latam_document_type_id.id or False,
            'l10n_ec_sri_payment_id': l10n_ec_sri_payment_id,
        }

        if move_type == 'in_refund':
            original, original_document_number = self._find_original_invoice(xml_data, partner_id)
            if original:
                vals['reversed_entry_id'] = original.id
                vals['invoice_origin'] = original.name
            elif original_document_number:
                vals['_sri_nc_original_not_found'] = original_document_number

        return vals

    def _find_product_for_code(self, partner, code_primary, code_auxiliar):
        """Orden de búsqueda:
        1) product.supplierinfo por RUC del proveedor + código del XML (principal o
           auxiliar): es la forma nativa de Odoo de decir "para este proveedor, este
           código = este producto" (se ve en la pestaña Compra de cada producto).
        2) Código interno / código auxiliar propio del producto (respaldo genérico,
           sin distinguir proveedor).
        """
        SupplierInfo = self.env["product.supplierinfo"]
        Product = self.env["product.product"]
        Mapping = self.env["sri.product.code.mapping"]
        company_domain = ['|', ('company_id', '=', False), ('company_id', '=', self.env.company.id)]
        codes = [c for c in (code_primary, code_auxiliar) if c]

        if partner and codes:
            mapping = Mapping.search([
                ('partner_id', '=', partner.commercial_partner_id.id),
                ('supplier_code', 'in', codes),
                ('product_id', '!=', False),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
            if mapping:
                return mapping.product_id

            info = SupplierInfo.search([
                ('partner_id', '=', partner.commercial_partner_id.id),
                ('product_code', 'in', codes),
            ] + company_domain, limit=1)
            if info and info.product_tmpl_id.product_variant_id:
                return info.product_tmpl_id.product_variant_id

        for code in codes:
            product_id = Product.search(company_domain + [
                '|', ('default_code', '=', code), ('l10n_ec_auxiliary_code', '=', code),
            ], limit=1)
            if product_id:
                return product_id
        return Product.browse()

    def _prepare_lines_move(self, xml_data):
        """Arma las líneas de factura. Un producto no encontrado NO bloquea la
        creación de la factura: la línea se crea sin producto (con la descripción
        del XML y el código guardado) y queda para que la revise quien confirme el
        documento."""
        ruc = xml_data.find('ruc').text if xml_data.find('ruc') else False
        partner = self.env["res.partner"].search([('vat', '=', ruc)], limit=1) if ruc else False

        msg_warning = ""
        lines = []
        for l in xml_data.find_all('detalle'):
            qty = l.cantidad.text
            name = l.descripcion.text
            price_unit = l.precioUnitario.text
            discount = 0
            try:
                xml_discount = float(l.descuento.text)
            except (AttributeError, TypeError, ValueError):
                xml_discount = 0.0
            try:
                qty_f = float(qty)
                price_unit_f = float(price_unit)
            except (TypeError, ValueError):
                qty_f = 0.0
                price_unit_f = 0.0
            gross = qty_f * price_unit_f
            if xml_discount and gross:
                discount = (xml_discount / gross) * 100

            tax_ids = []
            for tax in l.impuestos.find_all('impuesto'):
                tax_obj = self.env["account.tax"].search([
                    ('l10n_ec_code_ats', '=', tax.codigoPorcentaje.text),
                    ('type_tax_use', '=', 'purchase'),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if not tax_obj:
                    tax_obj = self.env["account.tax"].search([
                        ('l10n_ec_code_ats', '=', tax.codigo.text),
                        ('type_tax_use', '=', 'purchase'),
                        ('company_id', '=', self.env.company.id),
                    ], limit=1)
                if tax_obj:
                    tax_ids.append(tax_obj.id)

            code_primary = (
                (l.codigoPrincipal.text if l.codigoPrincipal and l.codigoPrincipal.text else False)
                or (l.codigoInterno.text if l.codigoInterno and l.codigoInterno.text else False)
            )
            code_auxiliar = (
                (l.codigoAuxiliar.text if l.codigoAuxiliar and l.codigoAuxiliar.text else False)
                or (l.codigoAdicional.text if l.codigoAdicional and l.codigoAdicional.text else False)
            )
            product_id = self._find_product_for_code(partner, code_primary, code_auxiliar)

            if not product_id:
                msg_warning += _("\n Producto con código %s no encontrado (proveedor: %s) — línea "
                                  "creada sin producto: %s") % (
                    code_primary or code_auxiliar or "-", partner.name if partner else "-", name)
                if partner and code_primary:
                    self.env["sri.product.code.mapping"]._get_or_create_pending(
                        partner, code_primary, name, self.env.company)

            uom_id = product_id.uom_id if product_id else False
            code = code_primary or code_auxiliar or False
            lines.append({
                'quantity': qty,
                'name': name,
                'product_uom_id': uom_id and uom_id.id or False,
                'price_unit': price_unit,
                'tax_ids': [Command.set(tax_ids)],
                'product_id': product_id and product_id.id or False,
                'is_imported': True,
                'l10n_ec_xml_supplier_code': code,
                'l10n_ec_xml_line': True,
                'l10n_ec_xml_original_name': name or False,
                'l10n_ec_xml_original_quantity': float(qty or 0),
                'l10n_ec_xml_original_price_unit': float(price_unit or 0),
                'discount': discount,
            })
        return lines, msg_warning

    def _reconcile_missing_products(self, move, xml_data):
        """Para una factura/NC que ya existía en Odoo: vuelve a leer las líneas del XML con
        la lógica ACTUAL de homologación y, para las líneas que sigan sin producto, les
        asigna el que corresponda. No toca cantidad/precio/descripción — al escribir el
        producto junto con el código, el propio account.move.line se encarga de mantenerlos
        exactamente como los trajo el XML (lee el adjunto y los recupera)."""
        ruc = xml_data.find('ruc').text if xml_data.find('ruc') else False
        if ruc and move.partner_id and (move.partner_id.vat or '').strip() != ruc.strip():
            return
        lines, _msg = self._prepare_lines_move(xml_data)
        move_lines = move.invoice_line_ids.sorted('id')
        if len(move_lines) != len(lines):
            return
        for line, resolved in zip(move_lines, lines):
            if line.product_id or not resolved.get('product_id'):
                continue
            line.write({
                'product_id': resolved['product_id'],
                'l10n_ec_xml_supplier_code': resolved.get('l10n_ec_xml_supplier_code')
                                              or line.l10n_ec_xml_supplier_code,
            })

    def _create_account_move(self, xml_data0, xml_data, file_name):
        document_number = '%s-%s-%s' % (
            xml_data.find('estab').text, xml_data.find('ptoEmi').text, xml_data.find('secuencial').text)

        vals = self._prepare_headers_move(xml_data0, xml_data)
        move_type = vals.get('move_type', 'in_invoice')
        partner_id = vals.get('partner_id')
        nc_original_not_found = vals.pop('_sri_nc_original_not_found', False)

        move_exists = self.env["account.move"].search([
            ('ref', '=', document_number),
            ('move_type', '=', move_type),
            ('partner_id', '=', partner_id),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if move_exists:
            if move_type == 'in_refund' and not move_exists.reversed_entry_id and not nc_original_not_found:
                original, _doc_num = self._find_original_invoice(xml_data, move_exists.partner_id)
                if original:
                    move_exists.write({'reversed_entry_id': original.id, 'invoice_origin': original.name})
            self._reconcile_missing_products(move_exists, xml_data)
            return move_exists, "\n %s : %s" % (document_number, "Factura ya existía en Odoo")

        lines, msg_warning = self._prepare_lines_move(xml_data)
        vals["line_ids"] = [Command.create(line) for line in lines]
        move_id = self.env["account.move"].create(vals)

        importe_total_tag = xml_data.find('importeTotal') or xml_data.find('valorModificacion')
        try:
            importe_total = float(importe_total_tag.text) if importe_total_tag and importe_total_tag.text else False
        except (TypeError, ValueError):
            importe_total = False

        msg_error = msg_warning or ""
        if importe_total is not False and abs(move_id.amount_total - importe_total) > 0.02:
            note = _("Verifica antes de confirmar: el total calculado en Odoo (%(odoo)s) no coincide "
                      "con el importe total del XML del SRI (%(sri)s).") % {
                "odoo": move_id.amount_total, "sri": importe_total,
            }
            move_id.message_post(body=note)
            msg_error = (msg_error + "\n" + note) if msg_error else note

        if nc_original_not_found:
            note = _("No se encontró en Odoo la factura original %(doc)s a la que modifica esta "
                      "Nota de Crédito — impórtala primero (o enlázala manualmente con el botón "
                      "'Añadir' en el campo de factura original) antes de generar el ATS, o el "
                      "reporte la rechazará.") % {"doc": nc_original_not_found}
            move_id.message_post(body=note)
            msg_error = (msg_error + "\n" + note) if msg_error else note

        return move_id, msg_error or False
