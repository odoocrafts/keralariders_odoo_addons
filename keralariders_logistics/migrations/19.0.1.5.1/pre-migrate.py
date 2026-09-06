"""Split the single India Post contract id into one contract per product.

India Post contracts a bulk customer per service and issued KeralaXpress two
contract numbers, one for Speed Post and one for Business Parcel, so the old
single ``indiapost_contract_id`` could only ever have held one of them. It held
the Speed Post contract in practice, which is where its value is moved rather
than dropped.

The old key is then deleted: left behind it would be a second, invisible source
of the Speed Post contract, and clearing the field on the settings screen would
appear not to work.
"""

OLD_KEY = 'keralariders_logistics.indiapost_contract_id'
NEW_KEY = 'keralariders_logistics.indiapost_sp_contract_id'


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s",
               (OLD_KEY,))
    row = cr.fetchone()
    legacy = (row[0] or '').strip() if row else ''

    if legacy:
        # Only seed: an administrator who has already filled the new field in
        # is the better authority than a value carried over from before.
        cr.execute("""
            INSERT INTO ir_config_parameter (key, value, create_uid, write_uid,
                                             create_date, write_date)
                 VALUES (%s, %s, 1, 1, now(), now())
            ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, write_date = now()
                  WHERE COALESCE(TRIM(ir_config_parameter.value), '') = ''
        """, (NEW_KEY, legacy))

    cr.execute("DELETE FROM ir_config_parameter WHERE key = %s", (OLD_KEY,))
