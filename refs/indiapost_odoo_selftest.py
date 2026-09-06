"""Offline self-test for the India Post integration, run inside `odoo shell`.

Exercises everything that does not need the India Post API: seed data, barcode
allocation and its check digit, packaging validation, the admin-only fulfilment
method guard, view rendering and the cron records. The API-facing behaviour is
verified separately by the probe scripts in this folder, which must run from the
whitelisted server.

Usage (from a host that can reach the Odoo container):

    odoo shell -d <db> --no-http < refs/indiapost_odoo_selftest.py
"""

import traceback

RESULTS = []


def check(label, fn):
    try:
        detail = fn()
        RESULTS.append(('PASS', label, detail or ''))
    except Exception as exc:  # noqa: BLE001 - a self-test reports, never raises
        RESULTS.append(('FAIL', label, '%s: %s' % (type(exc).__name__, exc)))
        traceback.print_exc()


ipc = __import__(
    'odoo.addons.keralariders_logistics.models.indiapost_common',
    fromlist=['x'],
)


IP_NETWORK_BLOCKED = 'India Post network is blocked in the self-test'


def _block_indiapost_network():
    """Patch the client so nothing can reach test.cept.gov.in.

    Returns a restore callable. Odoo shell is not under the test HTTP guard,
    so an unpatched requote on a production copy would hit the live sandbox.
    """
    from odoo.addons.keralariders_logistics.models.indiapost_client import (
        IndiapostApiError,
    )
    Client = type(env['logistics.indiapost.client'])
    original = Client._ip_request

    def _blocked(self, *args, **kwargs):
        raise IndiapostApiError(IP_NETWORK_BLOCKED)

    Client._ip_request = _blocked
    return lambda: setattr(Client, '_ip_request', original)


def seeded_offices():
    """The 14 Kerala HQ offices must exist; seed them if an upgrade has not.

    ``_ip_seed_kerala_offices`` is idempotent. Calling it here makes the
    check pass on a production copy that was installed before the seed left
    noupdate, and still asserts the real count afterwards.
    """
    Office = env['logistics.indiapost.office']
    created = Office._ip_seed_kerala_offices()
    offices = Office.search([('source', '=', 'seed')])
    assert len(offices) == 14, 'expected 14 seeded offices, got %d' % len(offices)
    kochi = offices.filtered(lambda o: o.pincode == '682001')
    assert kochi.office_id == '22360020', kochi.office_id
    assert all(o.is_bookable for o in offices), 'some seeded offices not bookable'
    extra = ', seeded %d this run' % created if created else ''
    return '14 district HQ offices, Kochi resolves to %s%s' % (
        kochi.office_id, extra)


def check_digit():
    # The vendor's own worked example.
    assert ipc.barcode_check_digit('47312482') == '9'
    assert ipc.build_barcode('ET', 21433001) == 'ET214330016IN'
    assert ipc.build_barcode('ET', 21434000) == 'ET214340000IN'
    assert ipc.barcode_is_wellformed('ET214330016IN')
    assert not ipc.barcode_is_wellformed('ET214330017IN')
    return 'serial 47312482 -> 9; range ends ET214330016IN..ET214340000IN'


def allocation():
    Range = env['logistics.indiapost.barcode.range']
    rng = env.ref('keralariders_logistics.indiapost_barcode_range_uat')
    before = rng.next_serial
    first = Range.allocate()
    second = Range.allocate()
    assert first.barcode != second.barcode, 'allocator issued a duplicate'
    assert first.barcode == ipc.build_barcode('ET', before), first.barcode
    assert rng.next_serial == before + 2, rng.next_serial
    issued = (first.barcode, second.barcode)
    # Roll back so the self-test does not consume real UAT barcodes.
    (first + second).unlink()
    rng.write({'next_serial': before})
    return 'issued %s then %s, counter restored to %d' % (
        issued[0], issued[1], before)


def packaging_rules():
    # The rule that bites: heavy and small cannot travel by Speed Post.
    errors, _w = ipc.validate_package(600, 10, 5, 5)
    assert errors and 'at least 14 cm x 9 cm' in errors[0], errors
    # The undocumented total limit.
    errors, _w = ipc.validate_package(1000, 150, 100, 60)
    assert any('300 cm' in e for e in errors), errors
    # A legitimate parcel passes.
    errors, _w = ipc.validate_package(1500, 30, 20, 15)
    assert not errors, errors
    assert ipc.volumetric_weight_g(30, 20, 15) == 1800
    assert ipc.chargeable_weight_g(1500, 30, 20, 15) == 1800
    # Volumetric weight applies to parcels only. A bulky 250 g document has a
    # volumetric weight of 464 g but India Post bills the 250 g.
    assert ipc.volumetric_weight_g(40, 29, 2) == 464
    assert ipc.chargeable_weight_g(250, 40, 29, 2) == 250
    return ('600 g at 10x5x5 rejected; 1.5 kg at 30x20x15 charges 1800 g; '
            'bulky documents still charge actual weight')


def conversions():
    assert ipc.kg_to_grams(0.2505) == 251, ipc.kg_to_grams(0.2505)
    assert ipc.kg_to_grams(1.5) == 1500, ipc.kg_to_grams(1.5)
    # Never zero: the tariff endpoint rejects a weightless article.
    assert ipc.kg_to_grams(0) == 1
    assert ipc.resolve_product_code(500) == 'SP_INLAND_DOC'
    assert ipc.resolve_product_code(501) == 'SP_INLAND_PARCEL'
    assert ipc.resolve_shape(200) == 'DOC'
    assert ipc.resolve_shape(800) == 'NROL'
    assert ipc.resolve_shape(800, cylindrical=True) == 'ROLL'
    assert ipc.normalize_mobile('+91 98765 43210') == '9876543210'
    assert ipc.normalize_pincode('682001') == '682001'
    for bad in ('68200', '9999999', 'abcdef'):
        try:
            ipc.normalize_pincode(bad)
        except ipc.IndiapostDataError:
            pass
        else:
            raise AssertionError('accepted bad pincode %r' % bad)
    return 'grams round up, 500 g is a document, 501 g a parcel'


def pickup_format():
    import datetime
    moment = datetime.datetime(2026, 12, 15, 13, 0, 0)
    assert ipc.format_pickup_datetime(moment) == '12/15/2026 01:00:00 PM', \
        ipc.format_pickup_datetime(moment)
    slot_start = ipc.pickup_datetime(datetime.date(2026, 12, 15), '13:00-16:00')
    assert slot_start.hour == 13, slot_start
    return 'MM/DD/YYYY hh:mm:ss AM/PM, slot start honoured'


def admin_only_fulfilment():
    from odoo.exceptions import AccessError
    partner = env['res.partner'].create({'name': 'Self-test Seller'})
    seller = env['logistics.seller'].create({
        'name': 'Self-test Seller',
        'partner_id': partner.id,
        'zip': '682001',
    })
    assert seller.fulfilment_method == 'indiapost', seller.fulfilment_method

    portal_user = env['res.users'].create({
        'name': 'Self-test Portal',
        'login': 'kx_selftest_portal',
        'group_ids': [(6, 0, [env.ref('base.group_portal').id])],
    })
    as_portal = seller.with_user(portal_user).sudo()
    try:
        as_portal.write({'fulfilment_method': 'own_network'})
    except AccessError:
        outcome = 'AccessError raised even under sudo()'
    else:
        raise AssertionError('a portal user changed the fulfilment method')

    # An administrator can.
    seller.write({'fulfilment_method': 'own_network'})
    assert seller.fulfilment_method == 'own_network'
    seller.write({'fulfilment_method': 'indiapost'})
    return outcome


def shipment_defaults():
    seller = env['logistics.seller'].search(
        [('name', '=', 'Self-test Seller')], limit=1)
    district = env['logistics.district'].search([], limit=1)
    shipment = env['logistics.shipment'].create({
        'seller_id': seller.id,
        'shipping_to_name': 'Self-test Customer',
        'shipping_to_address': '12 Test Road, Test Nagar',
        'shipping_to_zip': '695001',
        'shipping_to_mobile': '9876543210',
        'shipping_to_district_id': district.id,
        'item_description': 'Self-test article',
        'total_weight': 1.5,
        'length_cm': 30,
        'breadth_cm': 20,
        'height_cm': 15,
    })
    assert shipment.fulfilment_method == 'indiapost', shipment.fulfilment_method
    assert shipment.is_indiapost
    assert shipment.volumetric_weight_g == 1800, shipment.volumetric_weight_g
    assert shipment.chargeable_weight_g == 1800
    assert shipment.indiapost_product_code == 'SP_INLAND_PARCEL'
    assert shipment.indiapost_shape == 'NROL'
    assert shipment.indiapost_booking_state == 'to_book'
    assert shipment.indiapost_needs_quote

    from odoo.exceptions import ValidationError
    try:
        shipment.write({'total_weight': 0.6, 'length_cm': 10,
                        'breadth_cm': 5, 'height_cm': 5})
    except ValidationError:
        guard = 'small-heavy package refused by the model constraint'
    else:
        raise AssertionError('a 600 g 10x5x5 shipment was accepted')

    shipment.unlink()
    return guard


def own_network_pricing_untouched():
    """A hub-network shipment must still price off the slab table."""
    seller = env['logistics.seller'].search(
        [('name', '=', 'Self-test Seller')], limit=1)
    seller.write({'fulfilment_method': 'own_network'})
    district = env['logistics.district'].search([], limit=1)
    shipment = env['logistics.shipment'].create({
        'seller_id': seller.id,
        'shipping_to_name': 'Self-test Customer',
        'shipping_to_address': '12 Test Road, Test Nagar',
        'shipping_to_zip': '695001',
        'shipping_to_mobile': '9876543210',
        'shipping_to_district_id': district.id,
        'item_description': 'Self-test article',
        'total_weight': 1.5,
    })
    assert shipment.fulfilment_method == 'own_network'
    assert not shipment.is_indiapost
    assert shipment.indiapost_booking_state == 'not_required'
    assert not shipment.indiapost_needs_quote
    charge = shipment.delivery_charges_total
    slab = env['logistics.delivery.charges'].calculate_delivery_charge(
        1.5, shipment.shipping_from_district_id == shipment.shipping_to_district_id,
        package_id=None)
    assert abs(charge - slab) < 0.01, (charge, slab)
    shipment.unlink()
    seller.write({'fulfilment_method': 'indiapost'})
    return 'slab price %.2f preserved for hub-network sellers' % slab


def _selftest_seller():
    return env['logistics.seller'].search(
        [('name', '=', 'Self-test Seller')], limit=1)


def _selftest_portal_user():
    """The portal user created by :func:`admin_only_fulfilment`."""
    user = env['res.users'].search([('login', '=', 'kx_selftest_portal')], limit=1)
    assert user, 'the portal self-test user is missing'
    return user


def _shipment_vals(seller, **overrides):
    """A shipment India Post will accept: 1.5 kg at 30 x 20 x 15 cm."""
    district = env['logistics.district'].search([], limit=1)
    vals = {
        'seller_id': seller.id,
        'shipping_to_name': 'Self-test Customer',
        'shipping_to_address': '12 Test Road, Test Nagar',
        'shipping_to_zip': '695001',
        'shipping_to_mobile': '9876543210',
        'shipping_to_district_id': district.id,
        'item_description': 'Self-test article',
        'total_weight': 1.5,
        'length_cm': 30,
        'breadth_cm': 20,
        'height_cm': 15,
    }
    vals.update(overrides)
    return vals


def shipment_fulfilment_method_guard():
    """A seller must not be able to pick their own carrier per shipment.

    Regression for the hole that let an India Post seller create or update a
    shipment with fulfilment_method='own_network' over RPC, skipping the
    dimension and packaging validation and paying the cheaper weight slab
    instead of the live postal tariff. sudo() is used throughout because that
    is what the portal controllers do, and sudo() leaves env.user as the real
    user, which is what both guards check.
    """
    from odoo.exceptions import AccessError
    seller = _selftest_seller()
    seller.write({'fulfilment_method': 'indiapost'})
    portal_user = _selftest_portal_user()
    Shipment = env['logistics.shipment']

    # create: a caller-supplied carrier is discarded, not honoured...
    smuggled = Shipment.with_user(portal_user).sudo().create(
        _shipment_vals(seller, fulfilment_method='own_network'))
    assert smuggled.fulfilment_method == 'indiapost', \
        'a portal user chose their own carrier on create'
    assert smuggled.indiapost_booking_state == 'to_book', \
        smuggled.indiapost_booking_state

    # ...and the create still succeeds, because /my/shipments/create and the
    # bulk upload are legitimate portal creates.
    plain = Shipment.with_user(portal_user).sudo().create(_shipment_vals(seller))
    assert plain.fulfilment_method == 'indiapost'

    # write: refused outright, because a change cannot be silently corrected.
    try:
        smuggled.with_user(portal_user).sudo().write(
            {'fulfilment_method': 'own_network'})
    except AccessError:
        pass
    else:
        raise AssertionError('a portal user changed a shipment carrier')
    assert smuggled.fulfilment_method == 'indiapost'

    # An administrator can do both. with_env is needed because a recordset
    # created through with_user stays bound to that user.
    pinned = Shipment.create(
        _shipment_vals(seller, fulfilment_method='own_network'))
    assert pinned.fulfilment_method == 'own_network', \
        'an administrator could not pin the carrier on create'
    as_admin = smuggled.with_env(env)
    as_admin.write({'fulfilment_method': 'own_network'})
    assert as_admin.fulfilment_method == 'own_network'

    # And the admin-only "Divert to Hub Network" button still works, since it
    # writes the very field the guard protects.
    divert = Shipment.create(_shipment_vals(seller))
    assert divert.fulfilment_method == 'indiapost'
    divert.action_indiapost_switch_to_own_network()
    assert divert.fulfilment_method == 'own_network', 'divert button broken'
    assert divert.indiapost_booking_state == 'not_required'

    (as_admin + plain.with_env(env) + pinned + divert).unlink()
    return ('portal create silently resolves from the seller, portal write '
            'raises AccessError, admin create/write and the divert button work')


def quote_signature_staleness():
    """A stored India Post price must expire the moment its inputs change.

    Regression for the wallet being debited on an obsolete tariff: the old
    check compared only the banded weight, so a dimension edit (volumetric
    weight dominates - 600 g at 30x20x15 bills as 1800 g), an insurance
    declaration (~6% of value) or a VAS toggle all went unnoticed.
    """
    from odoo import fields as odoo_fields
    from odoo.exceptions import UserError
    seller = _selftest_seller()
    seller.write({'fulfilment_method': 'indiapost'})
    shipment = env['logistics.shipment'].create(_shipment_vals(seller))
    assert shipment.indiapost_needs_quote, 'an unquoted shipment looked priced'

    def store_quote():
        """Exactly what _ip_quote_and_store writes, without calling the API."""
        shipment.write({
            'indiapost_base_tariff': 100.0,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': 18.0,
            'indiapost_total_tariff': 118.0,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': odoo_fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })

    store_quote()
    assert not shipment.indiapost_needs_quote, 'a fresh quote looked stale'
    # The banded weight is unchanged by all of these, which is precisely why
    # the old weight-only comparison missed them.
    banded = shipment.indiapost_quoted_weight_g
    invalidators = [
        ('a dimension change', {'height_cm': 25}),
        ('an insurance declaration', {'indiapost_insurance_value': 50000}),
        ('a VAS toggle', {'indiapost_vas_pod': True}),
        ('another VAS toggle', {'indiapost_vas_reg': True}),
        ('a destination pincode change', {'shipping_to_zip': '673001'}),
    ]
    for label, vals in invalidators:
        store_quote()
        assert not shipment.indiapost_needs_quote, label
        shipment.write(vals)
        assert shipment.indiapost_needs_quote, \
            '%s did not invalidate the stored quote' % label
        assert ipc.band_weight(ipc.kg_to_grams(shipment.total_weight)) == banded, \
            'the banded weight moved, so this case proves nothing'

    # A weight change still invalidates, and only once it crosses a band.
    store_quote()
    shipment.write({'total_weight': 1.501})
    assert shipment.indiapost_needs_quote, 'a weight change was missed'

    # The wallet must not be debited while the signature does not match. The
    # HTTP client is blocked (and the tariff cache emptied) so the forced
    # re-quote fails even on a production copy with live credentials and a
    # funded wallet. That failure has to stop the debit.
    env['logistics.indiapost.tariff.cache'].sudo().search([]).unlink()
    params = env['ir.config_parameter'].sudo()
    params.set_param('keralariders_logistics.indiapost_enabled', 'True')
    params.set_param('keralariders_logistics.indiapost_username', 'kx_test_user')
    params.set_param('keralariders_logistics.indiapost_password', 'kx_test_secret')
    assert shipment.indiapost_needs_quote
    try:
        shipment.action_add_wallet_transaction()
    except UserError as exc:
        message = exc.args[0] if exc.args else str(exc)
        assert 'out of date' in message, message
        assert IP_NETWORK_BLOCKED in message, message
    else:
        raise AssertionError('a wallet was debited on a stale India Post rate')
    assert not shipment.wallet_transaction_id, 'a stale debit got through'

    # An article India Post has already booked and priced is exempt: their
    # calculated tariff is the number that was actually charged.
    store_quote()
    shipment.sudo().write({
        'indiapost_article_number': 'ET214330016IN',
        'indiapost_booking_state': 'booked',
    })
    shipment.write({'height_cm': 30})
    assert not shipment.indiapost_needs_quote, \
        'a booked article was queued for a pointless re-quote'

    signature = shipment._ip_quote_signature()
    shipment.sudo().write({'indiapost_article_number': False})
    shipment.unlink()
    return 'signature %s; 6 input changes invalidate, booked articles exempt' \
        % signature


def indiapost_skips_keralaxpress_pickup():
    """India Post collects from the seller, so no KeralaXpress DE is assigned.

    Regression for phantom pickup tasks: both auto-assignment call sites used
    to run for every shipment regardless of carrier.
    """
    from odoo.exceptions import UserError
    seller = _selftest_seller()
    seller.write({'fulfilment_method': 'indiapost'})
    Shipment = env['logistics.shipment']
    pincode = env['logistics.pincode'].search(
        [('name', '=', (seller.zip or '').strip())], limit=1)
    assert pincode, 'no logistics.pincode for the self-test seller origin'
    # Production already has pickup DEs on 682001. Take exclusive coverage
    # of this pincode so auto-assignment is deterministic; the transaction
    # rolls back at the end of the self-test.
    others = env['logistics.delivery.executive'].search([
        ('assigned_pickup_pincodes', 'in', pincode.ids),
    ])
    for other in others:
        other.write({'assigned_pickup_pincodes': [(3, pincode.id)]})
    de = env['logistics.delivery.executive'].create({
        'name': 'Self-test Pickup DE',
        'mobile': '9000006820',
        'is_pickup': True,
        'assigned_pickup_pincodes': [(6, 0, pincode.ids)],
    })

    def pickup_leg(shipment):
        return shipment.estimated_route_ids.filtered(
            lambda l: l.operation_type == 'pickup')[:1]

    indiapost = Shipment.create(
        _shipment_vals(seller, state='order_added'))
    assert indiapost.fulfilment_method == 'indiapost'
    assert not indiapost.pickup_executive_id, \
        'an India Post shipment was given a KeralaXpress pickup executive'
    assert not pickup_leg(indiapost).assigned_de_id, \
        'an India Post pickup leg was assigned to a KeralaXpress executive'
    assert not pickup_leg(indiapost).executive1_id, \
        'an India Post pickup leg still suggests a KeralaXpress executive'

    # The hub network is untouched: same seller, same pincode, own_network.
    own = Shipment.create(_shipment_vals(
        seller, state='order_added', fulfilment_method='own_network'))
    assert own.pickup_executive_id == de, \
        'a hub-network shipment lost its automatic pickup executive'
    assert pickup_leg(own).assigned_de_id == de, \
        'a hub-network pickup leg lost its assignment'

    # Requesting pickup on an order must not assign one either (order.py).
    order = env['logistics.order'].create({'seller_id': seller.id})
    via_order = Shipment.create(_shipment_vals(
        seller, state='order_added', order_id=order.id))
    via_order._needs_keralaxpress_pickup()._auto_assign_pickup_executive()
    assert not via_order.pickup_executive_id, \
        'the order pickup-request path assigned an India Post pickup'

    # The hub manager's assign action refuses rather than assigning silently.
    try:
        indiapost.action_assign_pickup_executive(de)
    except UserError as exc:
        message = exc.args[0] if exc.args else str(exc)
        assert 'India Post' in message, message
    else:
        raise AssertionError('a KeralaXpress pickup was assigned to India Post')
    own.action_assign_pickup_executive(de)

    # ...and the pending-pickup queue at /my/hub/pickups leaves them out,
    # while hub-network shipments stay listed.
    from odoo.addons.keralariders_logistics.controllers.portal import (
        LogisticsPortal,
    )
    hubs = indiapost.source_hub_id | own.source_hub_id
    assert hubs, 'the self-test shipments resolved to no source hub'
    queue = Shipment.search(LogisticsPortal()._hub_pending_pickup_domain(hubs))
    assert indiapost not in queue, \
        'an India Post shipment sits in the pending-pickup queue'
    assert own in queue, 'a hub-network shipment fell out of the queue'

    # A return journey is ours to collect whatever the carrier took it out.
    indiapost.write({'is_return_journey': True})
    assert indiapost._needs_keralaxpress_pickup() == indiapost, \
        'a returning India Post shipment cannot be collected by anyone'
    indiapost.write({'is_return_journey': False})

    (indiapost + own + via_order).unlink()
    order.unlink()
    de.unlink()
    return ('India Post gets no pickup DE, no leg assignment and no queue '
            'entry; hub network unchanged; returns still collectable')


def views_render():
    """Every view we touched or added must compile against the real fields."""
    problems = []
    checked = 0
    for model in ('logistics.shipment', 'logistics.order', 'logistics.seller',
                  'res.config.settings', 'logistics.indiapost.barcode.range',
                  'logistics.indiapost.barcode', 'logistics.indiapost.office',
                  'logistics.indiapost.tariff.cache', 'logistics.indiapost.log'):
        for view_type in ('form', 'list', 'search'):
            try:
                env[model].get_view(view_type=view_type)
                checked += 1
            except Exception as exc:  # noqa: BLE001
                problems.append('%s/%s: %s' % (model, view_type, exc))
    assert not problems, problems
    return '%d views compiled' % checked


def calculator_template_compiles():
    """The calculator template must compile, with both pricing branches present.

    Only the template is compiled here. Rendering it needs an HTTP request,
    because the portal layout calls request.csrf_token(); the rendered page is
    checked separately with a real request against /my/calculator.
    """
    view = env.ref('keralariders_logistics.portal_my_calculator')
    env['ir.qweb']._compile(view.id)
    arch = view.arch_db
    for marker in ('origin_pincode', 'dest_pincode', 'length_cm', 'breadth_cm',
                   'height_cm', 'insurance_value', 'origin_district_id',
                   'total_payable', 'chargeable_weight_g'):
        assert marker in arch, 'calculator template is missing %s' % marker
    shipment_form = env.ref('keralariders_logistics.portal_my_shipment_new')
    env['ir.qweb']._compile(shipment_form.id)
    for marker in ('length_cm', 'breadth_cm', 'height_cm',
                   'indiapost_pickup_slot', 'indiapost_pickup_date'):
        assert marker in shipment_form.arch_db, \
            'shipment form is missing %s' % marker
    return 'both templates compile and carry the India Post inputs'


def crons_present():
    names = []
    for ref in ('cron_indiapost_tracking_sync', 'cron_indiapost_office_refresh',
                'cron_indiapost_housekeeping'):
        cron = env.ref('keralariders_logistics.%s' % ref)
        assert cron.active, '%s is inactive' % ref
        names.append(cron.name)
    return '; '.join(names)


def access_rules():
    missing = []
    for model in ('logistics.indiapost.barcode.range', 'logistics.indiapost.barcode',
                  'logistics.indiapost.office', 'logistics.indiapost.tariff.cache',
                  'logistics.indiapost.log'):
        model_id = env['ir.model']._get_id(model)
        if not env['ir.model.access'].search_count([('model_id', '=', model_id)]):
            missing.append(model)
    assert not missing, missing
    return 'access rules present for all five new models'


def tracking_event_mapping():
    """The live API returns human-readable phrases, not the documented codes.

    Both paths are checked: the exact spreadsheet codes, and the free text the
    sandbox actually sent back.
    """
    tracking = __import__(
        'odoo.addons.keralariders_logistics.models.indiapost_tracking',
        fromlist=['x'],
    )
    cases = [
        # (raw scan, expected shipment state, expected custody event type)
        ('ITEM_BOOK', 'in_transit', 'indiapost_booked'),
        ('Item Booked', 'in_transit', 'indiapost_booked'),
        ('Item Pickedup', 'picked', 'pickup_scan'),
        ('Bag Dispatch', 'in_transit', 'indiapost_transit_scan'),
        ('Item received at Destination', 'at_destination_hub',
         'indiapost_transit_scan'),
        ('Item Invoiced', 'out_for_delivery', 'out_for_delivery'),
        ('Item Delivered to addressee', 'delivered', 'delivered'),
        ('Item Returned to Sender', 'returned', 'returned'),
        # On-hold says nothing about progress, so the status must not move.
        ('Item Kept on Hold', None, 'indiapost_transit_scan'),
        # Anything unrecognised is still recorded rather than dropped.
        ('Some brand new scan name', None, 'indiapost_transit_scan'),
    ]
    lines = []
    for raw, expected_state, expected_type in cases:
        state, event_type, label = tracking.classify_event(raw)
        assert state == expected_state, (raw, state, expected_state)
        assert event_type == expected_type, (raw, event_type, expected_type)
        lines.append('%s -> %s' % (raw, state or 'no state change'))
    # A late scan must never drag a shipment backwards.
    assert tracking._STATE_PROGRESS['delivered'] > \
        tracking._STATE_PROGRESS['in_transit']
    return '%d scans mapped; on-hold and unknown scans leave the status alone' \
        % len(cases)


def tracking_real_histories():
    """Replay three scan histories the sandbox actually returned.

    The first one matters most. India Post reported del_status "delivered", but
    the article was unclaimed, kept in deposit, had its return confirmed and was
    only then "Item Delivered to vinesh" — that is delivery back to the sender.
    ITEM_DELIVERY covers "addressee or sender" in the vendor's own note, so the
    mapper has to read the whole history rather than the last scan.
    """
    Tracking = env['logistics.indiapost.tracking']
    tracking = __import__(
        'odoo.addons.keralariders_logistics.models.indiapost_tracking',
        fromlist=['x'],
    )
    histories = [
        ('RK775227016IN', 'returned', [
            'Missent - Redirected to Mangaluru H.O',
            'Unclaimed',
            'Missent - Redirected to ',
            'Item Invoiced',
            'Kept in Deposit',
            'Returns Confirmed',
            'Item Invoiced',
            'Item Delivered to vinesh',
            'Item Delivered to vinesh',
        ]),
        ('EY011867595IN', 'in_transit', [
            'Item Bagged', 'Item Dispatched', 'Item Received',
            'Item Bagged', 'Item Dispatched', 'Item Received',
        ]),
        ('ED123456789IN', 'in_transit', ['Item Received', 'Item Bagged']),
    ]
    seller = env['logistics.seller'].search(
        [('name', '=', 'Self-test Seller')], limit=1)
    district = env['logistics.district'].search([], limit=1)
    outcomes = []
    for article, expected, scans in histories:
        shipment = env['logistics.shipment'].create({
            'seller_id': seller.id,
            'shipping_to_name': 'Self-test Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'shipping_to_district_id': district.id,
            'item_description': 'Self-test article',
            'total_weight': 1.5,
            'length_cm': 30, 'breadth_cm': 20, 'height_cm': 15,
        })
        parsed = []
        for raw in scans:
            state, event_type, label = tracking.classify_event(raw)
            parsed.append({'state': state, 'event_type': event_type,
                           'label': label, 'raw': raw, 'moment': None,
                           'office': '', 'office_id': ''})
        target = Tracking._ip_target_state(shipment, parsed)
        assert target == expected, (article, target, expected)
        outcomes.append('%s -> %s' % (article, target))
        shipment.unlink()
    return '; '.join(outcomes)


_restore_network = _block_indiapost_network()
try:
    check('seed: Kerala district HQ offices', seeded_offices)
    check('barcode: modulo-11 check digit', check_digit)
    check('barcode: concurrent-safe allocation', allocation)
    check('rules: Speed Post packaging limits', packaging_rules)
    check('rules: weight and shape conversions', conversions)
    check('rules: pickup date format', pickup_format)
    check('security: fulfilment method is admin only', admin_only_fulfilment)
    check('shipment: India Post defaults and guards', shipment_defaults)
    check('shipment: hub network pricing untouched', own_network_pricing_untouched)
    check('security: shipment carrier is admin only', shipment_fulfilment_method_guard)
    check('tariff: quote signature catches stale prices', quote_signature_staleness)
    check('pickup: India Post gets no KeralaXpress DE',
          indiapost_skips_keralaxpress_pickup)
    check('views: backend views render', views_render)
    check('views: calculator template compiles', calculator_template_compiles)
    check('data: cron jobs installed', crons_present)
    check('security: access rules for new models', access_rules)
    check('tracking: event phrase mapping', tracking_event_mapping)
    check('tracking: real sandbox scan histories', tracking_real_histories)
finally:
    _restore_network()

print('\n' + '=' * 78)
for status, label, detail in RESULTS:
    print('%-4s %-46s %s' % (status, label, detail))
print('=' * 78)
print('%d passed, %d failed' % (
    sum(1 for r in RESULTS if r[0] == 'PASS'),
    sum(1 for r in RESULTS if r[0] == 'FAIL'),
))
env.cr.rollback()
