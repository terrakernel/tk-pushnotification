from __future__ import annotations

from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

# FCM tokens are ~160 characters today; this only stops abuse.
MAX_TOKEN_LENGTH = 4096

# Firebase treats a token unused for 270 days as expired and rejects it.
# https://firebase.google.com/docs/cloud-messaging/manage-tokens
TOKEN_STALE_DAYS = 270

PLATFORMS = [('android', 'Android'), ('ios', 'iOS'), ('web', 'Web')]


class TkPushDevice(models.Model):
    _name = 'tk.push.device'
    _description = 'Push Notification Device'
    _order = 'last_seen desc, id desc'

    user_id = fields.Many2one('res.users', string='User', required=True, index=True, ondelete='cascade')
    partner_id = fields.Many2one(related='user_id.partner_id', store=True, index=True)
    provider_id = fields.Many2one('tk.push.provider', string='Provider', required=True, index=True, ondelete='cascade')
    token = fields.Char(required=True, copy=False, groups='tk_pushnotification.group_push_manager')
    platform = fields.Selection(PLATFORMS, required=True)
    device_name = fields.Char(help="As reported by the app, e.g. 'Pixel 9'.")
    app_id = fields.Char(string='App ID', help="Android package name, iOS bundle id or web origin.")
    app_version = fields.Char()
    os_version = fields.Char(string='OS Version')
    last_seen = fields.Datetime(
        default=fields.Datetime.now,
        help="Last time the app registered this token. Apps should register on every start.",
    )
    active = fields.Boolean(default=True)
    deactivation_reason = fields.Char(readonly=True)

    _token_unique = models.Constraint(
        'UNIQUE(provider_id, token)', 'This token is already registered for this provider.',
    )

    @api.depends('device_name', 'platform', 'user_id')
    def _compute_display_name(self):
        platforms = dict(PLATFORMS)
        for device in self:
            label = device.device_name or platforms.get(device.platform) or self.env._("Device")
            device.display_name = f'{label} ({device.user_id.name})' if device.user_id else label

    # ------------------------------------------------------------------
    # App-facing API
    #
    # Callable through JSON-2 (POST /json/2/tk.push.device/register with a
    # user API key), XML/JSON-RPC, or the session routes in controllers/main.py.
    # Users hold no create right on this model: these methods write as
    # superuser, and only ever on behalf of the calling user.
    # ------------------------------------------------------------------

    @api.model
    def register(self, token, platform, provider_code=None, device_name=None,
                 app_id=None, app_version=None, os_version=None):
        """Attach the FCM ``token`` to the calling user. Idempotent.

        Apps should call this at every start and whenever FCM rotates the
        token. A token already registered to another user moves to this one:
        that is what happens when someone signs out and a colleague signs in
        on the same phone.
        """
        device = self._register(
            token, platform, provider_code=provider_code, device_name=device_name,
            app_id=app_id, app_version=app_version, os_version=os_version,
        )
        return {'device_id': device.id}

    @api.model
    def unregister(self, token, provider_code=None):
        """Forget ``token`` if it belongs to the calling user. Call it on sign-out."""
        provider = self.env['tk.push.provider'].sudo()._get_for_code(provider_code)
        devices = self.sudo().search([
            ('provider_id', '=', provider.id),
            ('token', '=', token),
            ('user_id', '=', self.env.uid),
        ])
        devices.unlink()
        return {'removed': len(devices)}

    @api.private
    def _register(self, token, platform, provider_code=None, **details):
        user = self.env.user
        if user._is_public():
            raise AccessError(self.env._("Sign in before registering a device for push notifications."))
        if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_LENGTH:
            raise UserError(self.env._("A valid push token is required."))
        if platform not in dict(PLATFORMS):
            raise UserError(self.env._(
                "Unknown platform %(platform)r, expected one of %(choices)s.",
                platform=platform, choices=', '.join(dict(PLATFORMS)),
            ))
        provider = self.env['tk.push.provider'].sudo()._get_for_code(provider_code)
        if not provider:
            raise UserError(self.env._("No push notification provider is configured for %s.", provider_code or 'this app'))

        vals = {
            'user_id': user.id,
            'platform': platform,
            'last_seen': fields.Datetime.now(),
            'active': True,
            'deactivation_reason': False,
            **{key: value or False for key, value in details.items() if isinstance(value, (str, type(None)))},
        }
        token = token.strip()
        Device = self.sudo().with_context(active_test=False)
        device = Device.search([('provider_id', '=', provider.id), ('token', '=', token)], limit=1)
        if device:
            if device.user_id != user:
                # Same phone, new person: what was queued for the previous
                # user must not show up on their successor's lock screen.
                device._cancel_pending('DEVICE_REASSIGNED')
            device.write(vals)
        else:
            device = Device.create({**vals, 'provider_id': provider.id, 'token': token})
        return device

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _deactivate(self, reason):
        if self:
            self.sudo().write({'active': False, 'deactivation_reason': reason})
            self._cancel_pending(reason)

    def _cancel_pending(self, reason):
        """Cancel deliveries still queued for these devices."""
        messages = self.env['tk.push.message'].sudo().search([
            ('device_id', 'in', self.ids), ('state', '=', 'queued'),
        ])
        if messages:
            messages.write({'state': 'cancelled', 'error_code': reason})
            messages.notification_id._update_state()

    def unlink(self):
        self._cancel_pending('DEVICE_REMOVED')
        return super().unlink()

    @api.autovacuum
    def _gc_stale_devices(self):
        """Retire tokens Firebase has expired, then forget long-retired ones."""
        now = fields.Datetime.now()
        stale = self.sudo().search([('last_seen', '<', now - timedelta(days=TOKEN_STALE_DAYS))])
        stale._deactivate('STALE')
        retention = self.env['tk.push.message']._retention_days()
        self.sudo().with_context(active_test=False).search([
            ('active', '=', False),
            ('write_date', '<', now - timedelta(days=retention)),
        ]).unlink()
