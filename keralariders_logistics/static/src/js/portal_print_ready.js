/** Seller portal: keep India Post Print disabled until the ARN is stored.
 *
 * India Post booking runs in the background after Request Pickup. Until it
 * stores the article number the 100x150 label would print the KeralaXpress
 * AWB barcode instead of the ARN, so the server renders a disabled
 * ``.kx-print-awb-pending`` button. This polls ``/my/print_status`` and swaps
 * in the normal ``.kx-print-awb`` link (which opens the paper-size modal)
 * once the state is ``ready``. Polling stops on failure or timeout and the
 * button shows "India Post booking is still in progress".
 */
(function () {
    'use strict';

    var STATUS_URL = '/my/print_status';
    var FAST_MS = 4000;
    var SLOW_MS = 10000;
    var SLOW_AFTER_MS = 60000;
    var TIMEOUT_MS = 180000;
    var MAX_ERRORS = 3;

    function pendingButtons(state) {
        var selector = '.kx-print-awb-pending';
        if (state) {
            selector += '[data-kx-print-state="' + state + '"]';
        }
        return Array.prototype.slice.call(document.querySelectorAll(selector));
    }

    function printHref(kind, id) {
        return '/my/' + (kind === 'order' ? 'orders' : 'shipments') + '/' + id + '/print';
    }

    function showWaiting(btn) {
        btn.dataset.kxPrintState = 'failed';
        btn.removeAttribute('aria-busy');
        var loading = btn.querySelector('.kx-print-pending-loading');
        var waiting = btn.querySelector('.kx-print-pending-waiting');
        if (loading) {
            loading.classList.add('d-none');
        }
        if (waiting) {
            waiting.classList.remove('d-none');
        }
    }

    function makeReady(btn) {
        var link = document.createElement('a');
        link.href = printHref(btn.dataset.kxPrintKind, btn.dataset.kxPrintId);
        link.className = btn.className.replace(/\bkx-print-awb-pending\b/, '').trim() + ' kx-print-awb';
        if (btn.dataset.kxPrintTitle) {
            link.title = btn.dataset.kxPrintTitle;
        }
        var text = btn.dataset.kxPrintText || '';
        var icon = document.createElement('i');
        icon.className = 'fa fa-print' + (text ? ' me-1' : '');
        link.appendChild(icon);
        if (text) {
            link.appendChild(document.createTextNode(' ' + text));
        }
        btn.parentNode.replaceChild(link, btn);
    }

    function applyState(btn, state) {
        if (state === 'ready') {
            makeReady(btn);
        } else if (state === 'failed') {
            showWaiting(btn);
        } else if (!state) {
            // Cancelled (or no longer the seller's) while we waited.
            btn.parentNode.removeChild(btn);
        }
    }

    function start() {
        var initial = pendingButtons('pending');
        if (!initial.length) {
            return;
        }
        if (!window.fetch) {
            initial.forEach(showWaiting);
            return;
        }
        var startedAt = Date.now();
        var errors = 0;

        function stop() {
            pendingButtons('pending').forEach(showWaiting);
        }

        function schedule() {
            var elapsed = Date.now() - startedAt;
            if (elapsed >= TIMEOUT_MS) {
                stop();
                return;
            }
            window.setTimeout(poll, elapsed >= SLOW_AFTER_MS ? SLOW_MS : FAST_MS);
        }

        function poll() {
            var buttons = pendingButtons('pending');
            if (!buttons.length) {
                return;
            }
            if (document.hidden) {
                schedule();
                return;
            }
            var ids = {shipment: [], order: []};
            buttons.forEach(function (btn) {
                var kind = btn.dataset.kxPrintKind === 'order' ? 'order' : 'shipment';
                if (ids[kind].indexOf(btn.dataset.kxPrintId) < 0) {
                    ids[kind].push(btn.dataset.kxPrintId);
                }
            });
            var url = STATUS_URL + '?shipments=' + encodeURIComponent(ids.shipment.join(','))
                + '&orders=' + encodeURIComponent(ids.order.join(','));
            fetch(url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error('HTTP ' + response.status);
                    }
                    return response.json();
                })
                .then(function (data) {
                    errors = 0;
                    buttons.forEach(function (btn) {
                        var bucket = btn.dataset.kxPrintKind === 'order' ? data.orders : data.shipments;
                        var id = btn.dataset.kxPrintId;
                        if (!bucket || !Object.prototype.hasOwnProperty.call(bucket, id)) {
                            return;
                        }
                        applyState(btn, bucket[id]);
                    });
                    schedule();
                })
                .catch(function () {
                    errors += 1;
                    if (errors >= MAX_ERRORS) {
                        stop();
                        return;
                    }
                    schedule();
                });
        }

        schedule();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
