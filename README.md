# tk-pushnotification

Odoo addon that delivers push notifications to your own mobile and web apps
through **Firebase Cloud Messaging** (HTTP v1 API).

- **Odoo version:** 20.0
- **Module:** `tk_pushnotification`
- **License:** LGPL-3

> **Not to be confused with Odoo's built-in push.** In 20.0, `mail.push` /
> `mail.push.device` deliver Discuss notifications to browsers over VAPID web
> push, Enterprise `social_push_notifications` reuses that same web push for
> website visitors, and Enterprise `mail_mobile` reaches Odoo's own mobile app
> through Odoo's in-house OCN service. None of them talk to a Firebase project
> of yours. This module is for *your* apps — a customer app, a field-service
> app — and does not interact with any of them.

## How it works

Apps register their FCM token against the signed-in Odoo user. Server code
then notifies users in one call. Each notification is fanned out into one
delivery row per device, written to an **outbox** in the caller's transaction,
and drained by a cron:

```
business code: tk.push.notification.send(users, "Order shipped", ...)
        │
        ▼
  tk.push.notification ──▶ one tk.push.message per active device   (same transaction)
        │                         │
        │                         ├── ROLLBACK ──▶ rows vanish, nobody is notified
        │                         │
        │                         └── COMMIT ────▶ cron woken (ir.cron._trigger)
        │                                               │
        ▼                                               ▼
  state: queued                           batch: SELECT ... FOR UPDATE SKIP LOCKED
                                                        │
                                          POST fcm.googleapis.com/v1/.../messages:send
                                                        │
                       ┌───────────────┬────────────────┼──────────────────┐
                       ▼               ▼                ▼                  ▼
                     sent      invalid token      transient error     permanent error
                               → device retired   → retry w/ backoff  → failed
```

### Delivery semantics

| Property | Behaviour |
|---|---|
| Rolled-back transaction | **Never** notifies. The outbox rows roll back with it. |
| Committed transaction | Delivered **at least once**. A crash between FCM accepting a message and the cron committing can send it twice. |
| FCM unreachable / 429 / 5xx | Retried with exponential backoff (30 s doubling to 1 h, ±10% jitter, `Retry-After` honoured), up to the provider's *Max Attempts* (default 5). |
| Credentials broken | Every queued message is retried as above; nothing is lost until attempts run out. |
| Dead token (`UNREGISTERED`, `SENDER_ID_MISMATCH`, malformed) | Delivery marked *Invalid Token*, device deactivated, anything else queued for it cancelled. |
| User signs out / token moves to another user | Deliveries still queued for that device are **cancelled**, so the next person on a shared phone never sees the previous user's notifications. |
| Topic invalid, not in the topic list, or archived | **Never raises.** The notification is saved with a *Failed* delivery (`TOPIC_INVALID`, `TOPIC_UNKNOWN`, `TOPIC_ARCHIVED`) and FCM is not called, so the business operation that sent it goes through. Archived after queueing: the delivery is *cancelled* before sending. |
| Ordering | None. |
| Throughput | Sequential HTTP/1.1 keep-alive requests, roughly 10–20 messages/s per worker. For broadcasts use an FCM topic: one request regardless of audience. |

This is the opposite trade-off from `tk_pubsub`, which publishes from a
post-commit hook and accepts losing events: a push that silently disappears is
a support ticket, so the outbox costs one row per device in exchange for
retries and an audit trail.

## Setup

There is nothing to `pip install`. The module needs only what Odoo itself
requires: Google's OAuth2 service-account flow (an RS256-signed JWT exchanged
for an access token) is implemented on top of `cryptography` and `requests`,
both hard dependencies of Odoo, instead of pulling in google-auth.

Put `tk_pushnotification` on your addons path and install it. Settings
administrators get the *Push Notifications / Administrator* group
automatically.

### 1. Create a provider

**Settings → Push Notifications → Providers.** A provider is one Firebase
project.

| Field | Notes |
|---|---|
| Code | Stable identifier apps send when registering, e.g. `customer_app`. Needed only when one database serves several apps; apps sending none are attached to the first provider. |
| Authentication | *Application Default Credentials* stores no secret in the database: it reads the service account key file named by `GOOGLE_APPLICATION_CREDENTIALS`, or else asks the GCP metadata server (GCE, GKE workload identity, Cloud Run). User credentials from `gcloud auth application-default login` are not supported. *Service Account Key*: paste the key JSON into the provider. See [Credentials in production](#credentials-in-production) for which to choose and which service account to use. |
| Dry Run | FCM validates every message but delivers none. Turn this on in staging databases restored from production. Deliveries are then recorded as *Validated (Dry Run)*, never as *Sent*. |
| Max Attempts / Request Timeout | Retry budget and per-request timeout. |

**Test Connection** authenticates and sends a dry-run message to a token FCM
is certain to refuse. A token refusal proves the key, project id and
permissions are right without notifying anyone.

#### Credentials in production

**Use a dedicated service account.** The default `firebase-adminsdk-…`
account works, but it holds broad Firebase admin rights. Create one for
Odoo instead: Google Cloud console → IAM & Admin → Service accounts →
Create, in the Firebase project, granting only **Firebase Cloud Messaging
API Admin** (`roles/firebasecloudmessaging.admin`). A leaked key then can
send pushes and nothing else.

Then, in order of preference:

1. **Odoo runs on Google Cloud** (GCE, GKE, Cloud Run): attach that service
   account to the workload and choose *Application Default Credentials*. No
   key exists to leak or rotate.
2. **Elsewhere**: generate a key for it (service account → Keys → Add key →
   JSON) and put the file on the server, outside the addons path, owned by
   the Odoo user with mode `0400`, e.g. `/etc/odoo/firebase-fcm.json`. Set
   `GOOGLE_APPLICATION_CREDENTIALS` to that path in the Odoo process's
   environment (`Environment=` in the systemd unit; a read-only mounted
   secret plus `environment:` in Docker) and choose *Application Default
   Credentials*.
3. **Hosts where you can set neither files nor environment**: choose
   *Service Account Key* and paste the JSON. Only push administrators can
   read the field, but the key is then part of every database backup and of
   every staging copy restored from one (Dry Run stops those copies sending;
   they still hold a working key).

Limits of option 2: the environment variable is process-wide, so every
database and provider served by that Odoo shares one Firebase project (use
option 3 for per-database keys). Each worker caches its client, and the key
file is not part of the cache fingerprint: restart Odoo after replacing the
file.

### 2. Register devices from your app

Apps should register **on every start** and whenever FCM rotates the token,
and unregister on sign-out. Registration is idempotent.

**With an API key, through JSON-2** (Odoo 19+):

```bash
curl -X POST https://odoo.example.com/json/2/tk.push.device/register \
  -H "Authorization: bearer $ODOO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"token": "<fcm token>", "platform": "android",
       "provider_code": "customer_app", "app_version": "2.4.0"}'
# → {"device_id": 12}
```

**With a session cookie**, for webviews and web clients:

```bash
curl -X POST https://odoo.example.com/tk_push/device/register \
  -H "Content-Type: application/json" -b "session_id=..." \
  -d '{"jsonrpc": "2.0", "method": "call",
       "params": {"token": "<fcm token>", "platform": "ios"}}'
```

| Parameter | |
|---|---|
| `token` | **Required.** The FCM registration token. |
| `platform` | **Required.** `android`, `ios` or `web`. |
| `provider_code` | Which provider (Firebase project) the token belongs to. |
| `device_name`, `app_id`, `app_version`, `os_version` | Optional, shown to administrators. |

`unregister(token, provider_code=None)` removes the calling user's device.
Users and portal users may read and delete their own devices; nobody but
administrators can create or edit them directly.

## Sending

The main use: **when a condition is met in Odoo, notify someone.** An order is
confirmed, a ticket is escalated, a stock level drops: call the module
directly from wherever that happens.

The call never slows down or breaks the business operation, because it sends
nothing itself. It only writes outbox rows in the current transaction:

- the operation rolls back → nothing is sent;
- it commits → the cron delivers within moments, retrying if FCM struggles.

### Who can be reached

| Audience | How | Notes |
|---|---|---|
| Specific users | `send(users, ...)` | Every active device each user registered: phone, tablet, browser. |
| An Odoo group | `send(group.all_user_ids, ...)` | `all_user_ids` includes members through implied groups. Inactive users are skipped. |
| Contacts | `send(partners.user_ids, ...)` | Only contacts that have a login. |
| An FCM topic | `send(topic='promo', ...)` | Whoever subscribed, **Odoo user or not**: see below. |

**Topics** are the way to reach app users who have no Odoo account, and to
target devices rather than people: a kitchen tablet, a POS terminal, a
warehouse scanner. The device is subscribed outside Odoo: Android and iOS apps
do it themselves with the Firebase SDK (`subscribeToTopic('kitchen-orders')`);
web apps cannot, the Firebase JS SDK has no topic API, so a server of yours
(e.g. ODXProxy) subscribes their token. Odoo then sends a single message and
FCM fans it out. Keep in mind:

- **Topics must be listed** under *Push Notifications → Topics* (`tk.push.topic`,
  one list per provider). FCM accepts a send to any well-formed topic name,
  subscribed or not, so a typo would "succeed" and reach nobody; the list turns
  that into a visible failed delivery instead. Sending to a topic that is
  missing from the list, archived or not a valid name never raises: see
  *Delivery semantics*. Add the topic, then *Retry* the delivery.
- Topic names may contain only letters, digits and `- _ . ~ %`, without the
  `/topics/` prefix. A listed topic cannot be renamed once notifications were
  sent to it (its subscribers stay on the old name in FCM): archive it and
  list a new one.
- **Offered in Apps** marks the topics app users may opt into. Any signed-in
  user or API client can read those, and only those, to build the app's list:
  `search_read` on `tk.push.topic` with `[('provider_code', '=', 'customer_app')]`,
  fields `name`, `label`, `description`. Leave it off for topics the app
  subscribes to on its own.
- Odoo does not know who is subscribed, or how many; FCM only confirms it
  accepted the message for the topic. The list says which topics are
  available, not who listens.

### From your module's code

Add `tk_pushnotification` to your module's `depends`, then:

```python
def action_confirm(self):
    res = super().action_confirm()
    for order in self:
        order.user_id._tk_push_send(
            "Order confirmed",
            f"{order.name} for {order.partner_id.name}",
            data={'model': 'sale.order', 'id': order.id},  # read by the app
            record=order,                                  # linked for auditing
            link_url=f"/my/orders/{order.id}",
        )
    return res
```

`users._tk_push_send()` notifies those users whoever the caller is, portal
users included. `env['tk.push.notification'].send()` is the same call but runs
with the caller's rights, and only push administrators may create
notifications: use it under `sudo()`, or where the caller is an administrator.

```python
self.env['tk.push.notification'].sudo().send(topic='ops-alerts', title="Stock low", body=product.name)
```

### From an Automation Rule, with no module

**Settings → Technical → Automation Rules** (module `base_automation`): pick the model and trigger (e.g.
*On update*, with a filter on the condition), action *Execute Code*:

```python
for rec in records:
    rec.user_id._tk_push_send("Ticket escalated", rec.name, data={'id': rec.id})
```

### From another system

Through JSON-2, with a push administrator's API key:

```bash
curl -X POST https://odoo.example.com/json/2/tk.push.notification/send \
  -H "Authorization: bearer $ODOO_API_KEY" -H "Content-Type: application/json" \
  -d '{"users": [7, 12], "title": "Report ready", "body": "Q3 sales", "data": {"report": "q3"}}'
```

### Options

`send(users=None, title=None, body=None, **options)` and `_tk_push_send(title=None, body=None, **options)`
take the same keywords:

| Keyword | |
|---|---|
| `data` | `dict` → FCM `data`, read by the app. FCM only carries strings, so other values are JSON-encoded. |
| `topic` | Send to an FCM topic instead of users: a `tk.push.topic` record, or the name of a listed topic. By name, the provider is the one listing it (if several do, pass `provider`; else the first provider). |
| `provider` | Record or code: restrict to one Firebase project's devices. |
| `record` | The Odoo record this is about, linked on the notification for auditing. |
| `image_url`, `link_url` | Rich image; tap target (`data.link`, and `webpush.fcm_options.link` when HTTPS). |
| `priority` | `'high'` (default) or `'normal'`. |
| `ttl` | Seconds the notification stays deliverable. `0`/omitted = FCM default (4 weeks). |
| `collapse_key` | Newer notifications with the same key replace undelivered older ones. |
| `scheduled_date` | Do not send before this UTC datetime. |
| `overrides` | `dict` deep-merged into each FCM message last: the escape hatch for anything else, e.g. `{'android': {'notification': {'channel_id': 'orders'}}, 'apns': {'payload': {'aps': {'badge': 3}}}}`. See [Per-platform content](#per-platform-content). |

Leave `title` and `body` empty to send a **data-only** (silent) message: it is
sent to iOS as a background push (`content-available`, priority 5).

FCM limits the notification and data payload to 4 KB. `send()` refuses a
larger one straight away with a `ValidationError`, in the caller's transaction,
rather than letting it fail later as an undelivered message. The size is
estimated as the UTF-8 JSON of `notification` + `data` (overrides included),
which slightly overcounts: a message within a few bytes of the limit may be
refused although FCM would have taken it. Reserved data keys (`from`,
`notification`, `message_type`, `google…`, `gcm…`) are refused the same way.

### Per-platform content

Android, Apple devices and browsers each display a notification differently,
and FCM has one block per platform (`android`, `apns`, `webpush`) next to
the shared `notification` and `data`. `send()` fills the shared part and the
platform settings that follow from its keywords; anything else goes in
`overrides`.

**What Odoo builds from the keywords**

| Keyword | Shared | Android (`android`) | Apple (`apns`) | Web (`webpush`) |
|---|---|---|---|---|
| `title`, `body`, `image_url` | `notification.title/body/image` | — | `aps.sound: "default"`, header `apns-push-type: alert` | — |
| no title and no body | — | — | `aps.content-available: 1`, `apns-push-type: background`, `apns-priority: 5` | — |
| `data` | `data` (values JSON-encoded to strings) | — | — | — |
| `link_url` | `data.link` | — | — | `fcm_options.link` (HTTPS only) |
| `priority` | — | `priority: HIGH/NORMAL` | `apns-priority: 10/5` | header `Urgency: high/normal` |
| `ttl` | — | `ttl: "<n>s"` | `apns-expiration` | header `TTL` |
| `collapse_key` | — | `collapse_key` | `apns-collapse-id` (64 chars max) | — |

**A complete example**

An order-shipped notification that uses each platform's own features:

```python
order.user_id._tk_push_send(
    "Order shipped",
    f"{order.name} is on its way",
    record=order,
    data={'model': 'sale.order', 'id': order.id, 'screen': 'order'},
    link_url=f"https://shop.example.com/my/orders/{order.id}",
    image_url="https://shop.example.com/static/push/shipped.png",
    collapse_key=f"order-{order.id}",   # a newer update replaces this one if undelivered
    ttl=24 * 3600,                      # no point delivering it after a day
    overrides={
        'android': {
            'notification': {
                'channel_id': 'orders',          # channel the app created; else Android's fallback channel
                'icon': 'ic_stat_order',         # white-on-transparent drawable in the app
                'color': '#1E88E5',
                'tag': f'order-{order.id}',      # replaces this order's previous notification in the tray
                'click_action': 'OPEN_ORDER',    # intent filter action in the app
            },
        },
        'apns': {
            'payload': {
                'aps': {
                    # Setting `alert` replaces what FCM derives from title/body: repeat them.
                    'alert': {
                        'title': "Order shipped",
                        'subtitle': order.partner_id.name,
                        'body': f"{order.name} is on its way",
                    },
                    'thread-id': 'orders',                 # groups notifications in Notification Center
                    'category': 'ORDER',                   # action buttons registered by the app
                    'mutable-content': 1,                  # lets the app's service extension show the image
                    'interruption-level': 'time-sensitive',  # needs the Time Sensitive capability
                },
            },
        },
        'webpush': {
            'notification': {
                'icon': 'https://shop.example.com/static/push/icon-192.png',
                'badge': 'https://shop.example.com/static/push/badge-72.png',
                'tag': f'order-{order.id}',
                'renotify': True,                # alert again although the tag is the same
                'actions': [{'action': 'track', 'title': "Track parcel"}],
            },
        },
    },
)
```

How the platforms differ, and what that example accounts for:

- **Images.** Android and browsers show `image_url` as they are. iOS shows it
  only if the app has a Notification Service Extension that downloads it
  (Firebase's `FIRMessagingExtensionHelper`) **and** the push has
  `mutable-content: 1`. Android limits images to 1 MB; JPEG, PNG and BMP work
  everywhere.
- **Grouping and replacing.** `collapse_key` drops older *undelivered*
  messages. To replace a notification already *shown*, use `tag` on Android
  and the web; on Apple devices, `apns-collapse-id`, which `collapse_key` sets
  for you.
- **Taps.** Android opens the activity whose intent filter matches
  `click_action` (by default, the launcher activity); iOS opens the app, which
  reads `data`; browsers open `webpush.fcm_options.link`. Pass the target in
  `data` as well (`link_url` does), so each app can route the tap itself.
- **Buttons.** iOS shows the buttons of the `category` the app registered.
  Browsers show `webpush.notification.actions`, and the service worker gets
  the chosen `action` in its `notificationclick` handler. On Android, buttons
  need the app to build the notification itself from a data message.
- **Silent (data-only) messages.** Android hands them to the app's
  `onMessageReceived`, even in the background. iOS wakes the app only now and
  then: Apple throttles background pushes. A browser's service worker
  *must* show a notification for every push, or Chrome shows a generic
  "site updated in the background" one.
- **Data.** Every platform receives the same `data`. If you set
  `android.data` or `webpush.data` in `overrides`, it *replaces* `data` for
  that platform, and its values must already be strings: `send()` encodes
  only the `data` keyword.

**Rules for `overrides`**

- Dictionaries are merged key by key, so you add to what `send()` built;
  lists and plain values replace it.
- `title` and `body` in `android.notification`, in `apns.payload.aps.alert`
  or in `webpush.notification` override the shared ones on that platform
  only. That is how to send a shorter text to watches, or a different one to
  the web.
- Don't set the target (`token`, `fid`, `topic`, `condition`): Odoo sets it.
  Don't add `android_v2` either: it cannot be combined with `android`, which
  `send()` always sets, so FCM would refuse the message.
- The 4 KB size check covers the shared `notification` and `data` only. Keep
  the platform blocks short: FCM refuses an oversized one, and it shows up as
  a failed delivery (`INVALID_ARGUMENT`) instead of an error in `send()`.

**Fields you can set**

Taken from the [FCM HTTP v1 reference](https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages),
except the `aps` and web notification fields, which FCM passes through to
[Apple](https://developer.apple.com/documentation/usernotifications/generating-a-remote-notification)
and to the browser's
[Notification API](https://developer.mozilla.org/docs/Web/API/ServiceWorkerRegistration/showNotification).
Durations are strings in seconds with an `s` suffix (`"3600s"`).

| Block | Fields |
|---|---|
| Shared `notification` | `title`, `body`, `image` |
| Shared `data` | any string keys and string values, except `from`, `message_type`, `google.*`, `gcm.notification.*` |
| `fcm_options` | `analytics_label` |
| `android` | `collapse_key` (at most 4 active at a time per device), `priority` (`NORMAL`, `HIGH`), `ttl` (default and maximum 4 weeks), `restricted_package_name`, `data`, `notification`, `fcm_options.analytics_label`, `direct_boot_ok`, `bandwidth_constrained_ok`, `restricted_satellite_ok` |
| `android.notification` | `title`, `body`, `image`, `icon`, `color` (`#rrggbb`), `sound` (`default` or a file in `res/raw`), `tag`, `click_action`, `channel_id`, `title_loc_key`, `title_loc_args`, `body_loc_key`, `body_loc_args`, `ticker`, `sticky`, `event_time`, `local_only`, `notification_priority` (`PRIORITY_MIN`, `PRIORITY_LOW`, `PRIORITY_DEFAULT`, `PRIORITY_HIGH`, `PRIORITY_MAX`), `default_sound`, `default_vibrate_timings`, `vibrate_timings`, `default_light_settings`, `light_settings` (`color` `{red, green, blue, alpha}` 0–1, `light_on_duration`, `light_off_duration`), `visibility` (`PRIVATE`, `PUBLIC`, `SECRET`), `notification_count`, `proxy` (`ALLOW`, `DENY`, `IF_PRIORITY_LOWERED`) |
| `apns.headers` | `apns-push-type` (`alert`, `background`, …), `apns-priority` (`10` now, `5` power-friendly, `1` lowest), `apns-expiration` (Unix time; `0` = now or never), `apns-collapse-id` |
| `apns.payload.aps` | `alert` (`title`, `subtitle`, `body`, `launch-image`, `title-loc-key`, `title-loc-args`, `subtitle-loc-key`, `subtitle-loc-args`, `loc-key`, `loc-args`), `badge`, `sound` (a name, or `{critical, name, volume}`), `thread-id`, `category`, `content-available`, `mutable-content`, `target-content-id`, `interruption-level` (`passive`, `active`, `time-sensitive`, `critical`), `relevance-score`, `filter-criteria`; Live Activities only: `timestamp`, `event`, `content-state`, `stale-date`, `dismissal-date`, `attributes-type`, `attributes` |
| `apns.payload` | your own keys next to `aps`, read by the app |
| `apns` (other) | `fcm_options` (`analytics_label`, `image`, which overrides the shared image), `live_activity_token` |
| `webpush.headers` | `TTL` (seconds), `Urgency` (`very-low`, `low`, `normal`, `high`), `Topic` (replaces an undelivered message with the same value) |
| `webpush.notification` | `title`, `body`, `icon`, `badge`, `image`, `tag`, `renotify`, `requireInteraction`, `silent`, `timestamp`, `vibrate`, `lang`, `dir`, `actions` (`[{action, title, icon}]`; Chrome shows at most 2), `data` |
| `webpush` (other) | `data`, `fcm_options` (`link` HTTPS only, `analytics_label`) |

Notifications are not translated per recipient: send one per language if you
need to (`users.grouped('lang')`).

Notifications can also be composed by hand under **Settings → Push
Notifications → Notifications**, which is the quickest way to test a device.

## Monitoring

- **Deliveries** opens filtered on failures. *Group by Error* shows at a glance
  whether you have a credentials problem (`CONFIG`, `PERMISSION_DENIED`), a
  payload problem (`INVALID_ARGUMENT`) or a topic missing from the list
  (`TOPIC_UNKNOWN`, `TOPIC_INVALID`, `TOPIC_ARCHIVED`).
- Failed deliveries can be retried from the list.
- Processed notifications, and their deliveries, are deleted after 30 days.
  Change it with the system parameter `tk_pushnotification.retention_days`.
- Devices not re-registered for 270 days are retired, matching the age at
  which Firebase expires tokens.

## Development

```bash
# FCM client unit tests: pure Python, no Odoo needed
python -m unittest discover -s tests

# Odoo integration tests
odoo-bin -d test_push -i tk_pushnotification --test-tags /tk_pushnotification --stop-after-init

# Or with Docker
docker compose up -d db
docker compose run --rm odoo -d test_push -i tk_pushnotification \
    --test-tags /tk_pushnotification --stop-after-init
```

Layout:

```
tk_pushnotification/
├── tools/fcm.py              FCM HTTP v1 client and Google OAuth2: build, sign, send, classify. No Odoo imports.
├── models/
│   ├── push_provider.py      Firebase project + credentials, cached client per worker
│   ├── push_device.py        token registry, register/unregister API, token GC
│   ├── push_topic.py         the list of topics that may be sent to
│   ├── push_notification.py  public send() API, fan-out
│   ├── push_message.py       the outbox and its cron
│   └── res_users.py          _tk_push_send() helper
├── controllers/main.py       session routes for register/unregister
├── static/description/       Odoo Apps Store page: index.html, icon.png, banner.png
└── tests/                    Odoo TransactionCase tests (FCM mocked)
tests/test_fcm.py             standalone tests for tools/fcm.py
store/                        HTML sources of icon.png and banner.png; re-render with store/render.sh (needs Chrome)
```

## Roadmap

- Bridge Discuss / chatter notifications to users' app devices.
- *Send Push Notification* server action, for automation rules without code.
- Concurrent sends for large audiences.
- Huawei Push Kit, for Huawei phones without Google Play services (`provider_type` is the extension point).
  Direct APNs is deliberately out: Apple requires HTTP/2, which neither the
  standard library nor `requests` speaks, and FCM already relays to APNs.
