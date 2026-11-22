from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from ..models.push_provider import TkPushProvider
from ..tools import fcm
from .common import PushCase

UNREGISTERED = fcm.SendResult(error_code='UNREGISTERED', error_message='gone', token_invalid=True)
UNAVAILABLE = fcm.SendResult(error_code='UNAVAILABLE', error_message='try later', retryable=True)
BAD_PAYLOAD = fcm.SendResult(error_code='INVALID_ARGUMENT', error_message='nope')


@tagged('post_install', '-at_install')
class TestPushDelivery(PushCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Notification = cls.env['tk.push.notification']
        cls.Message = cls.env['tk.push.message']

    def test_send_fans_out_per_device(self):
        self.register(self.user, 'phone')
        self.register(self.user, 'tablet', platform='ios')
        self.register(self.colleague, 'other')
        notification = self.Notification.send(self.user, "Hello", "World", data={'order_id': 7})
        self.assertEqual(notification.state, 'queued')
        self.assertEqual(len(notification.message_ids), 2)
        self.assertEqual(set(notification.message_ids.mapped('token')), {'phone', 'tablet'})

        with self.fake_fcm() as client:
            self.Message._process_queue()

        self.assertEqual(notification.state, 'done')
        self.assertEqual(notification.sent_count, 2)
        payload = client.sent[0][0]
        self.assertEqual(payload['notification'], {'title': 'Hello', 'body': 'World'})
        self.assertEqual(payload['data'], {'order_id': '7'})

    def test_no_device_closes_immediately(self):
        notification = self.Notification.send(self.user, "Nobody home")
        self.assertEqual(notification.state, 'done')
        self.assertFalse(notification.message_ids)

    def test_dead_token_deactivates_device(self):
        device = self.register(self.user, 'dead')
        notification = self.Notification.send(self.user, "Hi")
        with self.fake_fcm({'dead': UNREGISTERED}):
            self.Message._process_queue()
        self.assertEqual(notification.message_ids.state, 'invalid')
        self.assertFalse(device.active)
        self.assertEqual(device.deactivation_reason, 'UNREGISTERED')
        # The next notification skips it.
        self.assertFalse(self.Notification.send(self.user, "Again").message_ids)

    def test_transient_error_backs_off_then_fails(self):
        self.provider.max_attempts = 2
        self.register(self.user, 'flaky')
        notification = self.Notification.send(self.user, "Hi")
        message = notification.message_ids

        with self.fake_fcm({'flaky': UNAVAILABLE}):
            self.Message._process_queue()
            self.assertEqual(message.state, 'queued')
            self.assertEqual(message.attempt_count, 1)
            self.assertGreater(message.next_attempt, fields.Datetime.now())

            # Not due yet: nothing is sent.
            self.Message._process_queue()
            self.assertEqual(message.attempt_count, 1)

            message.next_attempt = fields.Datetime.now() - timedelta(seconds=1)
            self.Message._process_queue()

        self.assertEqual(message.state, 'failed')
        self.assertEqual(message.attempt_count, 2)
        self.assertEqual(notification.state, 'done')
        self.assertEqual(notification.failed_count, 1)

        message.action_retry()
        self.assertEqual(message.state, 'queued')
        self.assertEqual(notification.state, 'queued')

    def test_permanent_error_fails_without_retry(self):
        self.register(self.user, 'tok')
        notification = self.Notification.send(self.user, "Hi")
        with self.fake_fcm({'tok': BAD_PAYLOAD}):
            self.Message._process_queue()
        self.assertEqual(notification.message_ids.state, 'failed')
        self.assertEqual(notification.message_ids.attempt_count, 1)

    def test_signed_out_device_is_cancelled(self):
        self.register(self.user, 'tok')
        notification = self.Notification.send(self.user, "Hi")
        self.env['tk.push.device'].with_user(self.user).unregister('tok')
        message = notification.message_ids
        self.assertEqual(message.state, 'cancelled')
        self.assertEqual(message.error_code, 'DEVICE_REMOVED')
        self.assertEqual(notification.state, 'done')
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertFalse(client.sent)

    def test_reassigned_device_drops_previous_users_messages(self):
        self.register(self.user, 'shared-phone')
        notification = self.Notification.send(self.user, "Private to the first user")
        self.register(self.colleague, 'shared-phone')
        self.assertEqual(notification.message_ids.state, 'cancelled')
        self.assertEqual(notification.message_ids.error_code, 'DEVICE_REASSIGNED')
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertFalse(client.sent)

    def test_scheduled_notification_waits(self):
        self.register(self.user, 'tok')
        later = fields.Datetime.now() + timedelta(hours=1)
        notification = self.Notification.send(self.user, "Later", scheduled_date=later)
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertFalse(client.sent)
        self.assertEqual(notification.state, 'queued')

    def test_topic(self):
        notification = self.Notification.send(topic='news', title="Breaking")
        self.assertEqual(notification.provider_id, self.provider)
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertEqual(client.sent[0][0]['topic'], 'news')
        self.assertEqual(notification.sent_count, 1)

    def test_invalid_topic_is_rejected_on_save(self):
        for topic, message in (('/topics/news', 'prefix'), ('news alerts', 'not a valid')):
            with self.subTest(topic=topic), self.assertRaisesRegex(ValidationError, message), \
                    self.env.cr.savepoint():
                self.Notification.send(topic=topic, title="Hi")

    def test_dry_run_is_validated_not_sent(self):
        self.provider.validate_only = True
        self.register(self.user, 'tok')
        notification = self.Notification.send(self.user, "Hi")
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertTrue(client.sent[0][1], "validate_only must reach FCM")
        message = notification.message_ids
        self.assertEqual(message.state, 'validated')
        self.assertFalse(message.sent_date)
        self.assertTrue(message.provider_message_id)
        self.assertEqual(notification.state, 'done')
        self.assertEqual(notification.sent_count, 0)
        self.assertEqual(notification.validated_count, 1)

    def test_oversized_payload_is_refused_at_send(self):
        self.register(self.user, 'tok')
        with self.assertRaisesRegex(ValidationError, 'at most 4096'), self.env.cr.savepoint():
            self.Notification.send(self.user, "Hi", "x" * 5000)
        with self.assertRaisesRegex(ValidationError, 'at most 4096'), self.env.cr.savepoint():
            self.Notification.send(self.user, "Hi", data={'blob': 'é' * 2100})  # 2 bytes each
        self.assertFalse(self.Message.search([]))
        # Just under the limit still goes through.
        notification = self.Notification.send(self.user, None, "x" * 4000)
        self.assertEqual(len(notification.message_ids), 1)

    def test_reserved_data_key_is_refused_at_send(self):
        with self.assertRaisesRegex(ValidationError, 'reserved'), self.env.cr.savepoint():
            self.Notification.send(self.user, "Hi", data={'from': 'me'})

    def test_config_error_retries(self):
        self.register(self.user, 'tok')
        notification = self.Notification.send(self.user, "Hi")

        def broken(provider):
            raise fcm.FcmConfigError("key revoked")

        with patch.object(TkPushProvider, '_fcm_get_client', broken):
            self.Message._process_queue()
        message = notification.message_ids
        self.assertEqual(message.state, 'queued')
        self.assertEqual(message.error_code, 'CONFIG')
        self.assertEqual(message.attempt_count, 1)

    def test_dead_token_cancels_its_other_queued_messages(self):
        self.register(self.user, 'dead')
        first = self.Notification.send(self.user, "One")
        second = self.Notification.send(self.user, "Two")
        with self.fake_fcm({'dead': UNREGISTERED}) as client:
            # Deliver only the first one: the second must not be attempted.
            self.Message._send_batch(first.message_ids)
        self.assertEqual(first.message_ids.state, 'invalid')
        self.assertEqual(second.message_ids.state, 'cancelled')
        self.assertEqual(len(client.sent), 1)

    def test_rollback_sends_nothing(self):
        self.register(self.user, 'tok')
        with self.assertRaises(ZeroDivisionError), self.env.cr.savepoint():
            self.Notification.send(self.user, "Hi")
            1 / 0  # noqa: B018
        self.assertFalse(self.Message.search([]))

    def test_ordinary_user_cannot_send_directly(self):
        with self.assertRaises(AccessError):
            self.Notification.with_user(self.user).send(self.colleague, "Hi")

    def test_users_helper_sends_on_behalf(self):
        self.register(self.colleague, 'tok')
        notification = self.colleague.with_user(self.user)._tk_push_send("Hi", "From a colleague")
        self.assertEqual(len(notification.message_ids), 1)
