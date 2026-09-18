"""Order-level India Post actions.

A ``logistics.order`` is a seller's batch of shipments, which lines up neatly
with the bulk booking endpoint: one order becomes one ``articles`` array.
"""

import logging
import threading

from odoo import api, fields, models, modules, SUPERUSER_ID, _
from odoo.exceptions import UserError, ValidationError
from odoo.modules.registry import Registry

from .indiapost_client import IndiapostApiError
from .indiapost_shipment import quote_failure_reason

_logger = logging.getLogger(__name__)

# Cron-only: a worker that died mid-book can leave this flag True forever.
_STALE_BOOKING_MINUTES = 5


def _ip_autobook_order_in_new_cursor(dbname, order_id):
    """Book India Post after pickup has committed. Runs off the HTTP thread."""
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            order = env['logistics.order'].browse(order_id).exists()
            if order:
                order._ip_autobook_after_pickup()
    except Exception:
        _logger.exception(
            'India Post background autobook failed for order id %s', order_id,
        )


class Order(models.Model):
    _inherit = 'logistics.order'

    indiapost_shipment_count = fields.Integer(
        string='India Post Shipments', compute='_compute_indiapost_counts',
    )
    indiapost_booked_count = fields.Integer(
        string='Booked with India Post', compute='_compute_indiapost_counts',
    )
    indiapost_pending_count = fields.Integer(
        string='Awaiting India Post Booking', compute='_compute_indiapost_counts',
    )

    @api.depends('shipment_ids.fulfilment_method',
                 'shipment_ids.indiapost_booking_state',
                 'shipment_ids.indiapost_article_number')
    def _compute_indiapost_counts(self):
        for order in self:
            indiapost = order.shipment_ids.filtered(
                lambda s: s.fulfilment_method == 'indiapost')
            order.indiapost_shipment_count = len(indiapost)
            order.indiapost_booked_count = len(indiapost.filtered(
                lambda s: s.indiapost_booking_state == 'booked'))
            order.indiapost_pending_count = len(indiapost._ip_bookable())

    def _ip_indiapost_shipments(self):
        return self.shipment_ids.filtered(
            lambda s: s.fulfilment_method == 'indiapost' and s.state != 'cancelled')

    def action_request_pickup(self):
        """Debit and request pickup; India Post books after this request commits.

        Wallet debit stays synchronous. Autobook must not run inside the portal
        HTTP request: a hung India Post call used to block the only Odoo thread
        and 502 the site. The admin Book with India Post button remains the
        on-request retry.
        """
        res = super().action_request_pickup()
        last_notify = None
        for order in self:
            notify = order._ip_schedule_autobook_after_pickup()
            if notify:
                last_notify = notify
        return last_notify or res

    def _ip_needs_autobook(self):
        self.ensure_one()
        shipments = self._ip_indiapost_shipments()
        if not shipments:
            return False
        if shipments._ip_bookable():
            return True
        return bool(shipments.filtered(
            lambda s: s.indiapost_article_number and not s.indiapost_label_pdf))

    def _ip_schedule_autobook_after_pickup(self):
        """Queue India Post booking so pickup HTTP can return immediately.

        Tests share the request cursor and assert on the booked record, so they
        still run autobook inline. Production registers a post-commit daemon
        thread with a new cursor; ``cron_indiapost_autobook`` is the safety net.
        """
        self.ensure_one()
        if not self._ip_needs_autobook():
            return None

        if modules.module.current_test:
            errors = self._ip_autobook_after_pickup()
            return self._ip_pickup_autobook_notify(errors)

        order_id = self.id
        dbname = self.env.cr.dbname

        def _spawn_thread():
            thread = threading.Thread(
                target=_ip_autobook_order_in_new_cursor,
                args=(dbname, order_id),
                name='keralaxpress-indiapost-autobook-%s' % order_id,
                daemon=True,
            )
            thread.start()

        try:
            self.env.cr.postcommit.add(_spawn_thread)
        except Exception:
            _logger.warning(
                'Could not register post-commit India Post autobook for %s',
                self.name, exc_info=True,
            )
            _spawn_thread()
        return None

    def _ip_pickup_autobook_notify(self, errors):
        """Backend notification used only when autobook ran in this request."""
        self.ensure_one()
        indiapost = self._ip_indiapost_shipments()
        if not indiapost:
            return None
        booked = indiapost.filtered(
            lambda s: s.indiapost_booking_state == 'booked')
        if errors:
            return indiapost._ip_notify(
                _('Pickup requested — India Post booking had errors'),
                _('Pickup is requested and the wallet has been charged.\n\n%s')
                % '\n'.join(errors),
                kind='danger', sticky=True,
            )
        if booked:
            return indiapost._ip_notify(
                _('Pickup requested and booked with India Post'),
                _('%s shipment(s) booked.') % len(booked),
            )
        return None

    def _ip_autobook_after_pickup(self):
        """Run the same book + label-fetch the admin buttons run.

        Idempotent: already-booked articles are not sent again. Failures stay
        on ``indiapost_booking_state`` / ``indiapost_booking_error`` so the
        portal can flash India Post's message. Returns a list of error strings.
        """
        self.ensure_one()
        shipments = self._ip_indiapost_shipments()
        if not shipments:
            return []

        bookable = shipments._ip_bookable()
        if bookable:
            try:
                bookable.action_indiapost_book()
            except (UserError, ValidationError, IndiapostApiError) as exc:
                message = quote_failure_reason(exc)
                for shipment in bookable:
                    if shipment.indiapost_booking_state != 'booked':
                        shipment._ip_record_booking_error(message)
            except Exception as exc:
                _logger.exception(
                    'India Post auto-book failed for order %s', self.name)
                message = str(exc)
                for shipment in bookable:
                    if shipment.indiapost_booking_state != 'booked':
                        shipment._ip_record_booking_error(message)

        errors = [
            '%s: %s' % (shipment.name, shipment.indiapost_booking_error)
            for shipment in shipments
            if shipment.indiapost_booking_state == 'error'
            and shipment.indiapost_booking_error
        ]

        to_label = shipments.filtered(
            lambda s: s.indiapost_article_number and not s.indiapost_label_pdf)
        if to_label:
            try:
                to_label.action_indiapost_fetch_label()
            except Exception:
                _logger.warning(
                    'India Post auto-label fetch failed for order %s',
                    self.name, exc_info=True)
        return errors

    @api.model
    def cron_indiapost_autobook(self, limit=40, order_ids=None):
        """Book India Post shipments whose pickup already committed.

        Covers a daemon thread that never ran (process restart between commit
        and spawn) and workers that died with ``indiapost_booking_in_progress``.
        """
        Client = self.env['logistics.indiapost.client']
        if not Client._ip_is_configured():
            _logger.info('India Post autobook cron skipped: not configured.')
            return 0

        Shipment = self.env['logistics.shipment'].sudo()
        stale_before = fields.Datetime.subtract(
            fields.Datetime.now(), minutes=_STALE_BOOKING_MINUTES)
        stale = Shipment.search([
            ('fulfilment_method', '=', 'indiapost'),
            ('indiapost_booking_in_progress', '=', True),
            ('indiapost_booking_state', '!=', 'booked'),
            ('write_date', '<', stale_before),
        ])
        if stale:
            stale.write({'indiapost_booking_in_progress': False})
            _logger.warning(
                'Cleared stale India Post in-progress flag on %s shipment(s).',
                len(stale),
            )

        domain = [
            ('fulfilment_method', '=', 'indiapost'),
            ('state', '=', 'pickup_requested'),
            ('indiapost_booking_state', 'in', ('to_book', 'error')),
            ('indiapost_article_number', '=', False),
            ('indiapost_booking_in_progress', '=', False),
        ]
        if order_ids:
            domain.append(('order_id', 'in', list(order_ids)))
        pending = Shipment.search(domain, limit=limit, order='id')
        orders = pending.mapped('order_id')
        done = 0
        for order in orders:
            try:
                order._ip_autobook_after_pickup()
                done += 1
            except Exception:
                _logger.exception(
                    'India Post autobook cron failed for order %s', order.name)
        return done

    def action_indiapost_book_order(self):
        """Book every unbooked India Post shipment in this order in one call."""
        self.ensure_one()
        shipments = self._ip_indiapost_shipments()
        if not shipments:
            raise UserError(_(
                'Order %s has no India Post shipments to book.'
            ) % self.name)
        return shipments.action_indiapost_book()

    def action_indiapost_quote_order(self):
        """Refresh India Post rates for the whole order."""
        self.ensure_one()
        shipments = self._ip_indiapost_shipments()
        if not shipments:
            raise UserError(_(
                'Order %s has no India Post shipments to price.'
            ) % self.name)
        return shipments.action_indiapost_quote()

    def action_indiapost_fetch_labels(self):
        """Download India Post labels for every booked shipment in the order."""
        self.ensure_one()
        booked = self._ip_indiapost_shipments().filtered(
            lambda s: s.indiapost_article_number)
        if not booked:
            raise UserError(_(
                'No shipment in order %s has been booked with India Post yet.'
            ) % self.name)
        return booked.action_indiapost_fetch_label()

    def action_indiapost_sync_tracking(self):
        self.ensure_one()
        booked = self._ip_indiapost_shipments().filtered(
            lambda s: s.indiapost_article_number)
        return self.env['logistics.indiapost.tracking'].action_ip_sync_now(booked)
