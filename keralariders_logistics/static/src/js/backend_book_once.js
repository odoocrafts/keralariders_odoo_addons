/** @odoo-module **/

const BOOK_ONCE_NAMES = new Set([
    'action_request_pickup',
    'action_indiapost_book',
    'action_indiapost_book_order',
]);

document.addEventListener('click', (ev) => {
    const btn = ev.target && ev.target.closest && ev.target.closest('button');
    if (!btn || !BOOK_ONCE_NAMES.has(btn.getAttribute('name'))) {
        return;
    }
    if (btn.dataset.kxBookBusy === '1') {
        ev.preventDefault();
        ev.stopImmediatePropagation();
        return;
    }
    btn.dataset.kxBookBusy = '1';
    btn.setAttribute('aria-busy', 'true');
    if (!btn.querySelector('.kx-book-once-spinner')) {
        const spinner = document.createElement('i');
        spinner.className = 'fa fa-spinner fa-spin me-1 kx-book-once-spinner';
        btn.insertBefore(spinner, btn.firstChild);
    }
    window.setTimeout(() => {
        btn.disabled = true;
    }, 0);
}, true);
