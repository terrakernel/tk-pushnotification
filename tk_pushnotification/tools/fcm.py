"""Firebase Cloud Messaging HTTP v1 client.

This module deliberately imports nothing from Odoo: it builds FCM messages,
sends them and classifies the responses, and can be unit-tested on its own.
The Odoo models own everything stateful (queueing, retries, device lifecycle)
and only ask this module two questions: *what do I send* and *what happened*.

It also needs nothing beyond what Odoo itself requires: Google's OAuth2
service-account flow is implemented here with ``cryptography`` and
``requests``, both hard dependencies of Odoo, instead of pulling in
google-auth.

References:
- https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages
- https://developers.google.com/identity/protocols/oauth2/service-account#httprest
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
FCM_SEND_URL = 'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'

GOOGLE_TOKEN_URI = 'https://oauth2.googleapis.com/token'
JWT_BEARER_GRANT = 'urn:ietf:params:oauth:grant-type:jwt-bearer'
JWT_LIFETIME = 3600  # the maximum Google accepts

# GCE, GKE (workload identity), Cloud Run and Cloud Functions all serve the
# attached service account's tokens here.
METADATA_ROOT = 'http://metadata.google.internal/computeMetadata/v1'
METADATA_HEADERS = {'Metadata-Flavor': 'Google'}
METADATA_TIMEOUT = 3

# Refresh this long before the token actually expires, so that a token never
# lapses between being handed out and reaching FCM.
TOKEN_EXPIRY_MARGIN = 300

# FcmError.errorCode values meaning the token will never work again: the app
# was uninstalled, the token expired, or it belongs to another Firebase project.
TOKEN_DEAD_CODES = frozenset({'UNREGISTERED', 'SENDER_ID_MISMATCH'})

# Transient conditions FCM asks clients to retry with exponential backoff.
RETRYABLE_CODES = frozenset({'QUOTA_EXCEEDED', 'UNAVAILABLE', 'INTERNAL'})
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# FCM refuses these as data keys.
RESERVED_DATA_KEYS = frozenset({'from', 'notification', 'message_type'})
RESERVED_DATA_PREFIXES = ('google', 'gcm')

# The FcmError detail carries the precise reason behind a generic HTTP status.
FCM_ERROR_TYPE = 'type.googleapis.com/google.firebase.fcm.v1.FcmError'


class FcmConfigError(Exception):
    """The client cannot be built or authenticated: no message can be sent."""


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of one send. Exactly one of ``message_id`` / ``error_code`` is set."""

    message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    token_invalid: bool = False
    retry_after: float | None = None  # seconds, from the Retry-After header

    @property
    def ok(self) -> bool:
        return self.message_id is not None


# ----------------------------------------------------------------------
# Message building
# ----------------------------------------------------------------------

def encode_data(data: Mapping | None) -> dict[str, str]:
    """FCM data payloads are a flat str -> str map. JSON-encode anything else."""
    encoded = {}
    for key, value in (data or {}).items():
        key = str(key)
        if key in RESERVED_DATA_KEYS or key.lower().startswith(RESERVED_DATA_PREFIXES):
            raise ValueError(f"'{key}' is a reserved FCM data key")
        if value is None:
            continue
        encoded[key] = value if isinstance(value, str) else json.dumps(value, default=str)
    return encoded


def deep_merge(base: dict, override: Mapping) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_message(
    *,
    token: str | None = None,
    topic: str | None = None,
    title: str | None = None,
    body: str | None = None,
    image: str | None = None,
    data: Mapping | None = None,
    link: str | None = None,
    priority: str = 'high',
    ttl: int | None = None,
    collapse_key: str | None = None,
    overrides: Mapping | None = None,
    now: float | None = None,
) -> dict:
    """Return the ``message`` object of an FCM v1 send request.

    One logical notification is expanded into the platform blocks each OS
    needs, so that callers think in terms of title / body / priority and not
    in APNs header names. ``overrides`` is deep-merged last and wins, as the
    escape hatch for anything this function does not model.

    ``ttl`` is in seconds: ``None`` keeps FCM's default (4 weeks), ``0`` means
    deliver now or drop.
    """
    if bool(token) == bool(topic):
        raise ValueError("Exactly one of token or topic must be given")
    if priority not in ('high', 'normal'):
        raise ValueError(f"Unknown priority {priority!r}")
    if ttl is not None and ttl < 0:
        raise ValueError("ttl cannot be negative")

    message: dict = {'token': token} if token else {'topic': topic}
    high = priority == 'high'

    notification = {key: value for key, value in (('title', title), ('body', body), ('image', image)) if value}
    if notification:
        message['notification'] = notification

    payload = encode_data(data)
    if link:
        payload.setdefault('link', link)
    if payload:
        message['data'] = payload

    android: dict = {'priority': 'HIGH' if high else 'NORMAL'}
    if ttl is not None:
        android['ttl'] = f'{int(ttl)}s'
    if collapse_key:
        android['collapse_key'] = collapse_key
    message['android'] = android

    # APNs rejects priority 10 for pushes without an alert, and silently drops
    # background pushes not flagged content-available.
    if notification:
        apns_headers = {'apns-push-type': 'alert', 'apns-priority': '10' if high else '5'}
        aps: dict = {'sound': 'default'}
    else:
        apns_headers = {'apns-push-type': 'background', 'apns-priority': '5'}
        aps = {'content-available': 1}
    if ttl is not None:
        now = time.time() if now is None else now
        apns_headers['apns-expiration'] = str(int(now) + int(ttl)) if ttl else '0'
    if collapse_key:
        apns_headers['apns-collapse-id'] = collapse_key[:64]  # APNs hard limit
    message['apns'] = {'headers': apns_headers, 'payload': {'aps': aps}}

    webpush: dict = {'headers': {'Urgency': 'high' if high else 'normal'}}
    if ttl is not None:
        webpush['headers']['TTL'] = str(int(ttl))
    if link and link.startswith('https://'):  # FCM requires HTTPS here
        webpush['fcm_options'] = {'link': link}
    message['webpush'] = webpush

    if overrides:
        message = deep_merge(message, overrides)
    return message


# ----------------------------------------------------------------------
# Response classification
# ----------------------------------------------------------------------

def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        return max(parsedate_to_datetime(value).timestamp() - time.time(), 0.0)
    except (TypeError, ValueError):
        return None


def parse_response(status_code: int, body: str, headers: Mapping | None = None) -> SendResult:
    """Turn an FCM HTTP response into a :class:`SendResult`."""
    try:
        decoded = json.loads(body) if body else {}
    except ValueError:
        decoded = {}
    if not isinstance(decoded, dict):
        decoded = {}

    if 200 <= status_code < 300:
        return SendResult(message_id=decoded.get('name') or '')

    error = decoded.get('error') if isinstance(decoded.get('error'), dict) else {}
    status = error.get('status')
    message = error.get('message') or (body or '')[:500] or f'HTTP {status_code}'
    fcm_code = next(
        (
            detail.get('errorCode')
            for detail in error.get('details') or ()
            if isinstance(detail, dict) and detail.get('@type') == FCM_ERROR_TYPE
        ),
        None,
    )
    code = fcm_code or status or f'HTTP_{status_code}'

    # A malformed or truncated token comes back as a plain INVALID_ARGUMENT,
    # indistinguishable by code from a malformed payload: only the text tells.
    token_invalid = fcm_code in TOKEN_DEAD_CODES or (
        code == 'INVALID_ARGUMENT' and 'registration token' in message.lower()
    )
    retryable = not token_invalid and (
        fcm_code in RETRYABLE_CODES or status_code in RETRYABLE_HTTP_STATUSES
    )
    retry_after = _parse_retry_after((headers or {}).get('Retry-After')) if retryable else None
    return SendResult(
        error_code=code,
        error_message=message,
        retryable=retryable,
        token_invalid=token_invalid,
        retry_after=retry_after,
    )


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _token_error(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f'HTTP {response.status_code}: {response.text[:200]}'
    if isinstance(body, dict):
        error = body.get('error')
        description = body.get('error_description')
        if isinstance(error, dict):  # Google APIs style
            return error.get('message') or json.dumps(error)
        if error:
            return f'{error}: {description}' if description else str(error)
    return f'HTTP {response.status_code}'


class _Credentials:
    """An OAuth2 access token and the means to renew it."""

    token: str | None = None
    expiry: float = 0.0  # epoch seconds

    @property
    def valid(self) -> bool:
        return bool(self.token) and time.time() < self.expiry - TOKEN_EXPIRY_MARGIN

    def refresh(self, session, timeout: float) -> None:
        """Fetch a new token. Raises :class:`FcmConfigError`."""
        try:
            token, expires_in = self._fetch(session, timeout)
        except requests.RequestException as err:
            raise FcmConfigError(f"Could not reach the Google token endpoint: {err}") from err
        self.token = token
        self.expiry = time.time() + expires_in

    def _fetch(self, session, timeout: float) -> tuple[str, float]:
        raise NotImplementedError

    @staticmethod
    def _parse_token(response) -> tuple[str, float]:
        if response.status_code != 200:
            raise FcmConfigError(f"Google refused to issue an access token: {_token_error(response)}")
        try:
            body = response.json()
            return body['access_token'], float(body.get('expires_in', 3600))
        except (ValueError, KeyError, TypeError) as err:
            raise FcmConfigError(f"Unexpected token response from Google: {err}") from err


class ServiceAccountCredentials(_Credentials):
    """Two-legged OAuth2 with a service account key: a JWT signed with the
    account's RSA key (RS256) is exchanged for an access token."""

    def __init__(self, client_email: str, private_key, *, private_key_id: str | None = None,
                 token_uri: str = GOOGLE_TOKEN_URI, scope: str = FCM_SCOPE):
        self.client_email = client_email
        self._private_key = private_key
        self.private_key_id = private_key_id
        self.token_uri = token_uri
        self.scope = scope

    @classmethod
    def from_info(cls, info: Mapping) -> ServiceAccountCredentials:
        """Build from the parsed JSON key file. Raises :class:`FcmConfigError`."""
        if not isinstance(info, Mapping) or info.get('type') != 'service_account':
            raise FcmConfigError("Not a service account key: its 'type' must be 'service_account'")
        missing = [key for key in ('client_email', 'private_key') if not info.get(key)]
        if missing:
            raise FcmConfigError(f"The service account key lacks: {', '.join(missing)}")
        try:
            private_key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
        except (ValueError, TypeError) as err:
            raise FcmConfigError(f"The service account private key cannot be read: {err}") from err
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise FcmConfigError("The service account private key is not an RSA key")
        return cls(
            info['client_email'],
            private_key,
            private_key_id=info.get('private_key_id'),
            token_uri=info.get('token_uri') or GOOGLE_TOKEN_URI,
        )

    def _assertion(self, now: int) -> str:
        header = {'alg': 'RS256', 'typ': 'JWT'}
        if self.private_key_id:
            header['kid'] = self.private_key_id
        claims = {
            'iss': self.client_email,
            'scope': self.scope,
            'aud': self.token_uri,
            'iat': now,
            'exp': now + JWT_LIFETIME,
        }
        signing_input = '.'.join(
            _b64url(json.dumps(part, separators=(',', ':')).encode()) for part in (header, claims)
        )
        signature = self._private_key.sign(signing_input.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
        return f'{signing_input}.{_b64url(signature)}'

    def _fetch(self, session, timeout):
        response = session.post(
            self.token_uri,
            data={'grant_type': JWT_BEARER_GRANT, 'assertion': self._assertion(int(time.time()))},
            timeout=timeout,
        )
        return self._parse_token(response)


class MetadataCredentials(_Credentials):
    """The service account attached to the GCP workload, via the metadata server."""

    def __init__(self, scope: str = FCM_SCOPE):
        self.scope = scope

    def _fetch(self, session, timeout):
        response = session.get(
            f'{METADATA_ROOT}/instance/service-accounts/default/token',
            params={'scopes': self.scope},
            headers=METADATA_HEADERS,
            timeout=min(timeout, METADATA_TIMEOUT),
        )
        return self._parse_token(response)

    @staticmethod
    def project_id(session) -> str | None:
        try:
            response = session.get(f'{METADATA_ROOT}/project/project-id',
                                   headers=METADATA_HEADERS, timeout=METADATA_TIMEOUT)
        except requests.RequestException:
            return None
        return response.text.strip() if response.status_code == 200 else None


def application_default_credentials(session, project_id: str | None = None) -> tuple[_Credentials, str | None]:
    """Resolve Application Default Credentials, as far as a server needs them.

    1. ``GOOGLE_APPLICATION_CREDENTIALS`` naming a service account key file;
    2. otherwise the GCP metadata server.

    User credentials from ``gcloud auth application-default login`` are not
    supported: FCM would also need a quota project for them, and a server
    should not run on a person's identity anyway.
    """
    path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if path:
        try:
            with open(path, encoding='utf-8') as file:
                info = json.load(file)
        except (OSError, ValueError) as err:
            raise FcmConfigError(f"Cannot read GOOGLE_APPLICATION_CREDENTIALS ({path}): {err}") from err
        if info.get('type') != 'service_account':
            raise FcmConfigError(
                f"GOOGLE_APPLICATION_CREDENTIALS ({path}) holds '{info.get('type')}' credentials; "
                "only service account keys are supported"
            )
        return ServiceAccountCredentials.from_info(info), project_id or info.get('project_id')

    project_id = project_id or os.environ.get('GOOGLE_CLOUD_PROJECT') or MetadataCredentials.project_id(session)
    if not project_id:
        raise FcmConfigError(
            "No Application Default Credentials: GOOGLE_APPLICATION_CREDENTIALS is not set "
            "and the GCP metadata server is unreachable. Use a service account key instead."
        )
    return MetadataCredentials(), project_id


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------

class FcmClient:
    """Sends messages to one Firebase project.

    Holds an OAuth2 access token (valid for an hour) and a keep-alive HTTP
    session, so it is meant to be built once and reused: building one per
    message would add a token round-trip to every send.
    """

    def __init__(self, project_id: str, credentials, *, timeout: float = 10.0, session=None):
        if not project_id:
            raise FcmConfigError("A Firebase project id is required")
        self.project_id = project_id
        self.url = FCM_SEND_URL.format(project_id=project_id)
        self.timeout = timeout
        self._credentials = credentials
        self._session = session or requests.Session()
        self._lock = threading.Lock()

    @classmethod
    def from_service_account_info(cls, info: Mapping, *, project_id: str | None = None, **kwargs) -> FcmClient:
        credentials = ServiceAccountCredentials.from_info(info)
        return cls(project_id or info.get('project_id'), credentials, **kwargs)

    @classmethod
    def from_application_default(cls, *, project_id: str | None = None, session=None, **kwargs) -> FcmClient:
        session = session or requests.Session()
        credentials, project_id = application_default_credentials(session, project_id)
        return cls(project_id, credentials, session=session, **kwargs)

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid OAuth2 token, refreshing it when it is about to expire.

        Raises :class:`FcmConfigError` when Google refuses to issue one: every
        later send would fail the same way, so callers should stop the batch.
        """
        with self._lock:
            if force_refresh or not self._credentials.valid:
                self._credentials.refresh(self._session, self.timeout)
            return self._credentials.token

    def _post(self, payload: dict, *, force_refresh: bool = False):
        token = self.access_token(force_refresh=force_refresh)
        return self._session.post(
            self.url,
            json=payload,
            headers={'Authorization': f'Bearer {token}'},
            timeout=self.timeout,
        )

    def send(self, message: dict, *, validate_only: bool = False) -> SendResult:
        """Send one message. Never raises except :class:`FcmConfigError`."""
        payload = {'message': message}
        if validate_only:
            payload['validate_only'] = True
        try:
            response = self._post(payload)
            if response.status_code == 401:
                # The token was revoked or expired early: one fresh attempt.
                response = self._post(payload, force_refresh=True)
        except requests.RequestException as err:
            return SendResult(error_code='NETWORK', error_message=str(err), retryable=True)
        return parse_response(response.status_code, response.text, response.headers)
