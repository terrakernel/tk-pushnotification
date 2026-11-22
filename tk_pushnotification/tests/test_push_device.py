import json

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import FAKE_KEY, PushCase


@tagged('post_install', '-at_install')
class TestPushDevice(PushCase):

    def test_register_is_idempotent(self):
        first = self.register(self.user, 'tok-1', device_name='Pixel')
        second = self.register(self.user, 'tok-1', app_version='2.0')
        self.assertEqual(first, second)
        self.assertEqual(first.user_id, self.user)
        self.assertEqual(first.provider_id, self.provider)
        self.assertEqual(first.app_version, '2.0')

    def test_token_moves_to_new_user(self):
        device = self.register(self.user, 'shared-phone')
        self.register(self.colleague, 'shared-phone')
        self.assertEqual(device.user_id, self.colleague)
        self.assertEqual(self.env['tk.push.device'].search_count([('token', '=', 'shared-phone')]), 1)

    def test_register_reactivates_dead_token(self):
        device = self.register(self.user, 'tok-dead')
        device._deactivate('UNREGISTERED')
        self.assertFalse(device.active)
        self.register(self.user, 'tok-dead')
        self.assertTrue(device.active)
        self.assertFalse(device.deactivation_reason)

    def test_register_rejects_bad_input(self):
        with self.assertRaises(UserError):
            self.register(self.user, '   ')
        with self.assertRaises(UserError):
            self.register(self.user, 'tok', platform='symbian')
        with self.assertRaises(UserError):
            self.register(self.user, 'tok', provider_code='no_such_app')

    def test_public_user_cannot_register(self):
        with self.assertRaises(AccessError):
            self.register(self.env.ref('base.public_user'), 'tok-public')

    def test_unregister_only_own_device(self):
        self.register(self.user, 'tok-mine')
        result = self.env['tk.push.device'].with_user(self.colleague).unregister('tok-mine')
        self.assertEqual(result['removed'], 0)
        result = self.env['tk.push.device'].with_user(self.user).unregister('tok-mine')
        self.assertEqual(result['removed'], 1)

    def test_users_see_only_their_devices(self):
        mine = self.register(self.portal, 'tok-portal')
        self.register(self.colleague, 'tok-colleague')
        Device = self.env['tk.push.device'].with_user(self.portal)
        self.assertEqual(Device.search([]), mine)
        with self.assertRaises(AccessError):
            Device.create({'user_id': self.portal.id, 'provider_id': self.provider.id,
                           'token': 'forged', 'platform': 'android'})


@tagged('post_install', '-at_install')
class TestPushProvider(PushCase):

    def test_project_id_comes_from_key(self):
        self.assertFalse(self.provider.fcm_project_id)
        client = self.provider._fcm_build_client()
        self.assertEqual(client.project_id, 'tk-test-project')

    def test_rejects_broken_keys(self):
        broken = json.loads(FAKE_KEY)
        broken['private_key'] = '-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n'
        for key in ('not json', json.dumps({'type': 'authorized_user'}), json.dumps(broken)):
            with self.subTest(key=key[:30]), self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.provider.fcm_service_account_key = key
                self.provider.flush_recordset()
