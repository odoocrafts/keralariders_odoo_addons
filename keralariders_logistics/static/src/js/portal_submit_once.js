/** Disable pickup/booking submit buttons after the first click.
 *
 * Same idea as Scan and Pay "I Have Paid": ignore a second submit, keep the
 * button disabled, and show a spinner until the HTTP request finishes.
 *
 * Confirm dialogs use form onsubmit="return confirm(...)". Capture only
 * blocks a second submit; the bubble listener arms the spinner after
 * confirm has had a chance to cancel (defaultPrevented).
 */
(function () {
    'use strict';

    function isSubmitOnce(form) {
        return form && form.classList && form.classList.contains('kx-submit-once');
    }

    document.addEventListener('submit', function (ev) {
        var form = ev.target;
        if (!isSubmitOnce(form)) {
            return;
        }
        if (form.dataset.submitted === '1') {
            ev.preventDefault();
            ev.stopPropagation();
            return false;
        }
    }, true);

    document.addEventListener('submit', function (ev) {
        var form = ev.target;
        if (!isSubmitOnce(form) || ev.defaultPrevented) {
            return;
        }
        if (form.dataset.submitted === '1') {
            return;
        }
        form.dataset.submitted = '1';
        var buttons = form.querySelectorAll('button[type="submit"]');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            btn.disabled = true;
            btn.setAttribute('aria-busy', 'true');
            var label = btn.querySelector('.kx-submit-once-label');
            var loading = btn.querySelector('.kx-submit-once-loading');
            if (label) {
                label.classList.add('d-none');
            }
            if (loading) {
                loading.classList.remove('d-none');
            }
        }
    }, false);
})();
