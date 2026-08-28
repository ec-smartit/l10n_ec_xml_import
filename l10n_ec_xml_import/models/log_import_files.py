from odoo import models, fields


class LogImportFiles(models.Model):
    _name = "log.import.files"
    _description = "Log Archivos Importados"

    name = fields.Text("Detalle")
    date = fields.Date("Fecha", default=fields.Date.today())
    import_type = fields.Selection([
        ("txt", "TXT (descarga automática)"),
        ("xml", "XML / ZIP (subida manual)"),
        ("retention", "Retención (histórico)"),
        ("provider", "Proveedor (histórico)"),
    ], default="txt", string="Tipo de Importación")
