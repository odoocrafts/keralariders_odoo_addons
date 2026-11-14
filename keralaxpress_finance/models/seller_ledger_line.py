from odoo import fields, models, tools

from .cod_ledger_line import COD_CATEGORIES

WALLET_CATEGORIES = [
    ('recharge', 'Wallet recharge'),
    ('booking', 'Booking charge'),
    ('cancel_refund', 'Cancellation refund'),
    ('scan_adjustment', 'India Post rate adjustment'),
    ('cod_delivery_refund', 'Delivery charge refund on COD pickup'),
    ('cod_delivery_restore', 'Delivery charge restored on COD return'),
    ('manual', 'Manual adjustment'),
]

LEDGER_CATEGORIES = WALLET_CATEGORIES + COD_CATEGORIES

# The prefixes keralariders_logistics writes on wallet lines it posts for a
# shipment. The ledger reads them; it never writes wallet lines itself.
WALLET_CATEGORY_SQL = """
    CASE
        WHEN w.recharge_request_id IS NOT NULL
          OR EXISTS (
                SELECT 1 FROM logistics_wallet_recharge_request r
                 WHERE r.wallet_transaction_id = w.id)
          OR w.reference LIKE 'Recharge - %'
            THEN 'recharge'
        WHEN w.reference LIKE 'IP-CANCEL:%' THEN 'cancel_refund'
        WHEN w.reference LIKE 'IP-SCAN-ADJ:%' THEN 'scan_adjustment'
        WHEN w.reference LIKE 'KX-DLV-REFUND:%' THEN 'cod_delivery_refund'
        WHEN w.reference LIKE 'KX-DLV-RESTORE:%' THEN 'cod_delivery_restore'
        WHEN w.shipment_id IS NOT NULL AND w.amount < 0 THEN 'booking'
        ELSE 'manual'
    END
"""


class SellerLedgerLine(models.Model):
    """Seller bookkeeping: prepaid wallet and pending COD side by side.

    Wallet lines move only the wallet balance and COD lines only the COD
    balance. Both running balances are computed over the seller's full
    history, so a date filter never changes what a line's balance reads.
    """
    _name = 'kx.seller.ledger.line'
    _description = 'Seller Ledger Line'
    _auto = False
    _order = 'date desc, id desc'
    _rec_name = 'reference'

    seller_id = fields.Many2one('logistics.seller', string='Seller', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    source = fields.Selection(
        [('wallet', 'Wallet'), ('cod', 'COD')], string='Source', readonly=True)
    category = fields.Selection(LEDGER_CATEGORIES, string='Type', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    description = fields.Char(string='Description', readonly=True)
    shipment_id = fields.Many2one('logistics.shipment', string='Shipment', readonly=True)
    wallet_transaction_id = fields.Many2one(
        'logistics.wallet.transaction', string='Wallet Transaction', readonly=True)
    transfer_id = fields.Many2one(
        'logistics.account.transfer', string='COD Transfer', readonly=True)
    amount = fields.Monetary(string='Amount', readonly=True, currency_field='currency_id')
    credit = fields.Monetary(string='Credit', readonly=True, currency_field='currency_id')
    debit = fields.Monetary(string='Debit', readonly=True, currency_field='currency_id')
    entry_type = fields.Selection(
        [('credit', 'Credit'), ('debit', 'Debit')], string='Cr/Dr', readonly=True)
    wallet_amount = fields.Monetary(string='Wallet Movement', readonly=True, currency_field='currency_id')
    cod_amount = fields.Monetary(string='COD Movement', readonly=True, currency_field='currency_id')
    wallet_balance = fields.Monetary(
        string='Wallet Balance', readonly=True, currency_field='currency_id',
        aggregator=False)
    cod_balance = fields.Monetary(
        string='COD Balance', readonly=True, currency_field='currency_id',
        aggregator=False)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    def init(self):
        cod_query = self.env['kx.cod.ledger.line']._movements_query()
        company_currency = self.env.company.currency_id.id or 'NULL'
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                WITH lines AS (
                    SELECT w.id * 10 + 5 AS id,
                           wl.seller_id AS seller_id,
                           w.transaction_date AS date,
                           'wallet' AS source,
                           {WALLET_CATEGORY_SQL} AS category,
                           w.reference AS reference,
                           w.description AS description,
                           w.shipment_id AS shipment_id,
                           w.id AS wallet_transaction_id,
                           NULL::integer AS transfer_id,
                           w.amount AS amount,
                           w.amount AS wallet_amount,
                           0::numeric AS cod_amount,
                           COALESCE(w.currency_id, {company_currency}) AS currency_id
                      FROM logistics_wallet_transaction w
                      JOIN logistics_wallet wl ON wl.id = w.wallet_id
                    UNION ALL
                    SELECT c.id,
                           c.seller_id,
                           c.date,
                           'cod' AS source,
                           c.category,
                           c.reference,
                           c.description,
                           c.shipment_id,
                           NULL::integer AS wallet_transaction_id,
                           c.transfer_id,
                           c.amount,
                           0::numeric AS wallet_amount,
                           c.amount AS cod_amount,
                           c.currency_id
                      FROM ({cod_query}) c
                )
                SELECT l.*,
                       GREATEST(l.amount, 0) AS credit,
                       GREATEST(-l.amount, 0) AS debit,
                       CASE WHEN l.amount >= 0 THEN 'credit' ELSE 'debit' END AS entry_type,
                       SUM(l.wallet_amount) OVER w AS wallet_balance,
                       SUM(l.cod_amount) OVER w AS cod_balance
                  FROM lines l
                WINDOW w AS (
                    PARTITION BY l.seller_id
                    ORDER BY l.date, l.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                )
            )
        """)
