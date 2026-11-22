import json
from contextlib import contextmanager
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo.tests import TransactionCase

from ..models.push_provider import TkPushProvider
from ..tools import fcm


def make_service_account_key(project_id='tk-test-project'):
    """A structurally real key (fresh RSA key pair) that Google has never heard of."""
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    return json.dumps({
        'type': 'service_account',
        'project_id': project_id,
        'private_key_id': 'test-key-id',
        'private_key': pem,
        'client_email': f'push@{project_id}.iam.gserviceaccount.com',
        'token_uri': 'https://oauth2.googleapis.com/token',
    })


FAKE_KEY = make_service_account_key()


class FakeFcmClient:
    """Stands in for fcm.FcmClient: records payloads, replays scripted results."""

    def __init__(self, results=None):
        self.project_id = 'tk-test-project'
        self.sent = []
        self.results = dict(results or {})  # token -> SendResult

    def access_token(self, *, force_refresh=False):
        return 'fake-token'

    def send(self, message, *, validate_only=False):
        self.sent.append((message, validate_only))
        target = message.get('token') or message.get('topic')
        return self.results.get(target, fcm.SendResult(message_id=f'projects/tk-test-project/messages/{len(self.sent)}'))


class PushCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env['tk.push.provider'].create({
            'name': 'Test App',
            'code': 'test_app',
            'fcm_service_account_key': FAKE_KEY,
        })
        cls.user = cls.env['res.users'].create({
            'name': 'Push User', 'login': 'push_user', 'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.colleague = cls.env['res.users'].create({
            'name': 'Push Colleague', 'login': 'push_colleague',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.portal = cls.env['res.users'].create({
            'name': 'Push Portal', 'login': 'push_portal',
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def register(self, user, token, platform='android', **kw):
        result = self.env['tk.push.device'].with_user(user).register(token, platform, **kw)
        return self.env['tk.push.device'].browse(result['device_id'])

    @contextmanager
    def fake_fcm(self, results=None):
        client = FakeFcmClient(results)
        with patch.object(TkPushProvider, '_fcm_get_client', lambda provider: client):
            yield client
