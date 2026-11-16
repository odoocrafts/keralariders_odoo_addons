from odoo import fields, models, tools

from odoo.addons.keralariders_logistics.models.account import (
    _COD_CREDIT_TYPES,
    _COD_SETTLEMENT_TYPES,
)

COD_CATEGORIES = [
    ('cod_collected', 'COD collected'),
    ('cod_pickup_advance', 'COD credited on pickup'),
    ('cod_return', 'COD reversed on return'),
    ('cod_settlement', 'COD settled to seller'),
    ('cod_payout', 'COD payout to bank'),
    ('cod_charge', 'COD approval deduction'),
]


def _sql_list(values):
    return ', '.join("'%s'" % v for v in values)


class CodLedgerLine(models.Model):
    """One signed movement of a seller's pending COD.

    Mirrors ``logistics.account.transfer._seller_cod_balance_parts``: posted
    credit types add, posted settlement types subtract. A COD withdrawal is
    split in two so the settlement charge kept by the company shows on its
    own line; together they still take the full requested amount off the
    balance, exactly as the withdrawal does.
    """
    _name = 'kx.cod.ledger.line'
    _description = 'COD Ledger Line'
    _auto = False
    _order = 'date desc, id desc'
    _rec_name = 'reference'

    transfer_id = fields.Many2one('logistics.account.transfer', string='Transfer', readonly=True)
    seller_id = fields.Many2one('logistics.seller', string='Seller', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    category = fields.Selection(COD_CATEGORIES, string='Type', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    description = fields.Char(string='Description', readonly=True)
    shipment_id = fields.Many2one('logistics.shipment', string='Shipment', readonly=True)
    amount = fields.Monetary(string='Amount', readonly=True, currency_field='currency_id')
    credit = fields.Monetary(string='Credit', readonly=True, currency_field='currency_id')
    debit = fields.Monetary(string='Debit', readonly=True, currency_field='currency_id')
    balance = fields.Monetary(
        string='COD Balance', readonly=True, currency_field='currency_id',
        aggregator=False,
        help='Seller pending COD after this line, over the full history.',
    )
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)
    fulfilment_method = fields.Selection(
        [
            ('own_network', 'KeralaXpress'),
            ('indiapost', 'India Post'),
        ],
        string='Carrier',
        readonly=True,
    )

    def _movements_query(self):
        """Signed COD movements, one row each, without the running balance."""
        credit_types = _sql_list(_COD_CREDIT_TYPES)
        settlement_types = _sql_list(
            [t for t in _COD_SETTLEMENT_TYPES if t != 'cod_withdrawal'])
        company_currency = self.env.company.currency_id.id or 'NULL'
        return f"""
            SELECT t.id * 10 + 1 AS id,
                   t.id AS transfer_id,
                   t.related_seller_id AS seller_id,
                   t.transfer_date AS date,
                   CASE t.transfer_type
                        WHEN 'cod_payment' THEN 'cod_collected'
                        WHEN 'cod_pickup' THEN 'cod_pickup_advance'
                        WHEN 'cod_return' THEN 'cod_return'
                        WHEN 'cod_withdrawal' THEN 'cod_payout'
                        ELSE 'cod_settlement'
                   END AS category,
                   t.name AS reference,
                   COALESCE(t.reference, t.description) AS description,
                   t.shipment_id AS shipment_id,
                   CASE
                        WHEN t.transfer_type IN ({credit_types}) THEN t.amount
                        WHEN t.transfer_type = 'cod_withdrawal'
                            THEN -(t.amount - COALESCE(t.cod_charge_amount, 0))
                        ELSE -t.amount
                   END AS amount,
                   COALESCE(t.currency_id, {company_currency}) AS currency_id,
                   s.fulfilment_method AS fulfilment_method
              FROM logistics_account_transfer t
              JOIN logistics_seller s ON s.id = t.related_seller_id
             WHERE t.state = 'posted'
               AND t.related_seller_id IS NOT NULL
               AND t.transfer_type IN ({credit_types}, {settlement_types}, 'cod_withdrawal')
            UNION ALL
            SELECT t.id * 10 + 2 AS id,
                   t.id AS transfer_id,
                   t.related_seller_id AS seller_id,
                   t.transfer_date AS date,
                   'cod_charge' AS category,
                   t.name AS reference,
                   'COD settlement charge '
                       || ROUND(COALESCE(t.cod_charge_percent, 0)::numeric, 2)::text
                       || '%' AS description,
                   t.shipment_id AS shipment_id,
                   -t.cod_charge_amount AS amount,
                   COALESCE(t.currency_id, {company_currency}) AS currency_id,
                   s.fulfilment_method AS fulfilment_method
              FROM logistics_account_transfer t
              JOIN logistics_seller s ON s.id = t.related_seller_id
             WHERE t.state = 'posted'
               AND t.related_seller_id IS NOT NULL
               AND t.transfer_type = 'cod_withdrawal'
               AND COALESCE(t.cod_charge_amount, 0) > 0
        """

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT m.*,
                       GREATEST(m.amount, 0) AS credit,
                       GREATEST(-m.amount, 0) AS debit,
                       SUM(m.amount) OVER (
                           PARTITION BY m.seller_id
                           ORDER BY m.date, m.id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS balance
                  FROM ({self._movements_query()}) m
            )
        """)
