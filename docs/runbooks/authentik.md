# Runbook: Authentik, and putting an app behind it with Traefik ForwardAuth

This is how I deployed Authentik as the lab's identity provider and then put n8n behind it, so that opening n8n in a fresh browser sends you to an Authentik login first. From reading the chart docs to the private-window test. Every file, every command, every click in the Authentik UI, with the failures where they happened.

The second half is written so the same steps work for the next app. n8n is the first one through.

Prerequisites: n8n deployed and public through Traefik as in `n8n.md`. Same cluster, repo, tunnel and SOPS setup as that runbook.

---

## Contents

0. Step zero: what Authentik is and what I need to give it
1. Folders
2. Namespace and HelmRepository
3. HelmRelease
4. Base kustomization
5. Staging: the Secret
6. Staging: kustomizations
7. Flux Kustomization
8. Push, reconcile, watch it come up
9. First login over a port-forward
10. Public: tunnel entry and DNS
11. The mixed-content failure, how I found it, how I fixed it
12. Forward auth, part one: Authentik UI (provider, application, outpost)
13. Forward auth, part two: ExternalName Service
14. Forward auth, part three: ForwardAuth Middleware
15. Forward auth, part four: Ingress annotation and callback path
16. Forward auth, part five: Flux ordering
17. Push and test the right way
18. Doing this for the next app
19. Verification commands
20. Rollback
21. What broke, consolidated
22. Decisions
23. Loose ends

---

## 0. Step zero: what Authentik is and what I need to give it

### 0.1 What it is

An identity provider. The way I explain it: every app stops running its own door lock and trusts the badge the security desk issued. Traefik gets a ForwardAuth middleware that asks Authentik "is this person logged in?" on every request before passing traffic to the app. Not logged in, you get bounced to the Authentik login page and sent back afterwards.

Traffic path for a protected app:

```
browser (https)
  -> Cloudflare edge
  -> cloudflared pod
  -> traefik.kube-system.svc.cluster.local:80
  -> Ingress for the app
       -> ForwardAuth middleware asks the Authentik outpost
            -> no session: 302 to auth.projectpattie.com, log in,
               302 back to <app>/outpost.goauthentik.io/callback, cookie set
            -> session: continue
  -> app Service -> pod
```

### 0.2 What the Helm chart deploys

From docs.goauthentik.io, Installation, Kubernetes: the chart deploys an **authentik server** (login UI, API, and an embedded outpost, port 9000), an **authentik worker** (background jobs), and a bundled **PostgreSQL** StatefulSet. The docs say the bundled Postgres is intended for testing. Acceptable for a homelab, noted.

Older docs and my own first diagram also show Redis. Current versions (late 2025 onward) do not ship it; Authentik uses Postgres for sessions and background jobs too, and a `redis:` block in values is ignored. Three pods, not four.

### 0.3 What I have to supply

The docs' minimum values:

- `authentik.secret_key`: a long random string used to sign cookies and tokens. Change it and every session is invalidated.
- `authentik.postgresql.password` and `postgresql.auth.password`: the same password, once for the server to connect with and once for the bundled database to be created with.
- `server.ingress`: class, host.

Plus one I chose to add:

- `AUTHENTIK_BOOTSTRAP_PASSWORD` (and optionally `AUTHENTIK_BOOTSTRAP_EMAIL`), env vars the server reads on first start. With them set, Authentik creates the `akadmin` user with that password and never shows the open initial-setup page at `/if/flow/initial-setup/`. The alternative is to deploy without it and visit that page once over a port-forward. I chose bootstrap because it is declarative: rebuild the cluster from git and the admin exists without anyone clicking. The cost is one more Secret key. The open-window risk of the setup page is also gone.

### 0.4 Version

artifacthub.io/packages/helm/goauthentik/authentik, version at the top. On the day I built it: `2026.8.4`. Pinned.

### 0.5 Where it lives

Two choices: `apps/base/authentik` like any other app, or `infrastructure/` next to monitoring. I chose `infrastructure/auth/`, mirroring `infrastructure/monitoring/` exactly, because every app will eventually depend on Authentik, it should reconcile before `apps` does, and the Prometheus HelmRelease layout was already a known shape to copy.

```
infrastructure/auth/controllers/base/authentik/
  namespace.yaml
  repository.yaml       HelmRepository
  release.yaml          HelmRelease
  kustomization.yaml
infrastructure/auth/controllers/staging/authentik/
  secret.yaml           SOPS
  middleware.yaml       Traefik headers middleware (section 11)
  kustomization.yaml
infrastructure/auth/controllers/staging/kustomization.yaml
clusters/dev/auth.yaml  Flux Kustomization auth-controllers
```

### 0.6 Names decided up front

- Namespace: `authentik`
- Secret: `authentik-secrets`, keys `AUTHENTIK_SECRET_KEY`, `POSTGRES_PASSWORD`, `AUTHENTIK_BOOTSTRAP_PASSWORD`
- Hostname: `auth.projectpattie.com`
- Admin user: `akadmin`
- Flux Kustomization: `auth-controllers`

---

## 1. Folders

```bash
cd ~/Developer/homelab-kubecraft
ls -R infrastructure/monitoring        # the model
cat clusters/dev/*.yaml                # the existing Flux Kustomizations
mkdir -p infrastructure/auth/controllers/base/authentik infrastructure/auth/controllers/staging/authentik
```

---

## 2. Namespace and HelmRepository

Both are values-only copies of the kube-prometheus-stack files:

```bash
cp infrastructure/monitoring/controllers/base/kube-prometheus-stack/namespace.yaml infrastructure/auth/controllers/base/authentik/
cp infrastructure/monitoring/controllers/base/kube-prometheus-stack/repository.yaml infrastructure/auth/controllers/base/authentik/
sed -i '' 's/kube-prometheus-stack/authentik/g; s/monitoring/authentik/g' infrastructure/auth/controllers/base/authentik/namespace.yaml
sed -i '' 's/kube-prometheus-stack/authentik/g; s/monitoring/authentik/g; s#https://.*#https://charts.goauthentik.io#' infrastructure/auth/controllers/base/authentik/repository.yaml
cat infrastructure/auth/controllers/base/authentik/namespace.yaml infrastructure/auth/controllers/base/authentik/repository.yaml
```

`g` at the end of a sed substitution means every match on the line, not just the first. `#` as the separator in the last one because the replacement is a URL full of slashes.

Result:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: authentik
```

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: authentik
  namespace: authentik
spec:
  interval: 24h
  url: https://charts.goauthentik.io
```

A HelmRepository tells Flux where to download charts from. It checks for new chart versions every 24h; nothing upgrades on its own because the release pins a version.

---

## 3. HelmRelease

`infrastructure/auth/controllers/base/authentik/release.yaml` (the final version, after section 11; what was added there is marked)

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: authentik
  namespace: authentik
spec:
  interval: 30m
  chart:
    spec:
      chart: authentik
      version: "2026.8.4"
      sourceRef:
        kind: HelmRepository
        name: authentik
        namespace: authentik
      interval: 12h
  values:
    authentik:
      error_reporting:
        enabled: false
    global:
      env:
        - name: AUTHENTIK_BOOTSTRAP_PASSWORD
          valueFrom:
            secretKeyRef:
              name: authentik-secrets
              key: AUTHENTIK_BOOTSTRAP_PASSWORD
        - name: AUTHENTIK_BOOTSTRAP_EMAIL
          value: craventhegreat@gmail.com
        - name: AUTHENTIK_WEB__BASE_URL            # added in section 11
          value: https://auth.projectpattie.com
    server:
      ingress:
        enabled: true
        ingressClassName: traefik
        annotations:                                # added in section 11
          traefik.ingress.kubernetes.io/router.middlewares: authentik-forwarded-proto-https@kubernetescrd
        hosts:
          - auth.projectpattie.com
    postgresql:
      enabled: true
  valuesFrom:
    - kind: Secret
      name: authentik-secrets
      valuesKey: AUTHENTIK_SECRET_KEY
      targetPath: authentik.secret_key
    - kind: Secret
      name: authentik-secrets
      valuesKey: POSTGRES_PASSWORD
      targetPath: authentik.postgresql.password
    - kind: Secret
      name: authentik-secrets
      valuesKey: POSTGRES_PASSWORD
      targetPath: postgresql.auth.password
```

Written with a heredoc (`cat > file <<'EOF'`), pasted flush left.

### 3.1 Two places to put settings

- `values:` is plain config, safe for GitHub.
- `valuesFrom:` pulls one key out of a Kubernetes Secret and drops it into the chart's values tree at `targetPath`. `valuesKey` is the key in the Secret; `targetPath` is where in the values it lands. So `targetPath: authentik.secret_key` means "set `authentik: { secret_key: <value> }`." It is the Helm version of `secretKeyRef`. The secret never appears in the file.
- `POSTGRES_PASSWORD` is used twice on purpose: once for the server to connect with (`authentik.postgresql.password`), once for the bundled database to be created with (`postgresql.auth.password`). One key, two targets, no drift.

### 3.2 The bootstrap password is not a chart value

It is an env var the server reads. So it goes through `global.env`, which the chart passes to every pod, with an ordinary `secretKeyRef`: the same pattern as the n8n Deployment, just inside Helm values. `AUTHENTIK_BOOTSTRAP_EMAIL` is a plain value.

### 3.3 The rest

- `server.ingress` is the chart creating the Ingress for me, same as Grafana's did. No separate ingress.yaml.
- `postgresql.enabled: true` is the bundled database. No `redis:` block (section 0.2).
- `error_reporting: false` turns off Authentik sending crash data upstream. Personal choice.
- `interval: 30m` on the release and `12h` on the chart: how often Flux checks the release matches what is declared, and how often it checks the chart source.

Check:

```bash
k apply --dry-run=client -f infrastructure/auth/controllers/base/authentik/release.yaml
```

---

## 4. Base kustomization

`infrastructure/auth/controllers/base/authentik/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - repository.yaml
  - release.yaml
```

```bash
k kustomize infrastructure/auth/controllers/base/authentik | head -5
```

---

## 5. Staging: the Secret

### 5.1 Generate

```bash
openssl rand -base64 48   # AUTHENTIK_SECRET_KEY; docs ask for 50+ chars, this is 64
openssl rand -hex 24      # POSTGRES_PASSWORD
```

The bootstrap admin password I chose myself and put in a password manager, because it is one I type.

### 5.2 Write, replace placeholders, encrypt, prove

```bash
cat > infrastructure/auth/controllers/staging/authentik/secret.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: authentik-secrets
  namespace: authentik
type: Opaque
stringData:
  AUTHENTIK_SECRET_KEY: "PASTE-BASE64-HERE"
  POSTGRES_PASSWORD: "PASTE-HEX-HERE"
  AUTHENTIK_BOOTSTRAP_PASSWORD: "YOUR-CHOSEN-ADMIN-PASSWORD"
EOF
vim infrastructure/auth/controllers/staging/authentik/secret.yaml     # ci" on each placeholder, paste, :wq
sops --encrypt --in-place infrastructure/auth/controllers/staging/authentik/secret.yaml
grep -c 'ENC\[' infrastructure/auth/controllers/staging/authentik/secret.yaml
```

Expect `4` (three values plus the `mac` line SOPS adds).

What broke, twice:

- The overlay build failed with `MalformedYAMLError: yaml: line 7: did not find expected key`. Line 7 was the first value line; a paste onto it had broken the quoting. I checked the shape without leaking the values:
  ```bash
  sed 's/: ".*/: "***"/' infrastructure/auth/controllers/staging/authentik/secret.yaml
  ```
  which prints every line with anything after `: "` replaced by `***`. Fixed the quoting in vim.
- `grep -c 'ENC\['` printed `0`. I had pasted real values in and not yet run `sops`. One `git add` away from a plaintext secret on GitHub. Encrypted, re-ran the grep, got `4`. This check is not optional.

---

## 6. Staging: kustomizations

Copies of the monitoring ones:

```bash
cp infrastructure/monitoring/controllers/staging/kustomization.yaml infrastructure/auth/controllers/staging/kustomization.yaml
cp infrastructure/monitoring/controllers/staging/kube-prometheus-stack/kustomization.yaml infrastructure/auth/controllers/staging/authentik/kustomization.yaml
sed -i '' 's/kube-prometheus-stack/authentik/g; s/monitoring/authentik/g; /tls-secret.yaml/d' infrastructure/auth/controllers/staging/kustomization.yaml infrastructure/auth/controllers/staging/authentik/kustomization.yaml
```

`/tls-secret.yaml/d` deletes that line; Authentik has no TLS secret because Cloudflare terminates TLS at the edge for every public app.

`infrastructure/auth/controllers/staging/authentik/kustomization.yaml` (final, with the middleware from section 11):

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: authentik
resources:
  - ../../base/authentik/
  - secret.yaml
  - middleware.yaml
```

`infrastructure/auth/controllers/staging/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - authentik
```

What broke: I ran `vim .../authentik/kustomization` (no `.yaml`) by typo, which created a second, empty kustomization file. Kustomize refused with `Found multiple kustomization files under: .../staging/authentik`. `rm` the stray one. `ls` the folder before building.

Check:

```bash
k kustomize infrastructure/auth/controllers/staging | grep '^kind:'
```

Namespace, Secret, HelmRelease, HelmRepository (plus Middleware once section 11 is done).

---

## 7. Flux Kustomization

This file is the top of the chain. Flux reads `clusters/dev/`, finds it, and that is how the folder becomes a reconciled thing on the cluster.

```bash
cp clusters/dev/monitoring.yaml clusters/dev/auth.yaml
sed -i '' 's/monitoring/auth/g' clusters/dev/auth.yaml
cat clusters/dev/auth.yaml
```

One `sed` covers both things that change, because both contain the word `monitoring`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: auth-controllers
  namespace: flux-system
spec:
  interval: 1m0s
  retryInterval: 1m
  timeout: 5m
  sourceRef:
    kind: GitRepository
    name: flux-system-kubecraft
  path: ./infrastructure/auth/controllers/staging
  prune: true
  decryption:
    provider: sops
    secretRef:
      name: sops-age
```

`decryption` is what lets Flux read the SOPS secret: provider sops, private key in the cluster Secret `sops-age`. `prune: true` means removing a manifest from git deletes it from the cluster.

---

## 8. Push, reconcile, watch it come up

```bash
git add infrastructure/auth clusters/dev/auth.yaml
git commit -m "feat(authentik): add helm release, encrypted secret and flux kustomization"
git push
flux reconcile source git flux-system-kubecraft
```

The **source** gets reconciled this time, not a Kustomization. `auth-controllers` does not exist on the cluster until Flux re-reads `clusters/dev/` and creates it, so there is nothing to reconcile by name yet.

```bash
flux get kustomizations
```

`auth-controllers` appears alongside `apps` and `monitoring-controllers`, Ready True.

```bash
flux get helmreleases -n authentik
k -n authentik get pods
```

First install: `Running 'install' action with timeout of 5m0s`, three pods created.

### 8.1 ErrImagePull on the worker

```
authentik-worker-744767dbf8-zxth6   0/1   ErrImagePull   0
```

`ErrImagePull` on its own tells you nothing. The Events do:

```bash
k -n authentik describe pod -l app.kubernetes.io/component=worker | tail -15
```

```
Warning  Failed   Failed to pull image "ghcr.io/goauthentik/server:2026.8.4": ... read tcp [2601:...]:47136->[2606:...]:443: read: connection reset by peer
Warning  Failed   Error: ErrImagePull
Normal   BackOff  Back-off pulling image
Normal   Pulling  (x2 over 86s)
Normal   Pulled   Successfully pulled image in 39.827s
Normal   Created  Container created
Normal   Started  Container started
```

Read top to bottom: the pull failed with a connection reset (over IPv6 to GitHub's registry), Kubernetes backed off, retried 40 seconds later, succeeded, started. Nothing to fix. The last Event tells you whether it is still a problem. Had it said `not found` the tag would be wrong; `toomanyrequests` would be a registry rate limit.

### 8.2 Running 0/1 for minutes

```bash
k -n authentik logs deploy/authentik-server --tail=30
```

```
  Applying authentik_stages_authenticator_static.0008_initial... OK
  Applying authentik_stages_authenticator_static.0009_throttling... OK
```

Database migrations, hundreds of them on a fresh install, each `OK`. The readiness probe does not pass until they finish. Normal. About eight minutes on this hardware.

```bash
k -n authentik get pods
```

```
authentik-postgresql-0              1/1   Running
authentik-server-7c97b9d647-p8nv4   1/1   Running
authentik-worker-744767dbf8-zxth6   1/1   Running
```

Three, not four. No Redis (section 0.2). I removed the `redis:` block from values afterwards.

---

## 9. First login over a port-forward

Before any DNS or tunnel entry exists, so nothing is public yet:

```bash
k -n authentik get svc                                   # find the server Service name and port
k -n authentik port-forward svc/authentik-server 9000:80
```

http://localhost:9000. Username `akadmin`, password is the bootstrap value from the Secret. Lands on the Application Dashboard ("No Applications available"). The admin was created straight from git; no setup page.

If the bootstrap password is lost:

```bash
sops --decrypt infrastructure/auth/controllers/staging/authentik/secret.yaml | grep BOOTSTRAP
```

Never paste that output anywhere.

Ctrl-C the port-forward.

---

## 10. Public: tunnel entry and DNS

Same four steps as n8n:

1. `apps/base/cloudflared/configmap.yaml`, above the `http_status:404` catch-all:
   ```yaml
       - hostname: auth.projectpattie.com
         service: http://traefik.kube-system.svc.cluster.local:80
       - service: http_status:404
   ```
2. DNS:
   ```bash
   cloudflared tunnel route dns homelab-kubecraft auth.projectpattie.com
   ```
3. Push and apply:
   ```bash
   git add apps/base/cloudflared/configmap.yaml
   git commit -m "feat(cloudflared): route auth.projectpattie.com through traefik"
   git push
   flux reconcile kustomization apps --with-source
   ```
4. Restart the tunnel, because it reads its ConfigMap at startup only:
   ```bash
   k -n cloudflared rollout restart deployment cloudflared
   ```

Then https://auth.projectpattie.com in a private window. The Authentik page loaded, with the background image and logo, and showed:

> The request failed and the interceptors did not return an alternative response

![Authentik interceptors error on the public URL](images/authentik-interceptors-error.png)

---

## 11. The mixed-content failure, how I found it, how I fixed it

That message is Authentik's frontend saying an API call returned something it did not expect. The order I investigated in is the lesson.

### 11.1 Server logs, filtered, while reloading the page

```bash
k -n authentik logs deploy/authentik-server -f | grep -E '"status": (4|5)|warning|error'
```

Two things. Nothing failed server side: no 4xx, no 5xx on any public request. And every public request line said `"scheme": "http"`:

```
{"auth_via": "unauthenticated", "domain_url": "auth.projectpattie.com", "event": "/if/flow/default-authentication-flow/?next=%2F", ... "scheme": "http", "status": 200, ...}
```

Plus a warning on the newest server pod:

```
"message": "No base URL is configured. A configured base URL will be required in a future release. Set it in the system settings or via the AUTHENTIK_WEB__BASE_URL environment variable."
```

### 11.2 Is the API reachable through Cloudflare at all?

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://auth.projectpattie.com/api/v3/root/config/
```

`200`. So Cloudflare and Traefik were not blocking `/api/`. Whatever was failing was not at the edge.

### 11.3 Browser DevTools

Cmd+Option+I on the broken page. The Elements tab showed a Cloudflare beacon script injected into the page, a side clue that Cloudflare was modifying responses, but not the cause.

![Elements tab showing Cloudflare injected script](images/authentik-elements-cloudflare-beacon.png)

The Network tab, Fetch/XHR filter, reload: the flow executor request showed status `(blocked)`, 0 bytes, 0 ms.

![Network tab: executor request blocked](images/authentik-network-blocked.png)

Click it, Headers pane: request URL `http://auth.projectpattie.com/api/v3/flows/executor/default-authentication-flow/?query=...`. **http.**

![Headers pane: request URL is http, not https](images/authentik-network-headers-http.png)

### 11.4 Root cause

The page was served over https. Authentik built its API URL with http. The browser blocked the call as mixed content. The server never received it, which is why the logs were clean.

Authentik saw http because Traefik does not trust forwarded headers by default. Cloudflare terminates TLS and sends `X-Forwarded-Proto: https` to the tunnel; Traefik overwrites it with what Traefik itself saw, which is plain http from the cloudflared pod; Authentik reads http. Over the port-forward it had worked because both sides agreed on http. Grafana and n8n do not care about their own scheme, so they had never shown this.

### 11.5 First attempt: the base URL setting

Added `AUTHENTIK_WEB__BASE_URL: https://auth.projectpattie.com` to `global.env` (section 3) because Authentik was asking for it. Pushed, reconciled `auth-controllers`. Helm did a real rolling update, visible in `k -n authentik get pods -w`: new server and worker pods came up, went `1/1`, then the old ones were terminated. Zero downtime, because Deployments without a pinned RWO disk can run old and new side by side.

Still broken. The frontend builds its API URL from the request scheme, not from that setting. Kept the setting because Authentik warns without it.

### 11.6 The fix: a Traefik headers Middleware

`infrastructure/auth/controllers/staging/authentik/middleware.yaml`

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: forwarded-proto-https
spec:
  headers:
    customRequestHeaders:
      X-Forwarded-Proto: https
```

Before Traefik hands a request to Authentik, it sets `X-Forwarded-Proto: https`. Safe to hardcode because every request through the tunnel really was https at Cloudflare. Register it: `- middleware.yaml` in the staging authentik kustomization.

Attach it to the chart's Ingress through `server.ingress.annotations` in `release.yaml`:

```yaml
        annotations:
          traefik.ingress.kubernetes.io/router.middlewares: authentik-forwarded-proto-https@kubernetescrd
```

The annotation value is always `<namespace>-<middleware name>@kubernetescrd`, Traefik's naming rule for middlewares defined as Kubernetes objects.

### 11.7 The trap on the way

I added the annotation before the Middleware file existed. The heredoc that should have written it had failed: I pasted the block with leading spaces, so the closing `EOF` never matched and the prompt sat at `>` until I hit Ctrl-C. An Ingress annotation referencing a Middleware that does not exist makes Traefik refuse the whole route, which would have taken the login page from "broken" to "nothing loads." Caught it before pushing. Order from now on:

```bash
k kustomize infrastructure/auth/controllers/staging | grep -B1 -A3 'kind: Middleware'   # confirm it exists in the build
grep -n annotations -A1 infrastructure/auth/controllers/base/authentik/release.yaml       # then confirm the annotation
```

Also while editing `release.yaml` by hand I managed to get `annotations:` indented too far and the new `AUTHENTIK_WEB__BASE_URL` entry landed inside the ingress block instead of `global.env`. Rather than fix it line by line I overwrote the whole file with a heredoc (one `>`) containing the final version from section 3, then re-added the annotation with two `sed` lines that set the indentation exactly:

```bash
sed -i '' 's/^ *annotations:$/        annotations:/' infrastructure/auth/controllers/base/authentik/release.yaml
sed -i '' 's/^ *traefik.ingress.kubernetes.io/          traefik.ingress.kubernetes.io/' infrastructure/auth/controllers/base/authentik/release.yaml
```

### 11.8 Push

```bash
git add infrastructure/auth
git commit -m "fix(authentik): force X-Forwarded-Proto https via traefik middleware"
git push
flux reconcile kustomization auth-controllers --with-source
```

Helm updated the Ingress; no pod restart. Reload: the Authentik login page, "Welcome to authentik!", Email or Username field. Signed in as `akadmin` over the public URL. Session three done.

![Authentik login page working on auth.projectpattie.com](images/authentik-login-working.png)

---

## 12. Forward auth, part one: Authentik UI (provider, application, outpost)

Everything in this section is clicking, in the Authentik admin interface. Top right of the user dashboard, **Admin interface**.

### 12.1 Create the application and its provider with the wizard

Left menu, **Applications**, **Applications**, blue **New Application** button (not the arrow next to it).

**Step 1, Application**
- Application Name: `n8n`
- Slug: `n8n` (fills itself)
- Group: empty
- Policy engine mode: ANY
- UI Settings: leave collapsed
- **Next**

![Wizard step 1, application name and slug](images/authentik-wizard-1-application.png)

**Step 2, Choose a Provider**: tiles for OAuth2/OpenID, SAML, SAML from Metadata, RAC, Proxy, Radius, LDAP, SCIM. Click **Proxy Provider**, **Next**.

![Wizard step 2, provider type tiles](images/authentik-wizard-2-provider-type.png)

**Step 3, Configure Proxy Provider**
- Provider Name: `n8n-provider-2`. See 12.2 for why the `-2`.
- Authorization Flow: click "Select a flow...", choose **default-provider-authorization-implicit-consent**
- Three tabs: Proxy, Forward auth (single application), Forward auth (domain level). Click **Forward auth (single application)**. The Internal host field disappears; Traefik does the proxying, so Authentik only needs to know the public address. The tab's own text says it: use with traefik's forwardAuth, and `/outpost.goauthentik.io` on each domain must be routed to the outpost (section 15).
- External host: `https://n8n.projectpattie.com`
- Token validity: leave `hours=24`
- **Next**

![Wizard step 3, proxy provider with forward auth tab](images/authentik-wizard-3-proxy-provider.png)

**Step 4, Configure Bindings**: "No bound policies." Leave empty, **Next**. Bindings are per-app access rules. Empty means any logged-in Authentik user can reach n8n, which is right for a one-person lab. Groups come later.

![Wizard step 4, bindings left empty](images/authentik-wizard-4-bindings.png)

**Step 5, Review**: Application n8n, Policy ANY, Provider Type Proxy, Name n8n-provider-2, Mode Forward auth (single application), External Host https://n8n.projectpattie.com, Basic-Auth No. **Create Application**.

![Wizard step 5, review](images/authentik-wizard-5-review.png)

The Applications list now shows n8n linked to n8n-provider-2, type Proxy Provider.

![Applications list with n8n linked to n8n-provider-2](images/authentik-applications-n8n.png)

### 12.2 What went wrong, and the cleanup

I had first gone to **Applications, Providers, New Provider** and created a standalone `n8n-provider` with the same settings. The Providers list then showed it with a warning, "Provider not assigned to any application."

![Providers list after first create, provider not assigned warning](images/authentik-providers-unassigned.png)

I expected the Application wizard to let me pick it. It does not; step 2 offers provider *types* only and always creates a new provider. So the wizard's provider got the `-2` name, and the orphan needed deleting: **Applications, Providers**, tick the checkbox next to `n8n-provider`, **Delete**, confirm.

If doing it again: skip the Providers page, use the wizard first.

### 12.3 Assign the provider to the embedded outpost

**Applications, Outposts**. One row, `authentik Embedded Outpost`, type Proxy. Click the pencil icon.

Edit Outpost dialog: Outpost Name and Type already set, Integration empty (leave it). Under Applications, two columns: Available Applications on the left lists `n8n` with `n8n-provider-2` under it; click it so it moves to Selected Applications on the right and shows "1 item selected." **Save Changes**.

![Edit Outpost with n8n in Selected Applications](images/authentik-outpost-n8n-selected.png)

The outpost is the piece Traefik talks to. This tells it which apps it answers for. Without this step the ForwardAuth check returns an error for n8n no matter what git says.

---

## 13. Forward auth, part two: ExternalName Service

The n8n Ingress will need to send one path, `/outpost.goauthentik.io`, to the Authentik server, because that is where the browser lands after logging in and the outpost needs to receive it on n8n's hostname to set the cookie for that domain. But an Ingress can only point at Services in its own namespace. An ExternalName Service is an alias: a Service in the `n8n` namespace whose only job is to say "I really mean `authentik-server.authentik.svc.cluster.local`." A mail-forwarding address.

`apps/staging/n8n/authentik-service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: authentik-server
spec:
  type: ExternalName
  externalName: authentik-server.authentik.svc.cluster.local
  ports:
    - name: http
      port: 80
```

Goes in the n8n overlay, not in base, because it only exists for this cluster's SSO wiring.

---

## 14. Forward auth, part three: ForwardAuth Middleware

`apps/staging/n8n/middleware.yaml`

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: authentik-forward-auth
spec:
  forwardAuth:
    address: http://authentik-server.authentik.svc.cluster.local:80/outpost.goauthentik.io/auth/traefik
    trustForwardHeader: true
    authResponseHeaders:
      - X-authentik-username
      - X-authentik-groups
      - X-authentik-email
      - X-authentik-name
      - X-authentik-uid
```

- `address` is the outpost's "is this person logged in?" endpoint; `/auth/traefik` is the Traefik-flavored one.
- `trustForwardHeader: true` passes the original host and scheme along so Authentik can build the correct redirect back to n8n.
- `authResponseHeaders` are what n8n would see about the logged-in user, if it cared. Community n8n does not use them; they cost nothing to pass.

Register both new files in `apps/staging/n8n/kustomization.yaml`:

```yaml
resources:
  - ../../base/n8n
  - secret.yaml
  - ingress.yaml
  - authentik-service.yaml
  - middleware.yaml
patches:
  - path: deployment-patch.yaml
```

Check:

```bash
k kustomize apps/staging/n8n | grep '^kind:'
```

Three Services now (n8n, n8n-postgres, the authentik-server alias) and a Middleware among the rest.

---

## 15. Forward auth, part four: Ingress annotation and callback path

Two changes to the n8n Ingress: the annotation that attaches the middleware, and a second path rule so the outpost callback goes to Authentik instead of n8n. Since the paths list changes shape, overwrite the file with a heredoc.

`apps/staging/n8n/ingress.yaml` (final)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: n8n
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: n8n-authentik-forward-auth@kubernetescrd
spec:
  ingressClassName: traefik
  rules:
    - host: n8n.projectpattie.com
      http:
        paths:
          - path: /outpost.goauthentik.io
            pathType: Prefix
            backend:
              service:
                name: authentik-server
                port:
                  number: 80
          - path: /
            pathType: Prefix
            backend:
              service:
                name: n8n
                port:
                  number: 80
```

- Annotation value: `n8n-authentik-forward-auth`, namespace dash middleware name, `@kubernetescrd`.
- The outpost path goes to the ExternalName alias from section 13; everything else goes to n8n. Traefik picks the longest matching prefix, so order in the list does not matter, but convention puts the specific one first.
- The middleware applies to the whole Ingress, including the callback path. That is fine; the outpost handles its own path.

Check:

```bash
k kustomize apps/staging/n8n | grep -A2 annotations
```

---

## 16. Forward auth, part five: Flux ordering

`clusters/dev/apps.yaml`, under `spec:`, same indent as `interval`:

```yaml
  dependsOn:
    - name: auth-controllers
```

Flux now applies `apps` only after `auth-controllers` is healthy. On a cluster rebuild, the n8n Middleware never points at an Authentik Service that does not exist yet.

---

## 17. Push and test the right way

```bash
git add apps/staging/n8n clusters/dev/apps.yaml
git commit -m "feat(n8n): protect with authentik forward auth via traefik"
git push
flux reconcile kustomization apps --with-source
k -n n8n get middleware,ingress
```

```
middleware.traefik.io/authentik-forward-auth   67s
ingress.networking.k8s.io/n8n   traefik   n8n.projectpattie.com   10.0.0.106,10.0.0.195,10.0.0.20   80
```

Then I opened n8n in my normal browser and landed straight on the n8n home screen with no Authentik in sight. For a second it looked like it had not worked. It had. That browser already held cookies for both Authentik and n8n, so it sailed through, which proves nothing.

**Test auth in a private window, always.** Cmd+Shift+N, https://n8n.projectpattie.com:

- Address bar changes to `auth.projectpattie.com/if/flow/default-authentication-flow/?client_id=...`
- Page says "Welcome to authentik! Log in to continue to n8n."
- Log in as `akadmin`
- Redirected back to `n8n.projectpattie.com`, which shows n8n's own sign-in, then n8n.

![Forward auth redirect: Log in to continue to n8n](images/n8n-forward-auth-redirect.png)

n8n still shows its own login after Authentik's. That is a second lock, not a bug. Community n8n has no SSO support, so the two stay separate.

---

## 18. Doing this for the next app

Four pieces per app, in this order:

1. **Authentik UI**: Applications, Applications, New Application wizard. Proxy Provider, Forward auth (single application), external host `https://<app>.projectpattie.com`, implicit-consent flow, empty bindings. Then Applications, Outposts, edit the Embedded Outpost, add the new application, save.
2. **ExternalName Service** in the app's overlay, identical to section 13 (same name, same target). One per namespace.
3. **ForwardAuth Middleware** in the app's overlay, identical to section 14. One per namespace.
4. **Ingress**: annotation `<namespace>-authentik-forward-auth@kubernetescrd`, and the `/outpost.goauthentik.io` path to `authentik-server` port 80 before the app's own path.

Register the two new files in the overlay kustomization, build, confirm the Middleware is in the output before pushing the annotation, push, reconcile, test in a private window.

Apps whose tunnel entry still points straight at the Service (Linkding, Homepage, Mealie, ntfy, littlelink, Audiobookshelf) first need the same move n8n made in `n8n.md` sections 12 to 16: an Ingress through Traefik and the tunnel entry pointed at `traefik.kube-system.svc.cluster.local:80`.

Order I plan: Grafana (already has an Ingress), then Homepage, Linkding, Audiobookshelf.

---

## 19. Verification commands

```bash
flux get kustomizations
flux get helmreleases -n authentik
k -n authentik get pods,svc,ingress,middleware
k -n n8n get svc,ingress,middleware
curl -s -o /dev/null -w '%{http_code}\n' https://auth.projectpattie.com/api/v3/root/config/     # 200
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://n8n.projectpattie.com/        # 302 to auth.projectpattie.com
```

---

## 20. Rollback

- Take n8n out from behind Authentik but keep it public: remove the `annotations` block and the `/outpost.goauthentik.io` path from `apps/staging/n8n/ingress.yaml`, push, reconcile. The Middleware and ExternalName Service can stay; unused objects are harmless.
- Remove Authentik: do the n8n Ingress rollback first or n8n's route breaks on the missing middleware target. Then delete `clusters/dev/auth.yaml` and the `dependsOn` block in `apps.yaml`, push. `prune: true` removes the namespace and the Postgres PVC. Authentik's users and config are gone.
- Undo a bad tunnel ConfigMap: `git revert`, push, reconcile, `rollout restart deployment cloudflared`.

---

## 21. What broke, consolidated

| Symptom | Cause | Fix |
|---|---|---|
| `MalformedYAMLError: yaml: line 7` building the overlay | Paste broke the quoting on a secret value line | Inspect masked with `sed 's/: ".*/: "***"/'`; fix quotes |
| `grep -c 'ENC\['` printed `0` | Secret written with real values, `sops` not yet run | Encrypt, re-check, then `git add`. Never skip the grep |
| `Found multiple kustomization files` | `vim .../kustomization` typo created a second file | `rm` the stray file |
| Worker `ErrImagePull` / `ImagePullBackOff` | Transient `connection reset by peer` pulling from ghcr.io | Nothing; Events showed the retry succeeded 40s later |
| Server and worker `Running 0/1` for ~8 minutes | First-boot migrations | Wait; `logs --tail=30` shows `Applying ... OK` |
| Three pods, no Redis | Chart no longer ships Redis | Remove `redis:` from values |
| Public page: "The request failed and the interceptors did not return an alternative response" | Mixed content: page https, API URL built as http, because Traefik overwrote `X-Forwarded-Proto` | Traefik headers Middleware `X-Forwarded-Proto: https` via Ingress annotation; also set `AUTHENTIK_WEB__BASE_URL` |
| Base URL setting alone did not fix it | Frontend builds its URL from the request scheme, not that setting | The Middleware is the fix; keep the setting anyway |
| Heredoc prompt stuck at `>`, middleware file never written | Pasted lines had leading spaces | Ctrl-C, paste flush left |
| Would have taken the whole auth route down | Annotation added before the Middleware existed | Build and grep for `kind: Middleware` first, then add the annotation |
| `annotations:` mis-indented, env var landed in the wrong block | Hand-editing release.yaml in vim | Overwrite the file with a heredoc; `sed` with `^ *` to set exact indentation |
| Wizard would not reuse the provider I made first | Application wizard only creates new providers | Use the wizard first; delete the orphan from Providers |
| Forward auth "worked" with no redirect | Existing cookies in my normal browser | Always test in a private window |

---

## 22. Decisions

- **Authentik under `infrastructure/`, not `apps/`,** mirroring monitoring. Platform piece other apps depend on; `apps` now `dependsOn` it.
- **Bootstrap admin password over the initial-setup page.** Declarative; rebuild from git and the admin exists. One more Secret key.
- **Bundled Postgres** despite the docs calling it test-only. Acceptable for a homelab.
- **`error_reporting: false`.** Personal choice.
- **One `POSTGRES_PASSWORD` key targeting two chart values.** No drift.
- **Forward auth single application, empty bindings.** Any Authentik user can reach n8n. Revisit with groups.
- **ExternalName Service and ForwardAuth Middleware live in each app's overlay,** not in the authentik tree, so each app's SSO wiring travels with the app.
- **`X-Forwarded-Proto: https` hardcoded by Middleware** rather than configuring Traefik's trusted-IPs for forwarded headers. Smaller change, scoped to one Ingress; revisit if more apps need it.
- **n8n keeps its own login behind Authentik.** Community n8n has no SSO. Two locks.
- **Confirm before fixing.** The base-URL attempt was a fix applied before the browser confirmed the diagnosis. The curl and the Network tab took two minutes and should have come first.

---

## 23. Loose ends

- Grafana, Homepage, Linkding, Audiobookshelf behind Authentik, one at a time (section 18).
- Groups and bindings once there is more than one user.
- Decide whether to move Authentik's database off the bundled chart Postgres.
- Rotate the tunnel credential that was printed to a chat log during the earlier secrets module.
- Password manager, now that `akadmin` is one more thing to remember.

Screenshots referenced above go in `docs/runbooks/images/` under the filenames shown.
