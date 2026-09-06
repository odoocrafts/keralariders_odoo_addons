"""Order-level India Post actions.

A ``logistics.order`` is a seller's batch of shipments, which lines up neatly
with the bulk booking endpoint: one order becomes one ``articles`` array.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError


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
