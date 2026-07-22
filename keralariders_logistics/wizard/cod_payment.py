from odoo import models, fields, api

class CODPaymentWizard(models.TransientModel):
    _name = "logistics.cod.payment.wizard"

    shipment_id = fields.Many2one('logistics.shipment', string="Shipment", required=True)
    seller_id = fields.Many2one('logistics.seller', string="Seller", required=True)
    amount = fields.Monetary(string="Amount")
    @api.onchange('amount')
    def _onchange_amount(self):
        # Force amount to be positive
        if self.amount < 0:
            self.amount = -self.amount

    from_account_id = fields.Many2one('logistics.account', string="From Account", required=True)
    to_account_id = fields.Many2one('logistics.account', string="To Account", required=True)
    date = fields.Date(string="Date", required=True, default=fields.Date.context_today)
    reference = fields.Text(string="Reference")
    def action_create_transfer(self):
        transfer = self.env['logistics.account.transfer'].create({
            'from_account_id': self.from_account_id.id,
            'to_account_id': self.to_account_id.id,
            'transfer_date': self.date,
            'amount': self.amount,
            'reference': self.reference,
            'related_seller_id': self.seller_id.id,
            'shipment_id': self.shipment_id.id
        })
        self.shipment_id.cod_payment_transfer_ids = [(4, transfer.id)]

    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
