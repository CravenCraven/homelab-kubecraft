# Capstone: Audiobookshelf through GitOps

This is my runbook for the KubeCraft capstone exercise. I deployed Audiobookshelf to my k3s homelab using nothing but Git and Flux, gave it storage that survives restarts, stopped it running as root, and put it on the public internet.

I wrote this so I can rebuild it from scratch, and so I can explain every piece without looking anything up.

## What the exercise asked for

**Stage 1**

- The app uses port 3005
- The port is set through a ConfigMap
- A ClusterIP Service on port 3005
- Reach the app through a port-forward and upload an audio file

**Stage 2**

- Persistent volumes for every volume the Audiobookshelf docs suggest
- Audiobooks and configuration survive a pod restart

**Stage 3**

- Run as the `node` user, not root
- Reachable from the public internet

**The rule over all of it:** everything goes through GitOps. No `kubectl apply`.

## Where everything lives

Repo: `homelab-kubecraft`

```
apps/
├── base/
│   ├── audiobookshelf/
│   │   ├── namespace.yaml
│   │   ├── configmap.yaml
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── storage.yaml
│   │   └── kustomization.yaml
│   └── cloudflared/
│       └── configmap.yaml        (one rule added for this app)
└── staging/
    ├── kustomization.yaml        (one line added: audiobookshelf)
    └── audiobookshelf/
        └── kustomization.yaml
```

- `base` is the generic recipe for the app.
- `staging` is this cluster's version of it. Flux reads staging, and staging points back at base.
- I have no `production` folder yet. That waits until there is a second cluster.

## How the pieces fit together

I think of the app as a small shop inside a big building, and the building is the cluster.

| Piece | What it is in plain words |
|---|---|
| Git repo | The blueprint. Nothing gets built unless it is drawn here first. |
| Flux | The contractor who reads the blueprint and builds whatever changed. |
| Namespace | The shop's own room. |
| ConfigMap | A settings card on the wall that says "use port 3005". |
| Deployment | The manager who keeps one worker (the pod) running and hands them the settings card. |
| Service | The shop's internal phone extension. It always rings the worker, wherever they sit today. |
| Port-forward | A temporary private line from my Mac to that extension. |
| PVCs | Storage lockers that stay put when the worker is replaced. |
| securityContext | The worker's ID badge. It makes them a guest, not the building owner. |
| Cloudflare tunnel | The public front door. |

The full path of a request once everything is done:

```
phone -> DNS (abs) -> Cloudflare -> tunnel -> Service :3005 -> pod running as node -> volumes
```

## Before I started

1. Pull the repo so my Mac matches GitHub.
   ```
   cd ~/Developer/homelab-kubecraft
   git pull
   ```
2. Look up the newest Audiobookshelf release on its GitHub releases page. I used `2.37.1`.
   - The release is named `v2.37.1` but the image tag has no `v`: `ghcr.io/advplyr/audiobookshelf:2.37.1`
   - I pin the exact version. With `latest` the app could change under me without any change in Git, and then Git no longer describes what is running.

## Stage 1: get it running on port 3005

### 1. Make the folders

```
mkdir -p apps/base/audiobookshelf apps/staging/audiobookshelf
```

`-p` creates missing parent folders and does not complain if one already exists. I did not commit yet, because Git ignores empty folders and an empty path breaks Flux.

### 2. Namespace

`apps/base/audiobookshelf/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: audiobookshelf
```

The room the app lives in. Without it, Kubernetes rejects every other file, because they all say `namespace: audiobookshelf`.

### 3. ConfigMap

`apps/base/audiobookshelf/configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: audiobookshelf-config
  namespace: audiobookshelf
data:
  PORT: "3005"
```

- Audiobookshelf listens on port 80 unless it is handed an environment variable called `PORT`.
- The quotes matter. ConfigMap values have to be text, and without quotes YAML reads `3005` as a number.
- A ConfigMap does nothing on its own. The Deployment has to pull it in.

I got the "why" wrong the first time. I guessed the port lives in a ConfigMap so the Kustomization can see it. That is not it. Kustomize sees every file the same way. The real reason is that settings should be separate from the thing that runs the app. I can change a setting in one small file, and reuse the same Deployment somewhere else with a different card.

### 4. Deployment (Stage 1 version)

`apps/base/audiobookshelf/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: audiobookshelf
  namespace: audiobookshelf
spec:
  replicas: 1
  selector:
    matchLabels:
      app: audiobookshelf
  template:
    metadata:
      labels:
        app: audiobookshelf
    spec:
      containers:
        - name: audiobookshelf
          image: ghcr.io/advplyr/audiobookshelf:2.37.1
          envFrom:
            - configMapRef:
                name: audiobookshelf-config
          ports:
            - containerPort: 3005
```

- `replicas: 1` keeps exactly one pod running. If it dies, the Deployment starts another.
- `selector` and `template.labels` must match. The Deployment stamps pods with `app: audiobookshelf` and manages the pods carrying that stamp.
- `envFrom` with `configMapRef` is the plug. It hands every key in the ConfigMap to the app as an environment variable. This is how `PORT=3005` reaches Audiobookshelf.
- `containerPort: 3005` is a label for humans and tools. The ConfigMap is what really changes the port.

If the `envFrom` block were missing, the card would still exist but the app would never see it. It would start on port 80 and my port-forward to 3005 would hit nothing.

### 5. Service

`apps/base/audiobookshelf/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: audiobookshelf
  namespace: audiobookshelf
spec:
  type: ClusterIP
  selector:
    app: audiobookshelf
  ports:
    - port: 3005
      targetPort: 3005
```

- Pods get a new address every time they restart. The Service is the fixed extension that always finds the pod.
- `ClusterIP` means it only works inside the cluster. That is why I need a port-forward from my Mac.
- `selector` finds the pod by the same label the Deployment stamps on it.
- `port` is what callers dial. `targetPort` is the port on the pod.

### 6. Kustomization files

`apps/base/audiobookshelf/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
```

`apps/staging/audiobookshelf/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: audiobookshelf
resources:
  - ../../base/audiobookshelf
```

Then one new line at the bottom of the `resources` list in `apps/staging/kustomization.yaml`:

```yaml
  - audiobookshelf
```

A `kustomization.yaml` is a table of contents. Flux does not scan folders for YAML. It only applies what is listed. A file that is not on the list never gets deployed, even if it is sitting right there in the folder.

### 7. Preview, push, watch

```
kubectl kustomize apps/staging
```
Builds the YAML the way Flux will and prints it, without touching the cluster.

```
git status
git add apps/base/audiobookshelf apps/staging
git commit -m "Add: audiobookshelf capstone stage 1"
git push
```
Check, pick, save, send.

```
flux get kustomizations --watch
```
Watch Flux apply the commit. I wait for `apps` to show `Ready True`, then Ctrl+C.

```
kubectl get pods -n audiobookshelf
```
One pod, `1/1`, `Running`.

### 8. Port-forward and upload

```
kubectl logs -n audiobookshelf deploy/audiobookshelf
```
Look for `Listening on port :3005`. That is the proof the ConfigMap was read.

```
kubectl port-forward -n audiobookshelf svc/audiobookshelf 3005:3005
```
Opens a private line from my Mac's port 3005 to the Service's port 3005. The terminal sits there looking frozen. That is normal. It only works while the command runs.

Then in the browser at `http://localhost:3005`:

1. Create the root user. The first visitor gets to make the admin account, so I claim it here, privately, long before the app is public.
2. Add a library called Audiobooks with the folder `/audiobooks`.
3. Click the upload icon in the top bar (the arrow pointing up), pick the library, drop in an MP3, upload.

Ctrl+C in the terminal closes the line.

## Stage 2: make the data survive

Up to here everything lives inside the container, like notes on a whiteboard. Restart the pod and the whiteboard is wiped, admin account and all. A PersistentVolumeClaim (PVC) is a request slip that says "I need a locker this big". The pod then plugs the locker in at a folder.

### 1. Four PVCs

The Audiobookshelf docs suggest four volumes: `/config`, `/metadata`, `/audiobooks`, `/podcasts`.

`apps/base/audiobookshelf/storage.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: abs-config
  namespace: audiobookshelf
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: abs-metadata
  namespace: audiobookshelf
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: abs-audiobooks
  namespace: audiobookshelf
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 15Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: abs-podcasts
  namespace: audiobookshelf
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 15Gi
```

- `---` separates several objects in one file.
- `ReadWriteOnce` means one node at a time can use the locker.
- No `storageClassName`, so it uses the cluster default, which is k3s local-path.
- `/config` holds the SQLite database. `/metadata` holds covers, logs, and backups.

### 2. Plug them into the Deployment

Three additions to `deployment.yaml`:

```yaml
spec:
  replicas: 1
  strategy:
    type: Recreate
```

```yaml
          volumeMounts:
            - name: config
              mountPath: /config
            - name: metadata
              mountPath: /metadata
            - name: audiobooks
              mountPath: /audiobooks
            - name: podcasts
              mountPath: /podcasts
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: abs-config
        - name: metadata
          persistentVolumeClaim:
            claimName: abs-metadata
        - name: audiobooks
          persistentVolumeClaim:
            claimName: abs-audiobooks
        - name: podcasts
          persistentVolumeClaim:
            claimName: abs-podcasts
```

- `volumes` is the list of lockers this pod may use. Each one gets a short nickname and points at a PVC by `claimName`.
- `volumeMounts` says where each locker shows up inside the container.
- `strategy: Recreate` stops the old pod fully before starting the new one. The default starts the new pod first, and then two pods fight over a one-at-a-time locker and the rollout hangs.

One locker, start to finish:

```
app writes to /config
  -> volumeMounts: name config, mountPath /config
  -> volumes: name config, claimName abs-config
  -> storage.yaml: PVC abs-config
  -> a real folder on the node's disk
```

The full final Deployment is in Stage 3.

### 3. Register and push

Add `- storage.yaml` to the `resources` list in `apps/base/audiobookshelf/kustomization.yaml`, then:

```
kubectl kustomize apps/staging
git add apps/base/audiobookshelf
git commit -m "Add: audiobookshelf capstone stage 2 storage"
git push
flux reconcile kustomization apps --with-source
```

`reconcile` tells Flux to compare Git with the cluster right now. `--with-source` makes it download the newest commit from GitHub first.

```
kubectl get pvc -n audiobookshelf
kubectl get pods -n audiobookshelf -o wide
```

Four PVCs showing `Bound`, and a new pod. `-o wide` adds the NODE column.

This stage did not go in cleanly. See "What broke" below.

### 4. The restart test

1. Port-forward and set the app up again. The Stage 1 admin account and upload are gone, which is the point. They lived on the whiteboard.
2. Create the root user, add the Audiobooks library at `/audiobooks`, upload an MP3, play it.
3. Ctrl+C the port-forward.
4. Delete the pod.
   ```
   kubectl delete pod -n audiobookshelf -l app=audiobookshelf
   ```
   `-l` picks the pod by label so I do not need the random name. Only the pod goes. The Deployment is still there and starts a replacement.
5. Wait for the new pod to show `1/1 Running`, port-forward again, refresh the browser.

**Result:** it logged me straight back in and the library was still there. The new pod accepted my browser's saved login, which means it read the same database from the `/config` volume. A wiped app would have shown "Create Root User" again.

## Stage 3: non-root, then public

The order is on purpose. Lock it down first, then open the door.

### 1. Run as the node user

Before the change:

```
kubectl exec -n audiobookshelf deploy/audiobookshelf -- whoami
root
```

`exec` runs a command inside the running container. Everything after `--` is that command.

Root inside a container is the all-powerful user. If someone finds a bug in the app, they are root too. The image comes with an ordinary user called `node` (user ID 1000), so I run as that.

The catch: my volumes were filled in Stage 2 while the app was root, so the files in them belonged to root. An ordinary user cannot write to root's files. So a small helper container runs first, hands the four folders to user 1000, and exits.

Final `apps/base/audiobookshelf/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: audiobookshelf
  namespace: audiobookshelf
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: audiobookshelf
  template:
    metadata:
      labels:
        app: audiobookshelf
    spec:
      initContainers:
        - name: fix-permissions
          image: busybox:1.36
          command: ["sh", "-c", "chown -R 1000:1000 /config /metadata /audiobooks /podcasts"]
          securityContext:
            runAsUser: 0
          volumeMounts:
            - name: config
              mountPath: /config
            - name: metadata
              mountPath: /metadata
            - name: audiobooks
              mountPath: /audiobooks
            - name: podcasts
              mountPath: /podcasts
      containers:
        - name: audiobookshelf
          image: ghcr.io/advplyr/audiobookshelf:2.37.1
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          envFrom:
            - configMapRef:
                name: audiobookshelf-config
          ports:
            - containerPort: 3005
          volumeMounts:
            - name: config
              mountPath: /config
            - name: metadata
              mountPath: /metadata
            - name: audiobooks
              mountPath: /audiobooks
            - name: podcasts
              mountPath: /podcasts
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: abs-config
        - name: metadata
          persistentVolumeClaim:
            claimName: abs-metadata
        - name: audiobooks
          persistentVolumeClaim:
            claimName: abs-audiobooks
        - name: podcasts
          persistentVolumeClaim:
            claimName: abs-podcasts
```

The app's `securityContext`:

- `runAsUser: 1000` runs the app as the node user.
- `runAsGroup: 1000` puts it in the matching group.
- `runAsNonRoot: true` is a safety net. Kubernetes refuses to start the container if it would run as root.
- `allowPrivilegeEscalation: false` means the app cannot promote itself later.

The init container:

- Init containers run before the app and have to finish first.
- `chown -R 1000:1000` means change owner, everything inside, to user 1000 and group 1000.
- `sh -c` runs the text as a shell command.
- `runAsUser: 0` is root. Only root can give files away. It runs for a second and exits. The app itself never runs as root.

This is also why the exercise picked port 3005 back in Stage 1. Ordinary users cannot open ports below 1024, so the default port 80 would have blocked the node user.

```
git add apps/base/audiobookshelf/deployment.yaml
git commit -m "Add: run audiobookshelf as non-root node user"
git push
flux reconcile kustomization apps --with-source
```

After the change:

```
kubectl exec -n audiobookshelf deploy/audiobookshelf -- whoami
Defaulted container "audiobookshelf" out of: audiobookshelf, fix-permissions (init)
node
```

The "Defaulted container" line is only a notice. The pod has two containers now and kubectl is saying which one it picked.

```
kubectl logs -n audiobookshelf deploy/audiobookshelf --tail=20
```

`--tail=20` shows the last 20 lines. I looked for three things and found all of them:

- `Db connection was successful` (the node user can use the database)
- `Initializing watcher for "Audiobooks"` (my library came back)
- `Listening on port :3005`

No permission errors.

### 2. Add the hostname to the tunnel

My public apps go through one Cloudflare tunnel called `homelab-kubecraft`. Its config is a list of rules: when a request arrives for this hostname, send it to this Service.

In `apps/base/cloudflared/configmap.yaml` I added one rule, above the catch-all 404 line:

```yaml
    - hostname: abs.projectpattie.com
      service: http://audiobookshelf.audiobookshelf.svc.cluster.local:3005
    - service: http_status:404
```

- The address reads as: Service name, then namespace, then `svc.cluster.local`, then the Service port.
- It is the same ClusterIP Service from Stage 1. The tunnel is one more caller dialling that extension.
- Rules are read top to bottom and the 404 line catches everything. A rule placed below it never gets reached.

```
git add apps/base/cloudflared/configmap.yaml
git commit -m "Add: abs.projectpattie.com route to cloudflared tunnel"
git push
flux reconcile kustomization apps --with-source
kubectl rollout restart deployment -n cloudflared
kubectl get pods -n cloudflared
```

cloudflared reads its config when it starts, so `rollout restart` replaces the pods and they load the new rule. It changes no configuration. Git is still the source of truth. My other public sites drop for a few seconds while this happens.

Both cloudflared pods came back `1/1 Running`, and the tunnel page in Cloudflare showed `abs.projectpattie.com` in its list of routes.

### 3. The DNS record

The tunnel knowing a hostname is only half of it. The internet also has to know the name exists.

```
cloudflared tunnel route dns homelab-kubecraft abs.projectpattie.com
```

This creates a DNS record that sends the hostname into the tunnel. It answered with `Added CNAME abs.projectpattie.com which will route to this tunnel`.

In the Cloudflare DNS list it shows up like my other public apps: type Tunnel, `homelab-kubecraft`, Proxied.

This step is outside Git, because DNS lives outside the cluster.

### 4. Test

Open `https://abs.projectpattie.com`. The login page loads and the root account works.

The honest test for "public" is a phone with Wi-Fi turned off, so the request really comes from outside the house.

## What broke

### A stray letter at the top of the Deployment

After pasting the Stage 2 Deployment in vim, nothing happened. No PVCs at all:

```
kubectl get pvc -n audiobookshelf
No resources found in audiobookshelf namespace.
```

`flux get kustomizations` had the answer on the `apps` row:

```
failed to decode Kubernetes apiVersion, kind and name from: {"iapiVersion":"apps/v1","kind":"Deployment", ...
```

`iapiVersion`. I pressed `i` to get into insert mode and a second `i` got typed into the file. Flux could not tell what kind of object it was looking at.

How I narrowed it down, in order:

1. `cat apps/base/audiobookshelf/kustomization.yaml` to check `storage.yaml` was listed. It was.
2. `git log --oneline -3` to check the commit was pushed. It was.
3. `flux get kustomizations` to read the actual error.

Fix: open the file, cursor on the stray letter, press `x`, `:wq`, then commit and push.

Two things I took from it:

- `head -1 <file>` after every paste. It prints the first line so I can see the damage before I push.
- Flux refused the whole commit and left Stage 1 running. The `apps` row stayed on the old revision. A bad commit did not take down the working app.

### Flux still showing the old error after my fix

I pushed the fix and `flux get kustomizations` showed the same error. The fix was fine. Flux had not downloaded it yet. The `flux-system-kubecraft` row was still on the old commit.

Flux checks GitHub on a timer. To make it look now:

```
flux reconcile kustomization apps --with-source
```

Lesson: check the revision on the source row before deciding a fix did not work.

### Kubernetes rejected the switch to Recreate

Next error, same row:

```
spec.strategy.rollingUpdate: Forbidden: may not be specified when strategy `type` is 'Recreate'
```

I never wrote any rolling-update settings. Kubernetes did. My Stage 1 Deployment had no strategy, so Kubernetes filled in the default (RollingUpdate) along with its settings on the live object. When Git then said Recreate, those leftovers were still sitting there, and the two cannot exist together.

- First try: adding `rollingUpdate: null` under `strategy`. Same error.
- What worked: remove the old Deployment and let Flux build a fresh one from Git.
  ```
  kubectl delete deployment audiobookshelf -n audiobookshelf
  flux reconcile kustomization apps
  ```

A brand-new Deployment has no leftovers, so Recreate went in clean and all four PVCs bound.

I am comfortable calling this GitOps. I did not create or configure anything by hand. I cleared a stale object out of the way, and everything that exists afterwards came from Git. It was also safe at that moment, because the Stage 1 pod had no storage to lose.

Lesson: Kubernetes fills in defaults I never wrote, and they can come back to bite when I change my mind. If a single-replica app is ever going to get a volume, set `strategy: Recreate` from the first commit.

### The wrong kind of DNS record

For the public hostname I started adding an A record pointing at my node's private address, because that is how I did Grafana and Linkding in the Traefik module. That recipe is for local-only access. A private address cannot be reached from outside the house.

I have two kinds of records and they do different jobs:

| | Local only | Public |
|---|---|---|
| My examples | grafana, lds | links, home, mealie, me, ntfy, abs |
| Type | A | Tunnel |
| Points at | the node's private address | the homelab-kubecraft tunnel |
| Proxy status | DNS only | Proxied |
| Works from | home network | anywhere |

Before the right record existed, the browser said:

```
DNS_PROBE_FINISHED_NXDOMAIN
```

NXDOMAIN means non-existent domain. The phone book had no entry, so the request never reached Cloudflare, the tunnel, or the app. Everything I had built was fine. One `cloudflared tunnel route dns` command later, it loaded.

Lesson: going public takes two things. The tunnel rule tells the tunnel where to send visitors. The DNS record tells the internet the name exists.

## Decisions

- **Port in a ConfigMap.** The exercise required it, and it keeps settings apart from the workload.
- **Pinned image tag `2.37.1`.** Git should describe exactly what is running.
- **PVCs named `abs-...`.** So `abs-config` (a volume holding the database) is not confused with `audiobookshelf-config` (the ConfigMap holding settings).
- **Sizes: 1Gi config, 2Gi metadata, 15Gi audiobooks, 15Gi podcasts.**
- **No node pinning.** The exercise does not ask for it. The pod landed on `biggie-smalls`, and with local-path storage that is where the data lives now. The volume is a real folder on that node's disk, so the pod cannot hop to another node. If that node is off, the app is down.
- **`strategy: Recreate`.** One pod, ReadWriteOnce volumes.
- **Init container for permissions.** The volumes were filled as root before I switched users.
- **Hostname `abs.projectpattie.com`.** One level under the domain, same as my other public apps.
- **App login before public exposure.** The root account was created over the port-forward, before any public route existed.

## Health check

When I want to know it is all still fine:

```
flux get kustomizations
kubectl get pods -n audiobookshelf -o wide
kubectl get pvc -n audiobookshelf
kubectl exec -n audiobookshelf deploy/audiobookshelf -- whoami
kubectl logs -n audiobookshelf deploy/audiobookshelf --tail=20
kubectl get pods -n cloudflared
```

Good looks like: `apps` is `Ready True`, one pod `1/1 Running`, four PVCs `Bound`, `whoami` says `node`, the logs end with `Listening on port :3005`, and the cloudflared pods are `Running`.

If the public URL fails, the error says which link is broken:

| What the browser says | Where to look |
|---|---|
| Site can't be reached / NXDOMAIN | The DNS record is missing |
| Error 1033 | The tunnel is not connected |
| 404 | The tunnel did not match the hostname. Typo in the rule. |
| 502 | The tunnel could not reach the Service. Wrong address or port in the rule. |

## Commits

| Commit | Message |
|---|---|
| `add69d6` | Add: audiobookshelf capstone stage 1 |
| `831c439` | Add: audiobookshelf capstone stage 2 storage |
| `ff2c4e0` | Fix: typo in audiobookshelf deployment apiVersion |
| `de92103` | Fix: clear rollingUpdate when switching audiobookshelf to Recreate |
| `67aaecb` | Add: run audiobookshelf as non-root node user |
| (next) | Add: abs.projectpattie.com route to cloudflared tunnel |

## Command cheat sheet

| Command | What it does |
|---|---|
| `git pull` | Get the latest repo before starting |
| `mkdir -p <path>` | Make folders, including missing parents |
| `kubectl kustomize <folder>` | Preview the final YAML without applying it |
| `git status` / `add` / `commit -m` / `push` | Check, pick, save, send |
| `git log --oneline -3` | Last three commits, one line each |
| `flux get kustomizations` | Did Flux apply my commit, and if not, why |
| `flux get kustomizations --watch` | Same, live |
| `flux reconcile kustomization apps --with-source` | Fetch from GitHub and apply right now |
| `kubectl get pods -n <ns>` | Is the pod running |
| `kubectl get pods -n <ns> -o wide` | Same, plus which node |
| `kubectl get pvc -n <ns>` | Are the volumes Bound |
| `kubectl logs -n <ns> deploy/<name> --tail=20` | Last 20 lines the app printed |
| `kubectl port-forward -n <ns> svc/<name> 3005:3005` | Private line from my Mac to a Service |
| `kubectl exec -n <ns> deploy/<name> -- whoami` | Run a command inside the container |
| `kubectl delete pod -n <ns> -l app=<label>` | Delete a pod by label. The Deployment replaces it. |
| `kubectl rollout restart deployment -n <ns>` | Replace pods so they reload their config |
| `cloudflared tunnel route dns <tunnel> <hostname>` | Create the DNS record for a tunnel hostname |
| `head -1 <file>` | First line of a file |
| `cat <file>` | Whole file |

## How I explain this out loud

**What did you build?**
I deployed Audiobookshelf to my k3s cluster entirely through GitOps with Flux. Its port comes from a ConfigMap, it has four persistent volumes, it runs as a non-root user, and it is published through a Cloudflare tunnel.

**Why a ConfigMap for the port?**
Configuration should be separate from the workload. I can change a setting without editing the Deployment, and reuse the same Deployment across environments.

**How do you know the storage works?**
I uploaded a file, deleted the pod, and the replacement came back with my login and my library intact.

**Why non-root?**
If the app is compromised, the attacker gets an ordinary user and not root. I set the user and group, block privilege escalation, and have Kubernetes refuse to start the container as root.

**Tell me about something that broke.**
Switching a running Deployment to the Recreate strategy was rejected, because Kubernetes had filled in rolling-update defaults on the live object that I never wrote. I read the Flux error, understood where the leftover fields came from, removed the stale Deployment, and let Flux recreate it from Git.

**How does traffic get from the internet to the pod?**
DNS points the hostname at Cloudflare, Cloudflare sends it down the tunnel, the tunnel forwards it to the ClusterIP Service on 3005, and the Service finds the pod by its label.

## Things to know about this setup

- The data is tied to one node because of local-path storage.
- There is no backup of these volumes yet.
- The DNS record was created by hand with the `cloudflared` CLI. It is the one step that is not in Git.

## Links

- Audiobookshelf Docker install docs: https://audiobookshelf.org/docs/documentation/install/docker
- Audiobookshelf releases: https://github.com/advplyr/audiobookshelf/releases
- Course: KubeCraft, GitOps Deployment Masterclass, capstone exercise
