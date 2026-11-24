{
    'name': 'Tk-FCM',
    'summary': 'Push notifications to your own Android, iOS and web apps through Firebase Cloud Messaging',
    'description': """
Deliver push notifications from Odoo to your own mobile and web apps through
Firebase Cloud Messaging (HTTP v1 API).

Apps register their FCM tokens against the signed-in user. Server code then
notifies users with one call; every notification is expanded into one message
per device and written to an outbox in the same transaction, then delivered by
a cron with retries, backoff and automatic clean-up of dead tokens.

External service: notifications are sent to Google Firebase Cloud Messaging
(fcm.googleapis.com) with the Firebase project and credentials you configure.
Each delivery sends the device's FCM token or the topic name, and the
notification's title, body, image URL, link and data. Nothing is sent before
a provider is configured; devices are registered only by your app, for its
signed-in user. No usage statistics are collected.
""",
    'version': '20.0.1.1.0',
    'category': 'Technical',
    'author': 'TerraKernel',
    'website': 'https://github.com/terrakernel/tk-pushnotification',
    'license': 'LGPL-3',
    'images': ['static/description/banner.png', 'static/description/main_screenshot.png'],
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
