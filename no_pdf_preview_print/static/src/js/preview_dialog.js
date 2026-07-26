/** @odoo-module **/
// Copyright 2026 Naim OUDAYET
// License LGPL-3

import { Component, markup, useRef, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { useHotkey } from "@web/core/hotkeys/hotkey_hook";
import { useService } from "@web/core/utils/hooks";

export class PreviewDialog extends Component {
    static template = "no_pdf_preview_print.PreviewDialog";
    static components = { Dialog };
    static props = {
        reportUrl: { type: String },
        reportName: { type: String, optional: true },
        onDownload: { type: Function },
        close: { type: Function },
    };

    setup() {
        this.iframeRef = useRef("previewIframe");
        this.state = useState({ loading: true, error: false });
        this.hotkey = useService("hotkey");

        // Hotkeys via Odoo's service rather than a raw document listener: it
        // handles input-field bypass, dialog stacking (only the top dialog's
        // hotkeys fire), and namespace conflict warnings for free. Esc is
        // bound by the Dialog component itself — no need to handle it.
        useHotkey("p", () => this.onPrint());
        useHotkey("d", () => this.onDownload());
    }

    get dialogTitle() {
        return this.props.reportName || _t("PDF Preview");
    }

    // One translatable string for the whole footer hotkey legend — translators
    // can reorder verb/key combinations to fit natural word order (esp. RTL).
    // Per ODOO_GUIDELINES §12.6: NEVER split a sentence across multiple _t()
    // calls. markup() lets us keep <kbd> styling without t-raw / unsafe HTML.
    get hotkeyHintMarkup() {
        return markup(_t(
            "<kbd>P</kbd> Print · <kbd>D</kbd> Download · <kbd>Esc</kbd> Close"
        ));
    }

    onIframeLoad() {
        this.state.loading = false;
        // Once the PDF viewer mounts, the iframe steals keyboard focus and
        // a parent-document listener stops seeing keystrokes. registerIframe
        // attaches the hotkey service to iframe.contentWindow so P/D still
        // fire from inside the PDF area. Same mechanism html_editor uses for
        // its <iframe> body (web/core/hotkeys/hotkey_service.js:registerIframe).
        // try/catch covers the edge case where contentWindow isn't accessible
        // (cross-origin or browser PDF sandbox).
        const iframe = this.iframeRef.el;
        if (iframe?.contentWindow) {
            try {
                this.hotkey.registerIframe(iframe);
            } catch {
                /* fall back to parent-document-only hotkeys */
            }
        }
    }

    onIframeError() {
        this.state.loading = false;
        this.state.error = true;
    }

    onPrint() {
        const iframe = this.iframeRef.el;
        if (iframe?.contentWindow) {
            iframe.contentWindow.focus();
            iframe.contentWindow.print();
        }
    }

    onDownload() {
        this.props.onDownload();
        this.props.close();
    }
}
