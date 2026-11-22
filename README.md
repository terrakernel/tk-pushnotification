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
| Authentication | *Service Account Key*: paste the JSON from Firebase console → Project settings → Service accounts → Generate new private key. The default `firebase-adminsdk-…` account already holds the needed permission (`cloudmessaging.messages.create`). *Application Default Credentials* stores no secret: it reads the service account key file named by `GOOGLE_APPLICATION_CREDENTIALS`, or else asks the GCP metadata server (GCE, GKE workload identity, Cloud Run) — preferred on GCP. User credentials from `gcloud auth application-default login` are not supported. |
| Dry Run | FCM validates every message but delivers none. Turn this on in staging databases restored from production. Deliveries are then recorded as *Validated (Dry Run)*, never as *Sent*. |
| Max Attempts / Request Timeout | Retry budget and per-request timeout. |

**Test Connection** authenticates and sends a dry-run message to a token FCM
is certain to refuse. A token refusal proves the key, project id and
permissions are right without notifying anyone.

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
warehouse scanner. The app subscribes the device itself with the Firebase SDK
(`subscribeToTopic('kitchen-orders')`), never registers with Odoo, and needs no
sign-in. Odoo then sends a single message and FCM fans it out. Keep in mind:

- Odoo does not know who is subscribed, or how many; FCM only confirms it
  accepted the message for the topic.
- Topic names may contain only letters, digits and `- _ . ~ %`, without the
  `/topics/` prefix. Invalid names are refused when the notification is saved.
- Subscriptions are managed by the app; there is no server-side subscribe.

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
| `topic` | Send to an FCM topic instead of users (needs a provider; defaults to the first). |
| `provider` | Record or code: restrict to one Firebase project's devices. |
| `record` | The Odoo record this is about, linked on the notification for auditing. |
| `image_url`, `link_url` | Rich image; tap target (`data.link`, and `webpush.fcm_options.link` when HTTPS). |
| `priority` | `'high'` (default) or `'normal'`. |
| `ttl` | Seconds the notification stays deliverable. `0`/omitted = FCM default (4 weeks). |
| `collapse_key` | Newer notifications with the same key replace undelivered older ones. |
| `scheduled_date` | Do not send before this UTC datetime. |
| `overrides` | `dict` deep-merged into each FCM message last: the escape hatch for anything else, e.g. `{'android': {'notification': {'channel_id': 'orders'}}, 'apns': {'payload': {'aps': {'badge': 3}}}}`. |

Leave `title` and `body` empty to send a **data-only** (silent) message: it is
sent to iOS as a background push (`content-available`, priority 5).

FCM limits the notification and data payload to 4 KB. `send()` refuses a
larger one straight away with a `ValidationError`, in the caller's transaction,
rather than letting it fail later as an undelivered message. The size is
estimated as the UTF-8 JSON of `notification` + `data` (overrides included),
which slightly overcounts: a message within a few bytes of the limit may be
refused although FCM would have taken it. Reserved data keys (`from`,
`notification`, `message_type`, `google…`, `gcm…`) are refused the same way.

Notifications are not translated per recipient: send one per language if you
need to (`users.grouped('lang')`).

Notifications can also be composed by hand under **Settings → Push
Notifications → Notifications**, which is the quickest way to test a device.

## Monitoring

- **Deliveries** opens filtered on failures. *Group by Error* shows at a glance
  whether you have a credentials problem (`CONFIG`, `PERMISSION_DENIED`) or a
  payload problem (`INVALID_ARGUMENT`).
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
│   ├── push_notification.py  public send() API, fan-out
│   ├── push_message.py       the outbox and its cron
│   └── res_users.py          _tk_push_send() helper
├── controllers/main.py       session routes for register/unregister
└── tests/                    Odoo TransactionCase tests (FCM mocked)
tests/test_fcm.py             standalone tests for tools/fcm.py
```

## Roadmap

- Bridge Discuss / chatter notifications to users' app devices.
- *Send Push Notification* server action, for automation rules without code.
- Concurrent sends for large audiences.
- Huawei Push Kit, for Huawei phones without Google Play services (`provider_type` is the extension point).
  Direct APNs is deliberately out: Apple requires HTTP/2, which neither the
  standard library nor `requests` speaks, and FCM already relays to APNs.
