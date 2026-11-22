"""Unit tests for tk_pushnotification/tools/fcm.py that need no Odoo.

    python -m unittest discover -s tests

The module is loaded by path: importing it as a package would import the
addon's __init__, and with it Odoo.
"""
import base64
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

FCM_PATH = pathlib.Path(__file__).resolve().parent.parent / 'tk_pushnotification' / 'tools' / 'fcm.py'
spec = importlib.util.spec_from_file_location('tk_fcm', FCM_PATH)
fcm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fcm  # dataclasses look the module up
spec.loader.exec_module(fcm)


def fcm_error(http_status, status, message, error_code=None):
    details = [{'@type': fcm.FCM_ERROR_TYPE, 'errorCode': error_code}] if error_code else []
    return json.dumps({'error': {'code': http_status, 'message': message, 'status': status, 'details': details}})


class TestBuildMessage(unittest.TestCase):

    def test_notification_message(self):
        message = fcm.build_message(token='t', title='Hi', body='There', data={'id': 7, 'ok': True, 'skip': None})
        self.assertEqual(message['token'], 't')
        self.assertEqual(message['notification'], {'title': 'Hi', 'body': 'There'})
        self.assertEqual(message['data'], {'id': '7', 'ok': 'true'})
        self.assertEqual(message['android'], {'priority': 'HIGH'})
        self.assertEqual(message['apns']['headers'], {'apns-push-type': 'alert', 'apns-priority': '10'})
        self.assertEqual(message['apns']['payload'], {'aps': {'sound': 'default'}})

    def test_data_only_message_is_background_on_ios(self):
        message = fcm.build_message(token='t', data={'sync': 'now'})
        self.assertNotIn('notification', message)
        self.assertEqual(message['apns']['headers'], {'apns-push-type': 'background', 'apns-priority': '5'})
        self.assertEqual(message['apns']['payload'], {'aps': {'content-available': 1}})

    def test_ttl_collapse_and_priority(self):
        message = fcm.build_message(
            topic='news', title='x', priority='normal', ttl=600, collapse_key='k' * 100, now=1000,
        )
        self.assertEqual(message['topic'], 'news')
        self.assertEqual(message['android'], {'priority': 'NORMAL', 'ttl': '600s', 'collapse_key': 'k' * 100})
        headers = message['apns']['headers']
        self.assertEqual(headers['apns-priority'], '5')
        self.assertEqual(headers['apns-expiration'], '1600')
        self.assertEqual(len(headers['apns-collapse-id']), 64)
        self.assertEqual(message['webpush']['headers'], {'Urgency': 'normal', 'TTL': '600'})

    def test_zero_ttl_means_now_or_never(self):
        message = fcm.build_message(token='t', title='x', ttl=0)
        self.assertEqual(message['android']['ttl'], '0s')
        self.assertEqual(message['apns']['headers']['apns-expiration'], '0')

    def test_link(self):
        message = fcm.build_message(token='t', title='x', link='https://example.com/o/1')
        self.assertEqual(message['data']['link'], 'https://example.com/o/1')
        self.assertEqual(message['webpush']['fcm_options'], {'link': 'https://example.com/o/1'})
        # FCM refuses non-HTTPS web links; apps still get it as data.
        message = fcm.build_message(token='t', title='x', link='myapp://order/1')
        self.assertNotIn('fcm_options', message['webpush'])
        self.assertEqual(message['data']['link'], 'myapp://order/1')

    def test_overrides_win(self):
        message = fcm.build_message(
            token='t', title='x',
            overrides={'android': {'notification': {'channel_id': 'orders'}, 'priority': 'NORMAL'}},
        )
        self.assertEqual(message['android'], {'priority': 'NORMAL', 'notification': {'channel_id': 'orders'}})

    def test_payload_size_counts_notification_and_data_in_utf8(self):
        message = fcm.build_message(token='t' * 500, title='é', data={'k': 'v'})
        # Only notification + data count: not the token, nor platform blocks.
        self.assertEqual(fcm.payload_size(message), len('{"notification":{"title":"é"},"data":{"k":"v"}}'.encode()))
        self.assertEqual(fcm.payload_size({'token': 't'}), 0)

    def test_oversized_payload_is_refused(self):
        fcm.build_message(token='t', body='x' * 4000)
        with self.assertRaisesRegex(ValueError, 'at most 4096'):
            fcm.build_message(token='t', body='x' * 4100)
        with self.assertRaisesRegex(ValueError, 'at most 4096'):
            fcm.build_message(token='t', data={'k': '€' * 1400})  # 3 bytes each in UTF-8
        # Overrides count too: they are merged before the check.
        with self.assertRaisesRegex(ValueError, 'at most 4096'):
            fcm.build_message(token='t', title='x', overrides={'data': {'big': 'x' * 4100}})

    def test_topic_names(self):
        for topic in ('news', 'Promo_2026', 'a-b.c~d%20e', 'x' * 200):
            with self.subTest(topic=topic):
                self.assertTrue(fcm.is_valid_topic(topic))
                self.assertEqual(fcm.build_message(topic=topic, title='x')['topic'], topic)
        for topic in ('', None, '/topics/news', 'news alerts', 'a/b', 'café', 'news\n'):
            with self.subTest(topic=topic):
                self.assertFalse(fcm.is_valid_topic(topic))

    def test_invalid_input(self):
        for kwargs in (
            {},
            {'token': 't', 'topic': 'x'},
            {'token': 't', 'priority': 'urgent'},
            {'token': 't', 'ttl': -1},
            {'token': 't', 'data': {'from': 'x'}},
            {'token': 't', 'data': {'google.foo': 'x'}},
            {'topic': '/topics/news'},
            {'topic': 'news alerts'},
            {'topic': 'nouvelles-é'},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fcm.build_message(**kwargs)


class TestParseResponse(unittest.TestCase):

    def test_success(self):
        result = fcm.parse_response(200, '{"name": "projects/p/messages/123"}')
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, 'projects/p/messages/123')

    def test_unregistered_token(self):
        result = fcm.parse_response(404, fcm_error(404, 'NOT_FOUND', 'Requested entity was not found.', 'UNREGISTERED'))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'UNREGISTERED')
        self.assertTrue(result.token_invalid)
        self.assertFalse(result.retryable)

    def test_sender_mismatch_is_dead_token(self):
        result = fcm.parse_response(403, fcm_error(403, 'PERMISSION_DENIED', 'SenderId mismatch', 'SENDER_ID_MISMATCH'))
        self.assertTrue(result.token_invalid)

    def test_malformed_token(self):
        body = fcm_error(400, 'INVALID_ARGUMENT', 'The registration token is not a valid FCM registration token',
                         'INVALID_ARGUMENT')
        self.assertTrue(fcm.parse_response(400, body).token_invalid)

    def test_malformed_payload_is_permanent_not_token(self):
        body = fcm_error(400, 'INVALID_ARGUMENT', 'Invalid value at message.android.ttl', 'INVALID_ARGUMENT')
        result = fcm.parse_response(400, body)
        self.assertFalse(result.token_invalid)
        self.assertFalse(result.retryable)

    def test_wrong_project_does_not_kill_tokens(self):
        result = fcm.parse_response(404, fcm_error(404, 'NOT_FOUND', 'Project not found'))
        self.assertEqual(result.error_code, 'NOT_FOUND')
        self.assertFalse(result.token_invalid)
        self.assertFalse(result.retryable)

    def test_quota_with_retry_after(self):
        result = fcm.parse_response(
            429, fcm_error(429, 'RESOURCE_EXHAUSTED', 'Quota', 'QUOTA_EXCEEDED'), {'Retry-After': '120'},
        )
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after, 120.0)

    def test_unavailable_and_bare_5xx(self):
        self.assertTrue(fcm.parse_response(503, fcm_error(503, 'UNAVAILABLE', 'x', 'UNAVAILABLE')).retryable)
        result = fcm.parse_response(502, '<html>Bad Gateway</html>')
        self.assertTrue(result.retryable)
        self.assertEqual(result.error_code, 'HTTP_502')

    def test_apns_auth_error_is_permanent(self):
        body = fcm_error(401, 'UNAUTHENTICATED', 'Auth error from APNS', 'THIRD_PARTY_AUTH_ERROR')
        result = fcm.parse_response(401, body)
        self.assertEqual(result.error_code, 'THIRD_PARTY_AUTH_ERROR')
        self.assertFalse(result.retryable)
        self.assertFalse(result.token_invalid)


class FakeCredentials:
    def __init__(self, fail=False):
        self.valid = False
        self.token = None
        self.refreshes = 0
        self.fail = fail

    def refresh(self, session, timeout):
        if self.fail:
            raise fcm.FcmConfigError('invalid_grant')
        self.refreshes += 1
        self.valid = True
        self.token = f'token-{self.refreshes}'


def response(status, body='', headers=None, json_body=None):
    resp = mock.Mock(status_code=status, text=json.dumps(json_body) if json_body is not None else body,
                     headers=headers or {})
    resp.json.side_effect = lambda: json.loads(resp.text)
    return resp


class TestClient(unittest.TestCase):

    def setUp(self):
        self.session = mock.Mock()
        self.credentials = FakeCredentials()
        self.client = fcm.FcmClient('proj', self.credentials, session=self.session, timeout=5)

    def test_send_uses_cached_token(self):
        self.session.post.return_value = response(200, '{"name": "projects/proj/messages/1"}')
        self.assertTrue(self.client.send({'token': 't'}).ok)
        self.assertTrue(self.client.send({'token': 't'}, validate_only=True).ok)
        self.assertEqual(self.credentials.refreshes, 1)
        url = self.session.post.call_args.args[0]
        kwargs = self.session.post.call_args.kwargs
        self.assertEqual(url, 'https://fcm.googleapis.com/v1/projects/proj/messages:send')
        self.assertEqual(kwargs['json'], {'message': {'token': 't'}, 'validate_only': True})
        self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer token-1'})
        self.assertEqual(kwargs['timeout'], 5)

    def test_401_refreshes_once(self):
        self.session.post.side_effect = [
            response(401, fcm_error(401, 'UNAUTHENTICATED', 'expired')),
            response(200, '{"name": "m"}'),
        ]
        self.assertTrue(self.client.send({'token': 't'}).ok)
        self.assertEqual(self.credentials.refreshes, 2)
        self.assertEqual(self.session.post.call_args.kwargs['headers'], {'Authorization': 'Bearer token-2'})

    def test_network_error_is_retryable(self):
        self.session.post.side_effect = requests.ConnectionError('reset')
        result = self.client.send({'token': 't'})
        self.assertEqual(result.error_code, 'NETWORK')
        self.assertTrue(result.retryable)

    def test_refresh_failure_raises_config_error(self):
        client = fcm.FcmClient('proj', FakeCredentials(fail=True), session=self.session)
        with self.assertRaises(fcm.FcmConfigError):
            client.send({'token': 't'})
        self.session.post.assert_not_called()

    def test_project_required(self):
        with self.assertRaises(fcm.FcmConfigError):
            fcm.FcmClient('', self.credentials)

    def test_bad_service_account_key(self):
        with self.assertRaises(fcm.FcmConfigError):
            fcm.FcmClient.from_service_account_info({'type': 'service_account', 'project_id': 'p'})


def b64url_decode(part):
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))


class TestServiceAccountCredentials(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.info = {
            'type': 'service_account',
            'project_id': 'proj',
            'private_key_id': 'kid-1',
            'private_key': cls.private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ).decode(),
            'client_email': 'push@proj.iam.gserviceaccount.com',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }

    def test_jwt_is_signed_rs256_with_google_claims(self):
        credentials = fcm.ServiceAccountCredentials.from_info(self.info)
        assertion = credentials._assertion(now=1_700_000_000)
        header, claims, signature = assertion.split('.')
        self.assertEqual(json.loads(b64url_decode(header)), {'alg': 'RS256', 'typ': 'JWT', 'kid': 'kid-1'})
        self.assertEqual(json.loads(b64url_decode(claims)), {
            'iss': 'push@proj.iam.gserviceaccount.com',
            'scope': fcm.FCM_SCOPE,
            'aud': 'https://oauth2.googleapis.com/token',
            'iat': 1_700_000_000,
            'exp': 1_700_003_600,
        })
        # Raises InvalidSignature if the signature is wrong.
        self.private_key.public_key().verify(
            b64url_decode(signature), f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256(),
        )

    def test_token_exchange(self):
        session = mock.Mock()
        session.post.return_value = response(200, '', json_body={'access_token': 'ya29.x', 'expires_in': 3599})
        credentials = fcm.ServiceAccountCredentials.from_info(self.info)
        self.assertFalse(credentials.valid)
        credentials.refresh(session, 7)
        self.assertTrue(credentials.valid)
        self.assertEqual(credentials.token, 'ya29.x')
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], 'https://oauth2.googleapis.com/token')
        self.assertEqual(kwargs['data']['grant_type'], 'urn:ietf:params:oauth:grant-type:jwt-bearer')
        self.assertEqual(kwargs['data']['assertion'].count('.'), 2)
        self.assertEqual(kwargs['timeout'], 7)

    def test_token_expiring_soon_is_not_valid(self):
        credentials = fcm.ServiceAccountCredentials.from_info(self.info)
        credentials.token = 'x'
        credentials.expiry = time.time() + fcm.TOKEN_EXPIRY_MARGIN - 1
        self.assertFalse(credentials.valid)

    def test_refused_grant(self):
        session = mock.Mock()
        session.post.return_value = response(400, '', json_body={
            'error': 'invalid_grant', 'error_description': 'Invalid JWT Signature.'})
        credentials = fcm.ServiceAccountCredentials.from_info(self.info)
        with self.assertRaisesRegex(fcm.FcmConfigError, 'invalid_grant: Invalid JWT Signature'):
            credentials.refresh(session, 5)

    def test_network_failure_is_config_error(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError('dns')
        with self.assertRaises(fcm.FcmConfigError):
            fcm.ServiceAccountCredentials.from_info(self.info).refresh(session, 5)

    def test_rejects_bad_keys(self):
        for info in (
            {**self.info, 'type': 'authorized_user'},
            {**self.info, 'client_email': ''},
            {**self.info, 'private_key': '-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n'},
            {**self.info, 'private_key': ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ).decode()},
        ):
            with self.subTest(info=info.get('type')), self.assertRaises(fcm.FcmConfigError):
                fcm.ServiceAccountCredentials.from_info(info)

    def test_application_default_from_key_file(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as file:
            json.dump(self.info, file)
        self.addCleanup(os.unlink, file.name)
        with mock.patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': file.name}):
            client = fcm.FcmClient.from_application_default(session=mock.Mock())
        self.assertEqual(client.project_id, 'proj')
        self.assertIsInstance(client._credentials, fcm.ServiceAccountCredentials)

    def test_application_default_rejects_user_credentials(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as file:
            json.dump({'type': 'authorized_user', 'refresh_token': 'x'}, file)
        self.addCleanup(os.unlink, file.name)
        with mock.patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': file.name}), \
                self.assertRaisesRegex(fcm.FcmConfigError, 'authorized_user'):
            fcm.FcmClient.from_application_default(session=mock.Mock())


class TestMetadataCredentials(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.dict(os.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('GOOGLE_APPLICATION_CREDENTIALS', None)
        os.environ.pop('GOOGLE_CLOUD_PROJECT', None)

    def test_metadata_server(self):
        session = mock.Mock()
        session.get.side_effect = [
            response(200, 'gcp-proj\n'),
            response(200, '', json_body={'access_token': 'meta-token', 'expires_in': 1800}),
        ]
        client = fcm.FcmClient.from_application_default(session=session)
        self.assertEqual(client.project_id, 'gcp-proj')
        self.assertEqual(client.access_token(), 'meta-token')
        url = session.get.call_args.args[0]
        kwargs = session.get.call_args.kwargs
        self.assertTrue(url.endswith('/instance/service-accounts/default/token'))
        self.assertEqual(kwargs['headers'], {'Metadata-Flavor': 'Google'})
        self.assertEqual(kwargs['params'], {'scopes': fcm.FCM_SCOPE})

    def test_explicit_project_skips_metadata_lookup(self):
        session = mock.Mock()
        client = fcm.FcmClient.from_application_default(project_id='given', session=session)
        self.assertEqual(client.project_id, 'given')
        session.get.assert_not_called()

    def test_nothing_available(self):
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError('no metadata server')
        with self.assertRaisesRegex(fcm.FcmConfigError, 'No Application Default Credentials'):
            fcm.FcmClient.from_application_default(session=session)


if __name__ == '__main__':
    unittest.main()
