import logging
import threading

from markupsafe import Markup
from odoo import api, models, modules, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

OPS_NOTIFICATION_PARAM = 'keralariders_logistics.ops_notification_email'


def _send_queued_mail_ids(dbname, mail_ids, context):
    """Open a fresh cursor and SMTP-send queued mails. Runs off the HTTP thread."""
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, context or {})
            mails = env['mail.mail'].browse(mail_ids).exists()
            outgoing = mails.filtered(lambda m: m.state == 'outgoing')
            if outgoing:
                outgoing.send(auto_commit=True, raise_exception=False)
    except Exception:
        _logger.exception(
            'KeralaXpress queued mail send failed for ids %s', mail_ids,
        )


class LogisticsMailNotify(models.AbstractModel):
    """Queue branded KeralaXpress mail without blocking the HTTP request.

    ``message_notify`` in Odoo 19 force-sends SMTP in a post-commit hook on
    the same request thread, which is what freezes seller/admin screens.
    These helpers create ``mail.mail`` in ``outgoing`` and, after commit,
    spawn a daemon thread to call ``send()``.
    """

    _name = 'logistics.mail.notify'
    _description = 'KeralaXpress queued outbound mail'

    @api.model
    def _kx_base_url(self):
        return (
            self.env['ir.config_parameter'].sudo().get_param('web.base.url')
            or ''
        ).rstrip('/')

    @api.model
    def _kx_email_from(self):
        """Follow company email / mail.default.from — never a hardcoded inbox."""
        company = self.env.company.sudo()
        partner = company.partner_id
        formatted = getattr(partner, 'email_formatted', False) or False
        if formatted:
            return formatted
        if company.email:
            name = company.name or 'KeralaXpress'
            return '%s <%s>' % (name, company.email)
        return self.env['ir.config_parameter'].sudo().get_param(
            'mail.default.from') or ''

    @api.model
    def _kx_support_email(self):
        company = self.env.company.sudo()
        email = (company.email or '').strip()
        if email and '@' in email:
            return email
        return ''

    @api.model
    def _kx_support_phone(self):
        company = self.env.company.sudo()
        return (company.phone or '').strip()

    @api.model
    def _kx_logistics_admin_users(self):
        admin_group = self.env.ref(
            'keralariders_logistics.group_logistics_admin',
            raise_if_not_found=False,
        )
        if not admin_group:
            return self.env['res.users']
        root_user = self.env.ref('base.user_root', raise_if_not_found=False)
        return admin_group.sudo().user_ids.filtered(
            lambda u: u.active and not u.share and (not root_user or u != root_user)
        )

    @api.model
    def _kx_team_email_to(self):
        """Ops inbox if configured, else logistics admins, else company email."""
        configured = (
            self.env['ir.config_parameter'].sudo().get_param(
                OPS_NOTIFICATION_PARAM) or ''
        ).strip()
        if configured:
            emails = []
            for raw in configured.replace(';', ',').split(','):
                email = raw.strip()
                if email and '@' in email and email not in emails:
                    emails.append(email)
            if emails:
                return ','.join(emails)

        emails = []
        try:
            for user in self._kx_logistics_admin_users():
                email = (
                    (user.partner_id.email or user.email or user.login or '')
                    .strip()
                )
                if email and '@' in email and email not in emails:
                    emails.append(email)
        except Exception:
            _logger.warning(
                'Could not resolve logistics admin emails for team notify',
                exc_info=True,
            )
        if emails:
            return ','.join(emails)

        company_email = (self.env.company.sudo().email or '').strip()
        if company_email and '@' in company_email:
            return company_email
        return ''

    @api.model
    def _kx_wrap_body(self, title, inner_html):
        title_html = html_escape(title)
        inner = inner_html if isinstance(inner_html, Markup) else Markup(inner_html)
        return Markup(
            '<div style="font-family:Arial,Helvetica,sans-serif;'
            'max-width:560px;color:#1f2937;line-height:1.55">'
            '<p style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;'
            'color:#2563eb;margin:0 0 8px 0">KeralaXpress</p>'
            '<h1 style="font-size:22px;margin:0 0 16px 0;color:#111827">%s</h1>'
            '%s'
            '<p style="margin:28px 0 0 0;font-size:12px;color:#6b7280">'
            'This is an automated message from KeralaXpress.</p>'
            '</div>'
        ) % (title_html, inner)

    @api.model
    def _kx_queue_mail(self, *, email_to, subject, body_html,
                       email_from=False, reply_to=False,
                       res_model=False, res_id=False):
        """Create an outgoing ``mail.mail`` and send it after commit, off-request.

        Never calls ``send()`` or ``send_after_commit()`` on the HTTP thread.
        Tests leave the row queued because ``modules.module.current_test`` skips
        the daemon thread.
        """
        email_to = (email_to or '').strip()
        if not email_to or not subject:
            return self.env['mail.mail']

        from_addr = email_from or self._kx_email_from()
        html = body_html if isinstance(body_html, Markup) else Markup(body_html or '')
        vals = {
            'subject': subject,
            'body_html': html,
            'body': html,
            'email_to': email_to,
            'email_from': from_addr or False,
            'auto_delete': True,
            'state': 'outgoing',
        }
        if reply_to:
            vals['reply_to'] = reply_to
        if res_model:
            vals['model'] = res_model
        if res_id:
            vals['res_id'] = res_id

        mail = self.env['mail.mail'].sudo().with_context(
            default_type=None,
            default_state='outgoing',
            mail_notify_force_send=False,
        ).create(vals)
        self._kx_schedule_async_send(mail.ids)
        return mail

    @api.model
    def _kx_schedule_async_send(self, mail_ids):
        """After a successful commit, SMTP-send in a daemon thread.

        During tests the thread is skipped so assertions can see ``outgoing``
        mail and so ``send()`` is never invoked in the request.
        """
        mail_ids = [mid for mid in (mail_ids or []) if mid]
        if not mail_ids:
            return
        if modules.module.current_test:
            return

        dbname = self.env.cr.dbname
        context = dict(self.env.context)

        def _spawn_thread():
            thread = threading.Thread(
                target=_send_queued_mail_ids,
                args=(dbname, list(mail_ids), context),
                name='keralaxpress-mail-send',
                daemon=True,
            )
            thread.start()

        try:
            self.env.cr.postcommit.add(_spawn_thread)
        except Exception:
            _logger.warning(
                'Could not register post-commit mail send for ids %s',
                mail_ids, exc_info=True,
            )
            # Last resort: still do not block this request on SMTP.
            _spawn_thread()
