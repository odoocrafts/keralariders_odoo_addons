"""res.config.settings only allows a short list of field types.

Opening Logistics / Settings calls ``default_get`` → ``_get_classified_fields``.
Any ``config_parameter`` field whose type is not boolean, integer, float, char,
selection, many2one or datetime raises and the form never renders. India Post
originally stored the consignor address as ``fields.Text``; this file exists so
that cannot come back.
"""

from odoo.tests import TransactionCase, tagged

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
