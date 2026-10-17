# Runbook: deploying n8n on the homelab, start to finish

This is how I deployed n8n into the homelab-kubecraft cluster, from reading the app's docs to a public URL on Homepage. Every folder, every file, every command, in the order I did them, with what each one does and the check that it worked before the next step. The mistakes are where they happened, not hidden at the end.

If I had to rebuild n8n from nothing, this file is enough. Adding single sign-on in front of it is a separate runbook, `authentik.md`.

Cluster context: k3s on three nodes (craventhegreat is the control plane, dell-node and biggie-smalls are workers), Flux v2, two repos on one cluster. This work lives in `CravenCraven/homelab-kubecraft`, reconciled by the Flux GitRepository `flux-system-kubecraft` from `./clusters/dev`. Secrets are SOPS with age. Public exposure is one Cloudflare tunnel named `homelab-kubecraft` running in the `cloudflared` namespace. I work from the Mac, in `~/Developer/homelab-kubecraft`, with `k` as my kubectl alias.

---

## Contents

0. Step zero: before writing any YAML
1. Folders
2. Namespace
3. Postgres: headless Service and StatefulSet
4. n8n Deployment
5. PVC
6. Service
7. Base kustomization and the first build check
8. Staging overlay: the Secret
9. Staging overlay: kustomization and enabling the app
10. Push, reconcile, watch it come up
11. First login and proof the data is in Postgres
12. Public, part one: Ingress through Traefik
13. Public, part two: public URL patch
14. Public, part three: tunnel entry
15. Public, part four: DNS record
16. Push, restart the tunnel, test from outside
17. Homepage tile
18. Verification commands
19. Rollback
20. What broke, consolidated
21. Decisions
22. Loose ends

---

## 0. Step zero: before writing any YAML

Nothing gets typed until I know what the app needs. For any self-hosted app these are the questions, and where I got the answers for n8n.

### 0.1 What does the app need to run?

The fastest source is the project's docker-compose example, because compose maps almost line for line onto a Kubernetes container block:

| docker-compose | Kubernetes |
|---|---|
| `image:` | `image:` |
| `ports: "5678:5678"` | `containerPort: 5678` |
| `environment:` | `env:` |
| `volumes: ./data:/home/node/.n8n` | `volumeMounts` + `volumes` (the right-hand side is the `mountPath`) |
| `user: "1000"` | `securityContext.runAsUser` |

For n8n that page is docs.n8n.io, Hosting, Installation, Docker Compose, the variant with Postgres. From it:

- Image: `docker.n8n.io/n8nio/n8n`
- Port: 5678, one port for the UI and webhooks
- Data path: `/home/node/.n8n`
- Runs as user `node`, uid 1000
- With Postgres: `DB_TYPE=postgresdb`, `DB_POSTGRESDB_HOST`, `DB_POSTGRESDB_PORT`, `DB_POSTGRESDB_DATABASE`, `DB_POSTGRESDB_USER`, `DB_POSTGRESDB_PASSWORD`
- Health endpoint: `/healthz`

Then the environment variable reference (Hosting, Configuration, Environment variables) for the ones compose does not show:

- `N8N_ENCRYPTION_KEY`: encrypts stored credentials. If not set, n8n invents one on first boot and writes it to the data volume. Lose that volume or change the key and every saved credential is unreadable forever. So I set it myself, from git, before first boot.
- `GENERIC_TIMEZONE` and `TZ`: the scheduler ("run every day at 9am") uses these. Without them, 9am means 9am UTC.
- `N8N_HOST`, `N8N_PROTOCOL`, `WEBHOOK_URL`: how n8n builds the URLs it shows for webhooks and sets cookies. Needed once it is public.

You never invent env var names. The app only reads the ones it was programmed to read. Type `DB_HOST` when n8n wants `DB_POSTGRESDB_HOST` and it silently falls back to SQLite.

### 0.2 Which version?

Open github.com/n8n-io/n8n/releases and take the newest one not marked pre-release or beta. On the day I built this that was `2.43.7`. Pinned, never `latest`. In GitOps git should say exactly what is running; with `latest`, two pods pulled an hour apart can be different software and git cannot tell you.

Postgres: `postgres:16`. Official image, runs as uid 999 (the `postgres` user baked in), listens on 5432, stores data at `/var/lib/postgresql/data`, bootstraps the first database from `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` on first start only.

### 0.3 SQLite or Postgres?

n8n runs fine on SQLite for one replica and it is officially supported. I chose Postgres because it is what you would run anywhere real and because it forced me to learn how a database fits into Kubernetes. The cost: a second pod, a StatefulSet, a second secret value, and a Service so n8n can find it.

### 0.4 Deployment or StatefulSet?

n8n: Deployment, with `strategy: Recreate`. One replica with a ReadWriteOnce disk; a RollingUpdate would deadlock waiting for a disk still attached to the old pod. Same rule as Linkding, Mealie, Audiobookshelf.

Postgres: StatefulSet. A database has to come back as the same pod with the same disk every time. A StatefulSet gives the pod a stable name (`n8n-postgres-0`), a stable DNS identity through a headless Service, and a disk created from a `volumeClaimTemplates` block that is named after the pod and reattached forever.

### 0.5 Where does it live in the repo?

Same shape as linkding and audiobookshelf:

```
apps/base/n8n/          generic manifests, no namespace on them
apps/staging/n8n/       this cluster's specifics: namespace stamp, Secret, Ingress, patch
apps/staging/kustomization.yaml   gets one new line: - n8n
```

Base is the app as it would run anywhere. Staging is this cluster. If I ever had a second cluster it would get its own overlay and the same base.

One file per object for small things. One file for a component with several objects that belong together: Postgres gets `postgres.yaml` holding its Service and StatefulSet, separated by `---`.

### 0.6 Where should the pods run?

I left it to the scheduler. The questions I should have asked, and will next time: which node has headroom (`k top nodes`), which node do I trust to stay up (biggie-smalls has lost power before), and should the app and its database share a node to avoid cross-node traffic. local-path storage pins a PVC to whichever node the pod first lands on, so this decision is permanent unless you recreate the PVC. See section 10 for how it turned out.

### 0.7 Names decided up front

- Namespace: `n8n`
- Labels: `app: n8n`, `app: n8n-postgres`
- Secret: `n8n-secrets`, keys `N8N_ENCRYPTION_KEY`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- PVCs: `n8n-data` (2Gi, written by me), `data-n8n-postgres-0` (5Gi, created by the StatefulSet)
- Services: `n8n` (ClusterIP, port 80), `n8n-postgres` (headless, 5432)
- Hostname: `n8n.projectpattie.com`, single level, because Cloudflare's free certificate only covers one level deep

---

## 1. Folders

```bash
cd ~/Developer/homelab-kubecraft
git status
mkdir -p apps/base/n8n apps/staging/n8n
```

`git status` first and last in every session. `-p` creates parents and does not complain if the folder exists.

---

## 2. Namespace

`apps/base/n8n/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: n8n
```

A Namespace is a labeled drawer. Everything n8n owns goes in it, so `k -n n8n get all` shows only n8n and deleting the namespace removes the whole app. No other base file carries `namespace:`; the overlay stamps it on everything in section 9.

Check:

```bash
k apply --dry-run=client -f apps/base/n8n/namespace.yaml
```

`--dry-run=client` parses the YAML and checks field names without touching the cluster. It prints `namespace/n8n created (dry run)` or an error with a line number. Its limit, learned the hard way in section 3: it checks that the YAML is well-formed, not that blocks are at the right level. It passed a file where `spec` was nested under `metadata`. Read the file after it passes.

---

## 3. Postgres: headless Service and StatefulSet

`apps/base/n8n/postgres.yaml`, Service first because the StatefulSet's `serviceName` refers to it.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: n8n-postgres
spec:
  clusterIP: None
  selector:
    app: n8n-postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: n8n-postgres
spec:
  serviceName: n8n-postgres
  replicas: 1
  selector:
    matchLabels:
      app: n8n-postgres
  template:
    metadata:
      labels:
        app: n8n-postgres
    spec:
      securityContext:
        runAsUser: 999
        runAsGroup: 999
        fsGroup: 999
      containers:
        - name: postgres
          image: postgres:16
          ports:
            - name: postgres
              containerPort: 5432
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_USER
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_PASSWORD
            - name: POSTGRES_DB
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_DB
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          readinessProbe:
            exec:
              command:
                - pg_isready
                - -U
                - $(POSTGRES_USER)
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              memory: 512Mi
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes:
          - ReadWriteOnce
        storageClassName: local-path
        resources:
          requests:
            storage: 5Gi
```

### 3.1 The Service, line by line

- `clusterIP: None`: headless. DNS returns the pod's own address instead of a virtual IP. Convention for StatefulSets.
- `selector: app: n8n-postgres`: how the Service finds its pod, by label. Must match the pod template's labels exactly or traffic goes nowhere.
- `ports`: a list, because a Service can expose more than one. `port` is the door people knock on, `targetPort` the door on the container.

### 3.2 The StatefulSet, line by line

- `apiVersion: apps/v1`: StatefulSet lives in the `apps` group like Deployment. Service was plain `v1` because it is a core object.
- `serviceName`: ties the pod's DNS identity to the headless Service. The pod is reachable as `n8n-postgres-0.n8n-postgres`.
- No `strategy:` field. A StatefulSet has none; it updates one pod at a time, which gives the same protection against the RWO deadlock that `Recreate` gives a Deployment.
- `selector.matchLabels` and `template.metadata.labels` must match, or the controller creates a pod it cannot find.
- The shape to hold in your head for any workload: outer `spec` is how to manage the pods (replicas, selector), `template` is the pod blueprint, inner `template.spec` is how to run the pod (containers, volumes, securityContext). Everything above `template:` is rules; everything below it is the pod.
- `securityContext` 999/999/999: 999 is the `postgres` user inside the image. `fsGroup` makes Kubernetes chown the mounted disk to that group before the container starts. Wrong uid and Postgres cannot write to its own disk and crashloops.
- `PGDATA` one directory below the mount, so Postgres never tries to initialize into a mount point that already has `lost+found` in it.
- The three `POSTGRES_*` vars come from the Secret with `valueFrom.secretKeyRef`: "look in Secret `n8n-secrets`, key `POSTGRES_USER`." The value never appears in the manifest. They are only read on the very first boot to create the user and database; after that Postgres uses what is on disk. Username and database name are not secrets, but keeping all three in one Secret means n8n and Postgres read the same values from one place and cannot drift.
- `readinessProbe` with `exec`: Postgres does not speak HTTP, so `httpGet` will not work. `pg_isready` is Postgres's own check. `$(POSTGRES_USER)` is Kubernetes expanding an env var from the same container, not a shell. `initialDelaySeconds: 5` waits before the first check; `periodSeconds: 10` repeats.
- `resources`: `requests` is what the scheduler reserves when picking a node, `limits` the ceiling. `250m` is a quarter of a core. No CPU limit on purpose: CPU limits throttle, memory limits kill. A memory ceiling is enough for a homelab database.
- `volumeMounts.name: data` must match the template's `metadata.name: data`.
- `volumeClaimTemplates` sits at the StatefulSet level (same indent as `template:`), not inside the pod. It is a PVC minus `apiVersion` and `kind`. The StatefulSet stamps out a PVC named `data-n8n-postgres-0` (template name, dash, pod name) and reattaches it to that pod name on every restart. This is the whole reason it is a StatefulSet. There is no `pvc.yaml` for Postgres.

### 3.3 How the file was actually built, and what broke

I wrote the Service by hand in vim and got it right on the second try (first try had dotted keys like `spec.clusterIP: None`, which is how people talk about YAML paths, not how the file is written; the file uses nesting). The StatefulSet was where vim and I fell out.

- **vim autoindent re-indented every pasted block.** A 14-line container block came out with `containers:` at column 0 and the dashes doubled. Fix: `:set paste` before pasting, `:set nopaste` after. `:set list` shows spaces as dots when a block looks right but is not.
- **`image: postgres: 16`** (space after the colon) makes YAML read it as a nested map. It is `postgres:16`.
- **`-name:`** needs a space after the dash. The dash is the list marker.
- **`containderPort`** typo. Caught by the dry run.
- **`PASSWOD`** in a `secretKeyRef` key. The dry run cannot catch this; Kubernetes would have failed at pod start with "key not found in secret" and never said typo. Fixed with `sed -i '' 's/PASSWOD/PASSWORD/' apps/base/n8n/postgres.yaml`, then lost the fix by `:q!` in vim, then fixed it again.
- **`spec:` nested under `template.metadata`** instead of beside it, so `securityContext` sat where Kubernetes would ignore it and the pod would have run as root. The client dry run passed. I only saw it by reading the `cat` output. Fixed with two fenced `sed` edits:
  ```bash
  sed -i '' '/^  template:/,/^      containers:/ s/^      spec:$/    spec:/' apps/base/n8n/postgres.yaml
  sed -i '' '/securityContext:/,/fsGroup:/ s/^  //' apps/base/n8n/postgres.yaml
  ```
  The fence (`/start/,/end/`) matters because `volumeClaimTemplates` also has a 6-space `spec:` that must stay.
- **vim got into a state where every key beeped** and nothing I typed landed. Closed the terminal tab, opened a new one. The file on disk is whatever was last saved.
- **The tail of the file got pasted twice**, once on top of the other. Fix from the shell, no editor:
  ```bash
  sed -i '' '/volumeMounts/,$d' apps/base/n8n/postgres.yaml     # delete from first match to end of file
  tail -1 apps/base/n8n/postgres.yaml                            # confirm the last line is key: POSTGRES_DB
  cat >> apps/base/n8n/postgres.yaml <<'EOF'
            volumeMounts:
              ...
  EOF
  ```
  `cat >>` appends; `<<'EOF'` takes everything up to the line `EOF` literally, spaces included, so nothing can re-indent it. Paste it flush left or the closing `EOF` does not match and the prompt sticks at `>`.

The rule that came out of all this: write anything longer than a few lines with a heredoc from the shell, use `sed` for surgical edits, use vim for changing a word. Read the file after every build.

Check:

```bash
k apply --dry-run=client -f apps/base/n8n/postgres.yaml
cat apps/base/n8n/postgres.yaml
```

Both objects print `created (dry run)`, and in the `cat`, `spec:` under `template` lines up with `metadata:`, `securityContext:` lines up with `containers:`, and the only thing at 2 spaces below `template:` is `volumeClaimTemplates:`.

Commit the checkpoint:

```bash
git add apps/base/n8n
git commit -m "wip(n8n): namespace and postgres statefulset"
git push
```

`wip` means work in progress, not wired up yet, so future me knows this commit was not meant to run. Nothing deploys yet because `apps/staging/kustomization.yaml` does not list n8n.

---

## 4. n8n Deployment

I first copied the Audiobookshelf Deployment, then saw more than half of it had to go (an initContainer, three of four volumes, `envFrom`). Copy when you are changing values, write fresh when you are changing structure. Deleted the copy and wrote this in three heredoc chunks.

`apps/base/n8n/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: n8n
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: n8n
  template:
    metadata:
      labels:
        app: n8n
    spec:
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        runAsNonRoot: true
      containers:
        - name: n8n
          image: docker.n8n.io/n8nio/n8n:2.43.7
          securityContext:
            allowPrivilegeEscalation: false
          ports:
            - name: http
              containerPort: 5678
          env:
            - name: GENERIC_TIMEZONE
              value: America/New_York
            - name: TZ
              value: America/New_York
            - name: DB_TYPE
              value: postgresdb
            - name: DB_POSTGRESDB_HOST
              value: n8n-postgres
            - name: DB_POSTGRESDB_PORT
              value: "5432"
            - name: DB_POSTGRESDB_DATABASE
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_DB
            - name: DB_POSTGRESDB_USER
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_USER
            - name: DB_POSTGRESDB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: POSTGRES_PASSWORD
            - name: N8N_ENCRYPTION_KEY
              valueFrom:
                secretKeyRef:
                  name: n8n-secrets
                  key: N8N_ENCRYPTION_KEY
          volumeMounts:
            - name: data
              mountPath: /home/node/.n8n
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 30
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              memory: 1Gi
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: n8n-data
```

### 4.1 The six questions every container block answers

1. **What runs:** `name`, `image`. Pinned version from step 0.2.
2. **What is it allowed to do:** `securityContext`. Pod level says who: uid 1000, the `node` user inside the n8n image; `fsGroup: 1000` hands the disk to that user, which is why no chown initContainer is needed (Audiobookshelf needed one). `runAsNonRoot: true` makes Kubernetes refuse to start the pod if the image tried to run as root. Container level adds `allowPrivilegeEscalation: false`: even if the process asks, it cannot gain more rights than it started with.
3. **Which door:** `ports`. 5678, named `http` so the Service and probes refer to the name, not the number.
4. **How is it configured:** `env`. Names from step 0.1. Plain `value` for anything safe on GitHub; `secretKeyRef` for anything not. `DB_POSTGRESDB_HOST: n8n-postgres` is the headless Service name from section 3, resolved by cluster DNS inside the namespace. `"5432"` is quoted because env values must be strings; unquoted, YAML reads a number and Kubernetes rejects it. Left side of each entry is what n8n expects, right side is our Secret's key; they do not have to match, the mapping is the point.
5. **Where does it keep files:** `volumeMounts`. `/home/node/.n8n` from the docs. The volume named `data` is defined at the bottom.
6. **How does Kubernetes know it is healthy:** `readinessProbe` ("do not send traffic until this passes"), `livenessProbe` ("if this keeps failing, restart me"), `resources`. `httpGet` here because n8n speaks HTTP; Postgres needed `exec`.

`volumes` sits at the pod level, lined up with `containers:`, because volumes belong to the pod, not to one container. A Deployment cannot create its own PVC, so this points at one written separately in section 5.

Check:

```bash
k apply --dry-run=client -f apps/base/n8n/deployment.yaml
cat apps/base/n8n/deployment.yaml
```

---

## 5. PVC

Copied from the Audiobookshelf PVC file, kept only the first block, changed three things: name, removed `namespace:`, size.

`apps/base/n8n/pvc.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: n8n-data
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 2Gi
```

2Gi is plenty. With Postgres the real data is in the database; this disk holds a config file and uploaded binaries. No `storageClassName` means the k3s default, which is `local-path`. The Postgres file spelled it out; both forms work, worth recognizing both.

Check: `k apply --dry-run=client -f apps/base/n8n/pvc.yaml`

---

## 6. Service

`apps/base/n8n/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: n8n
spec:
  type: ClusterIP
  selector:
    app: n8n
  ports:
    - name: http
      port: 80
      targetPort: http
```

- Normal ClusterIP, not headless: n8n is a web app that wants one stable address. Headless was for the database pod's fixed identity. `type: ClusterIP` is the default and could be omitted; writing it makes the intent obvious.
- `selector: app: n8n` matches the pod template label in the Deployment.
- Two different things both called `http`: `name: http` is a label for the Service's own port 80, so an Ingress can refer to it by name later. `targetPort: http` points at the container port named `http` in the Deployment (5678). Three doors in a row: Ingress to Service port 80, Service to container port 5678, container to n8n. If n8n ever changes its port I fix the number once, in the Deployment.

What broke: I had `namespace: audiobookshelf` left over from a copy, then deleted the value and left `namespace:` with nothing after it. An empty value means null and may be rejected. Delete the whole line.

Check: `k apply --dry-run=client -f apps/base/n8n/service.yaml`

---

## 7. Base kustomization and the first build check

`apps/base/n8n/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - pvc.yaml
  - postgres.yaml
  - deployment.yaml
  - service.yaml
```

A table of contents. Anything not listed is ignored, which is why nothing from this folder deployed yet. Each name must be a real file sitting next to this one (`ls apps/base/n8n` to check); Kustomize does not guess. Order does not matter to Kustomize; convention is namespace, storage, workloads, networking so a reader can follow. Top-level keys start at column 0.

Build the folder the way Flux will:

```bash
k kustomize apps/base/n8n
k kustomize apps/base/n8n | grep '^kind:'
```

The first prints all five files combined (Kustomize reorders and alphabetizes keys; that is normal). The second is a quick inventory: Namespace, Service, Service, PersistentVolumeClaim, Deployment, StatefulSet.

```bash
git add apps/base/n8n
git commit -m "feat(n8n): add deployment, pvc, service and kustomization"
```

---

## 8. Staging overlay: the Secret

### 8.1 Generate the random values

```bash
openssl rand -hex 32    # 64 hex chars: n8n encryption key
openssl rand -hex 24    # 48 hex chars: postgres password
```

Asks the OS for random bytes and prints them as hex. Username and database name I chose: `n8n` for both.

### 8.2 Write it in plain text

```bash
cat > apps/staging/n8n/secret.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: n8n-secrets
  namespace: n8n
type: Opaque
stringData:
  N8N_ENCRYPTION_KEY: "<paste 64 hex here>"
  POSTGRES_USER: "n8n"
  POSTGRES_PASSWORD: "<paste 48 hex here>"
  POSTGRES_DB: "n8n"
EOF
```

Then open it and replace the two placeholders with the generated strings (in vim, cursor on the placeholder, `ci"` deletes everything inside the quotes and drops into insert mode; paste; `Esc`; `:wq`).

- `stringData` lets me write plain text; Kubernetes base64-encodes it on apply. `data` would make me base64 it myself.
- Keep the quotes. A value made only of digits can get read as a number instead of text.
- This is the one file that carries `namespace:`, because a Secret is tied to one cluster.
- The four keys match exactly what the Deployment and StatefulSet reference.

### 8.3 Encrypt in place and prove it

```bash
sops --encrypt --in-place apps/staging/n8n/secret.yaml
head -12 apps/staging/n8n/secret.yaml
grep -c 'ENC\[' apps/staging/n8n/secret.yaml
```

`sops --encrypt --in-place` uses the rules in `.sops.yaml` at the repo root (which keys get encrypted, which age recipient). Afterwards every value under `stringData` reads `ENC[AES256_GCM,...]`, the keys stay readable, and a `sops:` block with the age recipient and a `mac` is appended. `encrypted_regex: ^(data|stringData)$` in that block is what says only the values got encrypted.

`grep -c 'ENC\['` counts encrypted lines. It must print more than zero before `git add`. No exceptions. In the Authentik runbook it printed `0` once and saved me from pushing a plaintext secret.

The encrypted file is safe to paste anywhere. The plain one is not.

---

## 9. Staging overlay: kustomization and enabling the app

`apps/staging/n8n/kustomization.yaml` (this is the version at the end of section 11; sections 12 and 13 add two more lines)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: n8n
resources:
  - ../../base/n8n
  - secret.yaml
```

- `namespace: n8n` stamps the namespace onto everything that comes from base. That is why no base file needed one.
- `../../base/n8n` is a relative path: up two folders from `apps/staging/n8n/`, then into `base/n8n`. Pointing at a folder means "use that folder's kustomization.yaml."

Build the overlay:

```bash
k kustomize apps/staging/n8n | head -20
```

Namespace first, then the Secret with `ENC[...]` values (Flux decrypts at apply time; Kustomize on the Mac does not). It just has to build without errors.

Enable it. In `apps/staging/kustomization.yaml`, add `- n8n` next to `- linkding` and `- audiobookshelf`:

```yaml
resources:
  - linkding
  - n8n
  - ../base/cloudflared
  - ../base/homepage
  - ../base/mealie
  - ../base/ntfy
  - ../base/littlelink
  - audiobookshelf
```

Two styles already in that list: a bare name means "this app has a staging overlay folder, use its kustomization.yaml"; `../base/<app>` means "no overlay, use base directly." n8n has an overlay, so bare name. This one line is what turns a folder in git into things running on the cluster.

```bash
git add apps/staging/n8n apps/staging/kustomization.yaml
git commit -m "feat(n8n): add staging overlay with encrypted secret and enable in apps"
git push
```

---

## 10. Push, reconcile, watch it come up

```bash
flux reconcile kustomization apps --with-source
```

Tells Flux "do not wait for the next poll, pull git now and apply the `apps` Kustomization." `--with-source` refreshes the GitRepository first; without it Flux re-applies what it already pulled. Output walks through: annotating GitRepository, fetched revision `main@sha1:...`, annotating Kustomization, applied revision.

```bash
k -n n8n get pods,pvc
```

At 26 seconds:

```
pod/n8n-59874ccc48-lb7ff   0/1   ContainerCreating   0   26s
pod/n8n-postgres-0         0/1   Running             0   26s
persistentvolumeclaim/data-n8n-postgres-0   Bound   ...   5Gi
persistentvolumeclaim/n8n-data              Bound   ...   2Gi
```

Reading it: both PVCs Bound on the first try; the Postgres PVC name is the template-plus-pod-name pattern. `n8n-postgres-0` is Running but `0/1`, meaning the process is up and the readiness probe (`pg_isready`) has not passed yet because Postgres is initializing the database. n8n is `ContainerCreating`, which at this age means the image is still downloading.

```bash
k -n n8n get pods -o wide
```

`-o wide` adds NODE and IP. At three minutes both were `1/1 Running`. n8n on `craventhegreat` (the control plane), Postgres on `biggie-smalls` (the node that gets unplugged). local-path has now pinned both PVCs to those nodes. If biggie-smalls loses power, n8n is up with no database, which is worse than both being down. Logged as a known risk; fix is a `nodeSelector` on both workloads and fresh PVCs. The other columns, NOMINATED NODE (preemption) and READINESS GATES (extra readiness conditions set by external systems), are `<none>` and will stay that way in this lab.

If something is wrong instead:

```bash
k -n n8n describe pod -l app=n8n
```

The Events at the bottom tell you whether it is a secret-not-found (`CreateContainerConfigError`), an image pull problem, or scheduling. `Pending` with no events usually means a PVC that cannot bind.

---

## 11. First login and proof the data is in Postgres

```bash
k -n n8n port-forward svc/n8n 5678:80
```

Forwards the Mac's port 5678 to port 80 on the n8n Service. Open http://localhost:5678. First visit is "Set up owner account." Create it with a strong password; this login is going to be internet-facing. Skip the survey screens. Ctrl-C ends the forward. That owner account is n8n's own lock and it has to exist before anything goes public.

```bash
k -n n8n exec n8n-postgres-0 -- psql -U n8n -d n8n -c '\dt'
```

`exec` runs a command inside the Postgres pod; `psql` is its client; `\dt` lists tables. Pages of n8n tables (`user`, `workflow_entity`, `credentials_entity`, `migrations`, ...). That is the proof the Deployment is talking to the StatefulSet and not a local SQLite file.

Part one done: n8n and Postgres running, deployed entirely from git, secrets encrypted, owner account set.

---

## 12. Public, part one: Ingress through Traefik

### 12.1 Why Traefik is in the path this time

Every public app before n8n had its own line in the tunnel ConfigMap pointing straight at the app's Service. That works until you want to put something in front of all of them. With Traefik in the path, the tunnel has one line ("send this hostname to Traefik") and Traefik decides where it goes by reading Ingress objects. Traefik is then the one place a login check can be attached to every app, which is what the Authentik runbook does.

An Ingress controller is a reverse proxy that reads its routing rules from Kubernetes objects instead of a config file. Traefik is the one k3s ships with. The two Ingresses already in the repo (linkding, Grafana) are the model.

### 12.2 The file

`apps/staging/n8n/ingress.yaml` (first version; the Authentik runbook adds an annotation and a second path)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: n8n
spec:
  ingressClassName: traefik
  rules:
    - host: n8n.projectpattie.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: n8n
                port:
                  number: 80
```

- Lives in staging, not base, because the hostname is this cluster's.
- `ingressClassName: traefik` says which controller owns this rule.
- `port.number: 80` is the **Service** port, not the container's 5678. The Service already does that hop. `port.name: http` would also work and survives a renumber; either is fine.

What broke: I copied the linkding Ingress and changed the port to `5376`, which is neither the Service port nor the container port, and I wrote it as `port: 5376` with `number: 80` nested under it. The dry run said `error parsing ... line 15: mapping values are not allowed in this context`. Line 15 was that line. `port:` is a parent key with no value of its own. Fix: `sed -i '' 's/port: 5376/port:/' apps/staging/n8n/ingress.yaml`.

Register it: add `- ingress.yaml` under `- secret.yaml` in `apps/staging/n8n/kustomization.yaml`.

Check:

```bash
k apply --dry-run=client -f apps/staging/n8n/ingress.yaml
k kustomize apps/staging/n8n | grep 'kind: Ingress'
```

Do not push yet. n8n needs its public URL before anyone hits it.

---

## 13. Public, part two: public URL patch

n8n builds the webhook URLs it shows you and sets its cookies from `N8N_HOST`, `N8N_PROTOCOL`, `WEBHOOK_URL`. That address is specific to this cluster, so it does not go in base. The overlay adds it with a Kustomize **patch**: a small file that says "take the Deployment from base and add these env vars." Kustomize merges it. First use of `patches:` in this repo.

`apps/staging/n8n/deployment-patch.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: n8n
spec:
  template:
    spec:
      containers:
        - name: n8n
          env:
            - name: N8N_HOST
              value: n8n.projectpattie.com
            - name: N8N_PROTOCOL
              value: https
            - name: WEBHOOK_URL
              value: https://n8n.projectpattie.com/
```

- Same `kind` and `name` as the base Deployment, so Kustomize knows which object to patch.
- Only the path down to the thing being changed. The container `name: n8n` is what lets the patch find the right container.
- Everything not mentioned stays as it is in base.

Register it in `apps/staging/n8n/kustomization.yaml` as a new top-level key, column 0, below `resources`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: n8n
resources:
  - ../../base/n8n
  - secret.yaml
  - ingress.yaml
patches:
  - path: deployment-patch.yaml
```

`patches` is a different key from `resources`: resources are whole objects to create, patches are changes to objects that come from base. What broke: I indented `patches:` two spaces so it looked like part of `resources`. It is top level. `<<` in vim shifts a line left.

Verify the merge added to the env list instead of replacing it:

```bash
k kustomize apps/staging/n8n | grep -A1 'N8N_HOST\|WEBHOOK_URL\|DB_TYPE'
```

The three new vars and `DB_TYPE` from base all appear. Added, not replaced.

```bash
git add apps/staging/n8n
git commit -m "feat(n8n): add traefik ingress and public url env patch"
```

---

## 14. Public, part three: tunnel entry

Confirm Traefik's Service name and port before pointing anything at it:

```bash
k -n kube-system get svc traefik
```

```
traefik   LoadBalancer   10.43.136.14   10.0.0.106,10.0.0.195,10.0.0.20   80:32342/TCP,443:30589/TCP
```

Service `traefik`, port 80. The three external IPs are all three nodes, because k3s's built-in load balancer binds Traefik on every node; that is why local-only hostnames work against any node IP.

Edit `apps/base/cloudflared/configmap.yaml`. The pattern for every entry is `http://<service>.<namespace>.svc.cluster.local:<port>`, the full cluster DNS name, because the tunnel pod lives in a different namespace from everything it points at. Add above the catch-all:

```yaml
    ingress:
    - hostname: links.projectpattie.com
      service: http://linkding.linkding.svc.cluster.local:9090
    ...
    - hostname: abs.projectpattie.com
      service: http://audiobookshelf.audiobookshelf.svc.cluster.local:3005
    - hostname: n8n.projectpattie.com
      service: http://traefik.kube-system.svc.cluster.local:80
    - service: http_status:404
```

In vim: `/http_status` Enter, `O` (capital, opens a line above), type the two lines at four-space indent, `Esc`, `:wq`.

**The `- service: http_status:404` line must stay last.** It is the catch-all with no hostname. cloudflared refuses to start without one, and that takes every app in the lab offline at once. I deleted it by accident during this edit and caught it in the `cat` before pushing. Also add `:80` even though `http://` defaults to it, so every entry spells its port.

---

## 15. Public, part four: DNS record

```bash
cloudflared tunnel route dns homelab-kubecraft n8n.projectpattie.com
```

Creates a proxied CNAME (orange cloud) from `n8n.projectpattie.com` to the tunnel's `<tunnel-id>.cfargotunnel.com`. Output: `Added CNAME n8n.projectpattie.com which will route to this tunnel`.

I forgot this step the first time and the browser said `DNS_PROBE_FINISHED_NXDOMAIN`. NXDOMAIN means the name does not exist in DNS. The request never reached Cloudflare, the tunnel, or the cluster, so nothing on the cluster was at fault.

Verify, skipping the Mac's local cache (which had cached the earlier "does not exist"):

```bash
dig +short n8n.projectpattie.com @1.1.1.1
```

Two Cloudflare IPs back means the record is live. If the browser still says NXDOMAIN, flush the Mac's cache:

```bash
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

My phone on 5G kept showing `ERR_NAME_NOT_RESOLVED` for about twenty minutes after the Mac worked. The carrier's resolver holds a negative answer. Airplane mode on and off, then wait.

---

## 16. Push, restart the tunnel, test from outside

```bash
git add apps/base/cloudflared/configmap.yaml
git commit -m "feat(cloudflared): route n8n.projectpattie.com through traefik"
git push
flux reconcile kustomization apps --with-source
k -n cloudflared rollout restart deployment cloudflared
k -n cloudflared get pods
```

cloudflared reads its ConfigMap at startup only, so the restart is required after every tunnel change. Two fresh pods with low AGE means it took.

Open https://n8n.projectpattie.com in a private window. n8n's sign-in page. Sign in with the owner account from section 11 and land on the n8n home screen. The Mac is not using any local shortcut here; there is no local DNS entry for n8n, so that request went to Cloudflare and back through the tunnel like anyone else's would.

Reading errors by layer, from this and the next runbook:

| What you see | Layer | Meaning |
|---|---|---|
| `DNS_PROBE_FINISHED_NXDOMAIN` / `ERR_NAME_NOT_RESOLVED` | DNS | Record missing or not propagated. Cluster not involved. |
| `413 Payload Too Large` with `cloudflare` in the body | Cloudflare edge | Free plan caps one request at 100 MB. Not the app. Upload via port-forward or a local-only Ingress. |
| `502` | Tunnel to Service | Tunnel reached the cluster, the Service did not answer. |
| `404` from Traefik | Traefik | Tunnel and Traefik fine, no Ingress matched the host. |
| Frontend error, clean server logs | Browser | Open the Network tab. See the Authentik runbook. |

---

## 17. Homepage tile

`apps/base/homepage/configmap.yaml`, under `services.yaml`, in the `Apps` group after littlelink:

```yaml
        - littlelink:
            href: https://me.projectpattie.com
            description: Link page
            icon: mdi-link-variant
        - n8n:
            href: https://n8n.projectpattie.com
            description: Automation
            icon: n8n.png
        - Audiobookshelf:
            href: https://abs.projectpattie.com
            description: Audiobooks and podcasts
            icon: audiobookshelf.png
```

Icons are names from the dashboard-icons set (`name.png`). `mdi-` icons are font icons with no extension.

What broke: my paste landed the two entries at 11 and 10 spaces instead of 12. Homepage tolerated 11 and silently dropped the 10. I had also accidentally added `.png` to littlelink's `mdi-link-variant`, which broke a tile that had been fine for weeks. Found it with `sed -n '35,50p'` on the file, fixed with line-ranged sed:

```bash
sed -i '' '37s/mdi-link-variant.png/mdi-link-variant/' apps/base/homepage/configmap.yaml
sed -i '' '39,41s/^           /            /' apps/base/homepage/configmap.yaml
sed -i '' '43,45s/^          /            /' apps/base/homepage/configmap.yaml
```

`39,41s/...` means lines 39 to 41 only. Then:

```bash
git add apps/base/homepage/configmap.yaml
git commit -m "feat(homepage): add n8n and audiobookshelf"
git push
flux reconcile kustomization apps --with-source
k -n homepage rollout restart deployment homepage
```

Homepage reads its ConfigMap at startup, same as cloudflared. home.projectpattie.com shows six Apps tiles with icons.

n8n is deployed, public, and on the dashboard. Continue in `authentik.md` to put a login in front of it.

---

## 18. Verification commands

```bash
flux get kustomizations
k -n n8n get pods,pvc,svc,ingress -o wide
k -n n8n exec n8n-postgres-0 -- psql -U n8n -d n8n -c '\dt'
k kustomize apps/staging/n8n | grep -A1 'N8N_HOST\|DB_TYPE'
curl -s -o /dev/null -w '%{http_code}\n' https://n8n.projectpattie.com/
dig +short n8n.projectpattie.com @1.1.1.1
```

Before the Authentik runbook the curl returns `200`; after it, `302`.

---

## 19. Rollback

- Remove n8n entirely: delete `- n8n` from `apps/staging/kustomization.yaml`, push, reconcile. `prune: true` on the Flux Kustomization removes everything including both PVCs. Data is gone.
- Take it off the internet but keep it running: remove the `n8n.projectpattie.com` entry from the cloudflared ConfigMap, push, reconcile, `rollout restart deployment cloudflared`. Optionally `cloudflared tunnel route dns` has no delete; remove the CNAME in the Cloudflare dashboard.
- Undo any single bad commit: `git revert <sha>`, push, reconcile, restart whichever pod reads a ConfigMap at startup.

---

## 20. What broke, consolidated

| Symptom | Cause | Fix |
|---|---|---|
| Dotted keys (`spec.clusterIP:`) in a Service | Writing YAML paths the way people say them | Nesting with indentation |
| Pasted blocks re-indented, dashes doubled, `containers:` at column 0 | vim autoindent on paste | `:set paste` / `:set nopaste`; or heredocs from the shell; `:set list` to count spaces |
| `image: postgres: 16` | Space after the colon makes a nested map | `postgres:16` |
| `-name:` | Missing space after the list dash | `- name:` |
| `containderPort`, `metadat`, `selecto` | Typos | Dry run catches these |
| `PASSWOD` in a `secretKeyRef` key | Typo the dry run cannot see | `sed -i '' 's/PASSWOD/PASSWORD/' file`; would have failed at pod start with "key not found" |
| Fix lost | `:q!` after fixing | Save before quitting; or make the fix with `sed` from the shell |
| `spec:` nested under `template.metadata`, dry run passed | Indentation; client dry run checks shape, not meaning | Fenced `sed` to re-indent; read the `cat` output after every build |
| Tail of file duplicated | Paste on top of a block I thought was deleted | `sed -i '' '/marker/,$d'` then `cat >>` with a heredoc |
| vim beeping at every key | Stuck in a mode | `Esc` x3, `:q!`; or close the terminal tab |
| Heredoc prompt stuck at `>` | Pasted lines had leading spaces, closing `EOF` did not match | Ctrl-C, re-paste flush left |
| `namespace:` with empty value | Deleted the value, not the line | `dd` the whole line |
| Ingress `line 15: mapping values are not allowed` | `port: 5376` with `number: 80` nested under it | `port:` has no value of its own; `number: 80` is its child |
| `patches:` indented under `resources:` | Indentation | Top-level key, column 0 |
| `DNS_PROBE_FINISHED_NXDOMAIN` | Forgot `cloudflared tunnel route dns` | Create the record; `dig @1.1.1.1`; flush local cache |
| Phone `ERR_NAME_NOT_RESOLVED` after the Mac worked | Carrier resolver negative cache | Airplane mode toggle, wait |
| Catch-all `http_status:404` missing from tunnel config | Deleted during edit | Must be last; would have taken the whole tunnel down |
| Homepage dropped a tile, littlelink icon broke | 10/11-space indents; `.png` on an `mdi-` icon | Line-ranged `sed`; `rollout restart deployment homepage` |
| `413 Payload Too Large`, `cloudflare` in the body (Audiobookshelf upload) | Cloudflare free plan 100 MB per request | Port-forward for uploads; local-only Ingress later |

---

## 21. Decisions

- **Postgres over SQLite.** More to learn, closer to real. Cost: a second pod, a StatefulSet, a Service, a second secret value.
- **StatefulSet for the database, Deployment for the app.** Databases need the same pod name and same disk every time.
- **One file per object for small apps; one file per component for a sub-component like a database.** `postgres.yaml` holds its Service and StatefulSet, Service first.
- **Base files carry no namespace; the overlay stamps it.** Exception: the Secret, tied to one cluster.
- **Cluster-specific config is an overlay patch, not a base edit.** First `patches:` in the repo.
- **Scheduler placement left alone.** Resulted in n8n on the control plane and Postgres on biggie-smalls, both PVCs pinned there. Known risk, fix with `nodeSelector` and fresh PVCs.
- **Tunnel points at Traefik for n8n, not at the app Service.** New pattern; makes a shared login check possible.
- **Single-level hostname.** Cloudflare's free certificate covers one level.
- **Copy when changing values, write fresh when changing structure.** Learned from the Audiobookshelf copy.
- **Heredocs and `sed` over vim for anything longer than a word.** Learned over two hours on night one.

---

## 22. Loose ends

- `nodeSelector` for n8n and n8n-postgres so they land together on a node that stays up, then recreate the PVCs.
- `apps/base/uptime-kuma/` is untracked locally; deploy it and add n8n.
- Local-only Ingress for Audiobookshelf uploads (the 413 problem).
- Password manager.
- Put n8n behind Authentik: `docs/runbooks/authentik.md`.
