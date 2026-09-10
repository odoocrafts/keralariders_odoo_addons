"""Shared fixtures so India Post tests do not depend on the database they meet.

A fresh install has the integration off. A production copy has live sandbox
credentials, a tariff cache and, until the seed function runs on upgrade, no
seeded offices. Tests that quote, debit or resolve an office must therefore
opt in here: set ``indiapost_enabled`` and credentials (or explicitly disable
them), seed any offices they need, flush the tariff cache, and patch
``_ip_request`` so a call can never leave the process. Ambient
``ir.config_parameter`` values are never trusted.
"""

import io
from types import SimpleNamespace
from unittest.mock import patch

from odoo.addons.keralariders_logistics.models.indiapost_client import (
    IndiapostApiError,
)
from odoo.tools.pdf import PdfFileReader, PdfFileWriter


CONFIG_PREFIX = 'keralariders_logistics.'
SP_CONTRACT = '41124829'
BP_CONTRACT = '41664688'
IP_NETWORK_BLOCKED = 'India Post network is blocked in tests'

# Offices the delivery-charge and contract fixtures actually book through.
TEST_OFFICES = (
    ('682001', '22360020', 'Kochi HO', 'ERNAKULAM'),
    ('695001', '22840005', 'DC Thiruvananthapuram GPO', 'THIRUVANANTHAPURAM'),
)

STUB_CREDENTIALS = (
    ('indiapost_enabled', 'True'),
    ('indiapost_environment', 'sandbox'),
    ('indiapost_username', '9999537187'),
    ('indiapost_password', 'kx_test_secret'),
    ('indiapost_customer_id', '9999537187'),
    ('indiapost_sp_contract_id', SP_CONTRACT),
    ('indiapost_bp_contract_id', BP_CONTRACT),
    ('indiapost_sender_name', 'KERALA XPRESS LOGISTICS'),
    ('indiapost_sender_company', 'KERALA XPRESS LOGISTICS'),
    ('indiapost_sender_address', 'Vazhiyambalam, Bypass NH66'),
    ('indiapost_sender_city', 'Thrissur'),
    ('indiapost_sender_state', 'Kerala'),
    ('indiapost_sender_pincode', '680681'),
    ('indiapost_sender_mobile', '9400662693'),
)


class IndiapostHermeticMixin:
    """Explicit India Post config, seeded offices, and a blocked HTTP client."""

    @classmethod
    def _ip_set_params(cls, pairs):
        params = cls.env['ir.config_parameter'].sudo()
        for key, value in pairs:
            params.set_param(CONFIG_PREFIX + key, value)

    @classmethod
    def _ip_enable_stub_credentials(cls):
        cls._ip_set_params(STUB_CREDENTIALS)

    @classmethod
    def _ip_disable(cls):
        cls._ip_set_params((
            ('indiapost_enabled', 'False'),
            ('indiapost_username', ''),
            ('indiapost_password', ''),
        ))

    @classmethod
    def _ip_seed_test_offices(cls, offices=TEST_OFFICES):
        """Pin the fixture pincodes to a seeded office so resolution is a
        cache read. Seeded rows never expire; a miss otherwise calls
        pincode-search, and no test here is about the API.
        """
        Office = cls.env['logistics.indiapost.office']
        for pincode, office_id, name, city in offices:
            vals = {
                'office_name': name,
                'office_type_code': 'HPO',
                'city_name': city,
                'state_name': 'KERALA',
                'delivery_office_flag': True,
                'is_bookable': True,
                'is_preferred': True,
                'source': 'seed',
                'last_synced': False,
            }
            office = Office.search([
                ('pincode', '=', pincode),
                ('office_id', '=', office_id),
            ], limit=1)
            if office:
                office.write(vals)
            else:
                Office.create(dict(vals, pincode=pincode, office_id=office_id))
            Office.search([
                ('pincode', '=', pincode),
                ('office_id', '!=', office_id),
            ]).write({'is_preferred': False})

    @classmethod
    def _ip_flush_tariff_cache(cls):
        cls.env['logistics.indiapost.tariff.cache'].sudo().search([]).unlink()

    @classmethod
    def _ip_block_network(cls):
        cls.startClassPatcher(patch.object(
            cls.registry['logistics.indiapost.client'],
            '_ip_request',
            side_effect=IndiapostApiError(IP_NETWORK_BLOCKED),
        ))

    @classmethod
    def _ip_ensure_test_barcode_range(cls):
        """A sandbox range tests can allocate from without touching the UAT block."""
        Range = cls.env['logistics.indiapost.barcode.range'].sudo()
        rng = Range.search([
            ('prefix', '=', 'TT'),
            ('environment', '=', 'sandbox'),
            ('start_serial', '=', 90000001),
        ], limit=1)
        if rng:
            return rng
        return Range.create({
            'name': 'TEST range TT (do not use in production)',
            'environment': 'sandbox',
            'prefix': 'TT',
            'start_serial': 90000001,
            'end_serial': 90009999,
            'next_serial': 90000001,
            'sequence': 0,
            'low_stock_threshold': 0,
        })

    @staticmethod
    def _ip_pdf_with_text(text):
        """Minimal PDF whose bytes contain ``(text)`` for sort-code parsing."""
        payload = (text or 'X').encode('ascii', 'replace')
        return (
            b'%PDF-1.4\n1 0 obj<<>>endobj\nstream\nBT ('
            + payload
            + b') Tj ET\nendstream\n%%EOF\n'
        )

    @staticmethod
    def _ip_blank_pdf_bytes():
        writer = PdfFileWriter()
        if hasattr(writer, 'add_blank_page'):
            writer.add_blank_page(width=595, height=842)
        else:
            writer.addBlankPage(width=595, height=842)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    @staticmethod
    def _ip_pdf_page_count(pdf_bytes):
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        if hasattr(reader, 'getNumPages'):
            return reader.getNumPages()
        return len(reader.pages)

    @staticmethod
    def _ip_stub_call(*args, **kwargs):
        """Booking + label responses with no HTTP and no live AWB consumption.

        ``call`` may be invoked as a bound model method ``(self, method, path,
        ...)`` or, when patched without autospec, as ``(method, path, ...)``.
        """
        operation = kwargs.get('operation')
        expect_pdf = kwargs.get('expect_pdf', False)
        body = kwargs.get('body')
        path = kwargs.get('path')
        if path is None:
            for arg in args:
                if isinstance(arg, str) and arg.startswith('/'):
                    path = arg
                    break
        if body is None:
            for arg in args:
                if isinstance(arg, (dict, list)):
                    body = arg
                    break
        is_label = (
            operation == 'label'
            or expect_pdf
            or (isinstance(path, str) and 'label' in path)
        )
        if is_label:
            return SimpleNamespace(
                status=200,
                payload=None,
                content=IndiapostHermeticMixin._ip_blank_pdf_bytes(),
                headers={'Content-Type': 'application/pdf'},
                is_pdf=True,
                attempts=1,
            )
        articles = body.get('articles') if isinstance(body, dict) else []
        valid = [{
            'index': index,
            'barcode_no': article.get('barcode_no'),
            'calculated_tariff': 52.0,
            'offset_number': 1,
            'block_number': 1,
        } for index, article in enumerate(articles or [])]
        return SimpleNamespace(
            status=200,
            payload={
                'success': True,
                'batch_id': 'TEST-BATCH',
                'correlation_id': 'TEST-CORR',
                'mail_booking_dom_id': 'TEST-MB',
                'valid_articles': valid,
                'error_articles': [],
            },
            content=b'{}',
            headers={},
            is_pdf=False,
            attempts=1,
        )

    def _ip_patch_call(self, side_effect=None):
        return patch.object(
            self.registry['logistics.indiapost.client'],
            'call',
            autospec=True,
            side_effect=side_effect or self._ip_stub_call,
        )

    @classmethod
    def _ip_make_hermetic(cls, enabled=True):
        if enabled:
            cls._ip_enable_stub_credentials()
        else:
            cls._ip_disable()
        cls._ip_seed_test_offices()
        cls._ip_flush_tariff_cache()
        cls._ip_ensure_test_barcode_range()
        cls._ip_block_network()
