# tk-pushnotification

Odoo **20.0** addon `tk_pushnotification`: push notifications to customers'
own apps via Firebase Cloud Messaging HTTP v1. README.md has the design and
delivery semantics; read it before changing models/push_message.py.

## Commands

- FCM client tests (no Odoo): `python -m unittest discover -s tests`
- Odoo tests: `odoo-bin -d test_push -i tk_pushnotification --test-tags /tk_pushnotification --stop-after-init`
  (or the `docker compose run` form in README.md)

## Odoo 20 API — verified against the 20.0 source, don't regress to 19 idioms

Local sources (Community + Enterprise): `/Volumes/BCKUP/odoo_source/`.
16.0–19.0 are unpacked; 20.0 is only `odoo_20.0+e.20260924.tar.gz` — extract it
somewhere temporary (packaged layout: addons live under `odoo/addons/`).
Check claims against it rather than memory.

- Security is `security/ir.access.csv`, model `ir.access` (ACLs and record
  rules merged; `ir.rule` is gone). Header:
  `id,name,model_id,group_id/id,operation,domain` — `model_id` is the model
  *name* (`tk.push.device`), `operation` a subset of `crud`, `domain` a domain
  string with `user`, `company_ids`, `time` available. Grants are additive.
- Groups: `res.groups.privilege` (with `category_id`) ← `res.groups.privilege_id`;
  `implied_ids`, `implied_by_ids`, `user_ids`. Users: `group_ids`, `all_group_ids`.
- `models.Constraint(...)` / `models.Index(...)` class attributes, not `_sql_constraints`.
- Translations: `self.env._("... %s", x)` (kwargs for `%(name)s`).
- Config params: `ir.config_parameter.get_str/get_int/get_bool/set_str/...`.
- Routes: `type='jsonrpc'` (not `'json'`). Don't import `request` in model code.
- Cron batching: `self.env['ir.cron']._commit_progress(n, remaining=...)`
  returns seconds left; `cron._trigger(at)` is transactional.
- Raw SQL: `self.env.execute_query(SQL(...))` after `flush_model(...)`.
- Binary fields are `BinaryValue` objects (no base64) — relevant if one is added.
- Don't name methods after ORM class attributes: `_register` is a bool on every
  model (registry visibility) and silently replaced our `_register` method.
- View access check (`Access Rights Inconsistency` at install): an element
  shown to users who can read the model may not depend on a `groups=` field
  (e.g. `readonly="notification_count"`); put `groups=` on the element/sheet.
- Button/stat icons are Material Symbols ligature names (`icon="smartphone"`,
  `"notifications"`, `"link"`, `"repeat"`), not `fa-*`: a `fa-` name renders a
  stray "-". Only names in Odoo's subset exist:
  `web/tooling/icons/icons_wishlist.txt`.
- Python 3.12–3.14, PostgreSQL 16+.

## Invariants

- **No dependencies beyond Odoo's own.** Only the standard library plus what
  Odoo 20 already requires (`requests`, `cryptography`, … — see Odoo's
  `requirements.txt`). No google-auth, firebase-admin, PyJWT: Google OAuth2
  (RS256 JWT bearer grant, metadata server) is hand-rolled in `tools/fcm.py`.
  Nothing to `pip install`, no `external_dependencies` in the manifest.
  Stick to APIs present in cryptography 41 (the `odoo:20.0` image ships 41.0.7;
  Odoo's requirements.txt pins 42), not just the latest.
- `tools/fcm.py` imports nothing from Odoo; keep it unit-testable standalone.
- Never call FCM inside the business transaction: write `tk.push.message`
  rows and let the cron send (outbox, at-least-once).
- When a device is removed, deactivated or reassigned to another user, cancel
  its queued deliveries (`_cancel_pending`) — shared-phone privacy.
- Only `tk.push.device.register/unregister` and `tk.push.notification.send`
  are public RPC surface; device writes go through sudo on behalf of `env.user` only.
  Signed-in users and portal users may also *read* `tk.push.topic` rows with
  `app_offered` set (ACL with domain), so apps can list opt-in topics.
- **Push never breaks the business flow on topic problems.** A topic that is
  invalid, not in `tk.push.topic` or archived is recorded as a failed delivery
  (`TOPIC_*` error codes) in `_enqueue`, and cancelled in the cron if it
  became so after queueing — never raised from `send()`. Payload problems
  (size, reserved keys, bad JSON) still raise: they are code bugs.
- Keep `res.users._tk_push_send()` single-underscore: Odoo 20's `safe_eval`
  blocks only names containing `__`, so Automation Rules ("Execute Code") can
  call it. Renaming it with `__` would break that documented use.
- House style follows sibling repo `../tk-odoo-pubsub` (TerraKernel, LGPL-3, `tk.` models).

## Odoo Apps Store (apps.odoo.com, free, LGPL-3)

Rules from the store's vendor guidelines and FAQ (read 2026-11-24):
- Manifest `name` ≤ 25 chars, no company name: it is `Tk-FCM` (user's choice).
  A manifest error unpublishes every module of the repo; bump `version` on
  each release and on any schema change.
- `static/description/index.html`: English, no JavaScript, no widgets or
  modals; style only Bootstrap 4 classes plus `color`, `font-*`, `margin-*`,
  `padding-*`, `border-*`. External links are invalidated (only own
  static files, YouTube, Teams, `mailto:`): don't link GitHub there.
- Data sent to another service must be disclosed in the manifest and the
  description: keep the "External service and your data" section and the
  manifest paragraph true when payloads or endpoints change.
- `icon.png` (real PNG) and `banner.png` (`images` key) are rendered from
  `store/*.html` by `store/render.sh`; edit the sources, not the PNGs.
- Screenshots (`main_screenshot.png` is the large one, `screenshot_*.png`
  in the page) come from a demo database: `store/screenshots/populate.py`
  (via `odoo shell`, FCM never called) and `store/screenshots/capture.py`
  (headless Chrome over `--remote-debugging-pipe`); usage in their headers.

## Scope decisions (settled with the user, don't reopen without asking)

- **FCM is the only provider.** It already relays to APNs and covers web.
  Direct APNs is out: Apple requires HTTP/2, which neither stdlib nor
  `requests` speaks (see the no-deps rule). Huawei Push Kit is the next
  provider *if* Huawei/no-Google-Play users appear; `provider_type` +
  `_<type>_send_messages` is the extension point.
- **No per-device targeting.** Audiences are users (incl. `group.all_user_ids`,
  `partners.user_ids`) or FCM topics. Topics are how to reach specific devices
  and app users who are not Odoo users: the app (or ODXProxy, for web apps,
  which cannot subscribe themselves) subscribes outside Odoo, and Odoo never
  learns who is subscribed. `tk.push.topic` lists which topics are
  *available* — no subscriber data, by decision. Device registration stays
  tied to an Odoo user.
