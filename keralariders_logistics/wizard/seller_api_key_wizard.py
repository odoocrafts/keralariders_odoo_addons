from odoo import fields, models


class SellerApiKeyWizard(models.TransientModel):
    """One-time display of an API key and secret minted from a seller profile.

    The secret is hashed on the credential before this dialog opens. Closing
    the dialog is the last time the plaintext can be copied.
    """

    _name = 'logistics.seller.api.key.wizard'
    _description = 'Seller API Key'

    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', required=True, readonly=True,
    )
    credential_id = fields.Many2one(
        'logistics.seller.api.credential', string='Credential',
        required=True, readonly=True,
    )
    api_key = fields.Char(string='API Key', readonly=True)
    api_secret = fields.Char(string='API Secret', readonly=True)
    replaced_active_key = fields.Boolean(readonly=True)
