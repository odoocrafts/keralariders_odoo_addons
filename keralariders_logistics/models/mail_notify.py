import logging
import threading

from markupsafe import Markup
from odoo import api, models, modules, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

OPS_NOTIFICATION_PARAM = 'keralariders_logistics.ops_notification_email'
FROM_NOTIFICATION_PARAM = 'keralariders_logistics.notification_email_from'
KX_FROM_DOMAIN = 'keralaxpress.com'
KX_FROM_FALLBACK = 'notifications@keralaxpress.com'
KX_FROM_DISPLAY = 'KERALA XPRESS LOGISTICS'


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
    def _kx_bare_email(self, value):
        """Return the address portion of a From header or raw mailbox."""
        text = (value or '').strip()
        if not text:
            return ''
        if '<' in text and '>' in text:
            text = text[text.rfind('<') + 1:text.rfind('>')].strip()
        return text.split()[0].strip('<>,"\'').lower()

    @api.model
    def _kx_email_domain(self, value):
        email = self._kx_bare_email(value)
        if '@' not in email:
            return ''
        return email.rsplit('@', 1)[-1].lower()

    @api.model
    def _kx_format_from(self, address, display_name=False):
        email = self._kx_bare_email(address) or KX_FROM_FALLBACK
        name = (display_name or self.env.company.sudo().name or KX_FROM_DISPLAY).strip()
        name = name.replace('<', '').replace('>', '') or KX_FROM_DISPLAY
        return '%s <%s>' % (name, email)

    @api.model
    def _kx_email_from(self):
        """From must be a keralaxpress.com mailbox — never the current user.

        Prefer notifications@ when that is mail.default.from, the company
        mailbox, or a dedicated config. Otherwise use mail.default.from or
        company email_formatted only if the domain is keralaxpress.com.
        """
        company = self.env.company.sudo()
        icp = self.env['ir.config_parameter'].sudo()
        display = (company.name or KX_FROM_DISPLAY).strip() or KX_FROM_DISPLAY
        dedicated = (icp.get_param(FROM_NOTIFICATION_PARAM) or '').strip()
        default_from = (icp.get_param('mail.default.from') or '').strip()
        company_email = (company.email or '').strip()
        formatted = (
            getattr(company.partner_id, 'email_formatted', None) or ''
        ).strip()

        sources = (dedicated, default_from, company_email, formatted)
        if any(self._kx_bare_email(raw) == KX_FROM_FALLBACK for raw in sources):
            return self._kx_format_from(KX_FROM_FALLBACK, display)

        if self._kx_email_domain(default_from) == KX_FROM_DOMAIN:
            return self._kx_format_from(default_from, display)
        if self._kx_email_domain(formatted) == KX_FROM_DOMAIN:
            return self._kx_format_from(formatted, display)
        if self._kx_email_domain(company_email) == KX_FROM_DOMAIN:
            return self._kx_format_from(company_email, display)
        if self._kx_email_domain(dedicated) == KX_FROM_DOMAIN:
            return self._kx_format_from(dedicated, display)
        return self._kx_format_from(KX_FROM_FALLBACK, display)

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
    def _kx_emails_from_partners(self, partners):
        emails = []
        for partner in partners:
            email = (partner.email or '').strip()
            if email and '@' in email and email not in emails:
                emails.append(email)
        return ','.join(emails)

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

        from_addr = self._kx_email_from()
        if email_from and self._kx_email_domain(email_from) == KX_FROM_DOMAIN:
            from_addr = self._kx_format_from(email_from)
        html = body_html if isinstance(body_html, Markup) else Markup(body_html or '')
        vals = {
            'subject': subject,
            'body_html': html,
            'body': html,
            'email_to': email_to,
            'email_from': from_addr,
            'auto_delete': True,
            'state': 'outgoing',
        }
        root_partner = self.env.ref('base.partner_root', raise_if_not_found=False)
        if root_partner:
            vals['author_id'] = root_partner.id
        if reply_to:
            vals['reply_to'] = reply_to
        if res_model:
            vals['model'] = res_model
        if res_id:
            vals['res_id'] = res_id

        mail = self.env['mail.mail'].sudo().with_context(
            default_type=None,
            default_author_id=vals.get('author_id') or False,
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


class MailActivityKx(models.Model):
    """Keep logistics To-Dos, but never email '"… assigned to you"' as the actor.

    A seller submitting a recharge schedules admin activities. Odoo 19 then
    ``message_notify``s each assignee from the current user, so From becomes
    the seller Gmail and Titan rejects it. Team mail goes through
    ``logistics.mail.notify`` instead.
    """

    _inherit = 'mail.activity'

    def action_notify(self):
        sendable = self.filtered(
            lambda act: not (act.res_model or '').startswith('logistics.')
        )
        if not sendable:
            return True
        return super(MailActivityKx, sendable).action_notify()
