// Copyright 2026 Naim OUDAYET
// License LGPL-3
import { describe, expect, test } from "@odoo/hoot";
import {
    getActiveIds,
    pdfPreviewHandler,
} from "@no_pdf_preview_print/js/preview_service";

describe("no_pdf_preview_print / getActiveIds", () => {
    test("returns context.active_ids array", () => {
        expect(getActiveIds({ context: { active_ids: [1, 2, 3] } })).toEqual([1, 2, 3]);
    });
    test("wraps context.active_id in an array", () => {
        expect(getActiveIds({ context: { active_id: 42 } })).toEqual([42]);
    });
    test("falls back to data.ids", () => {
        expect(getActiveIds({ data: { ids: [7, 8] } })).toEqual([7, 8]);
    });
    test("falls back to data.id", () => {
        expect(getActiveIds({ data: { id: 99 } })).toEqual([99]);
    });
    test("returns empty array when nothing present", () => {
        expect(getActiveIds({})).toEqual([]);
    });
    test("context.active_ids takes precedence over data.ids", () => {
        expect(
            getActiveIds({
                context: { active_ids: [1] },
                data: { ids: [99] },
            })
        ).toEqual([1]);
    });
    test("empty active_ids falls through to data.ids", () => {
        expect(
            getActiveIds({
                context: { active_ids: [] },
                data: { ids: [7] },
            })
        ).toEqual([7]);
    });
    test("empty data.ids falls through to data.id", () => {
        expect(getActiveIds({ data: { ids: [], id: 7 } })).toEqual([7]);
    });
    test("preserves order of IDs", () => {
        expect(getActiveIds({ context: { active_ids: [3, 1, 2] } })).toEqual([3, 1, 2]);
    });
    test("large IDs preserved without truncation", () => {
        expect(getActiveIds({ context: { active_ids: [999999999] } })).toEqual([999999999]);
    });
    test("null context does not crash", () => {
        expect(getActiveIds({ context: null, data: { id: 5 } })).toEqual([5]);
    });
});

describe("no_pdf_preview_print / pdfPreviewHandler", () => {
    function makeEnv() {
        const added = [];
        return {
            added,
            services: {
                dialog: {
                    add(Component, props) {
                        added.push({ Component, props });
                    },
                },
                user: { context: {} },
                rpc: () => Promise.resolve(),
                ui: { block() {}, unblock() {} },
            },
        };
    }

    test("returns false for qweb-html reports", () => {
        const env = makeEnv();
        expect(
            pdfPreviewHandler(
                { report_type: "qweb-html", report_name: "x" },
                {},
                env
            )
        ).toBe(false);
        expect(env.added.length).toBe(0);
    });
    test("returns false for qweb-text reports", () => {
        const env = makeEnv();
        expect(
            pdfPreviewHandler(
                { report_type: "qweb-text", report_name: "x" },
                {},
                env
            )
        ).toBe(false);
    });
    test("returns false when no IDs present", () => {
        const env = makeEnv();
        expect(
            pdfPreviewHandler(
                { report_type: "qweb-pdf", report_name: "x", context: {} },
                {},
                env
            )
        ).toBe(false);
        expect(env.added.length).toBe(0);
    });
    test("opens dialog for valid qweb-pdf action", () => {
        const env = makeEnv();
        const rc = pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "sale.report_saleorder",
                context: { active_ids: [1, 2] },
            },
            {},
            env
        );
        expect(rc).toBe(true);
        expect(env.added.length).toBe(1);
    });
    test("reportUrl contains the report_name", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "sale.report_saleorder",
                context: { active_ids: [5] },
            },
            {},
            env
        );
        expect(env.added[0].props.reportUrl).toInclude("sale.report_saleorder");
    });
    test("reportUrl contains comma-joined IDs", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "x",
                context: { active_ids: [5, 6, 7] },
            },
            {},
            env
        );
        expect(env.added[0].props.reportUrl).toInclude("5,6,7");
    });
    test("reportName prop uses action.name", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "x",
                name: "Invoice",
                context: { active_ids: [1] },
            },
            {},
            env
        );
        expect(env.added[0].props.reportName).toBe("Invoice");
    });
    test("reportName falls back to display_name", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "x",
                display_name: "Quotation",
                context: { active_ids: [1] },
            },
            {},
            env
        );
        expect(env.added[0].props.reportName).toBe("Quotation");
    });
    test("reportName defaults to empty string", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "x",
                context: { active_ids: [1] },
            },
            {},
            env
        );
        expect(env.added[0].props.reportName).toBe("");
    });
    test("onDownload prop is a callable function", () => {
        const env = makeEnv();
        pdfPreviewHandler(
            {
                report_type: "qweb-pdf",
                report_name: "x",
                context: { active_ids: [1] },
            },
            {},
            env
        );
        expect(typeof env.added[0].props.onDownload).toBe("function");
    });
    test("action without report_type is treated as qweb-pdf", () => {
        const env = makeEnv();
        const rc = pdfPreviewHandler(
            { report_name: "x", context: { active_ids: [1] } },
            {},
            env
        );
        expect(rc).toBe(true);
        expect(env.added.length).toBe(1);
    });
});
