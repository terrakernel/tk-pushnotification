from __future__ import annotations

import logging
import random
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools import SQL

from ..tools import fcm

_logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
DEFAULT_RETENTION_DAYS = 30
RETENTION_PARAM = 'tk_pushnotification.retention_days'


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait before attempt ``attempt + 1``: exponential with 10% jitter.

    ``Retry-After`` from FCM is a floor, never shortened.
    """
    delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)
    delay *= 1 + random.random() / 10
    return max(delay, retry_after or 0)


class TkPushMessage(models.Model):
    """The outbox: one delivery of a notification to one device or topic.

    Rows are written in the business transaction and drained by the
    ``ir_cron_push_send`` cron, which gives at-least-once delivery: a message
    can only be lost by deleting it, but a crash between FCM accepting it and
    the cron committing can send it twice.
    """

    _name = 'tk.push.message'
    _description = 'Push Notification Delivery'
    _order = 'id desc'

    notification_id = fields.Many2one(
        'tk.push.notification', required=True, index=True, ondelete='cascade', readonly=True,
    )
    provider_id = fields.Many2one('tk.push.provider', required=True, index=True, ondelete='cascade', readonly=True)
    device_id = fields.Many2one('tk.push.device', index='btree_not_null', ondelete='set null', readonly=True)
    user_id = fields.Many2one(related='device_id.user_id', string='User')
    # Snapshot, so the log keeps its meaning after the device is forgotten.
    token = fields.Char(readonly=True, groups='tk_pushnotification.group_push_manager')
    topic = fields.Char(readonly=True)

    state = fields.Selection(
        [
            ('queued', 'Queued'),
            ('sent', 'Sent'),
            ('validated', 'Validated (Dry Run)'),
            ('failed', 'Failed'),
            ('invalid', 'Invalid Token'),
            ('cancelled', 'Cancelled'),
        ],
        default='queued',
        required=True,
        readonly=True,
    )
    next_attempt = fields.Datetime(readonly=True, default=fields.Datetime.now)
    attempt_count = fields.Integer(readonly=True)
    last_attempt = fields.Datetime(readonly=True)
    sent_date = fields.Datetime(readonly=True)
    provider_message_id = fields.Char(string='Provider Message ID', readonly=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Text(readonly=True)

    # The cron scans only this, so its cost tracks the backlog, not the history.
    _queue_index = models.Index("(next_attempt, id) WHERE state = 'queued'")

    @api.model
    def _retention_days(self):
        return self.env['ir.config_parameter'].sudo().get_int(RETENTION_PARAM, DEFAULT_RETENTION_DAYS)

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    @api.model
    def _schedule_processing(self, at=None):
        """Wake the sender cron at ``at`` (or now). Takes effect only on commit."""
        now = fields.Datetime.now()
        self.env.ref('tk_pushnotification.ir_cron_push_send').sudo()._trigger(
            at if at and at > now else None,
        )

    @api.model
    def _process_queue(self, batch_size=100):
        """Cron entry point: deliver due messages, batch by batch.

        Each batch is locked with SKIP LOCKED, sent, and committed before the
        next one, so a crash loses at most one batch's bookkeeping and a slow
        FCM never holds locks for the whole backlog. Outside a cron (tests,
        shell) a single batch is processed and nothing is committed.
        """
        in_cron = bool(self.env.context.get('ir_cron_progress_id'))
        IrCron = self.env['ir.cron']
        while True:
            messages = self._lock_due_batch(batch_size)
            if not messages:
                break
            self._send_batch(messages)
            if not in_cron:
                return
            remaining = self._due_count()
            if IrCron._commit_progress(len(messages), remaining=remaining) <= 0:
                # Out of time: let the cron runner pick up where we stopped.
                self._schedule_processing()
                return

        upcoming = self.sudo().search_fetch(
            [('state', '=', 'queued')], ['next_attempt'], order='next_attempt', limit=1,
        )
        if upcoming:
            self._schedule_processing(upcoming.next_attempt)

    def _due_domain(self):
        return [('state', '=', 'queued'), ('next_attempt', '<=', fields.Datetime.now())]

    def _due_count(self):
        return self.sudo().search_count(self._due_domain())

    def _lock_due_batch(self, batch_size):
        self.flush_model(['state', 'next_attempt'])
        rows = self.env.execute_query(SQL(
            """
            SELECT id FROM tk_push_message
             WHERE state = 'queued' AND next_attempt <= %s
             ORDER BY next_attempt, id
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            fields.Datetime.now(), batch_size,
        ))
        return self.sudo().browse(row[0] for row in rows)

    def _send_batch(self, messages):
        # The device may have signed out since the message was queued.
        orphaned = messages.filtered(lambda m: m.device_id and not m.device_id.active)
        orphaned.write({'state': 'cancelled', 'error_code': 'DEVICE_INACTIVE'})
        started = fields.Datetime.now()
        for provider, batch in (messages - orphaned).grouped('provider_id').items():
            try:
                provider._send_messages(batch)
            except Exception as err:  # noqa: BLE001 - one bad provider must not stall the others
                _logger.exception("tk_pushnotification: delivery through provider %s crashed", provider.name)
                # Count an attempt against the messages this run did not reach,
                # so a deterministic crash cannot loop on them forever.
                unreached = batch.filtered(
                    lambda m: m.state == 'queued' and (not m.last_attempt or m.last_attempt < started)
                )
                unreached._apply_config_error(str(err))
        messages.notification_id._update_state()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _fcm_payload(self):
        self.ensure_one()
        return self.notification_id._fcm_message(token=self.token or None, topic=self.topic or None)

    def _apply_result(self, result: fcm.SendResult, *, dry_run=False):
        """Record the outcome of one attempt.

        ``dry_run``: FCM validated the message without delivering it, and
        answers with a message id all the same. Such deliveries are recorded
        as *validated*, never as *sent*, so a staging database cannot pass
        for one that notified people.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        attempts = self.attempt_count + 1
        vals = {
            'attempt_count': attempts,
            'last_attempt': now,
            'error_code': result.error_code or False,
            'error_message': result.error_message or False,
        }
        if result.ok and dry_run:
            vals.update(state='validated', provider_message_id=result.message_id)
        elif result.ok:
            vals.update(state='sent', sent_date=now, provider_message_id=result.message_id)
        elif result.token_invalid:
            vals['state'] = 'invalid'
        elif result.retryable and attempts < self.provider_id.max_attempts:
            vals['next_attempt'] = now + timedelta(seconds=backoff_delay(attempts, result.retry_after))
        else:
            vals['state'] = 'failed'
        self.write(vals)
        if result.token_invalid:
            # Also cancels whatever else is queued for the same dead token.
            self.device_id._deactivate(result.error_code)

    def _apply_config_error(self, error):
        """The provider could not send at all: retry everything later."""
        for message in self:
            message._apply_result(fcm.SendResult(error_code='CONFIG', error_message=error, retryable=True))

    def action_retry(self):
        self.filtered(lambda m: m.state == 'failed').write({
            'state': 'queued',
            'next_attempt': fields.Datetime.now(),
            'attempt_count': 0,
        })
        self.notification_id.filtered(lambda n: n.state == 'done').write({'state': 'queued', 'done_date': False})
        self._schedule_processing()
