from __future__ import annotations

import json
import logging
import threading

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..tools import fcm

_logger = logging.getLogger(__name__)

# An FcmClient holds an OAuth2 token and a keep-alive HTTP session, so it is
# cached per database and provider together with a fingerprint of the
# configuration it was built from. Each worker keeps its own cache; editing a
# provider changes the fingerprint, which is how other workers notice.
_clients: dict[tuple[str, int], tuple[fcm.FcmClient, str]] = {}
_clients_lock = threading.Lock()

# A well-formed request with a token FCM can never accept: used to check that
# credentials, project and permissions are right without reaching any device.
_PROBE_TOKEN = 'tk-pushnotification-connection-test'


class TkPushProvider(models.Model):
    _name = 'tk.push.provider'
    _description = 'Push Notification Provider'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    code = fields.Char(
        help="Stable identifier apps send when registering a device, e.g. "
             "'customer_app'. Lets one database serve several apps or Firebase "
             "projects. Apps that send none are attached to the first provider.",
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    provider_type = fields.Selection(
        [('fcm', 'Firebase Cloud Messaging')],
        string='Service',
        default='fcm',
        required=True,
    )

    fcm_project_id = fields.Char(
        string='Firebase Project ID',
        help="Taken from the service account key when left empty.",
    )
    fcm_auth_method = fields.Selection(
        [
            ('service_account', 'Service Account Key'),
            ('adc', 'Application Default Credentials'),
        ],
        string='Authentication',
        default='service_account',
        required=True,
        help="Application Default Credentials uses the server's ambient Google "
             "identity (workload identity, GOOGLE_APPLICATION_CREDENTIALS), so "
             "no secret is stored in the database.",
    )
    fcm_service_account_key = fields.Text(
        string='Service Account JSON',
        groups='tk_pushnotification.group_push_manager',
        copy=False,
        help="Contents of the JSON key of a service account holding the "
             "'Firebase Cloud Messaging API Admin' role.",
    )

    validate_only = fields.Boolean(
        string='Dry Run',
        help="FCM validates every message but delivers none. Use on staging "
             "databases restored from production.",
    )
    request_timeout = fields.Integer(default=10, required=True, help="Seconds to wait for FCM per message.")
    max_attempts = fields.Integer(
        default=5,
        required=True,
        help="Deliveries failing with a transient error are retried with "
             "exponential backoff until this many attempts were made.",
    )

    device_ids = fields.One2many('tk.push.device', 'provider_id', string='Devices')
    device_count = fields.Integer(compute='_compute_device_count')

    _code_unique = models.Constraint('UNIQUE(code)', 'Another provider already uses this code.')
    _request_timeout_positive = models.Constraint(
        'CHECK(request_timeout > 0)', 'The request timeout must be positive.',
    )
    _max_attempts_positive = models.Constraint('CHECK(max_attempts > 0)', 'At least one attempt is required.')

    def _compute_device_count(self):
        counts = dict(self.env['tk.push.device']._read_group(
            [('provider_id', 'in', self.ids)], ['provider_id'], ['__count'],
        ))
        for provider in self:
            provider.device_count = counts.get(provider, 0)

    @api.constrains('provider_type', 'fcm_auth_method', 'fcm_service_account_key', 'fcm_project_id')
    def _check_fcm_configuration(self):
        for provider in self.sudo():
            if provider.provider_type != 'fcm':
                continue
            if provider.fcm_auth_method == 'service_account':
                info = provider._fcm_parse_key()  # raises on bad input
                if not (provider.fcm_project_id or info.get('project_id')):
                    raise ValidationError(self.env._(
                        "Provider %s: set the Firebase project id, the key does not contain one.", provider.name,
                    ))

    def _fcm_parse_key(self):
        self.ensure_one()
        key = self.sudo().fcm_service_account_key
        if not key:
            raise ValidationError(self.env._("Provider %s needs a service account key.", self.name))
        try:
            info = json.loads(key)
        except ValueError as err:
            raise ValidationError(
                self.env._("The service account key of provider %s is not valid JSON: %s", self.name, err)
            ) from err
        try:
            fcm.ServiceAccountCredentials.from_info(info)  # also loads the RSA key
        except fcm.FcmConfigError as err:
            raise ValidationError(self.env._(
                "Provider %(name)s: %(error)s. Download a key from Firebase console > "
                "Project settings > Service accounts > Generate new private key.",
                name=self.name, error=err,
            )) from err
        return info

    @api.onchange('fcm_service_account_key')
    def _onchange_fcm_service_account_key(self):
        if self.fcm_project_id or not self.fcm_service_account_key:
            return
        try:
            self.fcm_project_id = json.loads(self.fcm_service_account_key).get('project_id')
        except (ValueError, AttributeError):
            pass  # reported by the constraint on save

    @api.model
    def _get_default(self):
        return self.search([], limit=1)

    @api.model
    def _get_for_code(self, code):
        """The provider apps address with ``code``, or the default one."""
        if code:
            return self.search([('code', '=', code)], limit=1)
        return self._get_default()

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _client_fingerprint(self):
        self.ensure_one()
        provider = self.sudo()
        return json.dumps([
            provider.provider_type,
            provider.fcm_project_id,
            provider.fcm_auth_method,
            provider.fcm_service_account_key or '',
            provider.request_timeout,
        ])

    def _fcm_build_client(self):
        self.ensure_one()
        provider = self.sudo()
        options = {'project_id': provider.fcm_project_id or None, 'timeout': provider.request_timeout}
        if provider.fcm_auth_method == 'service_account':
            try:
                info = provider._fcm_parse_key()
            except ValidationError as err:
                raise fcm.FcmConfigError(str(err)) from err
            return fcm.FcmClient.from_service_account_info(info, **options)
        return fcm.FcmClient.from_application_default(**options)

    def _fcm_get_client(self):
        """Return this provider's cached client. Raises ``fcm.FcmConfigError``."""
        self.ensure_one()
        key = (self.env.cr.dbname, self.id)
        fingerprint = self._client_fingerprint()
        with _clients_lock:
            cached = _clients.get(key)
            if cached and cached[1] == fingerprint:
                return cached[0]
            client = self._fcm_build_client()
            _clients[key] = (client, fingerprint)
            return client

    def _drop_client(self):
        with _clients_lock:
            for provider in self:
                _clients.pop((self.env.cr.dbname, provider.id), None)

    def write(self, vals):
        result = super().write(vals)
        self._drop_client()
        return result

    def unlink(self):
        self._drop_client()
        return super().unlink()

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    def _send_messages(self, messages):
        """Deliver ``messages`` (tk.push.message, all of this provider) and record the outcome."""
        self.ensure_one()
        getattr(self, f'_{self.provider_type}_send_messages')(messages)

    def _fcm_send_messages(self, messages):
        try:
            client = self._fcm_get_client()
        except fcm.FcmConfigError as err:
            _logger.error("tk_pushnotification: provider %s is misconfigured: %s", self.name, err)
            messages._apply_config_error(str(err))
            return

        for index, message in enumerate(messages):
            try:
                payload = message._fcm_payload()
            except ValueError as err:
                message._apply_result(fcm.SendResult(error_code='INVALID_PAYLOAD', error_message=str(err)))
                continue
            try:
                result = client.send(payload, validate_only=self.validate_only)
            except fcm.FcmConfigError as err:
                # No token, no sends: stop now rather than fail every message alike.
                _logger.error("tk_pushnotification: provider %s cannot authenticate: %s", self.name, err)
                messages[index:]._apply_config_error(str(err))
                self._drop_client()
                return
            message._apply_result(result, dry_run=self.validate_only)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def action_test_connection(self):
        """Authenticate and send a dry-run message to a token FCM will refuse.

        A refusal *of the token* proves the key, the project id and the
        service account's permissions are all right, without notifying anyone.
        """
        self.ensure_one()
        self._drop_client()
        try:
            client = self._fcm_get_client()
            client.access_token()
            result = client.send(fcm.build_message(token=_PROBE_TOKEN, title='Test'), validate_only=True)
        except fcm.FcmConfigError as err:
            raise UserError(str(err)) from err

        if not (result.ok or result.token_invalid):
            hints = {
                'PERMISSION_DENIED': self.env._(
                    "The service account needs the 'Firebase Cloud Messaging API Admin' role, and the "
                    "Firebase Cloud Messaging API must be enabled on project %s.", client.project_id,
                ),
                'NOT_FOUND': self.env._("Project %s was not found.", client.project_id),
            }
            raise UserError(self.env._(
                "FCM refused the request (%(code)s): %(message)s\n\n%(hint)s",
                code=result.error_code, message=result.error_message, hint=hints.get(result.error_code, ''),
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Connection successful"),
                'message': self.env._("Authenticated against Firebase project %s.", client.project_id),
                'sticky': False,
            },
        }

    def action_view_devices(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Devices"),
            'res_model': 'tk.push.device',
            'view_mode': 'list,form',
            'domain': [('provider_id', '=', self.id)],
            'context': {'default_provider_id': self.id},
        }
