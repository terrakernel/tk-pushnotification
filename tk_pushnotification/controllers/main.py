from odoo import http
from odoo.http import request


class TkPushController(http.Controller):
    """Session-authenticated device registration, for apps and web clients
    signed in with an Odoo session cookie. Clients using an API key can call
    the same model methods through JSON-2 instead:
    ``POST /json/2/tk.push.device/register``.
    """

    @http.route('/tk_push/device/register', type='jsonrpc', auth='user', methods=['POST'])
    def register(self, token, platform, provider_code=None, device_name=None,
                 app_id=None, app_version=None, os_version=None):
        return request.env['tk.push.device'].register(
            token, platform, provider_code=provider_code, device_name=device_name,
            app_id=app_id, app_version=app_version, os_version=os_version,
        )

    @http.route('/tk_push/device/unregister', type='jsonrpc', auth='user', methods=['POST'])
    def unregister(self, token, provider_code=None):
        return request.env['tk.push.device'].unregister(token, provider_code=provider_code)
