from datetime import datetime

from bs4 import BeautifulSoup
from odoo import Command, _, models
from odoo.exceptions import UserError


class CustomerRetentions(models.TransientModel):
    _name = 'import.customer.retentions'
    _description = 'Import Customer Retentions'

    def _process_xml_data(self, xml_string, filename):
        """Parse and validate individual XML string."""
        xml_string = xml_string.replace('\n', '').replace('\r', '').replace('\t', '').replace(' ', '')
        if not xml_string.startswith('<?xml'):
            raise UserError(_('The file %s is not a valid XML file.') % filename)
        try:
            return BeautifulSoup(xml_string, "xml")       
        except Exception as e:
            raise UserError(_('Error parsing XML file %s: %s') % (filename, str(e)))
    
    
    def _prepare_header_withhold(self,xml_data0,invoice_id,xml_data,xml_data1):
        xml_date = xml_data1.find('fechaEmision') or False
        xml_date = xml_date and xml_date.text or '' 
        if xml_date:
           xml_date = datetime.strptime(xml_date, "%d/%m/%Y").date()

        journal_id = self.env["account.journal"].search([('l10n_ec_withhold_type','=','out_withhold'),('company_id','=',self.env.company.id)],limit=1)
        estab = xml_data1.find('estab') and xml_data1.find('estab').text or ''
        ptoEmi = xml_data1.find('ptoEmi') and xml_data1.find('ptoEmi').text or ''
        secuencial = xml_data1.find('secuencial') and xml_data1.find('secuencial').text or ''
        vals = {
            'date': xml_date,
            'l10n_ec_withhold_date': xml_date,
            'journal_id': journal_id and journal_id.id or False,
            'partner_id': invoice_id.partner_id.id,
            'move_type': 'entry',
            'l10n_ec_withhold_foreign_regime': False,
            'l10n_ec_authorization_number': xml_data0.find('numeroAutorizacion').text,
            'ref': "Ret %s-%s-%s" %(estab,ptoEmi,secuencial)
        }
        return vals

    def _prepare_lines_withhold(self,xml_data,invoice_id,str_retention='retencion'):
        total_lines = []
        msg_error = ""
        base_total = 0
        taxsupport_code = False
        is_value_zero = True
        for line in xml_data.find_all(str_retention):
            nice_base_label_elements = []
            code_retention = line.find('codigoRetencion').text
            tax_id = self.env["account.tax"].search([('l10n_ec_code_ats','=',code_retention),('type_tax_use','=','sale'),('company_id','=',self.env.company.id)],limit=1)
            if not tax_id:
                raise UserError("Impuesto con codigo %s no encontrado"% code_retention)
            if tax_id.l10n_ec_code_base:
                nice_base_label_elements.append(tax_id.l10n_ec_code_base)
            nice_base_label_elements.append("{:.2f}%".format(abs(tax_id.amount)))
            nice_base_label_elements.append(invoice_id.name)
            nice_base_label = ", ".join(nice_base_label_elements)
            account_id = self.env.company.l10n_ec_tax_base_sale_account_id
            base = 0
            value = 0
            try:
                base = float(line.find('baseImponible').text)
            except (AttributeError, TypeError, ValueError):
                base = 0
            try:
                value = float(line.find('valorRetenido').text)
            except (AttributeError, TypeError, ValueError):
                value = 0
            if value != 0:
                is_value_zero = False
            elif value == 0:
                continue
            base_total += value
            vals_base_line = {
                'partner_id': invoice_id.partner_id.commercial_partner_id.id,
                'quantity': 1.0,
                'price_unit': base,                
                'debit': 0.0,
                'credit': base,
                'tax_base_amount': 0.0,
                'display_type': 'product',
                'l10n_ec_withhold_invoice_id': invoice_id.id,
                'l10n_ec_code_taxsupport': '00',
                'name': 'Base Ret: ' + nice_base_label,
                'tax_ids': [Command.set([tax_id.id])],
                'account_id': account_id and account_id.id or False,
            }
            total_lines.append(vals_base_line)
            taxsupport_code = line.get("taxsupport_code")
            account_id = self.env.company.l10n_ec_tax_base_sale_account_id
            vals_base_line_counterpart = {
                'partner_id': invoice_id.partner_id.commercial_partner_id.id,
                'quantity': 1.0,
                'price_unit': base,
                'debit': base,
                'credit': 0.0,
                'tax_base_amount': 0.0,
                'display_type': 'product',
                'l10n_ec_withhold_invoice_id': invoice_id.id,
                'l10n_ec_code_taxsupport': taxsupport_code,
                'name': 'Base Ret Cont: ' + nice_base_label,
                'account_id': account_id.id,
            }
            total_lines.append(vals_base_line_counterpart)
        if is_value_zero:
            msg_error = "\n Retencion en 0 se omite"
            return [],msg_error
        account = invoice_id.partner_id.property_account_receivable_id
        vals = {
                'partner_id': invoice_id.partner_id.commercial_partner_id.id,
                'quantity': 1.0,
                'price_unit': base_total,
                'debit': 0.0,
                'credit': base_total,
                'tax_base_amount': 0.0,
                'display_type': 'product',
                'l10n_ec_withhold_invoice_id': invoice_id.id,
                'l10n_ec_code_taxsupport': taxsupport_code,
                'name': 'Retención de: : %s'% invoice_id.name,
                'account_id': account.id,
            }
        total_lines.append(vals)
        return total_lines,msg_error

    def _action_retention(self,xml_data0,invoice_id,docs,str_retention,xml_data1):
        header_val = self._prepare_header_withhold(xml_data0,invoice_id,docs,xml_data1)
        line_ids,msg = self._prepare_lines_withhold(docs,invoice_id,str_retention)
        if msg:
            return msg, False
        header_val['line_ids'] = [Command.create(line) for line in line_ids]
        withhold = self.env['account.move'].create(header_val)
        withhold.action_post()

        invoices = withhold.line_ids.mapped("l10n_ec_withhold_invoice_id")
        for inv in invoices:
            wh_reconc = withhold.line_ids.filtered(
                lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
                    and l.l10n_ec_withhold_invoice_id == inv)
            inv_reconc = inv.line_ids.filtered(
                lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable') and not l.reconciled)
            (wh_reconc + inv_reconc).reconcile()
        return False, withhold



    def _find_sale_invoice(self, xml_data, doc_sus):
        """Busca la factura de venta a la que aplica la retención, validando DOS cosas:
        el número de documento (numDocSustento) Y que el cliente de esa factura tenga el
        mismo RUC de quien emite la retención (<ruc> del XML). Antes solo se validaba el
        número, lo que podía conciliar contra el cliente equivocado si coincidía el número
        de secuencial con el de otro cliente."""
        document_number = '%s-%s-%s' % (doc_sus[:3], doc_sus[3:6], doc_sus[6:])
        ruc_tag = xml_data.find('ruc')
        ruc_retenedor = ruc_tag.text.strip() if ruc_tag and ruc_tag.text else False

        candidates = self.env["account.move"].search([
            ('name', 'like', document_number),
            ('move_type', '=', 'out_invoice'),
            ('company_id', '=', self.env.company.id),
        ])
        if not candidates:
            return self.env["account.move"].browse(), document_number, "Documento No encontrado"

        if ruc_retenedor:
            matched = candidates.filtered(
                lambda m: (m.partner_id.commercial_partner_id.vat or '').strip() == ruc_retenedor)
            if not matched:
                otros = ', '.join(candidates.mapped('partner_id.name')) or '-'
                return self.env["account.move"].browse(), document_number, (
                    "Se encontró la factura %s pero es de otro cliente (RUC retención %s no "
                    "coincide con el cliente de esa factura: %s); no se concilia para evitar "
                    "aplicarla al cliente equivocado" % (document_number, ruc_retenedor, otros))
            candidates = matched

        if len(candidates) > 1:
            return self.env["account.move"].browse(), document_number, (
                "Hay más de una factura %s que coincide; revisa manualmente" % document_number)

        return candidates, document_number, False

    def _find_existing_withhold(self, invoice_id):
        """Busca directamente en las líneas de asiento el/los que ya reconciliaron una
        retención contra esta factura (en vez de confiar en el campo relacionado
        l10n_ec_withhold_ids, que puede no reflejar el dato más fresco justo en este punto
        del procesamiento)."""
        if not invoice_id:
            return self.env["account.move"].browse()
        line = self.env["account.move.line"].search([
            ("l10n_ec_withhold_invoice_id", "=", invoice_id.id),
        ], limit=1)
        return line.move_id if line else self.env["account.move"].browse()

    def _create_retention(self, xml_data0,xml_data,file_name):
        msg_error = ""
        last_withhold = False
        if xml_data.find_all('docSustento'):
            for docs in xml_data.find_all('docSustento'):
                doc_sus = docs.numDocSustento.text
                invoice_id, document_number, err = self._find_sale_invoice(xml_data, doc_sus)
                if err:
                    msg_error += "\n %s - %s : %s"%(file_name,document_number,err)
                    continue
                if invoice_id.state == 'cancel':
                    msg_error += "\n %s - %s : %s"%(file_name,document_number,"Documento en estado Cancelado")
                    continue
                if invoice_id.has_retention:
                    msg_error += "\n %s - %s : %s"%(file_name,document_number,"Ya tiene retencion asignada")
                    existing = self._find_existing_withhold(invoice_id)
                    if existing:
                        last_withhold = existing
                    continue
                msg, withhold = self._action_retention(xml_data0,invoice_id,docs,'retencion',xml_data)
                if withhold:
                    last_withhold = withhold
                if msg:
                    msg_error += "\n %s %s"%(file_name,msg)
                
        else:
            doc_sus = xml_data.find('numDocSustento').text
            invoice_id, document_number, err = self._find_sale_invoice(xml_data, doc_sus)
            if err:
                msg_error += "\n %s - %s : %s"%(file_name,document_number,err)
                return False,msg_error,False
            if invoice_id.state == 'cancel':
                msg_error += "\n %s - %s : %s"%(file_name,document_number,"Documento en estado Cancelado")
                return False,msg_error,False
            if invoice_id.has_retention:
                msg_error += "\n %s - %s : %s"%(file_name,document_number,"Ya tiene retencion asignada")
                return False,msg_error,self._find_existing_withhold(invoice_id)
            
            msg, withhold = self._action_retention(xml_data0,invoice_id,xml_data,'impuesto',xml_data)
            if withhold:
                last_withhold = withhold
            if msg:
                msg_error += "\n %s %s"%(file_name,msg)

        if msg_error:
            return False,msg_error,last_withhold
        return True,False,last_withhold
