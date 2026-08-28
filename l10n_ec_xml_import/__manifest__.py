{
    "name": "Comprobantes SRI Ecuador",
    "version": "19.0.4.0.0",
    "summary": "Import and reconcile SRI electronic vendor bills and withholdings",
    "description": """
Imports vendor bills and customer withholdings from Ecuador's SRI (tax authority) XML
documents, in a single wizard, with two ways to work:

* TXT (automatic download): upload the "Received Documents" TXT exported from the SRI
  online portal; for each new access key, the SRI Authorization Web Service is queried
  and the authorized XML is downloaded automatically.
* XML / ZIP (manual upload): upload one or several already-authorized XML files (or a
  ZIP). The module detects on its own whether each one is a vendor bill, credit note,
  debit note or withholding document.

In both cases, vendor bills are created in draft for review, and withholdings are
automatically reconciled against the matching customer invoice (validating both the
document number and the withholding party's tax ID).
""",
    "countries": ["ec"],
    "author": "smartit-ec.com / Custom Development",
    "support": "michael.freire@smartit-ec.com",
    "price": 390.00,
    "currency": "USD",
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
