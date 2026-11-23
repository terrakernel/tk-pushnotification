from __future__ import annotations

import json
import logging
from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..tools import fcm

_logger = logging.getLogger(__name__)


class TkPushNotification(models.Model):
    """One logical notification, fanned out into a tk.push.message per device.

    Sending is transactional: :meth:`send` writes the messages to the outbox in
    the caller's transaction, so a rolled-back business operation never
    notifies anyone, and a committed one always will, eventually.
    """

    _name = 'tk.push.notification'
    _description = 'Push Notification'
    _order = 'id desc'
    _rec_name = 'title'

    title = fields.Char(help="Leave empty, with an empty body, to send a data-only (silent) message.")
    body = fields.Text()
    image_url = fields.Char(string='Image URL', help="HTTPS URL of an image shown in the notification.")
    link_url = fields.Char(
        string='Link',
        help="Where tapping the notification should lead. Passed to apps as the "
             "'link' data key, and opened directly by web clients.",
    )
    data = fields.Text(
        string='Data (JSON)',
        help="JSON object delivered to the app as key/value data. Non-string values are JSON-encoded.",
    )
    overrides = fields.Text(
        string='FCM Overrides (JSON)',
        help="JSON deep-merged into every FCM message last, for options not "
             "modelled here, e.g. {\"android\": {\"notification\": {\"channel_id\": \"orders\"}}}.",
    )

    target = fields.Selection(
        [('users', 'Users'), ('topic', 'FCM Topic')],
        default='users',
        required=True,
    )
    user_ids = fields.Many2many('res.users', string='Recipients')
    topic_id = fields.Many2one(
        'tk.push.topic',
        string='Topic',
        index='btree_not_null',
        ondelete='set null',
        help="A topic from the topic list. Sets the provider.",
    )
    topic = fields.Char(
        string='Topic Name',
        compute='_compute_topic', store=True, readonly=False,
        help="The FCM topic name sent to, without the /topics/ prefix: the "
             "chosen topic's name. Kept when the topic is deleted.",
    )
    provider_id = fields.Many2one(
        'tk.push.provider',
        string='Provider',
        compute='_compute_provider_id', store=True, readonly=False,
        help="Restrict delivery to this provider's devices. Required for topics.",
    )

    priority = fields.Selection([('high', 'High'), ('normal', 'Normal')], default='high', required=True)
    ttl = fields.Integer(
        string='Time to Live (s)',
        help="Drop the notification if the device stays unreachable for this "
             "many seconds. 0 keeps FCM's default of 4 weeks.",
    )
    collapse_key = fields.Char(help="Newer notifications with the same key replace older undelivered ones.")
    scheduled_date = fields.Datetime(string='Send After', help="Leave empty to send immediately.")

    res_model = fields.Char(string='Related Model', index=True)
    res_id = fields.Many2oneReference(string='Related Record', model_field='res_model', index=True)

    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('queued', 'Sending'),
            ('done', 'Processed'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        readonly=True,
        index=True,
    )
    done_date = fields.Datetime(readonly=True)

    message_ids = fields.One2many('tk.push.message', 'notification_id', string='Deliveries')
    message_count = fields.Integer(compute='_compute_message_stats')
    sent_count = fields.Integer(compute='_compute_message_stats')
    validated_count = fields.Integer(
        compute='_compute_message_stats',
        string='Validated (Dry Run)',
        help="Accepted by FCM in dry-run mode: validated, not delivered.",
    )
    failed_count = fields.Integer(compute='_compute_message_stats')
    pending_count = fields.Integer(compute='_compute_message_stats')

    # Only assigned when a topic is chosen: a topic name or provider given
    # without one (send() by name, history of a deleted topic) is kept.
    @api.depends('topic_id')
    def _compute_topic(self):
        for notification in self.filtered('topic_id'):
            notification.topic = notification.topic_id.name

    @api.depends('topic_id')
    def _compute_provider_id(self):
        for notification in self.filtered('topic_id'):
            notification.provider_id = notification.topic_id.provider_id

    @api.depends('message_ids.state')
    def _compute_message_stats(self):
        stats = defaultdict(lambda: defaultdict(int))
        for notification, state, count in self.env['tk.push.message']._read_group(
            [('notification_id', 'in', self.ids)], ['notification_id', 'state'], ['__count'],
        ):
            stats[notification][state] = count
        for notification in self:
            counts = stats[notification]
            notification.message_count = sum(counts.values())
            notification.sent_count = counts['sent']
            notification.validated_count = counts['validated']
            notification.failed_count = counts['failed'] + counts['invalid']
            notification.pending_count = counts['queued']

    @api.constrains('data', 'overrides')
    def _check_json(self):
        for notification in self:
            for fname in ('data', 'overrides'):
                if notification[fname]:
                    notification._load_json(fname)

    @api.constrains('ttl')
    def _check_ttl(self):
        if any(notification.ttl < 0 for notification in self):
            raise ValidationError(self.env._("The time to live cannot be negative."))

    @api.constrains('title', 'body', 'image_url', 'link_url', 'data', 'overrides',
                    'priority', 'ttl', 'collapse_key')
    def _check_fcm_message(self):
        """Refuse, when saving, what FCM would refuse when sending: oversized
        payloads and reserved data keys. Caught here, the error reaches the
        developer who called send(); caught by FCM, it would only show up as a
        failed delivery, after the fact."""
        for notification in self:
            try:
                # Any token will do: only the content is being checked.
                notification._fcm_message(token='payload-check')
            except ValueError as err:
                raise ValidationError(self.env._("This notification cannot be sent through FCM: %s", err)) from err

    def _fcm_message(self, *, token=None, topic=None):
        """The FCM message for this notification, addressed to ``token`` or ``topic``."""
        self.ensure_one()
        return fcm.build_message(
            token=token,
            topic=topic,
            title=self.title or None,
            body=self.body or None,
            image=self.image_url or None,
            data=self._load_json('data') if self.data else None,
            link=self.link_url or None,
            priority=self.priority,
            ttl=self.ttl or None,
            collapse_key=self.collapse_key or None,
            overrides=self._load_json('overrides') if self.overrides else None,
        )

    def _load_json(self, fname):
        self.ensure_one()
        try:
            value = json.loads(self[fname])
        except ValueError as err:
            raise ValidationError(self.env._("%(field)s is not valid JSON: %(error)s",
                                             field=self._fields[fname].string, error=err)) from err
        if not isinstance(value, dict):
            raise ValidationError(self.env._("%s must be a JSON object.", self._fields[fname].string))
        return value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @api.model
    def send(self, users=None, title=None, body=None, *, data=None, record=None, topic=None,
             provider=None, image_url=None, link_url=None, priority='high', ttl=0,
             collapse_key=None, scheduled_date=None, overrides=None):
        """Notify ``users`` (a res.users recordset or ids), or an FCM ``topic``.

        Returns the queued tk.push.notification. Delivery happens after commit,
        from a cron, so this never blocks on the network::

            self.env['tk.push.notification'].send(
                order.user_id, "Order confirmed", f"{order.name} was confirmed",
                data={'order_id': order.id}, record=order,
            )

        ``topic`` is a tk.push.topic or a topic name from the topic list. A
        name that is invalid, missing from the list or archived does not
        raise: the notification is recorded with a failed delivery saying why,
        so a mistake here never undoes the caller's business operation.

        Runs with the caller's access rights: only push administrators may
        create notifications. Business code notifying on behalf of an ordinary
        user should go through ``res.users._tk_push_send()`` or call this with
        ``sudo()``.
        """
        if isinstance(provider, str):
            provider = self.env['tk.push.provider']._get_for_code(provider)
        topic_record = self.env['tk.push.topic']
        if isinstance(topic, models.BaseModel):
            topic_record, topic = topic, topic.name
            provider = topic_record.provider_id
        elif topic:
            if not provider:
                # The provider whose list has this topic, if only one has.
                listed = topic_record.sudo().search([('name', '=', topic)], limit=2)
                provider = listed.provider_id if len(listed) == 1 else None
                provider = provider or self.env['tk.push.provider']._get_default()
            topic_record = topic_record._find(provider, topic)  # archived ones too: linked, then refused
        if not isinstance(users, models.BaseModel):
            users = self.env['res.users'].browse(users or [])
        vals = {
            'title': title,
            'body': body,
            'image_url': image_url,
            'link_url': link_url,
            'data': json.dumps(data, default=str) if isinstance(data, dict) else data,
            'overrides': json.dumps(overrides) if isinstance(overrides, dict) else overrides,
            'target': 'topic' if topic else 'users',
            'topic_id': topic_record.id or False,
            'topic': topic,
            'user_ids': [fields.Command.set(users.ids)],
            'provider_id': provider.id if provider else False,
            'priority': priority,
            'ttl': ttl or 0,
            'collapse_key': collapse_key,
            'scheduled_date': scheduled_date,
        }
        if record:
            vals.update(res_model=record._name, res_id=record.id)
        notification = self.create(vals)
        notification._enqueue()
        return notification

    def action_send(self):
        for notification in self:
            if notification.state != 'draft':
                raise UserError(self.env._("Only draft notifications can be sent."))
            if notification.target == 'users' and not notification.user_ids:
                raise UserError(self.env._("Add at least one recipient."))
            if notification.target == 'topic' and not notification.topic_id:
                raise UserError(self.env._("Choose a topic."))
            if not (notification.title or notification.body or notification.data):
                raise UserError(self.env._("A notification needs a title, a body or data."))
        self._enqueue()

    def action_cancel(self):
        self.message_ids.filtered(lambda m: m.state == 'queued').write({'state': 'cancelled'})
        self.filtered(lambda n: n.state in ('draft', 'queued')).write({
            'state': 'cancelled', 'done_date': fields.Datetime.now(),
        })

    def action_reset_draft(self):
        self.filtered(lambda n: n.state == 'cancelled' and not n.message_ids).write({'state': 'draft'})

    def action_open_record(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
        }

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    def _enqueue(self):
        """Write one outbox message per target device and wake the sender."""
        Message = self.env['tk.push.message'].sudo()
        Topic = self.env['tk.push.topic']
        vals_list = []
        for notification in self:
            next_attempt = notification.scheduled_date or fields.Datetime.now()
            if notification.target == 'topic':
                if not notification.provider_id:
                    _logger.warning(
                        "tk_pushnotification: topic notification %s not sent: no push provider found",
                        notification.id,
                    )
                    continue
                vals = {
                    'notification_id': notification.id,
                    'provider_id': notification.provider_id.id,
                    'topic': notification.topic,
                    'next_attempt': next_attempt,
                }
                # Recorded, not raised: see send().
                problem = Topic._send_problem(notification.provider_id, notification.topic)
                if problem:
                    code, error = problem
                    _logger.warning("tk_pushnotification: notification %s not sent: %s", notification.id, error)
                    vals.update(state='failed', error_code=code, error_message=error)
                vals_list.append(vals)
                continue
            domain = [('user_id', 'in', notification.user_ids.ids), ('user_id.active', '=', True)]
            if notification.provider_id:
                domain.append(('provider_id', '=', notification.provider_id.id))
            devices = self.env['tk.push.device'].sudo().search(domain)
            vals_list += [{
                'notification_id': notification.id,
                'provider_id': device.provider_id.id,
                'device_id': device.id,
                'token': device.token,
                'next_attempt': next_attempt,
            } for device in devices]
            if not devices:
                _logger.info("tk_pushnotification: notification %s has no active device to reach", notification.id)

        Message.create(vals_list)
        self.write({'state': 'queued'})
        # Nothing to deliver: close right away instead of waiting for the cron.
        self._update_state()
        due = [vals['next_attempt'] for vals in vals_list if vals.get('state', 'queued') == 'queued']
        if due:
            Message._schedule_processing(min(due))

    def _update_state(self):
        """Close queued notifications with no delivery left pending."""
        queued = self.filtered(lambda n: n.state == 'queued')
        if not queued:
            return
        pending = {
            notification for [notification] in self.env['tk.push.message'].sudo()._read_group(
                [('notification_id', 'in', queued.ids), ('state', '=', 'queued')], ['notification_id'],
            )
        }
        finished = queued.filtered(lambda n: n not in pending)
        finished.write({'state': 'done', 'done_date': fields.Datetime.now()})

    @api.autovacuum
    def _gc_notifications(self):
        """Delete processed notifications, and their deliveries, past the retention period."""
        cutoff = fields.Datetime.subtract(
            fields.Datetime.now(), days=self.env['tk.push.message']._retention_days(),
        )
        self.sudo().search([('state', 'in', ('done', 'cancelled')), ('done_date', '<', cutoff)]).unlink()
