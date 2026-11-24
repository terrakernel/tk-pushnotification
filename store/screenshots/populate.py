# Demo data for the store screenshots, in a throwaway database:
#   docker compose run --rm odoo -d store_demo -i tk_pushnotification --stop-after-init
#   docker compose run --rm -T odoo odoo shell -d store_demo --no-http < store/screenshots/populate.py
#   docker compose run -d --service-ports --name tkshot odoo odoo --addons-path=/mnt/extra-addons \
#       -d store_demo --db-filter='^store_demo$' --max-cron-threads=0   # no cron: nothing is sent
# then capture.py (see its docstring). Log in is admin/admin.
# FCM is never called: outcomes are applied through _apply_result with made-up responses.
import secrets
from datetime import timedelta
from odoo import fields
from odoo.addons.tk_pushnotification.tools import fcm

env.company.name = 'Acme Outdoor'

Provider = env['tk.push.provider']
provider = Provider.create({
    'name': 'Customer App', 'code': 'customer_app',
    'fcm_auth_method': 'adc', 'fcm_project_id': 'acme-customer-app',
})
staff = Provider.create({
    'name': 'Field Service App', 'code': 'field_app',
    'fcm_auth_method': 'adc', 'fcm_project_id': 'acme-field-service',
})

internal = env.ref('base.group_user')
portal = env.ref('base.group_portal')
def user(name, login, group):
    return env['res.users'].create({'name': name, 'login': login, 'email': login, 'group_ids': [(6, 0, [group.id])]})

sofia = user('Sofia Marquez', 'sofia@acme.example', internal)
daniel = user('Daniel Okafor', 'daniel@acme.example', internal)
hannah = user('Hannah Weber', 'hannah@example.com', portal)
liam = user('Liam Chen', 'liam@example.com', portal)
amara = user('Amara Nwosu', 'amara@example.com', portal)

Device = env['tk.push.device']
def register(u, platform, name, app_id, app_version, os_version, code='customer_app'):
    tok = secrets.token_urlsafe(48)
    Device.with_user(u).register(tok, platform, provider_code=code, device_name=name,
                                 app_id=app_id, app_version=app_version, os_version=os_version)
    return tok

register(hannah, 'ios', 'iPhone 15', 'com.acme.shop', '2.4.0', 'iOS 26.1')
register(hannah, 'web', 'Chrome on macOS', 'https://shop.acme.example', '2.4.0', 'macOS 26')
register(liam, 'android', 'Pixel 9', 'com.acme.shop', '2.4.0', 'Android 16')
register(amara, 'android', 'Galaxy S25', 'com.acme.shop', '2.3.2', 'Android 15')
register(amara, 'ios', 'iPad Air', 'com.acme.shop', '2.4.0', 'iPadOS 26.1')
register(sofia, 'ios', 'iPhone 16 Pro', 'com.acme.field', '1.8.0', 'iOS 26.1', code='field_app')
register(daniel, 'android', 'Galaxy XCover 7', 'com.acme.field', '1.8.0', 'Android 15', code='field_app')
register(daniel, 'web', 'Edge on Windows', 'https://field.acme.example', '1.8.0', 'Windows 11', code='field_app')

Topic = env['tk.push.topic']
Topic.create({'provider_id': provider.id, 'name': 'promotions', 'label': 'Promotions and offers',
              'app_offered': True, 'description': 'Sales, discount codes and new arrivals.'})
Topic.create({'provider_id': provider.id, 'name': 'order-updates', 'label': 'Order updates',
              'app_offered': True, 'description': 'Shipping and delivery news for your orders.'})
Topic.create({'provider_id': staff.id, 'name': 'kitchen-orders', 'label': 'Kitchen display',
              'description': 'Subscribed by the kitchen tablets themselves.'})
Topic.create({'provider_id': staff.id, 'name': 'ops-alerts', 'label': 'Operations alerts',
              'description': 'Stock and system alerts for the operations team.'})

N = env['tk.push.notification']
counter = iter(range(1000, 9999))
def ok():
    return fcm.SendResult(message_id=f'projects/x/messages/0:17643{next(counter)}%{secrets.token_hex(8)}')

def outcome(notification, *results):
    for message, result in zip(notification.message_ids.sorted('id'), results):
        if result is not None:
            message._apply_result(result)
    notification._update_state()
    return notification

partner = hannah.partner_id
notes = [
    outcome(N.send(hannah, 'Order confirmed', 'S00042: 3 items, arriving Thursday.',
                   data={'model': 'sale.order', 'id': 42}, link_url='/my/orders/42', provider=provider), ok(), ok()),
    outcome(N.send(liam, 'Your order has shipped', 'S00039 is on its way. Track it in the app.',
                   link_url='/my/orders/39', provider=provider), ok()),
    outcome(N.send(topic='promotions', title='Winter sale: 20% off', body='Jackets and boots, this weekend only.',
                   provider=provider, image_url='https://shop.acme.example/static/push/winter.png'), ok()),
    outcome(N.send(daniel, 'Ticket escalated', 'HD-1187: printer offline at Warehouse 2.', provider=staff),
            ok(), fcm.SendResult(error_code='UNREGISTERED', error_message='Requested entity was not found.', token_invalid=True)),
    outcome(N.send(topic='kitchen-orders', title='New order #1042', body='2x Burger, 1x Salad, table 7.', provider=staff), ok()),
    outcome(N.send(sofia, 'Visit rescheduled', 'Weber residence moved to 14:30.', provider=staff),
            fcm.SendResult(error_code='UNAVAILABLE', error_message='The service is currently unavailable.', retryable=True)),
    N.send(topic='flash-sale', title='Flash sale', body='Two hours only.', provider=provider),
    outcome(N.send(amara, 'Invoice available', 'INV/2026/0117 is ready in your portal.', provider=provider), ok(), ok()),
]

# Spread the history over the last hours, newest last.
now = fields.Datetime.now()
for age, n in zip(range(len(notes), 0, -1), notes):
    when = now - timedelta(minutes=age * 23)
    env.cr.execute("UPDATE tk_push_notification SET create_date=%s WHERE id=%s", (when, n.id))
    env.cr.execute("UPDATE tk_push_message SET create_date=%s, last_attempt=CASE WHEN last_attempt IS NULL THEN NULL ELSE %s END,"
                   " sent_date=CASE WHEN sent_date IS NULL THEN NULL ELSE %s END WHERE notification_id=%s",
                   (when, when + timedelta(seconds=2), when + timedelta(seconds=2), n.id))
env.cr.commit()
print('DEMO OK', N.search_count([]), env['tk.push.message'].search_count([]), Device.with_context(active_test=False).search_count([]))
