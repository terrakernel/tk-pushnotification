from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    push_device_ids = fields.One2many(
        'tk.push.device', 'user_id', string='Push Devices',
        groups='tk_pushnotification.group_push_manager',
    )

    @api.private
    def _tk_push_send(self, title=None, body=None, **kwargs):
        """Push a notification to these users' devices, whoever the caller is.

        The entry point for business code, e.g. on order confirmation by a
        portal user. Takes the keyword arguments of tk.push.notification.send().
        """
        return self.env['tk.push.notification'].sudo().send(self, title, body, **kwargs)
