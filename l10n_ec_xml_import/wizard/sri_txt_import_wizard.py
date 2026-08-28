import base64
import csv
import io
import zipfile

from bs4 import BeautifulSoup

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.sri_received_document import TIPO_MAP

PROVIDER_ROOT_TAGS = ("factura", "notacredito", "notadebito", "liquidacioncompra")
RETENTION_ROOT_TAG = "comprobanteretencion"


class SriTxtImportWizard(models.TransientModel):
    _name = "sri.txt.import.wizard"
    _description = "Descargar Comprobantes - SRI (TXT o XML)"

    state = fields.Selection(
        selection=[("upload", "Cargar"), ("done", "Resultados")],
        default="upload", string="Estado",
    )

    import_mode = fields.Selection(
        selection=[
            ("txt", "TXT (descarga automática por clave de acceso)"),
            ("xml", "XML / ZIP (subida manual)"),
        ],
        string="Forma de importación", default="txt", required=True,
    )

    txt_file = fields.Binary(string="Archivo TXT")
    txt_filename = fields.Char(string="Nombre de archivo")
    auto_process = fields.Boolean(
        string="Descargar XML y procesar automáticamente",
        default=True,
        help="Si está marcado, los comprobantes quedan en cola para descargarse del SRI y "
             "crear/conciliar los documentos en segundo plano (no en este mismo clic, para no "
             "bloquear Odoo si el TXT trae muchas filas) — revisa el resultado en 'Doc. "
             "Electrónicos Descargados' en unos minutos. Si lo desmarcas, solo se listan las "
             "filas para procesarlas manualmente después.",
    )

    attachment_ids = fields.Many2many(
        "ir.attachment", string="Archivos XML o ZIP",
        help="Sube uno o varios XML autorizados del SRI, o un ZIP con varios. Se detecta solo "
             "si cada uno es factura/nota de crédito/nota de débito o retención.",
    )

    result_text = fields.Text(string="Resultados", readonly=True)

    def _decode_txt(self):
        raw = base64.b64decode(self.txt_file)
        for encoding in ("utf-8-sig", "utf-8", "iso-8859-1", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise UserError(_("No se pudo determinar la codificación del archivo TXT."))

    @staticmethod
    def _to_float(value):
        value = (value or "").strip().replace(",", ".")
        if not value:
            return 0.0
        try:
            return float(value)
        except ValueError:
            return 0.0

    @staticmethod
    def _to_date(value):
        value = (value or "").strip()
        if not value:
            return False
        try:
            day, month, year = value.split("/")
            return f"{year}-{month}-{day}"
        except Exception:
            return False

    def _reopen_self(self):
        """Reabre esta misma ventana flotante (sin cerrarla), ya con los resultados
        cargados en la parte de abajo."""
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_new_upload(self):
        """Botón 'Nueva carga': vuelve a la pantalla de subir archivo, sin cerrar la
        ventana, para encadenar otra importación."""
        self.ensure_one()
        self.write({
            "state": "upload",
            "txt_file": False,
            "txt_filename": False,
            "attachment_ids": [(5, 0, 0)],
            "result_text": False,
        })
        return self._reopen_self()

    def action_import(self):
        self.ensure_one()
        if self.import_mode == "xml":
            self._action_import_xml()
        else:
            self._action_import_txt()
        self.state = "done"
        return self._reopen_self()

    def _action_import_txt(self):
        if not self.txt_file:
            raise UserError(_("Sube el archivo TXT de Comprobantes Recibidos."))
        text = self._decode_txt()
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")

        company = self.env.company
        Doc = self.env["sri.received.document"]

        company_ruc = (company.vat or "").strip()
        company_cedula = company_ruc[:10] if len(company_ruc) == 13 else company_ruc
        if not company_ruc:
            raise UserError(_("La compañía activa no tiene RUC configurado (Ajustes > Empresas). "
                               "Configúralo antes de importar, para poder validar que los "
                               "comprobantes correspondan a esta empresa."))

        to_process = Doc.browse()
        skipped = 0
        skipped_other_company = 0
        total_rows = 0
        for row in reader:
            clave_acceso = (row.get("CLAVE_ACCESO") or "").strip()
            if not clave_acceso:
                continue
            total_rows += 1

            receptor = (row.get("IDENTIFICACION_RECEPTOR") or "").strip()
            if receptor and receptor not in (company_ruc, company_cedula):
                skipped_other_company += 1
                continue

            existing = Doc.search([
                ("clave_acceso", "=", clave_acceso), ("company_id", "=", company.id)], limit=1)
            if existing:
                if existing.move_id:
                    skipped += 1
                    continue
                existing.write({"state": "to_download", "error_message": False})
                to_process |= existing
                continue

            tipo_raw = (row.get("TIPO_COMPROBANTE") or "").strip()
            doc = Doc.create({
                "company_id": company.id,
                "ruc_emisor": (row.get("RUC_EMISOR") or "").strip(),
                "razon_social_emisor": (row.get("RAZON_SOCIAL_EMISOR") or "").strip(),
                "tipo_comprobante_raw": tipo_raw,
                "tipo_comprobante": TIPO_MAP.get(tipo_raw.lower(), "otro"),
                "serie_comprobante": (row.get("SERIE_COMPROBANTE") or "").strip(),
                "clave_acceso": clave_acceso,
                "fecha_autorizacion_txt": (row.get("FECHA_AUTORIZACION") or "").strip(),
                "fecha_emision": self._to_date(row.get("FECHA_EMISION")),
                "identificacion_receptor": receptor,
                "valor_sin_impuestos_txt": self._to_float(row.get("VALOR_SIN_IMPUESTOS")),
                "iva_txt": self._to_float(row.get("IVA")),
                "importe_total_txt": self._to_float(row.get("IMPORTE_TOTAL")),
                "numero_documento_modificado": (row.get("NUMERO_DOCUMENTO_MODIFICADO") or "").strip(),
                "state": "to_download",
            })
            to_process |= doc

        if not to_process:
            reason = _("ya tienen su factura/retención creada en Odoo") if skipped else (
                _("no corresponden al RUC de esta empresa (%s)") % company_ruc if skipped_other_company
                else _("no son filas válidas"))
            self.write({
                "state": "done",
                "result_text": _("===== TXT =====\nNo se importó nada: las %s fila(s) del archivo %s.")
                                % (total_rows, reason),
            })
            return

        if self.auto_process:
            to_process.write({"auto_download": True})
            cron = self.env.ref(
                "l10n_ec_xml_import.ir_cron_sri_download_pending", raise_if_not_found=False)
            if cron:
                cron._trigger()
            result_text = self._build_txt_queued_result_text(to_process, skipped, skipped_other_company)
        else:
            result_text = self._build_txt_result_text(to_process, skipped, skipped_other_company)

        self.write({"result_text": result_text})
        self.env["log.import.files"].create({
            "name": self.result_text, "import_type": "txt",
        })

    def _build_txt_queued_result_text(self, docs, skipped, skipped_other_company):
        lines = ["===== TXT =====",
                 "%s comprobante(s) quedaron en cola para descargarse y procesarse en segundo "
                 "plano:" % len(docs)]
        for d in docs:
            lines.append("· %s (%s) — clave %s" % (
                d.razon_social_emisor or "-", d.tipo_comprobante or "-", d.clave_acceso))
        if skipped:
            lines.append("%s comprobante(s) ya tenían factura/retención creada (se omitieron)." % skipped)
        if skipped_other_company:
            lines.append("%s fila(s) no correspondían al RUC de esta empresa (se omitieron)."
                          % skipped_other_company)
        lines.append("")
        lines.append("Revisa el resultado final en 'Doc. Electrónicos Descargados' / 'Doc. "
                      "Electrónicos Pendientes' en unos minutos.")
        return "\n".join(lines)

    def _build_txt_result_text(self, docs, skipped, skipped_other_company):
        processed = docs.filtered(lambda d: d.state == "processed")
        pending = docs.filtered(lambda d: d.state == "pending_review")
        errored = docs.filtered(lambda d: d.state == "error")

        lines = ["===== TXT =====", "Se procesaron %s comprobante(s):" % len(docs)]
        for d in docs:
            estado_txt = {
                "processed": "OK, registrado", "pending_review": "Pendiente de revisión",
                "error": "Error", "to_download": "Por descargar", "downloaded": "Descargado",
            }.get(d.state, d.state)
            detalle = " — %s" % d.error_message if d.error_message and d.state != "processed" else ""
            lines.append("· %s (%s) — clave %s — %s%s" % (
                d.razon_social_emisor or "-", d.tipo_comprobante or "-", d.clave_acceso, estado_txt, detalle))
        if skipped:
            lines.append("%s comprobante(s) ya tenían factura/retención creada (se omitieron)." % skipped)
        if skipped_other_company:
            lines.append("%s fila(s) no correspondían al RUC de esta empresa (se omitieron)."
                          % skipped_other_company)
        lines.append("")
        lines.append("Procesados correctamente: %s | Pendientes de revisión: %s | Con error: %s"
                      % (len(processed), len(pending), len(errored)))
        return "\n".join(lines)

    def _attach_xml(self, move, filename, xml_string):
        """Nunca deja más de un XML adjunto por documento: si ya hay uno (sin importar
        el nombre exacto), no crea otro — y limpia sobrantes previos."""
        if not move:
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
        attachment_name = filename if filename.lower().endswith(".xml") else "%s.xml" % filename
        Attachment.create({
            "name": attachment_name,
            "type": "binary",
            "datas": base64.b64encode(xml_string.encode("utf-8")),
            "res_model": "account.move",
            "res_id": move.id,
            "mimetype": "application/xml",
        })

    def _action_import_xml(self):
        if not self.attachment_ids:
            raise UserError(_("Sube al menos un archivo XML o ZIP."))

        provider_wizard = self.env["import.provider.invoices"]
        retention_wizard = self.env["import.customer.retentions"]

        number_success = 0
        number_error = 0
        detail_lines = ["===== XML =====", ""]

        for attachment in self.attachment_ids:
            if not attachment.datas:
                raise UserError(_("El archivo %s está vacío.") % attachment.name)
            file_name = attachment.name.lower()
            file_content = base64.b64decode(attachment.datas)

            xml_items = []
            if file_name.endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
                        xml_files = [f for f in zf.namelist() if f.lower().endswith(".xml")]
                        if not xml_files:
                            raise UserError(_("El ZIP %s no contiene archivos XML.") % attachment.name)
                        for fn in xml_files:
                            with zf.open(fn) as xf:
                                xml_string = xf.read().decode("utf-8").strip()
                                if xml_string:
                                    xml_items.append((fn, xml_string))
                except zipfile.BadZipFile:
                    raise UserError(_("El archivo %s no es un ZIP válido.") % attachment.name)
            elif file_name.endswith(".xml"):
                xml_string = file_content.decode("utf-8").strip()
                if not xml_string:
                    raise UserError(_("El archivo %s está vacío.") % attachment.name)
                xml_items.append((attachment.name, xml_string))
            else:
                raise UserError(_("Tipo de archivo no soportado para %s. Solo .xml y .zip.")
                                 % attachment.name)

            for fn, xml_string in xml_items:
                xml_data0 = provider_wizard._process_xml_data(xml_string, fn)
                xml_data = BeautifulSoup(xml_data0.find("comprobante").text, "xml")
                root = xml_data.find(True)
                root_tag = (root.name or "").lower() if root else ""

                if root_tag == RETENTION_ROOT_TAG:
                    ok, msg_error, withhold = retention_wizard._create_retention(xml_data0, xml_data, fn)
                    if ok:
                        number_success += 1
                        detail_lines.append("· %s — Retención conciliada" % fn)
                        if withhold:
                            self._attach_xml(withhold, fn, xml_string)
                    else:
                        number_error += 1
                        detail_lines.append("· %s — Error: %s" % (fn, msg_error))
                elif root_tag in PROVIDER_ROOT_TAGS:
                    move, msg_error = provider_wizard._create_account_move(xml_data0, xml_data, fn)
                    if move:
                        number_success += 1
                        detail_lines.append("· %s — Factura %s (%s)"
                                             % (fn, move.name or move.ref or "", "revisar: %s" % msg_error
                                                if msg_error else "OK"))
                        self._attach_xml(move, fn, xml_string)
                    else:
                        number_error += 1
                        detail_lines.append("· %s — Error: %s" % (fn, msg_error))
                else:
                    number_error += 1
                    detail_lines.append("· %s — tipo de comprobante '%s' no soportado" % (fn, root_tag))

        detail_lines.append("")
        detail_lines.append("Correctos: %s | Con error: %s" % (number_success, number_error))
        result_text = "\n".join(detail_lines)

        self.write({"result_text": result_text})
        self.env["log.import.files"].create({"name": result_text, "import_type": "xml"})
