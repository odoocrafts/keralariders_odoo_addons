# Copyright 2026 Naim OUDAYET
# License LGPL-3
{
    "name": "PDF Preview Before Print",
    "summary": "Preview PDF reports in a dialog before printing or downloading",
    "description": "PDF Preview Before Print intercepts the print/download action and shows "
                   "a clean full-screen preview before the document is printed or downloaded. "
                   "Keyboard shortcuts (P/D/Esc), single and batch reports, zero configuration.",
    "version": "19.0.1.3.0",
    "category": "Extra Tools",
    "website": "https://www.oudayet.com",
    "author": "Naim OUDAYET",
    "maintainers": ["naimoudayet"],
    "license": "LGPL-3",
    "application": False,
    "installable": True,
    "auto_install": False,
    "depends": ["web"],
    "data": [],
    "assets": {
        "web.assets_backend": [
            "no_pdf_preview_print/static/src/scss/preview.scss",
            "no_pdf_preview_print/static/src/xml/preview_dialog.xml",
            "no_pdf_preview_print/static/src/js/preview_dialog.js",
            "no_pdf_preview_print/static/src/js/preview_service.js",
        ],
        "web.assets_unit_tests": [
            "no_pdf_preview_print/static/tests/**/*.test.js",
        ],
    },
    "images": [
        "static/description/banner.png",
    ],
    "price": 0,
    "currency": "USD",
    "support": "contact@oudayet.com",
}
