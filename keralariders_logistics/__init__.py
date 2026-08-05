from . import models
from . import controllers
from . import wizard


def _post_init_assign_hub_pincodes(env):
    """Assign all district pincodes to their hubs after install/upgrade."""
    env['logistics.hub'].assign_all_hub_pincodes()
    # Ensure hub cash accounts exist for COD deposits
    for hub in env['logistics.hub'].search([]):
        hub.get_or_create_cash_account()
    # Prefer seeded company COD account in settings when unset
    ICP = env['ir.config_parameter'].sudo()
    if not ICP.get_param('keralariders_logistics.company_cod_account_id'):
        company_account = env['logistics.account'].search([('account_type', '=', 'company')], limit=1)
        if company_account:
            ICP.set_param('keralariders_logistics.company_cod_account_id', company_account.id)
