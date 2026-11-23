from __future__ import annotations

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..tools import fcm


class TkPushTopic(models.Model):
    """The list of FCM topics notifications may be sent to.

    FCM accepts a send to any well-formed topic name, subscribed or not, so a
    mistyped name would "succeed" and reach nobody. Sends are checked against
    this list instead; one to a topic missing from it, or archived, is recorded
    as a failed delivery rather than raised, so it never breaks the business
    operation that triggered it.

    It says which topics are *available*, nothing about who subscribed: apps
    (or a gateway such as ODXProxy) manage subscriptions with FCM directly.
    """

    _name = 'tk.push.topic'
    _description = 'Push Notification Topic'
    _order = 'provider_id, name'
    _rec_names_search = ['name', 'label']

    name = fields.Char(
        string='Topic',
        required=True,
        help="The FCM topic name apps subscribe to, without the /topics/ prefix. "
             "Letters, digits and - _ . ~ % only.",
    )
    label = fields.Char(translate=True, help="Human-readable name, e.g. for apps listing the topics on offer.")
    description = fields.Text(translate=True)
    provider_id = fields.Many2one(
        'tk.push.provider',
        string='Provider',
        required=True,
        ondelete='cascade',
        index=True,
        default=lambda self: self.env['tk.push.provider']._get_default(),
        help="Topics belong to one Firebase project.",
    )
    # Stored, so apps can filter on it without read access to providers.
    provider_code = fields.Char(related='provider_id.code', string='Provider Code', store=True)
    active = fields.Boolean(default=True, help="Archived topics cannot be sent to.")
    app_offered = fields.Boolean(
        string='Offered in Apps',
        help="Topics app users may choose to subscribe to. Signed-in users and "
             "API clients can read these, and only these, to build the list "
             "their app shows. Leave off for topics apps subscribe to on their own.",
    )

    # Apps may read offered topics; the notification history stays with push administrators.
    notification_ids = fields.One2many(
        'tk.push.notification', 'topic_id', string='Notifications',
        groups='tk_pushnotification.group_push_manager',
    )
    notification_count = fields.Integer(
        compute='_compute_notification_count', groups='tk_pushnotification.group_push_manager',
    )

    _name_provider_unique = models.Constraint(
        'UNIQUE(provider_id, name)', 'This provider already has a topic with this name.',
    )

    @api.depends('name', 'label')
    def _compute_display_name(self):
        for topic in self:
            topic.display_name = f'{topic.label} ({topic.name})' if topic.label else topic.name

    def _compute_notification_count(self):
        counts = dict(self.env['tk.push.notification']._read_group(
            [('topic_id', 'in', self.ids)], ['topic_id'], ['__count'],
        ))
        for topic in self:
            topic.notification_count = counts.get(topic, 0)

    @api.constrains('name')
    def _check_name(self):
        for topic in self:
            problem = self._name_problem(topic.name)
            if problem:
                raise ValidationError(problem)

    def write(self, vals):
        # Subscribers are attached to the name in FCM: renaming the record
        # would silently cut them off from everything sent from now on.
        if 'name' in vals or 'provider_id' in vals:
            renamed = self.filtered(lambda t: (
                ('name' in vals and vals['name'] != t.name)
                or ('provider_id' in vals and vals['provider_id'] != t.provider_id.id)
            ))
            used = self.env['tk.push.notification'].sudo().search_count(
                [('topic_id', 'in', renamed.ids)], limit=1,
            )
            if used:
                raise UserError(self.env._(
                    "A topic that notifications were sent to cannot be renamed or moved: "
                    "its subscribers stay on the old name in FCM. Archive it and create a new one."
                ))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_used(self):
        if self.env['tk.push.notification'].sudo().search_count([('topic_id', 'in', self.ids)], limit=1):
            raise UserError(self.env._(
                "Topics that notifications were sent to cannot be deleted. Archive them instead."
            ))

    # ------------------------------------------------------------------
    # Checks used when sending
    # ------------------------------------------------------------------

    @api.model
    def _name_problem(self, name):
        """Why ``name`` is not a valid FCM topic name, or ``None``."""
        if fcm.is_valid_topic(name):
            return None
        if not name:
            return self.env._("No topic was given.")
        hint = self.env._(" Leave out the /topics/ prefix.") if name.startswith('/topics/') else ''
        return self.env._(
            "%(topic)r is not a valid FCM topic name: use only letters, digits and - _ . ~ %%.%(hint)s",
            topic=name, hint=hint,
        )

    @api.model
    def _find(self, provider, name):
        """The listed topic ``name`` of ``provider``, archived or not."""
        if not (provider and name):
            return self.browse()
        return self.sudo().with_context(active_test=False).search(
            [('provider_id', '=', provider.id), ('name', '=', name)], limit=1,
        )

    @api.model
    def _send_problem(self, provider, name):
        """Why ``name`` cannot be sent to through ``provider``, as ``(code, message)``, or ``None``.

        Codes: ``TOPIC_INVALID`` (not a valid FCM name), ``TOPIC_UNKNOWN`` (not
        in the topic list), ``TOPIC_ARCHIVED``.
        """
        problem = self._name_problem(name)
        if problem:
            return 'TOPIC_INVALID', problem
        topic = self._find(provider, name)
        if not topic:
            return 'TOPIC_UNKNOWN', self.env._(
                "Topic %(topic)r is not in the topic list of provider %(provider)s. "
                "Add it under Push Notifications > Topics, then retry.",
                topic=name, provider=provider.name,
            )
        if not topic.active:
            return 'TOPIC_ARCHIVED', self.env._("Topic %r is archived.", name)
        return None

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def action_view_notifications(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Notifications"),
            'res_model': 'tk.push.notification',
            'view_mode': 'list,form',
            'domain': [('topic_id', '=', self.id)],
            'context': {'default_target': 'topic', 'default_topic_id': self.id},
        }
