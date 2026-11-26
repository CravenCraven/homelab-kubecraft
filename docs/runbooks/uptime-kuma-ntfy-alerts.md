# Runbook: Uptime Kuma alerts to ntfy

**What this does:** Uptime Kuma checks my apps every 60 seconds. When one goes down, it sends a push alert through my self-hosted ntfy server to my phone.

**Secrets (password manager, never in this repo):**
- ntfy server URL
- ntfy alert topic name (treat it like a password)
- ntfy username and password

> The ntfy login is **not** the Uptime Kuma login. Uptime Kuma logs into ntfy on my behalf, so it needs ntfy's credentials.

---

## 1. Create the alert topic

Topics don't have a create button. A topic exists once something subscribes or publishes to it.

1. Open the ntfy web app.
2. Click **Subscribe to topic**.
3. Enter the alert topic name and click **Subscribe**.

Don't use `test` or anything easy to guess.

## 2. Subscribe the phone

1. In the phone app, tap **+**.
2. Enter the alert topic name.
3. Turn on **Use another server** and type the full ntfy URL (`https://...`). The grey text is only a placeholder, so type it in.
4. Tap **Subscribe**.

## 3. Test ntfy by itself

Any directory works:

```bash
curl --user '<ntfy-user>' -d 'hello from my Mac' https://<ntfy-server>/<topic>
```

| Part | What it does |
|---|---|
| `curl` | Sends a web request |
| `--user '<ntfy-user>'` | Logs in. curl asks for the password, and typing won't show |
| `-d '...'` | Message body. Sending data makes it a POST, which means publish |
| URL | Server plus topic |

**Expected:** the phone shows the message.

## 4. Connect Uptime Kuma to ntfy

Uptime Kuma: profile icon, top right → **Settings** → **Notifications** → **Setup Notification**

| Field | Value |
|---|---|
| Notification Type | ntfy |
| Friendly Name | `ntfy homelab` |
| ntfy Topic | alert topic name |
| Server URL | my ntfy URL (replace the default `https://ntfy.sh`) |
| Priority | 5 (consider 3, keeping DOWN events at 5) |
| Priority for DOWN-events | 5 |
| Authentication Method | Username and Password (ntfy credentials) |
| Default enabled | On |
| Apply on all existing monitors | On |

Click **Test**, then **Save** once the phone buzzes.

## 5. Add a monitor

**Add New Monitor**:

| Field | Value |
|---|---|
| Monitor Type | HTTP(s) |
| Friendly Name | app name |
| URL | base URL or health endpoint (see below) |
| Heartbeat Interval | 60 |
| Retries | 2 recommended (currently 0) |
| Cache buster | Off |
| Notifications | `ntfy homelab` switch on (automatic because it's the default) |

Click **Save**. It should show **Up** with `200 - OK`.

**Faster:** open an existing monitor → **Clone** → change the name and URL → **Save**.

### Current monitors

| App | URL to monitor | Notes |
|---|---|---|
| pgAdmin | `https://pgadmin.<domain>` | Base URL only, drop `/login?next=/`. Goes straight to pgAdmin's login, not Authentik |
| Authentik | `https://auth.<domain>/-/health/ready/` | Health endpoint. The library page redirects to login, so it's no good for monitoring |

### Picking the URL

- Use the app's health endpoint if it has one.
- Otherwise use the base URL, without login redirects like `/login?next=...`.
- If the app sits behind Authentik forward auth, the monitor may only be checking Authentik's login page. Use a health endpoint or the in-cluster service URL instead.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Request failed with status code 401` on Test | Wrong credentials. Usually the Uptime Kuma login was used instead of ntfy's | Enter the ntfy username and password |
| 401 and the password is lost | Password isn't in the password manager | Reset it in the pod: `kubectl exec -it -n <ntfy-namespace> deploy/ntfy -- ntfy user change-pass <ntfy-user>`. Find the namespace with `kubectl get pods -A \| grep ntfy` |
| Phone gets nothing, web app does | Phone subscribed to ntfy.sh, not my server | Re-subscribe with **Use another server** on |
| Web app shows nothing, phone does | Browser isn't subscribed to that topic | Subscribe to the same topic in the web app |
| Strange messages on `test` | Old practice topic | Ignore it. Alerts go to the dedicated topic only |
| Can't find the ntfy switch on a monitor | Notification wasn't saved | Settings → Notifications, check it's listed |
| Clicked **Setup Notification** on a monitor and got a Telegram form | That button creates a *new* notification | Close it with the X and use the existing `ntfy homelab` switch |
| Too many alerts from blips | Retries = 0 and nodes are on WiFi | Set Retries to 2 |

## To do

- [ ] Save the ntfy password in the password manager
- [ ] Set Retries to 2 on pgAdmin and Authentik
- [ ] Add monitors for the remaining apps in `homelab-kubecraft` and `homelab-brasil`
- [ ] Decide whether Uptime Kuma metrics feed the status dashboard
