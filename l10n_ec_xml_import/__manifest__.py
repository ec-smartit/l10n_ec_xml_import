{
    "name": "Ecuador - Importación y Descarga Automática de Comprobantes SRI",
    "version": "19.0.4.0.0",
    "description": """
Importa facturas de proveedor y retenciones de cliente desde los XML del SRI, en una sola
ventana ("Importar Comprobantes Recibidos") con dos formas de trabajar:

* TXT (descarga automática): subes el TXT de "Comprobantes Recibidos" del portal SRI en
  Línea; por cada clave de acceso nueva se consulta el Web Service de Autorización del
  SRI y se descarga el XML autorizado.
* XML / ZIP (subida manual): subes uno o varios XML ya autorizados (o un ZIP). El módulo
  detecta solo si cada uno es factura/nota de crédito/nota de débito o retención.

En ambos casos, las facturas de compra quedan en borrador para revisión, y las retenciones
se concilian automáticamente contra la factura de venta correspondiente (validando también
el RUC del cliente, no solo el número de documento).
""",
    "countries": ["ec"],
    "author": "smartit-ec.com / Custom Development",
    "category": "Accounting/Localizations/Payments",
    "website": "http://www.smartit-ec.com",
    "license": "OPL-1",
    "depends": [
        "account",
        "l10n_ec_edi",
    ],
    "external_dependencies": {"python": ["bs4", "requests"]},
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/account_move.xml",
        "views/sri_received_document_views.xml",
        "views/sri_supplierinfo_views.xml",
        "wizard/sri_txt_import_wizard.xml",
        "views/menu_views.xml",
        "views/log_import_files.xml",
        "views/menu_reparent.xml",
    ],
    "demo": [],
    "installable": True,
    "application": True,
    "auto_install": False,
}
