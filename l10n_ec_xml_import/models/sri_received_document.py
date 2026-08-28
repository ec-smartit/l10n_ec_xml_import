import base64
import logging

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SRI_WS_URL_PRODUCCION = (
    "https://cel.sri.gob.ec/comprobantes-electronicos-ws/AutorizacionComprobantesOffline?wsdl"
)

TIPO_MAP = {
    "factura": "factura",
    "nota de crédito": "nota_credito",
    "nota de credito": "nota_credito",
    "nota de débito": "nota_debito",
    "nota de debito": "nota_debito",
    "comprobante de retención": "retencion",
    "comprobante de retencion": "retencion",
    "guía de remisión": "guia_remision",
    "guia de remision": "guia_remision",
    "liquidación de compra": "liquidacion_compra",
    "liquidacion de compra": "liquidacion_compra",
}

PROVIDER_DOC_TYPES = ("factura", "nota_credito", "nota_debito", "liquidacion_compra")


def _strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _iter_by_localname(root, tagname):
    for el in root.iter():
        if _strip_ns(el.tag) == tagname:
            yield el


def _findtext(root, tagname, default=None):
    for el in _iter_by_localname(root, tagname):
        if el.text and el.text.strip():
            return el.text.strip()
    return default


class SriReceivedDocument(models.Model):
    _name = "sri.received.document"
    _description = "Comprobante Electrónico Recibido (SRI) - descarga automática"
    _order = "fecha_emision desc, id desc"
    _rec_name = "clave_acceso"

    @api.model
    def _reparent_top_menu(self):
        """En instancias donde 'Contabilidad' (Enterprise) y 'Facturación' son dos apps
        con menú raíz distinto, el menú de este módulo se cuelga primero de
        account.menu_finance (Facturación) porque es el único que existe siempre al
        cargar los datos XML. Esto se ejecuta como parte de los datos del módulo (no
        como post_init_hook), así que corre tanto al instalar como en CADA actualización
        — así el menú no se vuelve a mover a Facturación cada vez que actualizas."""
        Menu = self.env["ir.ui.menu"]
        root = self.env.ref("l10n_ec_xml_import.menu_sri_received_root", raise_if_not_found=False)
        if not root:
            return
        apps = Menu.search([("parent_id", "=", False), ("id", "!=", root.id)])
        target = apps.filtered(lambda m: m.name and "contabilidad" in m.name.lower())
        if not target:
            target = apps.filtered(lambda m: m.name and "accounting" in m.name.lower())
        if target and root.parent_id.id != target[0].id:
            root.parent_id = target[0].id

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    ruc_emisor = fields.Char(string="RUC Emisor")
    razon_social_emisor = fields.Char(string="Razón Social Emisor")
    tipo_comprobante_raw = fields.Char(string="Tipo (texto original)")
    tipo_comprobante = fields.Selection(
        selection=[
            ("factura", "Factura"),
            ("nota_credito", "Nota de Crédito"),
            ("nota_debito", "Nota de Débito"),
            ("retencion", "Comprobante de Retención"),
            ("guia_remision", "Guía de Remisión"),
            ("liquidacion_compra", "Liquidación de Compra"),
            ("otro", "Otro"),
        ],
        string="Tipo de Comprobante",
    )
    serie_comprobante = fields.Char(string="Serie")
    clave_acceso = fields.Char(string="Clave de Acceso", required=True, index=True)
    fecha_autorizacion_txt = fields.Char(string="Fecha Autorización (TXT)")
    fecha_emision = fields.Date(string="Fecha Emisión")
    identificacion_receptor = fields.Char(string="Identificación Receptor")
    receptor_tipo = fields.Selection(
        selection=[("ruc", "RUC"), ("cedula", "Cédula del Receptor")],
        string="Tipo Identificación Receptor",
        compute="_compute_receptor_tipo", store=True, index=True,
        help="RUC (13 dígitos) = compra de la empresa. Cédula (10 dígitos) = comprobante "
             "emitido a la cédula personal de alguien, normalmente un gasto personal.",
    )
    valor_sin_impuestos_txt = fields.Float(string="Valor sin Impuestos (TXT)")
    iva_txt = fields.Float(string="IVA (TXT)")
    importe_total_txt = fields.Float(string="Importe Total (TXT)")
    numero_documento_modificado = fields.Char(string="N° Documento Modificado/Sustento")

    @api.depends("identificacion_receptor")
    def _compute_receptor_tipo(self):
        for rec in self:
            val = (rec.identificacion_receptor or "").strip()
            if len(val) == 13:
                rec.receptor_tipo = "ruc"
            elif len(val) == 10:
                rec.receptor_tipo = "cedula"
            else:
                rec.receptor_tipo = False

    state = fields.Selection(
        selection=[
            ("to_download", "Por descargar"),
            ("downloaded", "XML descargado"),
            ("processed", "Procesado"),
            ("pending_review", "Pendiente de revisión"),
            ("error", "Error"),
        ],
        default="to_download",
        string="Estado",
        index=True,
    )
    auto_download = fields.Boolean(
        string="En cola de descarga automática",
        help="Se marca cuando el comprobante quedó en cola para que el cron de descarga "
             "automática del SRI lo procese en segundo plano. Los comprobantes que el "
             "usuario deja pendientes a propósito (sin marcar 'Descargar XML y procesar "
             "automáticamente' en el asistente) no llevan esta marca y solo se procesan "
             "cuando se hace clic en 'Descargar XML y Procesar' manualmente.",
    )
    xml_authorized = fields.Binary(string="XML Autorizado", attachment=True)
    xml_filename = fields.Char(string="Nombre archivo XML")
    sri_estado_autorizacion = fields.Char(string="Estado SRI")
    error_message = fields.Text(string="Mensaje de error / observación")
    move_id = fields.Many2one("account.move", string="Factura / Asiento relacionado")
    move_state = fields.Selection(related="move_id.state", string="Estado Factura", store=True)
    numero_autorizacion = fields.Char(string="N° Autorización SRI")
    can_register = fields.Boolean(compute="_compute_can_register")

    @api.depends("move_state", "move_id", "tipo_comprobante", "xml_authorized")
    def _compute_can_register(self):
        for rec in self:
            if rec.move_state == "posted":
                rec.can_register = False
            elif rec.move_id:
                rec.can_register = True
            else:
                rec.can_register = bool(rec.tipo_comprobante in PROVIDER_DOC_TYPES and rec.xml_authorized)

    _sql_constraints = [
        ("clave_acceso_company_uniq", "unique(clave_acceso, company_id)",
         "Ya existe un comprobante importado con esta clave de acceso."),
    ]

    def _get_ws_url(self):
        return SRI_WS_URL_PRODUCCION

    def _build_soap_envelope(self, clave_acceso):
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                   xmlns:aut="http://ec.gob.sri.ws.autorizacion">
   <soapenv:Header/>
   <soapenv:Body>
      <aut:autorizacionComprobante>
         <claveAccesoComprobante>{clave_acceso}</claveAccesoComprobante>
      </aut:autorizacionComprobante>
   </soapenv:Body>
</soapenv:Envelope>"""

    def _call_sri_authorization_ws(self, clave_acceso):
        """Devuelve (estado, xml_autorizado_str, numero_autorizacion, mensaje)."""
        url = self._get_ws_url()
        envelope = self._build_soap_envelope(clave_acceso)
        headers = {"Content-Type": "text/xml;charset=UTF-8", "SOAPAction": ""}

        last_exc = None
        resp = None
        for attempt in range(3):
            try:
                resp = requests.post(url, data=envelope.encode("utf-8"), headers=headers,
                                      timeout=30, allow_redirects=False)
            except requests.RequestException as exc:
                last_exc = exc
                resp = None
                continue
            if resp.is_redirect or resp.is_permanent_redirect or resp.status_code in (301, 302, 303, 307, 308):
                last_exc = None
                resp = None
                continue
            break

        if resp is None:
            if last_exc is not None:
                raise UserError(_("No se pudo conectar al WS de Autorización del SRI: %s") % last_exc)
            raise UserError(_(
                "El servidor del SRI está redirigiendo la consulta hacia una dirección interna "
                "con un certificado que no coincide (problema de su infraestructura, no de este "
                "módulo). Intenta de nuevo en unos minutos."))

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise UserError(_("No se pudo conectar al WS de Autorización del SRI: %s") % exc)

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise UserError(_("Respuesta del SRI no es un XML válido: %s") % exc)

        autorizaciones = list(_iter_by_localname(root, "autorizacion"))
        if not autorizaciones:
            extra = [t for t in (
                _findtext(root, "mensaje"), _findtext(root, "informacionAdicional")) if t]
            if extra and any("rango" in t.lower() or "fuera" in t.lower() for t in extra):
                return "FUERA_DE_RANGO", None, None, _(
                    "El SRI rechazó la consulta porque este comprobante está fuera del rango de "
                    "fechas que el Web Service de Autorización permite consultar (suele ser "
                    "~30 días desde su emisión), aunque siga visible en el portal SRI en Línea. "
                    "No hay forma de descargarlo automáticamente: bájalo manualmente del portal "
                    "y súbelo con 'Descargar Comprobantes - SRI' en modo XML (uno por uno o en ZIP)."
                )
            detail = " (%s)" % "; ".join(extra) if extra else ""
            return "NO_ENCONTRADO", None, None, _(
                "El SRI no encontró ningún comprobante con esta clave de acceso%s. Si el documento "
                "tiene más de ~30 días de emitido, puede que esté fuera del rango que el Web "
                "Service acepta consultar; en ese caso, bájalo manualmente del portal SRI en Línea "
                "y súbelo con 'Descargar Comprobantes - SRI' en modo XML.") % detail

        chosen = None
        for aut in autorizaciones:
            if _findtext(aut, "estado") == "AUTORIZADO":
                chosen = aut
                break
        if chosen is None:
            chosen = autorizaciones[-1]

        estado = _findtext(chosen, "estado", "DESCONOCIDO")
        comprobante_xml = _findtext(chosen, "comprobante")
        numero_autorizacion = _findtext(chosen, "numeroAutorizacion")
        mensajes = [t for t in (_findtext(m, "mensaje") for m in _iter_by_localname(chosen, "mensaje")) if t]
        mensaje = "; ".join(mensajes) if mensajes else None
        return estado, comprobante_xml, numero_autorizacion, mensaje

    def action_download_and_process(self, commit=False):
        for doc in self:
            try:
                doc._download_and_process_one()
            except UserError as exc:
                doc.write({"state": "error", "error_message": str(exc)})
            except Exception as exc:
                _logger.exception("Error procesando comprobante %s", doc.clave_acceso)
                doc.write({"state": "error", "error_message": str(exc)})
            if commit:
                self.env.cr.commit()
        return True

    @api.model
    def _cron_download_pending(self, batch_size=20):
        """Procesa en segundo plano los comprobantes que el asistente de importación TXT
        dejó en cola (auto_download=True), confirmando cada uno con su propio commit para
        no perder lo ya procesado si el cron se interrumpe a media tanda. Si queda más
        cola de la que cabe en este lote, se vuelve a disparar a sí mismo para seguir."""
        domain = [("state", "=", "to_download"), ("auto_download", "=", True)]
        docs = self.search(domain, limit=batch_size)
        if not docs:
            return
        docs.action_download_and_process(commit=True)
        if self.search_count(domain):
            cron = self.env.ref(
                "l10n_ec_xml_import.ir_cron_sri_download_pending", raise_if_not_found=False)
            if cron:
                cron._trigger()

    def _download_and_process_one(self):
        self.ensure_one()

        clave = (self.clave_acceso or "").strip()
        if len(clave) != 49 or not clave.isdigit():
            self.write({
                "state": "error",
                "sri_estado_autorizacion": "CLAVE_INVALIDA",
                "error_message": _(
                    "La clave de acceso tiene %(largo)s dígitos (debe tener 49). Esto casi siempre "
                    "pasa porque el archivo TXT se abrió/guardó en Excel, que corrompe números tan "
                    "largos. Vuelve a descargar el TXT directamente del portal SRI en Línea (sin "
                    "abrirlo en Excel) y reimpórtalo.") % {"largo": len(clave)},
            })
            return

        estado, xml_str, numero_autorizacion, mensaje = self._call_sri_authorization_ws(self.clave_acceso)
        self.sri_estado_autorizacion = estado

        if estado != "AUTORIZADO" or not xml_str:
            self.write({
                "state": "error",
                "error_message": mensaje or _("El comprobante no está autorizado en el SRI (estado: %s).") % estado,
            })
            return

        self.write({
            "xml_authorized": base64.b64encode(xml_str.encode("utf-8")),
            "xml_filename": f"{self.clave_acceso}.xml",
            "numero_autorizacion": numero_autorizacion or False,
            "state": "downloaded",
            "error_message": False,
        })

        xml_data0 = BeautifulSoup(
            f"<autorizacion><numeroAutorizacion>{numero_autorizacion or ''}</numeroAutorizacion></autorizacion>",
            "xml",
        )
        xml_data = BeautifulSoup(xml_str, "xml")

        if self.tipo_comprobante in PROVIDER_DOC_TYPES:
            self._process_as_provider_document(xml_data0, xml_data)
        elif self.tipo_comprobante == "retencion":
            self._process_as_retention(xml_data0, xml_data)
        else:
            self.write({
                "state": "pending_review",
                "error_message": _("Tipo de comprobante '%s' descargado pero no se crea automáticamente "
                                    "(guía de remisión u otro); el XML queda adjunto para revisión manual.")
                                  % (self.tipo_comprobante_raw or self.tipo_comprobante),
            })

    def _process_as_provider_document(self, xml_data0, xml_data):
        if self.receptor_tipo == "cedula":
            self.write({
                "state": "pending_review",
                "error_message": _(
                    "Este comprobante fue emitido a la cédula del receptor (%s), no al RUC de "
                    "la empresa — parece un gasto personal. No se creó automáticamente como "
                    "factura de compra; revísalo manualmente si corresponde registrarlo."
                ) % (self.identificacion_receptor or ""),
            })
            return
        wizard_model = self.env["import.provider.invoices"]
        move, msg_error = wizard_model._create_account_move(xml_data0, xml_data, self.clave_acceso)
        if not move:
            self.write({"state": "error", "error_message": msg_error or _("No se pudo crear la factura.")})
            return

        self._attach_xml_to_move(move)
        already_existed = "ya existía" in (msg_error or "").lower() or "ya importada" in (msg_error or "").lower()
        if already_existed:
            if move.state == "posted":
                self.write({"move_id": move.id, "state": "processed", "error_message": False})
            else:
                self.write({
                    "move_id": move.id,
                    "state": "pending_review",
                    "error_message": _("Esta factura ya existía en Odoo (%s) en borrador — enlazada "
                                        "aquí, úsala para registrarla.") % move.name,
                })
            return

        self.write({
            "move_id": move.id,
            "state": "pending_review" if msg_error else "processed",
            "error_message": msg_error or False,
        })

    def _process_as_retention(self, xml_data0, xml_data):
        wizard_model = self.env["import.customer.retentions"]
        ok, msg_error, withhold = wizard_model._create_retention(xml_data0, xml_data, self.clave_acceso)
        if withhold:
            self.move_id = withhold.id
            if ok:
                self._attach_xml_to_move(withhold)
        self.write({
            "state": "processed" if ok else "pending_review",
            "error_message": msg_error or False,
        })

    def _attach_xml_to_move(self, move):
        """Adjunta el XML autorizado a la factura/asiento registrado en Odoo, para
        tenerlo a mano (auditoría, reenvío, etc.) sin tener que buscarlo en este modelo.
        Nunca deja más de un XML adjunto por documento: si ya hay uno (sin importar el
        nombre exacto), no crea otro — y limpia cualquier sobrante que ya se haya
        generado antes de esta corrección."""
        if not move or not self.xml_authorized:
            return
        Attachment = self.env["ir.attachment"]
        existing = Attachment.search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", move.id),
            ("mimetype", "in", ("application/xml", "text/xml")),
        ])
        if existing:
            if len(existing) > 1:
                (existing - existing[:1]).unlink()
            return
        filename = self.xml_filename or "%s.xml" % self.clave_acceso
        Attachment.create({
            "name": filename,
            "type": "binary",
            "datas": self.xml_authorized,
            "res_model": "account.move",
            "res_id": move.id,
            "mimetype": "application/xml",
        })

    def action_post_move(self):
        """Lleva a la ventana de 'Facturas' de Proveedores con esta factura cargada, para
        que la revises y la registres tú mismo (ya no la confirma automáticamente).
        Si todavía no existe (p. ej. quedó pendiente por venir con cédula del receptor y
        decidiste registrarla igual), la crea en este momento en estado borrador."""
        self.ensure_one()
        if not self.move_id:
            if self.tipo_comprobante not in PROVIDER_DOC_TYPES or not self.xml_authorized:
                raise UserError(_("Este comprobante no tiene una factura para registrar."))
            xml_data = BeautifulSoup(base64.b64decode(self.xml_authorized), "xml")
            xml_data0 = BeautifulSoup(
                "<autorizacion><numeroAutorizacion>%s</numeroAutorizacion></autorizacion>"
                % (self.numero_autorizacion or ""), "xml")
            wizard_model = self.env["import.provider.invoices"]
            move, msg_error = wizard_model._create_account_move(xml_data0, xml_data, self.clave_acceso)
            if not move:
                raise UserError(msg_error or _("No se pudo crear la factura."))
            self._attach_xml_to_move(move)
            self.write({
                "move_id": move.id,
                "state": "pending_review" if msg_error else "processed",
                "error_message": msg_error or False,
            })

        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_in_invoice_type")
        action = dict(action)
        action["views"] = [(False, "form")]
        action["view_mode"] = "form"
        action["res_id"] = self.move_id.id
        return action
