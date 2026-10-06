/**
 * Seller portal: copy the WhatsApp order template, fill Add Order from one
 * pasted message, and preview a bulk paste before create.
 *
 * Keep the aliases, separators, line order, and gram-to-kilogram conversion
 * in step with models/whatsapp_order_paste.py. The copy button reads
 * data-template, which the page renders from WHATSAPP_ORDER_TEMPLATE.
 * Bulk preview and create re-parse on the server with that module. This
 * file fills the single Add Order form locally.
 */
(function () {
    'use strict';

    var LINE_RE = /^\s*(.+?)\s*[:\-\u2013\u2014]\s*(.*)\s*$/;
    var NUMBER_RE = /\d+(?:\.\d+)?/;
    var DIM_RE = /(\d+(?:\.\d+)?)\s*[xX\u00d7]\s*(\d+(?:\.\d+)?)\s*[xX\u00d7]\s*(\d+(?:\.\d+)?)/;
    var LINE_KEYS = [
        'name', 'mobile', 'address', 'pincode', 'payment',
        'cod_amount', 'item', 'weight_g', 'dimensions'
    ];
    var ALIASES = {
        'name': 'name',
        'customer name': 'name',
        'receiver name': 'name',
        'receiver': 'name',
        'mobile': 'mobile',
        'mobile number': 'mobile',
        'phone': 'mobile',
        'phone number': 'mobile',
        'contact': 'mobile',
        'address': 'address',
        'address line': 'address',
        'shipping address': 'address',
        'pincode': 'pincode',
        'pin code': 'pincode',
        'pin': 'pincode',
        'zip': 'pincode',
        'zipcode': 'pincode',
        'zip code': 'pincode',
        'weight g': 'weight_g',
        'weight grams': 'weight_g',
        'weight gram': 'weight_g',
        'weight': 'weight_g',
        'wt': 'weight_g',
        'length cm': 'length_cm',
        'length': 'length_cm',
        'breadth cm': 'breadth_cm',
        'breadth': 'breadth_cm',
        'width cm': 'breadth_cm',
        'width': 'breadth_cm',
        'height cm': 'height_cm',
        'height': 'height_cm',
        'payment': 'payment',
        'payment type': 'payment',
        'cod amount': 'cod_amount',
        'cod amt': 'cod_amount',
        'order value': 'cod_amount',
        'total order value': 'cod_amount',
        'item': 'item',
        'item description': 'item',
        'description': 'item',
        'contents': 'item',
        'state': 'state',
        'service': 'service',
        'india post service': 'service'
    };
    var PREPAID = {prepaid: 1, 'pre paid': 1, paid: 1, online: 1, 'already paid': 1};
    var COD = {cod: 1, 'cash on delivery': 1, cash: 1};

    function normLabel(label) {
        return String(label || '').trim().toLowerCase().replace(/_/g, ' ')
            .replace(/[()*]/g, ' ').replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
    }

    function formatNumber(number, places) {
        var text = Number(number).toFixed(places).replace(/0+$/, '').replace(/\.$/, '');
        return text || '0';
    }

    function splitBlocks(text) {
        var normalized = String(text || '').replace(/\ufeff/g, '')
            .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
            .replace(/\u2028/g, '\n').replace(/\u2029/g, '\n');
        var blocks = [];
        var current = [];
        normalized.split('\n').forEach(function (line) {
            if (!line.trim() || line.trim() === '---') {
                if (current.length) {
                    blocks.push(current.join('\n'));
                    current = [];
                }
                return;
            }
            current.push(line);
        });
        if (current.length) {
            blocks.push(current.join('\n'));
        }
        return blocks;
    }

    function usesLabeledFormat(block) {
        var labeled = false;
        block.split('\n').forEach(function (line) {
            var match = LINE_RE.exec(line);
            if (match && ALIASES[normLabel(match[1])] === 'name') {
                labeled = true;
            }
        });
        return labeled;
    }

    function readFields(block) {
        var values = {};
        var lastKey = null;
        block.split('\n').forEach(function (line) {
            var match = LINE_RE.exec(line);
            var key = match ? ALIASES[normLabel(match[1])] : null;
            if (key) {
                values[key] = (match[2] || '').trim();
                lastKey = key;
                return;
            }
            if (lastKey && line.trim()) {
                values[lastKey] = (values[lastKey] + ' ' + line.trim()).trim();
            }
        });
        return values;
    }

    function readLineFields(block) {
        var lines = [];
        block.split('\n').forEach(function (line) {
            if (line.trim()) {
                lines.push(line.trim());
            }
        });
        var values = {};
        LINE_KEYS.forEach(function (key, index) {
            if (index < lines.length) {
                values[key] = lines[index];
            }
        });
        var dims = values.dimensions;
        delete values.dimensions;
        if (dims) {
            var match = DIM_RE.exec(dims);
            if (match) {
                values.length_cm = match[1];
                values.breadth_cm = match[2];
                values.height_cm = match[3];
            }
        }
        if (parsePayment(values.payment).payment === 'prepaid') {
            values.cod_amount = '0';
        }
        return values;
    }

    function parsePayment(raw) {
        if (raw == null || !String(raw).trim()) {
            return {payment: 'prepaid', error: ''};
        }
        var text = normLabel(raw);
        if (COD[text] || text.indexOf('cod') === 0) {
            return {payment: 'cod', error: ''};
        }
        if (PREPAID[text] || text.indexOf('prepaid') !== -1 || text.indexOf('pre paid') !== -1) {
            return {payment: 'prepaid', error: ''};
        }
        return {payment: 'prepaid', error: 'Payment must be Prepaid or COD.'};
    }

    function parseService(raw) {
        var text = normLabel(raw || '');
        if (!text) {
            return '';
        }
        if (text === 'sp' || text === 'speed post' || text.indexOf('speed') !== -1) {
            return 'SP';
        }
        if (text === 'bp' || text === 'business parcel' || text === 'normal parcel'
                || text.indexOf('business') !== -1 || text.indexOf('parcel') !== -1) {
            return 'BP';
        }
        return '';
    }

    function weightKg(raw) {
        var text = String(raw || '').trim().toLowerCase().replace(/,/g, '');
        if (!text) {
            return '';
        }
        var match = NUMBER_RE.exec(text);
        if (!match) {
            return '';
        }
        var number = parseFloat(match[0]);
        var kg = text.indexOf('kg') !== -1 ? number : number / 1000;
        kg = Math.round(kg * 1000) / 1000;
        if (!(kg > 0)) {
            return '';
        }
        return formatNumber(kg, 3);
    }

    function measure(raw) {
        var text = String(raw || '').trim().toLowerCase().replace(/,/g, '');
        if (!text) {
            return '';
        }
        var match = NUMBER_RE.exec(text);
        if (!match) {
            return '';
        }
        var number = Math.round(parseFloat(match[0]) * 1000) / 1000;
        if (!(number > 0)) {
            return '';
        }
        return formatNumber(number, 3);
    }

    function parseMoney(raw) {
        var text = String(raw == null ? '' : raw).trim().replace(/,/g, '');
        if (!text) {
            return null;
        }
        var match = NUMBER_RE.exec(text);
        if (!match) {
            return null;
        }
        return parseFloat(match[0]);
    }

    function mobile(raw) {
        var text = String(raw || '').trim();
        var digits = text.replace(/\D/g, '');
        if (!digits) {
            return '';
        }
        return text.charAt(0) === '+' ? '+' + digits : digits;
    }

    function pincode(raw) {
        return String(raw || '').replace(/\D/g, '');
    }

    function parseBlock(index, block) {
        var values = usesLabeledFormat(block) ? readFields(block) : readLineFields(block);
        if (!Object.keys(values).length) {
            return {
                index: index,
                ok: false,
                reason: 'Could not read this message. Use the template: one field per line.',
                fields: {},
                missing: []
            };
        }
        var payment = parsePayment(values.payment);
        var weight = weightKg(values.weight_g);
        var phone = mobile(values.mobile);
        var pin = pincode(values.pincode);
        var codAmount = Object.prototype.hasOwnProperty.call(values, 'cod_amount')
            ? parseMoney(values.cod_amount) : null;
        var missing = [];
        var missingNames = [];
        if (!String(values.name || '').trim()) {
            missing.push('Name');
            missingNames.push('shipping_to_name');
        }
        if (!phone) {
            missing.push('Mobile');
            missingNames.push('shipping_to_mobile');
        }
        if (!String(values.address || '').trim()) {
            missing.push('Address');
            missingNames.push('shipping_to_address');
        }
        if (!pin) {
            missing.push('Pincode');
            missingNames.push('shipping_to_zip');
        }
        if (!weight) {
            missing.push('Weight');
            missingNames.push('total_weight');
        }
        if (!String(values.item || '').trim()) {
            missing.push('Item');
            missingNames.push('item_description');
        }
        if (payment.payment === 'cod' && codAmount == null) {
            missing.push('COD amount');
            missingNames.push('total_order_value');
        }
        var fields = {order_payment_type: payment.payment};
        if (String(values.name || '').trim()) {
            fields.shipping_to_name = values.name.trim();
        }
        if (phone) {
            fields.shipping_to_mobile = phone;
        }
        if (String(values.address || '').trim()) {
            fields.shipping_to_address = values.address.trim();
        }
        if (pin) {
            fields.shipping_to_zip = pin;
        }
        if (weight) {
            fields.total_weight = weight;
        }
        if (String(values.item || '').trim()) {
            fields.item_description = values.item.trim();
        }
        [['length_cm', 'length_cm'], ['breadth_cm', 'breadth_cm'], ['height_cm', 'height_cm']].forEach(function (pair) {
            var value = measure(values[pair[0]]);
            if (value) {
                fields[pair[1]] = value;
            }
        });
        if (codAmount != null) {
            fields.total_order_value = formatNumber(codAmount, 2);
        } else if (payment.payment === 'prepaid') {
            fields.total_order_value = '0';
        }
        if (String(values.state || '').trim()) {
            fields.shipping_to_state_name = String(values.state).trim();
        }
        var service = parseService(values.service);
        if (service) {
            fields.indiapost_article_type = service;
        }
        var reasons = [];
        if (missing.length) {
            var labels = missing.slice();
            var weightRaw = String(values.weight_g || '').trim();
            if (weightRaw && !weight && labels.indexOf('Weight') !== -1) {
                labels.splice(labels.indexOf('Weight'), 1);
                if (labels.length) {
                    reasons.push('Missing ' + labels.join(', '));
                }
                reasons.push('Weight must be greater than 0.');
            } else {
                reasons.push('Missing ' + labels.join(', '));
            }
        }
        if (payment.error) {
            reasons.push(payment.error);
        }
        if (reasons.length) {
            return {
                index: index,
                ok: false,
                reason: reasons.join(' '),
                fields: fields,
                missing: missingNames
            };
        }
        return {index: index, ok: true, reason: '', fields: fields, missing: []};
    }

    function parseWhatsappOrders(text) {
        return splitBlocks(text).map(function (block, offset) {
            return parseBlock(offset + 1, block);
        });
    }

    function status(el, message, ok) {
        if (!el) {
            return;
        }
        el.textContent = message;
        el.classList.remove('d-none', 'text-success', 'text-danger');
        el.classList.add(ok ? 'text-success' : 'text-danger');
    }

    function copyWithExecCommand(text) {
        var area = document.createElement('textarea');
        area.value = text;
        // A rendered textarea is required. display:none and visibility:hidden
        // make execCommand('copy') fail.
        area.setAttribute('aria-hidden', 'true');
        area.style.position = 'fixed';
        area.style.top = '0';
        area.style.left = '0';
        area.style.width = '2em';
        area.style.height = '2em';
        area.style.padding = '0';
        area.style.border = 'none';
        area.style.outline = 'none';
        area.style.boxShadow = 'none';
        area.style.background = 'transparent';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.focus();
        area.select();
        if (area.setSelectionRange) {
            area.setSelectionRange(0, area.value.length);
        }
        var copied = false;
        try {
            copied = document.execCommand('copy');
        } catch (err) {
            copied = false;
        }
        document.body.removeChild(area);
        return copied;
    }

    function copyTemplate(button) {
        var text = button.getAttribute('data-template') || '';
        var tools = button.closest('.kx-wa-tools');
        var note = tools && tools.querySelector('.kx-wa-copy-status');
        var label = button.querySelector('.kx-wa-copy-label');

        function markCopied() {
            if (note) {
                note.textContent = '';
                note.classList.add('d-none');
            }
            if (!label) {
                return;
            }
            label.textContent = 'Copied';
            if (button._kxCopyTimer) {
                clearTimeout(button._kxCopyTimer);
            }
            button._kxCopyTimer = setTimeout(function () {
                label.textContent = 'Copy WhatsApp template';
                button._kxCopyTimer = null;
            }, 2000);
        }

        function markFailed() {
            if (button._kxCopyTimer) {
                clearTimeout(button._kxCopyTimer);
                button._kxCopyTimer = null;
            }
            if (label) {
                label.textContent = 'Copy WhatsApp template';
            }
            status(note, 'Could not copy the template.', false);
        }

        function fallback() {
            if (text && copyWithExecCommand(text)) {
                markCopied();
            } else {
                markFailed();
            }
        }

        if (!text) {
            markFailed();
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(markCopied, fallback);
        } else {
            fallback();
        }
    }

    function bindPasteToggles() {
        document.querySelectorAll('.kx-wa-paste-toggle').forEach(function (button) {
            var block = button.closest('.kx-wa-block');
            var panel = block && block.querySelector('.kx-wa-paste-panel');
            if (!panel) {
                return;
            }
            button.addEventListener('click', function () {
                var nowHidden = panel.classList.toggle('d-none');
                button.setAttribute('aria-expanded', nowHidden ? 'false' : 'true');
                if (!nowHidden) {
                    var area = panel.querySelector('textarea');
                    if (area) {
                        area.focus();
                    }
                }
            });
        });
    }

    function fieldEl(form, name) {
        return form.querySelector('[name="' + name + '"]');
    }

    function applySingle(form, parsedBlocks) {
        var note = document.getElementById('kx_wa_fill_status');
        form.querySelectorAll('.is-invalid').forEach(function (el) {
            el.classList.remove('is-invalid');
        });
        if (!parsedBlocks.length) {
            status(note, 'Paste a filled WhatsApp message first.', false);
            return;
        }
        var block = parsedBlocks[0];
        var fields = block.fields || {};
        [
            'shipping_to_name', 'shipping_to_mobile', 'shipping_to_address',
            'shipping_to_zip', 'total_weight', 'item_description',
            'length_cm', 'breadth_cm', 'height_cm'
        ].forEach(function (name) {
            var el = fieldEl(form, name);
            if (!el) {
                return;
            }
            if (Object.prototype.hasOwnProperty.call(fields, name)) {
                el.value = fields[name];
            } else if ((block.missing || []).indexOf(name) !== -1) {
                el.value = '';
            }
        });

        var payment = fieldEl(form, 'order_payment_type');
        if (payment && fields.order_payment_type) {
            payment.value = fields.order_payment_type;
            payment.dispatchEvent(new Event('change', {bubbles: true}));
        }
        var cod = fieldEl(form, 'total_order_value');
        if (cod && fields.order_payment_type === 'cod') {
            cod.value = fields.total_order_value || '';
        }

        if (fields.shipping_to_state_name) {
            var state = fieldEl(form, 'shipping_to_state_id');
            if (state) {
                var wanted = fields.shipping_to_state_name.toLowerCase();
                Array.prototype.forEach.call(state.options, function (option) {
                    if (option.text.trim().toLowerCase() === wanted) {
                        state.value = option.value;
                    }
                });
            }
        }
        if (fields.indiapost_article_type) {
            var radio = form.querySelector(
                'input[name="indiapost_article_type"][value="' + fields.indiapost_article_type + '"]'
            );
            if (radio) {
                radio.checked = true;
                radio.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }
        ['total_weight', 'length_cm', 'breadth_cm', 'height_cm'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) {
                el.dispatchEvent(new Event('input', {bubbles: true}));
            }
        });

        var missingNames = (block.missing || []).slice();
        form.querySelectorAll('[required]').forEach(function (el) {
            if (!el.value || !String(el.value).trim()) {
                el.classList.add('is-invalid');
                if (el.name && missingNames.indexOf(el.name) === -1) {
                    missingNames.push(el.name);
                }
            }
        });
        if (payment && payment.value === 'cod' && cod && !(cod.value && String(cod.value).trim())) {
            cod.classList.add('is-invalid');
        }

        var messages = [];
        if (parsedBlocks.length > 1) {
            messages.push('Only the first order was filled in. Use Bulk Upload Order for several messages.');
        }
        if (!block.ok) {
            messages.push(block.reason);
            messages.push('Highlighted fields still need a value. The order was not created.');
            status(note, messages.join(' '), false);
            var firstInvalid = form.querySelector('.is-invalid');
            if (firstInvalid && firstInvalid.scrollIntoView) {
                firstInvalid.scrollIntoView({behavior: 'smooth', block: 'center'});
            }
            return;
        }
        if (form.querySelector('.is-invalid')) {
            messages.push('Some required fields are still empty. Fill the highlighted ones, then click Create Order.');
            status(note, messages.join(' '), false);
            return;
        }
        messages.push('Form filled from the WhatsApp message. Check it, then click Create Order.');
        status(note, messages.join(' '), true);
    }

    function bindCopyButtons() {
        document.querySelectorAll('.kx-wa-copy').forEach(function (button) {
            button.addEventListener('click', function () {
                copyTemplate(button);
            });
        });
    }

    function bindSingleFill() {
        var button = document.getElementById('kx_wa_fill');
        var paste = document.getElementById('kx_wa_paste');
        var form = document.querySelector('form.kx-portal-shipment-form');
        if (!button || !paste || !form) {
            return;
        }
        button.addEventListener('click', function () {
            applySingle(form, parseWhatsappOrders(paste.value));
        });
    }

    function pluralOrders(count) {
        return count === 1 ? '1 order' : count + ' orders';
    }

    function renderPreview(panel, data) {
        panel.classList.remove('d-none', 'alert-success', 'alert-warning', 'alert-danger');
        panel.replaceChildren();
        var title = document.createElement('div');
        title.className = 'fw-semibold';
        var okCount = data.ok_count || 0;
        var failCount = data.fail_count || 0;
        if (!data.blocks || !data.blocks.length) {
            title.textContent = 'No orders found. Paste a filled template.';
            panel.classList.add('alert-warning');
        } else if (okCount && failCount) {
            title.textContent = pluralOrders(okCount) + ' ready. ' + failCount + ' skipped.';
            panel.classList.add('alert-warning');
        } else if (okCount) {
            title.textContent = pluralOrders(okCount) + ' ready to create as drafts.';
            panel.classList.add('alert-success');
        } else {
            title.textContent = 'No orders could be read.';
            panel.classList.add('alert-danger');
        }
        panel.appendChild(title);
        var list = document.createElement('ul');
        list.className = 'small mb-0 mt-2 ps-3';
        (data.blocks || []).forEach(function (block) {
            var item = document.createElement('li');
            if (block.ok) {
                item.textContent = 'Order ' + block.index + ': ' + block.name
                    + ', ' + block.mobile + ', ' + block.pincode + ', ' + block.payment;
            } else {
                item.textContent = 'Block ' + block.index + ': ' + block.reason;
            }
            list.appendChild(item);
        });
        if (list.childNodes.length) {
            panel.appendChild(list);
        }
        if (okCount) {
            var foot = document.createElement('div');
            foot.className = 'small mt-2 mb-0';
            foot.textContent = 'Create saves drafts only (Order Added). Pickup is not requested and the wallet is not charged.';
            panel.appendChild(foot);
        }
    }

    function bindBulk() {
        var form = document.getElementById('kx_wa_bulk_form');
        if (!form) {
            return;
        }
        var paste = form.querySelector('[name="message"]');
        var previewBtn = document.getElementById('kx_wa_preview_btn');
        var createBtn = document.getElementById('kx_wa_create_btn');
        var panel = document.getElementById('kx_wa_preview');
        var previewed = null;

        function lockCreate() {
            previewed = null;
            if (createBtn) {
                createBtn.disabled = true;
            }
        }

        if (paste) {
            paste.addEventListener('input', lockCreate);
        }

        if (previewBtn) {
            previewBtn.addEventListener('click', function () {
                var token = form.querySelector('[name="csrf_token"]');
                previewBtn.disabled = true;
                fetch('/my/orders/whatsapp_preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({
                        csrf_token: token ? token.value : '',
                        message: paste ? paste.value : ''
                    })
                }).then(function (response) {
                    return response.json().then(function (data) {
                        return {ok: response.ok, data: data};
                    });
                }).then(function (result) {
                    previewBtn.disabled = false;
                    if (!result.ok || !result.data || result.data.error) {
                        lockCreate();
                        renderPreview(panel, {
                            ok_count: 0,
                            fail_count: 0,
                            blocks: [{
                                index: 1,
                                ok: false,
                                reason: (result.data && result.data.error) || 'Could not read the messages. Try again.'
                            }]
                        });
                        return;
                    }
                    renderPreview(panel, result.data);
                    previewed = paste ? paste.value : '';
                    if (createBtn) {
                        createBtn.disabled = !(result.data.ok_count > 0);
                    }
                }).catch(function () {
                    previewBtn.disabled = false;
                    lockCreate();
                    renderPreview(panel, {
                        ok_count: 0,
                        fail_count: 0,
                        blocks: [{index: 1, ok: false, reason: 'Could not read the messages. Try again.'}]
                    });
                });
            });
        }

        form.addEventListener('submit', function (ev) {
            if (!paste || paste.value !== previewed) {
                ev.preventDefault();
                if (createBtn) {
                    createBtn.disabled = true;
                }
                return;
            }
            if (createBtn) {
                createBtn.disabled = true;
            }
        });
    }

    // Odoo 19 serves web.assets_frontend JS from web.assets_frontend_lazy,
    // after window "load". DOMContentLoaded has already fired by then, so a
    // listener registered here would never run and the buttons would do nothing.
    function start() {
        bindCopyButtons();
        bindPasteToggles();
        bindSingleFill();
        bindBulk();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
