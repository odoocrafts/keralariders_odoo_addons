"""res.config.settings only allows a short list of field types.

Opening Logistics / Settings calls ``default_get`` → ``_get_classified_fields``.
Any ``config_parameter`` field whose type is not boolean, integer, float, char,
selection, many2one or datetime raises and the form never renders. India Post
originally stored the consignor address as ``fields.Text``; this file exists so
that cannot come back.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.indiapost_client import (
    CONFIG_PREFIX,
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


ALLOWED_CONFIG_TYPES = (
    'boolean', 'integer', 'float', 'char', 'selection', 'many2one', 'datetime',
)


@tagged('post_install', '-at_install')
class TestIndiapostSettings(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_disable()
        cls._ip_block_network()

    def test_indiapost_config_parameter_types_are_classified(self):
        Settings = self.env['res.config.settings']
        fields = Settings._fields

        self.assertEqual(
            fields['indiapost_sender_address'].type, 'char',
            'Consignor address must be Char: Text crashes Settings on open.',
        )

        illegal = []
        for name, field in fields.items():
            if not name.startswith('indiapost_'):
                continue
            if not getattr(field, 'config_parameter', None):
                continue
            if field.type not in ALLOWED_CONFIG_TYPES:
                illegal.append('%s (%s)' % (name, field.type))
        self.assertFalse(
            illegal,
            'India Post settings fields with illegal types: %s' % ', '.join(illegal),
        )

        # This is the production crash path: opening the form.
        Settings.default_get(list(Settings.fields_get()))
        Settings._get_classified_fields()

    def test_webhook_urls_follow_web_base_url(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://erp.keralaxpress.com')
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(
            settings.indiapost_booking_webhook_url,
            'https://erp.keralaxpress.com/indiapost/bookingeventwebhook',
        )
        self.assertEqual(
            settings.indiapost_other_webhook_url,
            'https://erp.keralaxpress.com/indiapost/othereventwebhook',
        )
        self.assertFalse(getattr(
            settings._fields['indiapost_booking_webhook_url'],
            'config_parameter', None))
        self.assertTrue(settings._fields['indiapost_webhooks_enabled'].type
                         in ('boolean',))

    def test_production_environment_uses_the_documented_host(self):
        """Selecting production must not keep the UAT host as the base URL."""
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(CONFIG_PREFIX + 'indiapost_environment', 'production')
        params.set_param(CONFIG_PREFIX + 'indiapost_base_url', SANDBOX_BASE_URL)

        settings = self.env['logistics.indiapost.client']._ip_settings()
        self.assertEqual(settings['indiapost_base_url'], PRODUCTION_BASE_URL)

        wizard = self.env['res.config.settings'].new({
            'indiapost_environment': 'sandbox',
            'indiapost_base_url': PRODUCTION_BASE_URL,
        })
        wizard._onchange_indiapost_environment()
        self.assertEqual(wizard.indiapost_base_url, SANDBOX_BASE_URL)

        wizard.indiapost_environment = 'production'
        wizard._onchange_indiapost_environment()
        self.assertEqual(wizard.indiapost_base_url, PRODUCTION_BASE_URL)

    def test_a_custom_base_url_is_left_alone(self):
        custom = 'https://proxy.example.invalid/beextcustomer'
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(CONFIG_PREFIX + 'indiapost_environment', 'production')
        params.set_param(CONFIG_PREFIX + 'indiapost_base_url', custom)

        settings = self.env['logistics.indiapost.client']._ip_settings()
        self.assertEqual(settings['indiapost_base_url'], custom)

        wizard = self.env['res.config.settings'].new({
            'indiapost_environment': 'production',
            'indiapost_base_url': custom,
        })
        wizard._onchange_indiapost_environment()
        self.assertEqual(wizard.indiapost_base_url, custom)

