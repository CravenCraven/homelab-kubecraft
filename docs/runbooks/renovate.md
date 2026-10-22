# Automated image updates with Renovate

This is my runbook for the KubeCraft "Automated image updates" module. I set up Renovate as a CronJob in my k3s cluster. Every hour it reads my repo, checks every container image I use against its registry, and opens a pull request when there is a newer version. I review the PR, merge it, and Flux deploys it.

I wrote this so I can rebuild it from scratch, and so I can explain every piece without looking anything up.

## What the module asked for

- Run Renovate inside the cluster on a schedule
- Give it a GitHub token, stored as an encrypted Secret
- Point it at my repo
- Add a `renovate.json` to the repo so it knows to read Kubernetes files
- See it open pull requests for image updates

## The big picture

```
  I push manifests          Renovate (CronJob, every hour)
        │                          │
        ▼                          ▼
  ┌──────────┐   reads     ┌──────────────┐  checks    ┌─────────────┐
  │  GitHub  │◄────────────│   Renovate   │───────────►│ Docker Hub, │
  │   repo   │             │     pod      │            │ ghcr.io ... │
  └──────────┘             └──────────────┘            └─────────────┘
        ▲                          │
        │   opens a PR:            │  "cloudflared → v2026.10.3"
        └──────────────────────────┘
        │
    I review + merge
        │
        ▼
     Flux sees the change → the cluster updates
```

The way I think about it: Renovate is an assistant who checks the store every hour for newer versions of everything on my shopping list. It never buys anything on its own. It leaves me a note (a pull request), and I decide.

## Where everything lives

Repo: `homelab-kubecraft`

```
homelab-kubecraft/
├── renovate.json                              ← tells Renovate what to scan (repo root)
├── clusters/dev/
│   ├── apps.yaml
│   ├── auth.yaml
│   ├── monitoring.yaml
│   └── infrastructure.yaml                    ← NEW: Flux entry for infrastructure/controllers
└── infrastructure/
    ├── auth/
    ├── monitoring/
    └── controllers/                           ← NEW
        ├── base/
        │   ├── kustomization.yaml             lists folders: renovate
        │   └── renovate/
        │       ├── namespace.yaml
        │       ├── configmap.yaml
        │       ├── cronjob.yaml
        │       └── kustomization.yaml         lists the three files above
        └── staging/
            ├── kustomization.yaml             lists folders: renovate
            └── renovate/
                ├── renovate-container-env.yaml   the SOPS-encrypted token
                └── kustomization.yaml            ../../base/renovate + the Secret
```

### Why it is in `infrastructure/` and not `apps/`

- `apps/` is for things people use in a browser: Linkding, Audiobookshelf, n8n.
- `infrastructure/` is for tools that run or look after the platform: monitoring, auth, and now Renovate.
- `controllers/` means something that runs and does work (Renovate opens PRs, Prometheus scrapes metrics). `configs/` would be settings for those tools.

Nobody visits Renovate. It is a worker for the platform.

### Why the Secret is in `staging/` and not `base/`

- `base/` is the recipe. It is the same for any cluster I would ever build: make a namespace, make a CronJob that runs Renovate.
- `staging/` is this cluster's details. The token belongs to this setup, and it is encrypted with this cluster's age key. A second cluster would need its own token and its own key.

Same rule as my n8n Secret, which lives in `apps/staging/n8n/`.

### How Flux finds everything

Flux follows a trail of `kustomization.yaml` files. A file does not have to be in any special place, it just has to be listed.

```
clusters/dev/infrastructure.yaml
   └─► infrastructure/controllers/staging/kustomization.yaml       "go into renovate/"
          └─► staging/renovate/kustomization.yaml
                 ├─► ../../base/renovate                           the recipe
                 │      └─► namespace.yaml, configmap.yaml, cronjob.yaml
                 └─► renovate-container-env.yaml                   the keys
```

`../../base/renovate` is directions counted from where the file lives, not from where my terminal is. From `staging/renovate/`: step out to `staging/`, step out to `controllers/`, then into `base/renovate/`.

## How the pieces fit together

| Piece | What it is in plain words |
|---|---|
| CronJob | An alarm clock. Every hour it creates a Job. |
| Job | One run of the task. It creates a Pod. |
| Pod | Runs the Renovate container. Renovate does its check, opens PRs, and exits. |
| Secret | Renovate's ID badge for GitHub (the token). |
| ConfigMap | The plain-text settings that are fine to read on GitHub. |
| `renovate.json` | The instruction sheet in the repo: what to look at. |
| Flux Kustomization | The switchboard entry that tells Flux to deploy this folder. |

```
CronJob   "every hour..."
  └── Job      "...make one run of this..."
        └── Pod     "...using this container"
```

That nesting is why the CronJob file is indented so deep: `jobTemplate` → `spec` → `template` → `spec`, one level per box.

### CronJob vs Deployment

A Deployment's job is to keep a pod alive forever. If Renovate ran as a Deployment, it would finish its check, exit, and Kubernetes would restart it over and over. A CronJob expects the pod to finish and marks it `Completed`.

A Deployment is a night guard who never leaves. A CronJob is a mail carrier: comes by, drops off the mail, leaves until the next round.

## Before I started

- Flux already watching `homelab-kubecraft` through the GitRepository `flux-system-kubecraft` (not `flux-system` like the course video)
- SOPS with age already wired into Flux, with a `decryption` block on each Flux Kustomization
- My age public key in `$AGE_PUBLIC` in my terminal
- The course video watched once all the way through, without building, so I could see the whole chain before typing anything

## Step 1: the GitHub token

I used a **fine-grained** token, not a classic one like the video.

- A classic token with the `repo` scope is a master key to every repo on my account: this one, my Brasil repo, my website, all of them.
- A fine-grained token is one key for one door. If it leaked, the damage stays inside `homelab-kubecraft`.

The bot runs every hour on its own with nobody watching, so limiting what it can touch is worth two extra minutes. Renovate cannot tell which kind of token it gets; nothing else in the module changes.

On github.com:

1. Profile picture → **Settings**
2. **Developer settings** (bottom of the left sidebar)
3. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
4. Name: `renovate-homelab-kubecraft`
5. Expiration: 90 days
6. Repository access: **Only select repositories** → `homelab-kubecraft`
7. Repository permissions (everything else stays No access):

| Permission | Access | Why Renovate needs it |
|---|---|---|
| Contents | Read and write | read my manifests, push its branches |
| Pull requests | Read and write | open the update PRs |
| Issues | Read and write | its Dependency Dashboard lives in an issue |
| Commit statuses | Read and write | mark its own commits |
| Workflows | Read and write | allowed to touch `.github/workflows` if I add any |
| Dependabot alerts | Read-only | see GitHub's security warnings |
| Metadata | Read-only | GitHub turns this on automatically |

Renovate's docs also list Members (read-only), but that is an Organization permission and my repo is on my personal account, so I skipped it.

8. **Generate token**, then straight into my password manager. GitHub only shows it once.

The permission list came from Renovate's GitHub platform docs, not the video. Videos age. Docs win.

![Fine-grained token settings](images/renovate-token.png)

## Step 2: the folders

```bash
cd ~/Developer/homelab-kubecraft
mkdir -p infrastructure/controllers/base/renovate
mkdir -p infrastructure/controllers/staging/renovate
tree infrastructure/controllers
```

- `mkdir -p` makes the folder and any missing parent folders, and does not complain if it already exists.
- `mkdir` can take several paths on one line separated by a space. I split it into two lines because one long line was easy to misread.

## Step 3: the encrypted Secret

I changed three things from the video here.

### 3a. Hold the token in a hidden variable

```bash
read -s RENOVATE_TOKEN
echo ${#RENOVATE_TOKEN}
```

- `read` waits for me to type something and stores it in a variable.
- `-s` is silent, nothing shows on screen. It looks like a password prompt with no message. Paste the token, press Enter.
- `${#RENOVATE_TOKEN}` prints the **length** of the variable, not its value. A fine-grained token is around 90 characters. If it prints `0`, the variable is empty.

**Why not paste the token into the command like the video:** every command I type is saved to `~/.bash_history` on my Mac. If the token is inside the command, it sits in that file in plain text forever. `read -s` keeps it out of the command, so it never gets written down.

### 3b. Create the Secret file straight into staging

```bash
kubectl create secret generic renovate-container-env \
  --namespace=renovate \
  --from-literal=RENOVATE_TOKEN="$RENOVATE_TOKEN" \
  --dry-run=client \
  -o yaml > infrastructure/controllers/staging/renovate/renovate-container-env.yaml
```

| Part | What it does |
|---|---|
| `create secret generic` | a Secret of plain key/value pairs |
| `renovate-container-env` | the Secret's name. Must match `secretRef: name:` in the CronJob |
| `--namespace=renovate` | **added.** Stamps the namespace into the file. A pod can only read Secrets in its own namespace |
| `--from-literal=RENOVATE_TOKEN=...` | the key is `RENOVATE_TOKEN`, the exact environment variable name Renovate looks for |
| `--dry-run=client` | build it on my Mac, send nothing to the cluster |
| `-o yaml` | print it as YAML |
| `> path` | **changed.** Save it straight into `staging/renovate/`, not the repo root |

The video saved it in the repo root. Between creating it and encrypting it, that file holds the token in plain text inside the repo folder. One `git add .` at the wrong moment and the token is on GitHub. So: create, encrypt right away, check.

### 3c. Encrypt it

```bash
sops --age=$AGE_PUBLIC \
  --encrypt --encrypted-regex '^(data|stringData)$' \
  --in-place infrastructure/controllers/staging/renovate/renovate-container-env.yaml
```

- `--age=$AGE_PUBLIC` locks it with my cluster's public key. Only the cluster has the private key.
- `--encrypted-regex '^(data|stringData)$'` scrambles only the `data:` section. `name` and `namespace` stay readable so Flux and Kubernetes know what it is.
- `--in-place` overwrites the file with the encrypted version, so no plain copy is left behind.

### 3d. Clean up and check

```bash
unset RENOVATE_TOKEN
cat infrastructure/controllers/staging/renovate/renovate-container-env.yaml
grep recipient apps/staging/n8n/secret.yaml
```

- `unset` deletes the variable from memory.
- In the `cat` output, `RENOVATE_TOKEN:` must be followed by a long `ENC[AES256_GCM,data:...` value. An empty token would show `data:,` with nothing after it.
- The `recipient: age1...` line must be identical to the one in a Secret I know works (my n8n one). Same key means Flux can decrypt it.

What a good file looks like (the scrambled parts shortened):

```yaml
apiVersion: v1
data:
    RENOVATE_TOKEN: ENC[AES256_GCM,data:QUK1OGx...,type:str]
kind: Secret
metadata:
    name: renovate-container-env
    namespace: renovate
sops:
    age:
        - recipient: age1...
    encrypted_regex: ^(data|stringData)$
```

## Step 4: the base files

### namespace.yaml

`infrastructure/controllers/base/renovate/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: renovate
```

The name has to match the `namespace: renovate` already stamped into the Secret, or the Secret would land in a room that does not exist.

### configmap.yaml

`infrastructure/controllers/base/renovate/configmap.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: renovate-configmap
  namespace: renovate
data:
  RENOVATE_AUTODISCOVER: "false"
  RENOVATE_GIT_AUTHOR: "Renovate Bot <bot@renovateapp.com>"
  RENOVATE_PLATFORM: "github"
```

| Setting | What it means |
|---|---|
| `RENOVATE_AUTODISCOVER: "false"` | only scan the repo I name in the CronJob's `args`, not every repo the token can reach |
| `RENOVATE_GIT_AUTHOR` | the name stamped on Renovate's commits, so bot commits stand out from mine in `git log` |
| `RENOVATE_PLATFORM: "github"` | which Git host to talk to (Renovate also works with GitLab, Forgejo and others) |

- ConfigMap vs Secret: ConfigMap is for settings that are fine to read in plain text on GitHub. The Secret is for the one value that is not.
- Every value under a ConfigMap's `data:` must be text. A bare `false` is a true/false value to YAML, and Kubernetes rejects it. Quote everything.
- `name` and `namespace` go **under** `metadata:`, two spaces in. `kind` is what the thing is, `name` is which one.

### cronjob.yaml

`infrastructure/controllers/base/renovate/cronjob.yaml`

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: renovate
  namespace: renovate
spec:
  schedule: "@hourly"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: renovate
              image: renovate/renovate:44.175.5
              args:
                - CravenCraven/homelab-kubecraft
              envFrom:
                - secretRef:
                    name: renovate-container-env
                - configMapRef:
                    name: renovate-configmap
          restartPolicy: Never
```

| Line | Why |
|---|---|
| `schedule: "@hourly"` | run at the top of every hour |
| `concurrencyPolicy: Forbid` | if a run is still going, skip the next one. Two Renovates at once would both try to create the same branch and PR and collide. Losing one run is fine, another comes in an hour |
| `image: renovate/renovate:44.175.5` | **changed from `:latest`.** Pinned to a real version I checked on Docker Hub. It is built for amd64 and arm64, so it runs on any of my three nodes. Pinning also means Renovate can open a PR to update itself |
| `args: - CravenCraven/homelab-kubecraft` | **changed from the teacher's repo.** Short `owner/repo` form, not a URL, no slash at the end |
| `envFrom` | load every key in the Secret and ConfigMap as environment variables. This is how `RENOVATE_TOKEN` and the three settings get into the container |
| `secretRef: name: renovate-container-env` | the **Kubernetes** name of the Secret, not the GitHub token's label. The cluster has never heard of `renovate-homelab-kubecraft` |
| `configMapRef: name: renovate-configmap` | the ConfigMap's name |
| `restartPolicy: Never` | Renovate finishes and exits. Restarting it would just run it again for no reason |

`restartPolicy` lines up with `containers`, not inside it, because it is a rule for the whole Pod.

How I keep the two names straight: each `Ref` points at the file of the same type. `secretRef` → look in the Secret file. `configMapRef` → look in the ConfigMap file.

### base/renovate/kustomization.yaml

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - configmap.yaml
  - cronjob.yaml
```

A kustomization never lists itself. It is the table of contents, not a chapter.

### base/kustomization.yaml

`infrastructure/controllers/base/kustomization.yaml`, one folder up, next to `renovate/`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - renovate
```

This one lists a **folder**. Kustomize walks into it and reads that folder's own kustomization. When I add another controller later, it goes on this list.

### Check base builds

```bash
k kustomize infrastructure/controllers/base
```

This follows the trail and prints the final YAML without sending anything to the cluster. You should see the Namespace, ConfigMap and CronJob separated by `---`.

## Step 5: the staging files

### staging/renovate/kustomization.yaml

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base/renovate
  - renovate-container-env.yaml
```

The recipe plus this cluster's keys. Compare my n8n one, which also has an Ingress, an Authentik Service and a Middleware. Renovate needs none of those because nobody visits it in a browser. I only added what the workload needed.

### staging/kustomization.yaml

`infrastructure/controllers/staging/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - renovate
```

### Check staging builds

```bash
k kustomize infrastructure/controllers/staging
```

Four things should print: Namespace, ConfigMap, CronJob, and the Secret still showing `ENC[...]`. Flux does the decrypting later, inside the cluster.

## Step 6: the Flux entry

`clusters/dev/` is the switchboard. Each file there points Flux at one folder:

```
clusters/dev/apps.yaml            ──► apps/staging
clusters/dev/auth.yaml            ──► infrastructure/auth/...
clusters/dev/monitoring.yaml      ──► infrastructure/monitoring/...
clusters/dev/infrastructure.yaml  ──► infrastructure/controllers/staging   NEW
```

I copied `monitoring.yaml` and changed two lines. When I copy a file, every line is guilty until proven innocent.

```bash
cp clusters/dev/monitoring.yaml clusters/dev/infrastructure.yaml
vim clusters/dev/infrastructure.yaml
```

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: infrastructure-controllers
  namespace: flux-system
spec:
  interval: 1m0s
  # dependsOn:
  #   - name: infra-configs
  retryInterval: 1m
  timeout: 5m
  sourceRef:
    kind: GitRepository
    name: flux-system-kubecraft
  path: ./infrastructure/controllers/staging
  prune: true
  decryption:
    provider: sops
    secretRef:
      name: sops-age
```

| Line | Changed or kept |
|---|---|
| `name: infrastructure-controllers` | changed, so I can tell it apart in `flux get kustomizations` |
| `path: ./infrastructure/controllers/staging` | changed |
| `sourceRef: name: flux-system-kubecraft` | kept. My repo's GitRepository, not the video's `flux-system` |
| `decryption` with `sops-age` | kept. This folder has an encrypted Secret. Without it, Flux would try to apply the scrambled Secret and Kubernetes would reject it |
| `prune: true` | kept. Delete it from Git and Flux deletes it from the cluster |
| the commented `dependsOn` | came over from the copy. Lines starting with `#` are ignored |

## Step 7: push

```bash
git status
grep ENC infrastructure/controllers/staging/renovate/renovate-container-env.yaml
git add clusters/dev/infrastructure.yaml infrastructure/controllers
git status
git commit -m "Add Renovate CronJob for automated image updates"
git push
```

- `git status` first, to make sure there is no stray plain-text Secret or extra file in the repo root.
- `grep ENC` to prove the Secret is encrypted before it leaves my Mac.
- `git add` with exact paths, not `git add .`, so nothing extra sneaks in. My untracked `apps/base/uptime-kuma/` stayed out of this commit.

## Step 8: make sure Flux applied it

```bash
flux reconcile source git flux-system-kubecraft
flux get kustomizations
k get cronjob,secret,configmap -n renovate
```

- `flux reconcile source git ...` tells Flux to check GitHub now instead of waiting.
- I want `infrastructure-controllers` in the list with `READY True`.
- `k get cronjob,secret,configmap` is one command, three kinds of things.

Good output:

```
NAME                     SCHEDULE   TIMEZONE   SUSPEND   ACTIVE   LAST SCHEDULE   AGE
cronjob.batch/renovate   @hourly    <none>     False     0        <none>          54s

NAME                            TYPE     DATA   AGE
secret/renovate-container-env   Opaque   1      54s

NAME                           DATA   AGE
configmap/kube-root-ca.crt     1      54s
configmap/renovate-configmap   3      54s
```

- `DATA 1` on the Secret is the one key, `RENOVATE_TOKEN`. `DATA 3` on the ConfigMap is the three settings.
- `kube-root-ca.crt` is one I did not make. Kubernetes drops it into every namespace so pods can trust the cluster's own certificate.

This is where my first push got stuck. See "What broke" below.

## Step 9: run it by hand and onboard

I did not want to wait for the top of the hour.

```bash
k create job --from=cronjob/renovate renovate-manual -n renovate
k get pods -n renovate -w
k describe pod -n renovate <pod-name>
k logs -n renovate job/renovate-manual
```

| Command | What it does |
|---|---|
| `create job --from=cronjob/renovate` | make one Job right now, using the CronJob as the recipe |
| `renovate-manual` | a name for this one-off run |
| `get pods -w` | watch, prints a new line each time the status changes. Ctrl+C to stop |
| `describe pod` | everything about one pod, with **Events** at the bottom |
| `logs job/...` | what Renovate printed. Add `-f` to follow it live |

The pod sat in `ContainerCreating` for about a minute. `describe` showed why: it was pulling a 428 MB image onto `biggie-smalls`, my arm64 node.

```
Normal  Pulling  ...  Pulling image "renovate/renovate:44.175.5"
Normal  Pulled   ...  Successfully pulled image ... in 40.316s
Normal  Started  ...  Container started
```

Then it went `Completed`. `0/1 READY` is normal for a finished Job.

The log, read like a story:

```
Renovate started                      the pod woke up, version 44.175.5
Repository started                    the token worked, it got into my repo
Branch created  renovate/configure    its own branch, not main
  "onboarding": true                  the "can I work here?" step
Dependency extraction complete
  "fileCount": 0, "depCount": 0       it found nothing to update yet
Onboarding PR created  #1             the note it left me
"result": "onboarding", exitCode 0    finished cleanly, waiting on me
```

### The onboarding PR

On the first run, if a repo has no `renovate.json`, Renovate opens a PR called **Configure Renovate** that adds one. It is a new employee on day one handing me "here is how I plan to work, OK?" before touching anything. Merging it is my sign-off.

1. github.com → `homelab-kubecraft` → **Pull requests**
2. Click the **title** of the PR. The list page has no merge button
3. **Files changed** to read it
4. Back on **Conversation**, scroll to the bottom → **Merge pull request** → **Confirm merge**
5. The badge next to the title turns purple: **Merged**

```bash
git pull
cat renovate.json
```

`git pull` brings the merge down to my Mac. Skip it and my next push gets rejected, because GitHub has a commit I do not.

The starter file only had the schema line:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json"
}
```

The PR page said my dependencies were already up to date. That was not true. It scanned zero files.

![Onboarding PR](images/renovate-onboarding-pr.png)

## Step 10: tell Renovate to read Kubernetes files

Renovate's Kubernetes manager matches no files by default, because there is no standard naming for Kubernetes YAML. That is the `fileCount: 0`.

The video used `fileMatch`. Renovate's current docs (v44) renamed it to `managerFilePatterns`, and the pattern is now wrapped in slashes.

| Video | Current docs |
|---|---|
| `"fileMatch": ["\\.yaml$"]` | `"managerFilePatterns": ["/\\.yaml$/"]` |

`renovate.json`:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "kubernetes": {
    "managerFilePatterns": ["/\\.yaml$/"]
  }
}
```

Reading `/\\.yaml$/`:

- `/ ... /` what is between the slashes is a search pattern
- `\\.` a literal dot (a plain `.` would mean any character)
- `yaml` the letters yaml
- `$` at the very end of the file name

Together: any file whose name ends in `.yaml`.

Adding it in vim: `2G` to jump to line 2, `A` to type at the end of the line, add a comma, Enter, type the block, Esc, `:wq`. JSON needs a comma between items.

```bash
python3 -m json.tool renovate.json
git add renovate.json
git commit -m "Add Kubernetes file pattern to Renovate config"
git push
k create job --from=cronjob/renovate renovate-manual-2 -n renovate
k logs -n renovate -f job/renovate-manual-2
```

- `python3 -m json.tool` prints the file back if it is valid JSON, or points at the broken line (usually a missing comma).
- The second Job needs a new name because `renovate-manual` already exists.

This time:

```
PR created  #2  update timothystewart6/littlelink-server docker digest to 0dc5c21
PR created  #3  update cloudflare/cloudflared docker tag to v2026.10.3
Deleting orphan branch renovate/configure     cleaning up its onboarding branch
"status": "onboarded"                         runs every hour on its own from now on
```

Only two PRs because Renovate's default limit is two new PRs per hour, so it does not bury me all at once. More arrive on the next runs.

### Two kinds of updates

- **Tag update** (cloudflared): the version label changes to `2026.10.3`. A new release.
- **Digest update** (littlelink): the label stays the same but the image behind it changed. A digest is the image's fingerprint (`sha256:...`). Same name, new contents, like a book reprinted with fixes but the same edition number.

## Step 11: merge an update and watch it deploy

The PR description shows exactly what changes:

| Package | Update | Change |
|---|---|---|
| timothystewart6/littlelink-server | digest | `5659ec2` → `0dc5c21` |

It also says **Automerge: Disabled by config**. Renovate never deploys anything on its own. A human approves every change.

Before merging, check what is running now:

```bash
k get deploy littlelink -n littlelink -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
```

- `-o jsonpath=...` prints one field instead of the whole object. Here, the first container's image.
- `; echo` adds a line break so the prompt does not run into the output.

Merge the PR on GitHub, then:

```bash
flux reconcile kustomization apps --with-source
k get deploy littlelink -n littlelink -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
k get pods -n littlelink
```

- `--with-source` fetches the latest from GitHub first.
- The fetch line should show the merge commit's hash. If the revision is the same as before, the merge did not happen yet.

Good output:

```
timothystewart6/littlelink-server@sha256:0dc5c21e63e1...
NAME                          READY   STATUS    RESTARTS   AGE
littlelink-85f567f55c-k5gxb   1/1     Running   0          65s
littlelink-85f567f55c-n6snj   1/1     Running   0          42s
```

The two pods are about 20 seconds apart. Kubernetes replaced them one at a time, so littlelink never went down.

The full loop: Renovate opens a PR → I review and merge → Flux applies it → Kubernetes rolls the pods.

![Merged littlelink PR](images/renovate-littlelink-merged.png)

### Rollback

The merged PR page has a **Revert** button. It opens a PR that undoes the change. Merge it and Flux rolls back to the old image. Revert the commit in Git and let Flux reconcile.

## What broke

### A broken `apps.yaml` blocked everything

After the first push, `infrastructure-controllers` never showed up.

```
flux-system-kubecraft  main@sha1:9d6492fc  False  False
  failed to decode Kubernetes YAML from .../clusters/dev/apps.yaml:
  MalformedYAMLError: yaml: line 10: mapping values are not allowed in this context
```

This was not from anything I had just built. `apps.yaml` had broken indentation from when I added `dependsOn: auth-controllers` during the Authentik work.

```
 6  spec:
 7   dependsOn:              1 space   wrong
 8     - name: auth-...      3 spaces  wrong
 9   interval: 10m0s         1 space   wrong
10    retryInterval: 1m      2 spaces  right
```

Why it mattered: `flux-system-kubecraft` reads every file in `clusters/dev/` as one batch. If one file cannot be read, none of them get applied. So my new `infrastructure.yaml` sat there waiting. The `apps` and `auth-controllers` Kustomizations still updated, because they already existed and read their own folders. Only the parent that creates new entries was stuck.

Why the error pointed at line 10 when lines 7 to 9 were wrong: YAML reads top to bottom. At line 7 it decided things inside `spec:` are one space in. Line 10 showed up at two spaces, did not fit, and that is where it complained. The error line is where YAML noticed the problem, not always where it started.

Fix: `cat -n clusters/dev/apps.yaml` to see line numbers, then in vim for lines 7, 8 and 9: `7G`, `I`, one space, Esc. Then:

```bash
git add clusters/dev/apps.yaml
git commit -m "Fix indentation in apps.yaml"
git push
flux reconcile kustomization flux-system-kubecraft --with-source
flux get kustomizations
```

Everything went `True`, including the new `infrastructure-controllers`.

### A missing closing quote in the ConfigMap

```
error: accumulating resources: ... accumulating resources from 'configmap.yaml':
MalformedYAMLError: yaml: line 9: found unexpected end of stream in File: configmap.yaml
```

How to read a long error: start at the end. The earlier parts are just the trail Kustomize followed. The end says `configmap.yaml`, line 9. "Unexpected end of stream" means the file ended while YAML was still waiting for something to close, almost always a missing `"`.

```bash
cat -n infrastructure/controllers/base/renovate/configmap.yaml
```

Line 9 was `RENOVATE_PLATFORM: "github` with no closing quote. In vim: `G`, `A`, `"`, Esc, `:wq`.

### The token variable was empty the first time

`echo ${#RENOVATE_TOKEN}` printed `0`. The `read` had not stored anything. Usual causes: running `read -s` without the variable name, running it in a different terminal tab (each tab has its own memory), or pressing Enter before pasting. I ran it again in the same window I used for the next steps and it worked.

### Typing a path with nothing in front of it

```
-bash: infrastructure/controllers/staging/renovate: No such file or directory
```

Two problems at once: the folder did not exist yet because `mkdir` had not run, and a line that is only a path makes bash try to **run** it like a program. To go into a folder you need `cd` in front.

### Vim could not save: E212

```
E212: Can't open file for writing
```

Vim could not find the folder to save into. A short path like `infrastructure/...` only works if my terminal is sitting in the repo. Fix in vim: `:w ~/Developer/homelab-kubecraft/infrastructure/...` with the full path. Habit: `pwd` before using a short path.

### Paste problems

- Pasting from a web page turned `<bot@renovateapp.com>` into a markdown link with `mailto:`. I typed that line by hand instead.
- A stray paste left `apiVersion: v1` and `sops:` at the top of the ConfigMap, and an old attempt left four extra lines at the top of a kustomization. Fixed in vim with `gg` then `dd` (or `4dd` for four lines).
- Habit: after pasting into a YAML file, `cat` it and look.

Quick ways to check a file from the terminal:

```bash
grep -c "^  " infrastructure/controllers/base/renovate/configmap.yaml
grep renovateapp infrastructure/controllers/base/renovate/configmap.yaml
```

- `grep -c` counts matching lines. `"^  "` matches lines that start with two spaces. The ConfigMap should give 5.
- `grep` needs **what to look for** and **where to look**. Leave out the file and it sits there waiting for input. Ctrl+C gets out.

### A typo in a folder name

`k kustomize infrastructure/controllers/stagin` failed with `no such file or directory`. Missing the `g`. Tab completion prevents this: type a few letters and press Tab.

### I merged the risky PR first

I meant to merge littlelink first, because it is a simple link page and if it breaks nothing else cares. I merged cloudflared instead. Every public app goes through that tunnel, so a bad cloudflared release would take all of them down at once.

It worked fine:

```
cloudflared-6db97f5ccb-r84gj   1/1   Running   0   57s
cloudflared-6db97f5ccb-wg9mw   1/1   Running   0   62s
```

Two new pods, swapped one at a time, so the tunnel never fully went down. I checked a public site from my phone off WiFi.

Part of the confusion was the GitHub UI. The PR **list** page has no merge button. You have to click into the PR. And `git log --oneline -3` after `git pull` is a faster way to see which PRs actually merged than hunting for purple badges.

## Decisions

| Decision | Why |
|---|---|
| Followed the teacher's layout, `infrastructure/controllers/{base,staging}/renovate/` | it drops straight into my existing `infrastructure/` tree, and diverging from the video has confused me before |
| Fine-grained token, one repo | least privilege. A bot that runs unattended every hour should only reach what it needs |
| `read -s` for the token | keeps it out of shell history |
| Secret created straight in `staging/renovate/` with `--namespace` | no plain-text token in the repo root, and the Secret lands in the same namespace as the CronJob |
| Pinned `renovate/renovate:44.175.5` instead of `:latest` | known version, works on arm64 and amd64, and Renovate can update itself through a PR |
| `managerFilePatterns` instead of the video's `fileMatch` | the video is out of date. The current docs renamed it |
| Automerge left off | I review every change before it deploys |
| Merge low-risk updates first, hold anything that touches every app or a database | if it breaks, the blast radius is small |

## Health check

```bash
flux get kustomizations
k get cronjob -n renovate
k get jobs -n renovate
k logs -n renovate job/<latest-job>
```

- `infrastructure-controllers` should be `READY True`.
- The CronJob's `LAST SCHEDULE` should be under an hour ago.
- The latest Job should be `Complete`, and its log should end in `"result": "done"` with `exitCode 0`.
- On GitHub, new PRs should keep showing up as images get new releases.

## Commits

| Commit | What |
|---|---|
| `e76a0b1` | Add Renovate CronJob for automated image updates |
| `8bdf888` | Fix indentation in apps.yaml |
| `52aa640` | Merge of PR #1, Configure Renovate (adds `renovate.json`) |
| `f1aebfd` | Add Kubernetes file pattern to Renovate config |
| `075252c` | Merge of PR #3, cloudflared to `2026.10.3` |
| `ee6c2a8` | Merge of PR #2, littlelink digest to `0dc5c21` |

## Command cheat sheet

| Command | What it does | When I use it |
|---|---|---|
| `mkdir -p a/b/c` | make folders, including missing parents | setting up a new app's folders |
| `read -s VAR` | store typed input in a variable without showing it | handling a token or password in the terminal |
| `echo ${#VAR}` | print the length of a variable, not its value | checking a secret variable is not empty |
| `unset VAR` | delete a variable from memory | after I am done with a secret |
| `kubectl create secret generic ... --dry-run=client -o yaml > file` | build a Secret file without touching the cluster | making a Secret to encrypt and commit |
| `sops --age=$AGE_PUBLIC --encrypt --encrypted-regex '^(data\|stringData)$' --in-place file` | encrypt only the data of a Secret file | right after creating a Secret file |
| `grep recipient file` | show the age key a file was locked with | checking a new Secret uses the same key |
| `cat -n file` | print a file with line numbers | an error mentions a line number |
| `grep -c "pattern" file` | count matching lines | quick checks on indentation |
| `k kustomize folder` | build the final YAML locally, no cluster | before every push |
| `cp a b` | copy a file | starting a new Flux entry from an existing one |
| `git add <exact paths>` | stage only what I name | every commit, instead of `git add .` |
| `git pull` | bring GitHub's latest to my Mac | after merging a PR on GitHub |
| `git log --oneline -3` | last three commits, one line each | checking what actually merged |
| `flux reconcile source git <repo>` | make Flux check GitHub now | right after a push |
| `flux reconcile kustomization <name> --with-source` | fetch, then re-apply one Kustomization | after a merge, or after fixing a file |
| `flux get kustomizations` | every Flux Kustomization and whether it is Ready | after every reconcile |
| `k get cronjob,secret,configmap -n renovate` | several kinds of resources in one command | checking a deploy landed |
| `k create job --from=cronjob/renovate <name> -n renovate` | run a CronJob right now | testing without waiting for the schedule |
| `k get pods -n renovate -w` | watch pod status change live | waiting for a pod to start or finish |
| `k describe pod -n renovate <pod>` | full details plus Events | a pod is stuck in ContainerCreating or erroring |
| `k logs -n renovate job/<name>` | the Job's output (`-f` to follow) | seeing what Renovate did |
| `k get deploy <app> -n <ns> -o jsonpath='{...image}'; echo` | print just the running image | before and after an update |
| `python3 -m json.tool file.json` | check a JSON file is valid | after editing `renovate.json` |

## How I explain this out loud

**What did you build?**
"I run Renovate as a CronJob in my k3s cluster. Every hour it reads my GitOps repo, checks every container image against its registry, and opens a pull request when there is a newer version. I review and merge, and Flux deploys it. It closes the GitOps loop, so I am not hand-editing image tags anymore."

**Why a CronJob and not a Deployment?**
"Renovate does its check and exits. A Deployment would keep restarting it. A CronJob expects the pod to finish and runs it again on a schedule. I set `concurrencyPolicy: Forbid` so two runs never collide on the same branch."

**How do you handle the token?**
"Fine-grained token scoped to one repo, following least privilege. I read it into a hidden shell variable so it never lands in my shell history, generate the Secret with a client-side dry run, and encrypt the data section with SOPS and age before it goes anywhere near Git. Flux decrypts it inside the cluster."

**Why does the Secret live in staging and not base?**
"Base holds what every cluster shares. The Secret goes in staging because the token and the encryption key belong to that one cluster."

**Tell me about something that broke.**
"I pushed Renovate and it never deployed. The Flux error pointed at a different file, `apps.yaml`, which had broken indentation from an earlier change. Flux builds that whole folder as one batch, so one bad file blocked my new entry. I found it with `cat -n`, fixed three lines, and everything reconciled. The lesson was that the error line is where YAML noticed the problem, not always where it started."

**How do you roll back a bad update?**
"Revert the merge commit in Git, usually with the Revert button on the PR, and let Flux reconcile. Git is the source of truth, so going back is just another commit."

**Do you auto-merge?**
"Not yet. Every update gets a human review. I merge low-risk ones first and hold anything that every app depends on, like the tunnel, or anything with a database, until I can watch it."

## Things to know about this setup

- The token expires 90 days after I made it. When it does, Renovate quietly stops opening PRs. Renew it on GitHub, then redo Step 3 and push.
- The PRs show as opened by **CravenCraven**, not Renovate Bot, because they are opened with my personal token. `RENOVATE_GIT_AUTHOR` only changes the name on the commits inside the PR.
- Renovate's commits show **Unverified** on GitHub because they are not signed.
- Two new PRs per hour by default.
- Pinning the Renovate image means Renovate will eventually open a PR to update itself.
- Only `homelab-kubecraft` is watched. Adding my Brasil repo means adding it to the token and to the CronJob's `args`.

## Links

- Renovate Kubernetes manager: https://docs.renovatebot.com/modules/manager/kubernetes/
- Renovate on GitHub, token permissions: https://docs.renovatebot.com/modules/platform/github/
- Renovate image tags: https://hub.docker.com/r/renovate/renovate/tags
- Kubernetes CronJob: https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/
