/** @odoo-module **/
// Copyright 2026 Naim OUDAYET
// License LGPL-3

/**
 * PDF Preview Report Handler
 *
 * Registers a handler in Odoo 19's "ir.actions.report handlers" registry
 * to intercept qweb-pdf report actions. Instead of the browser downloading
 * the file immediately, a full-screen preview dialog is shown first.
 *
 * The user can then Print, Download, or Close from the dialog.
 */

import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { rpc } from "@web/core/network/rpc";
import { PreviewDialog } from "./preview_dialog";
import { downloadReport } from "@web/webclient/actions/reports/utils";

export function pdfPreviewHandler(action, options, env) {
    if (action.report_type && action.report_type !== "qweb-pdf") {
        return false;
    }

    const activeIds = getActiveIds(action);
    if (!activeIds.length) {
        return false;
    }

    const reportUrl = `/report/pdf/${action.report_name}/${activeIds.join(",")}`;

    env.services.dialog.add(PreviewDialog, {
        reportUrl,
        reportName: action.name || action.display_name || "",
        onDownload() {
            // Both `user` and `rpc` became singleton imports in Odoo 18+
            // (no longer service-registry entries), so env.services.user
            // and env.services.rpc are undefined. downloadReport's first
            // arg is the rpc function itself.
            const ctx = { ...user.context, ...action.context };
            downloadReport(rpc, action, "pdf", ctx);
        },
    });

    return true;
}

/**
 * Extract record IDs from the various places Odoo puts them.
 */
export function getActiveIds(action) {
    if (action.context?.active_ids?.length) {
        return action.context.active_ids;
    }
    if (action.context?.active_id) {
        return [action.context.active_id];
    }
    if (action.data?.ids?.length) {
        return action.data.ids;
    }
    if (action.data?.id) {
        return [action.data.id];
    }
    return [];
}

registry
    .category("ir.actions.report handlers")
    .add("no_pdf_preview_print", pdfPreviewHandler);
