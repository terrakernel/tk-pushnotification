{
    'name': 'TerraKernel Push Notifications',
    'summary': 'Send mobile and web push notifications through Firebase Cloud Messaging',
    'description': """
Deliver push notifications from Odoo to your own mobile and web apps through
Firebase Cloud Messaging (HTTP v1 API).

Apps register their FCM tokens against the signed-in user. Server code then
notifies users with one call; every notification is expanded into one message
per device and written to an outbox in the same transaction, then delivered by
a cron with retries, backoff and automatic clean-up of dead tokens.
""",
    'version': '20.0.1.1.0',
    'category': 'Technical',
    'author': 'TerraKernel',
    'website': 'https://github.com/terrakernel/tk-pushnotification',
    'license': 'LGPL-3',
    'depends': ['base', 'web'],
    'data': [
        'security/push_groups.xml',
        'security/ir.access.csv',
        'data/push_cron.xml',
        'views/push_provider_views.xml',
        'views/push_device_views.xml',
        'views/push_topic_views.xml',
        'views/push_message_views.xml',
        'views/push_notification_views.xml',
        'views/push_menus.xml',
    ],
    'installable': True,
    'application': False,
}
