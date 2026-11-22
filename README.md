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
| Dry Run | FCM validates every message but delivers none. Turn this on in staging databases restored from production. |
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

From server code:

```python
self.env['tk.push.notification'].send(
    order.user_id,                      # res.users recordset or ids
    "Order confirmed",
    f"{order.name} was confirmed",
    data={'order_id': order.id},        # delivered to the app as string key/values
    record=order,                       # linked for auditing
    link_url=f"/my/orders/{order.id}",
)
```

`send()` runs with the caller's rights, and only push administrators may
create notifications. Business code notifying on behalf of an ordinary or
portal user should use the helper, which elevates:

```python
order.user_id._tk_push_send("Order confirmed", f"{order.name} was confirmed", record=order)
```

Every keyword of `send()`:

| Keyword | |
|---|---|
| `data` | `dict` → FCM `data`. Non-string values are JSON-encoded. |
| `topic` | Send to an FCM topic instead of users (needs a provider; defaults to the first). |
| `provider` | Record or code: restrict to one Firebase project's devices. |
| `image_url`, `link_url` | Rich image; tap target (`data.link`, and `webpush.fcm_options.link` when HTTPS). |
| `priority` | `'high'` (default) or `'normal'`. |
| `ttl` | Seconds the notification stays deliverable. `0`/omitted = FCM default (4 weeks). |
| `collapse_key` | Newer notifications with the same key replace undelivered older ones. |
| `scheduled_date` | Do not send before this UTC datetime. |
| `overrides` | `dict` deep-merged into each FCM message last — the escape hatch, e.g. `{'android': {'notification': {'channel_id': 'orders'}}}`. |

Leave `title` and `body` empty to send a **data-only** (silent) message: it is
sent to iOS as a background push (`content-available`, priority 5).

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
- Direct APNs and Huawei Push Kit providers (`provider_type` is the extension point).
