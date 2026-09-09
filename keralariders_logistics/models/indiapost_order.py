"""Order-level India Post actions.

A ``logistics.order`` is a seller's batch of shipments, which lines up neatly
with the bulk booking endpoint: one order becomes one ``articles`` array.
"""

import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .indiapost_client import IndiapostApiError
from .indiapost_shipment import quote_failure_reason

_logger = logging.getLogger(__name__)


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
        """Debit, request pickup, then book India Post shipments in the same step.

        Hub-network shipments are unchanged. Booking failures are recorded on
        the shipment and shown to the seller; they do not undo the wallet
        debit or the pickup request. The admin Book with India Post button
        remains the retry.
        """
        res = super().action_request_pickup()
        last_notify = None
        for order in self:
            errors = order._ip_autobook_after_pickup()
            indiapost = order._ip_indiapost_shipments()
            if not indiapost:
                continue
            booked = indiapost.filtered(
                lambda s: s.indiapost_booking_state == 'booked')
            if errors:
                last_notify = indiapost._ip_notify(
                    _('Pickup requested — India Post booking had errors'),
                    _('Pickup is requested and the wallet has been charged.\n\n%s')
                    % '\n'.join(errors),
                    kind='danger', sticky=True,
                )
            elif booked:
                last_notify = indiapost._ip_notify(
                    _('Pickup requested and booked with India Post'),
                    _('%s shipment(s) booked.') % len(booked),
                )
        return last_notify or res

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
