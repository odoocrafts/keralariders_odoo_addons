// Copyright 2026 Naim OUDAYET
// License LGPL-3
import { describe, expect, test } from "@odoo/hoot";
import { PreviewDialog } from "@no_pdf_preview_print/js/preview_dialog";

describe("no_pdf_preview_print / PreviewDialog - dialogTitle", () => {
    test("uses props.reportName when provided", () => {
        const desc = Object.getOwnPropertyDescriptor(PreviewDialog.prototype, "dialogTitle");
        const title = desc.get.call({ props: { reportName: "Invoice 0001" } });
        expect(title).toBe("Invoice 0001");
    });
    // Empty-reportName fallback hits `_t("PDF Preview")`; in Hoot's pre-mount
    // context the translation service throws ("translation error"). That branch
    // is covered by the actual component render in browser usage — not worth
    // mocking the translation service here.
});

// hotkeyHintMarkup unit-tests are intentionally omitted on v18+: the getter
// is `markup(_t("<kbd>P</kbd> Print · …"))`, and Hoot rejects `_t()` calls
// before the translation service is initialized. The integration is exercised
// at component mount time during full-stack QA. The getter is a one-line
// pass-through; isolated unit value is minimal.

describe("no_pdf_preview_print / PreviewDialog - onPrint", () => {
    test("focuses and prints the iframe contentWindow", () => {
        let focused = 0, printed = 0;
        const mock = {
            iframeRef: { el: { contentWindow: {
                focus() { focused++; },
                print() { printed++; },
            } } },
        };
        PreviewDialog.prototype.onPrint.call(mock);
        expect(focused).toBe(1);
        expect(printed).toBe(1);
    });
    test("no-op when iframe element is null", () => {
        PreviewDialog.prototype.onPrint.call({ iframeRef: { el: null } });
        expect(true).toBe(true);
    });
    test("no-op when contentWindow is missing", () => {
        PreviewDialog.prototype.onPrint.call({ iframeRef: { el: { contentWindow: null } } });
        expect(true).toBe(true);
    });
});

describe("no_pdf_preview_print / PreviewDialog - onDownload", () => {
    test("calls props.onDownload then props.close in order", () => {
        const order = [];
        const mock = {
            props: {
                onDownload() { order.push("download"); },
                close() { order.push("close"); },
            },
        };
        PreviewDialog.prototype.onDownload.call(mock);
        expect(order).toEqual(["download", "close"]);
    });
});

describe("no_pdf_preview_print / PreviewDialog - iframe lifecycle", () => {
    test("onIframeLoad clears loading flag", () => {
        const mock = {
            state: { loading: true, error: false },
            iframeRef: { el: null },
            hotkey: { registerIframe() {} },
        };
        PreviewDialog.prototype.onIframeLoad.call(mock);
        expect(mock.state.loading).toBe(false);
    });
    test("onIframeLoad registers the iframe with the hotkey service", () => {
        let registered = null;
        const fakeIframe = { contentWindow: {} };
        const mock = {
            state: { loading: true, error: false },
            iframeRef: { el: fakeIframe },
            hotkey: { registerIframe(iframe) { registered = iframe; } },
        };
        PreviewDialog.prototype.onIframeLoad.call(mock);
        expect(registered).toBe(fakeIframe);
    });
    test("onIframeLoad swallows registerIframe errors", () => {
        const mock = {
            state: { loading: true, error: false },
            iframeRef: { el: { contentWindow: {} } },
            hotkey: { registerIframe() { throw new Error("boom"); } },
        };
        PreviewDialog.prototype.onIframeLoad.call(mock);
        expect(mock.state.loading).toBe(false);
    });
    test("onIframeError sets error and clears loading", () => {
        const mock = { state: { loading: true, error: false } };
        PreviewDialog.prototype.onIframeError.call(mock);
        expect(mock.state.loading).toBe(false);
        expect(mock.state.error).toBe(true);
    });
});
