from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import FAKE_KEY, PushCase


@tagged('post_install', '-at_install')
class TestPushTopic(PushCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Topic = cls.env['tk.push.topic']
        cls.Notification = cls.env['tk.push.notification']
        cls.Message = cls.env['tk.push.message']
        cls.news = cls.Topic.create({
            'name': 'news', 'label': 'News', 'provider_id': cls.provider.id, 'app_offered': True,
        })
        cls.ops = cls.Topic.create({'name': 'ops-alerts', 'provider_id': cls.provider.id})

    def other_provider(self):
        return self.env['tk.push.provider'].create({
            'name': 'Other App', 'code': 'other_app', 'fcm_service_account_key': FAKE_KEY,
        })

    # ------------------------------------------------------------------
    # Refused topics are recorded, never raised
    # ------------------------------------------------------------------

    def assertRefused(self, notification, code):
        """One failed delivery with ``code``, the notification closed, FCM never called."""
        message = notification.message_ids
        self.assertEqual(len(message), 1)
        self.assertEqual(message.state, 'failed')
        self.assertEqual(message.error_code, code)
        self.assertTrue(message.error_message)
        self.assertEqual(notification.state, 'done')
        self.assertEqual(notification.failed_count, 1)
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertFalse(client.sent)

    def test_unlisted_topic_fails_without_raising(self):
        notification = self.Notification.send(topic='nwes', title="Typo")
        self.assertFalse(notification.topic_id)
        self.assertEqual(notification.topic, 'nwes')
        self.assertRefused(notification, 'TOPIC_UNKNOWN')

    def test_invalid_topic_name_fails_without_raising(self):
        for name in ('/topics/news', 'news alerts'):
            with self.subTest(topic=name):
                notification = self.Notification.send(topic=name, title="Hi")
                self.assertRefused(notification, 'TOPIC_INVALID')
        message = self.Notification.send(topic='/topics/news', title="Hi").message_ids
        self.assertIn('prefix', message.error_message)

    def test_archived_topic_fails_without_raising(self):
        self.news.active = False
        notification = self.Notification.send(topic='news', title="Hi")
        self.assertEqual(notification.topic_id, self.news)
        self.assertRefused(notification, 'TOPIC_ARCHIVED')

    def test_topic_archived_after_queueing_is_cancelled(self):
        notification = self.Notification.send(topic='news', title="Hi")
        self.assertEqual(notification.message_ids.state, 'queued')
        self.news.active = False
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertFalse(client.sent)
        self.assertEqual(notification.message_ids.state, 'cancelled')
        self.assertEqual(notification.message_ids.error_code, 'TOPIC_ARCHIVED')
        self.assertEqual(notification.state, 'done')

    def test_retry_after_listing_the_topic_sends(self):
        notification = self.Notification.send(topic='flash-sale', title="Hi")
        self.assertEqual(notification.message_ids.error_code, 'TOPIC_UNKNOWN')
        self.Topic.create({'name': 'flash-sale', 'provider_id': self.provider.id})
        notification.message_ids.action_retry()
        with self.fake_fcm() as client:
            self.Message._process_queue()
        self.assertEqual(client.sent[0][0]['topic'], 'flash-sale')
        self.assertEqual(notification.message_ids.state, 'sent')

    # ------------------------------------------------------------------
    # Addressing
    # ------------------------------------------------------------------

    def test_send_with_topic_record_uses_its_provider(self):
        other = self.other_provider()
        promo = self.Topic.create({'name': 'promo', 'provider_id': other.id})
        notification = self.Notification.send(topic=promo, title="Hi")
        self.assertEqual(notification.provider_id, other)
        self.assertEqual(notification.topic, 'promo')
        self.assertEqual(notification.message_ids.state, 'queued')

    def test_name_resolves_to_the_only_provider_listing_it(self):
        other = self.other_provider()
        promo = self.Topic.create({'name': 'promo', 'provider_id': other.id})
        notification = self.Notification.send(topic='promo', title="Hi")
        self.assertEqual(notification.provider_id, other)
        self.assertEqual(notification.topic_id, promo)
        self.assertEqual(notification.message_ids.state, 'queued')

    def test_same_name_is_per_provider(self):
        other = self.other_provider()
        self.Topic.create({'name': 'news', 'provider_id': other.id})
        with self.assertRaises(Exception), mute_logger('odoo.sql_db'), self.env.cr.savepoint():
            self.Topic.create({'name': 'news', 'provider_id': self.provider.id})  # UNIQUE(provider_id, name)
            self.env.flush_all()
        notification = self.Notification.send(topic='news', title="Hi", provider='other_app')
        self.assertEqual(notification.provider_id, other)

    def test_choosing_a_topic_sets_name_and_provider(self):
        notification = self.Notification.create({'target': 'topic', 'topic_id': self.ops.id, 'title': "Hi"})
        self.assertEqual(notification.topic, 'ops-alerts')
        self.assertEqual(notification.provider_id, self.provider)
        notification.action_send()
        self.assertEqual(notification.message_ids.topic, 'ops-alerts')

    def test_form_send_requires_a_topic(self):
        notification = self.Notification.create({'target': 'topic', 'title': "Hi"})
        with self.assertRaisesRegex(UserError, 'Choose a topic'):
            notification.action_send()

    # ------------------------------------------------------------------
    # The list itself
    # ------------------------------------------------------------------

    def test_listing_an_invalid_name_is_refused(self):
        with self.assertRaisesRegex(ValidationError, 'prefix'), self.env.cr.savepoint():
            self.Topic.create({'name': '/topics/x', 'provider_id': self.provider.id})

    def test_used_topic_cannot_be_renamed_or_deleted(self):
        self.Notification.send(topic='news', title="Hi")
        with self.assertRaisesRegex(UserError, 'renamed'), self.env.cr.savepoint():
            self.news.name = 'headlines'
        with self.assertRaisesRegex(UserError, 'Archive'), self.env.cr.savepoint():
            self.news.unlink()
        self.news.write({'name': 'news', 'label': 'Headlines'})  # same name, other fields: fine
        self.news.active = False

    def test_unused_topic_can_be_renamed_and_deleted(self):
        self.ops.name = 'ops'
        self.ops.unlink()

    def test_app_users_read_only_offered_topics(self):
        for user in (self.user, self.portal):
            with self.subTest(user=user.login):
                Topic = self.Topic.with_user(user)
                rows = Topic.search_read([('provider_code', '=', 'test_app')])
                self.assertEqual([row['name'] for row in rows], ['news'])
                self.assertNotIn('notification_count', rows[0])
                with self.assertRaises(AccessError):
                    Topic.create({'name': 'mine', 'provider_id': self.provider.id})
                with self.assertRaises(AccessError):
                    self.news.with_user(user).write({'label': 'Mine'})
