# Runbook: managing Cloudflare DNS with OpenTofu

Everything in my homelab lives in Git and Flux makes the cluster match it. Everything except the DNS records that make my apps public. Every one of those I made by hand with `cloudflared tunnel route dns`, and the only place they were written down was the Cloudflare dashboard.

This runbook is how I moved those records into OpenTofu, encrypted the state, stored it in my own cluster, and pulled the secrets from Vaultwarden so I never paste them. Every command is here in the order I ran it, with the output I got and the mistakes where they happened.

Where I worked: Tupac (my M3 Mac running Fedora Asahi, arm64), editor `nvim`, repo at `~/Developer/homelab-kubecraft`. The cluster is k3s on three nodes (craventhegreat control plane, dell-node and biggie-smalls workers), Flux reconciling `homelab-kubecraft` through `flux-system-kubecraft`. Public apps go out through one Cloudflare tunnel, `homelab-kubecraft`.

IDs in this file are placeholders: `<zone-id>`, `<tunnel-id>`, `<record-id>`. The real ones live in a gitignored file.

---

## Contents

0. What OpenTofu is, in my words
1. The shopping list for each file
2. Before you start
3. Install OpenTofu
4. Cloudflare API token
5. Load the token without saving it anywhere
6. Get the repo ready
7. `.gitignore`
8. `versions.tf` and the first `tofu init`
9. `variables.tf`
10. Look up the zone, the tunnel and every record
11. Decide what OpenTofu manages
12. `terraform.tfvars` and the example file
13. `dns.tf`
14. `imports.tf`
15. Plan, import, prove there's no drift
16. Commit and push
17. Drift drill
18. A namespace for the state, through Flux
19. Encrypt the state
20. Move the state into the cluster
21. Secrets from Vaultwarden with `bw`
22. `tofu-env`: one command per session
23. nvim commands I used
24. Shortcuts
25. Things to look out for
26. Verification commands
27. Rollback
28. Decisions

---

## 0. What OpenTofu is, in my words

Three pieces:

- **The list:** my `.tf` files. "These DNS records should exist, looking exactly like this."
- **The real world:** what actually exists in Cloudflare right now.
- **The notebook:** the state file. "Record `n8n` in my list is Cloudflare record ID `<record-id>`." That's how OpenTofu knows which real thing is which line.

`tofu plan` reads the list, checks the notebook, asks Cloudflare what's really there, and shows me the difference. It changes nothing. `tofu apply` does the same and then makes Cloudflare match the list. Then it stops. It doesn't watch anything.

How it's different from Flux:

| | Flux | OpenTofu |
|---|---|---|
| Where it runs | Inside my cluster, all the time | On my computer, only when I run it |
| What it manages | Things inside Kubernetes | Things outside Kubernetes (Cloudflare DNS) |
| Who starts it | Itself, every few minutes | Me, by typing `tofu apply` |
| Fixes drift? | Yes, on its own | Only tells me when I run `plan` |

That's why I never ran `flux reconcile` for the DNS part. Flux only reads the paths in `clusters/dev/`, and `opentofu/` isn't one of them. Pushing the folder to GitHub just stores the code.

---

## 1. The shopping list for each file

Same method I use for manifests. For each file: what I already knew (my pile), what I had to look up (docs pile), and where the answer came from.

### `.gitignore`

| Me | Docs |
|---|---|
| The repo is public, so state and my IDs can't go in | Which files OpenTofu creates that hold sensitive stuff |
| | Where: GitHub's official template, `github/gitignore`, file `Terraform.gitignore` |

### `versions.tf`

| Me | Docs |
|---|---|
| Pin versions, same reason I pin image tags | The provider's source name: `cloudflare/cloudflare` |
| Match the OpenTofu I have installed (1.11.5) | The newest provider release: 5.29.0, from Cloudflare's GitHub releases |
| | What `~>` means: allows newer minor versions, never the next major |

### `variables.tf`

| Me | Docs |
|---|---|
| Zone ID and tunnel ID shouldn't be typed into the code | Variable block syntax: `description`, `type`, `default` |
| My node IP is 10.0.0.106 | |

### `dns.tf`

| Me | Docs |
|---|---|
| Which hostnames: the list in `apps/base/cloudflared/configmap.yaml` | Resource name in provider v5: `cloudflare_dns_record` (v4 called it `cloudflare_record`) |
| Proxied, TTL auto: from the API listing of my real records | The field is `content`, not `value` |
| | Required fields: `zone_id`, `name`, `type`, `ttl` |
| | Where: Cloudflare provider docs, `dns_record` resource page |

### `imports.tf`

| Me | Docs |
|---|---|
| Don't delete and recreate live records, adopt them | Import ID format: `<zone_id>/<dns_record_id>` |
| | Import blocks can use `for_each` |
| | Where: the Import section at the bottom of the same `dns_record` page |

### `encryption.tf`

| Me | Docs |
|---|---|
| A Kubernetes Secret is only base64, anyone with access can read it | `pbkdf2` key provider + `aes_gcm` method |
| Passphrase from my password manager | Passphrase at least 16 characters |
| | Existing plain-text state needs an `unencrypted` fallback once |
| | `enforced = true` blocks plain text from ever being written |
| | Where: opentofu.org, State and Plan Encryption |

### `backend.tf`

| Me | Docs |
|---|---|
| State stuck on one laptop is a problem | `backend "kubernetes"` arguments: `secret_suffix`, `namespace`, `config_path`, `config_context` |
| My kube context is `homelab` on every machine | Secret name: `tfstate-{workspace}-{secret_suffix}` |
| | Lock is a Kubernetes Lease |
| | Where: opentofu.org, Backends, kubernetes |

---

## 2. Before you start

- A machine that can reach the cluster (`kubectl get nodes` works)
- The repo cloned
- A Cloudflare zone with records already in it
- Vaultwarden running if you want the `bw` part
- `python3` and `curl` (both already on Fedora)

---

## 3. Install OpenTofu

On Fedora it's in the normal repos. The OpenTofu docs page for Fedora says so.

```bash
sudo dnf install opentofu
tofu version
```

```
OpenTofu v1.11.5
on linux_arm64
```

Not the newest release (1.13.9 at the time), but it has everything I needed: import blocks with `for_each`, state encryption, the kubernetes backend. No reason to fight the package manager over it.

---

## 4. Cloudflare API token

The token is what lets OpenTofu talk to Cloudflare. I wanted it as small as possible: DNS on one domain, nothing else.

The profile menu at the top right of the dashboard no longer has **My Profile**. It only shows Appearance, Language, Timezone, Copy account ID and Log out. Cloudflare's docs link straight to the page, so I went there directly:

**https://dash.cloudflare.com/profile/api-tokens/**

1. **Create token**
2. **Token name:** `opentofu-dns`
3. Under **Policies**, click **Edit zone DNS**
4. In the **Edit Zone policy** row: `Include`, `Specific zone`, `projectpattie.com`
5. Open **DNS & Zones** (it shows `1/12`). Only **DNS** is checked, set to **Edit**. Nothing else.
6. **Token expiration:** No expiration
7. **Client IP address filtering:** left blank, because my home IP can change and a filter would lock me out
8. **Continue to summary**
9. Summary showed: name `opentofu-dns`, `projectpattie.com` in my account, `DNS: Edit`, No expiration, All IP addresses allowed
10. **Create Token**, copied it into my password manager. It's only shown once.

New tokens start with `cfut_`. That prefix lets secret scanners spot a leaked token.

---

## 5. Load the token without saving it anywhere

```bash
read -s -p "Token: " CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN && echo
```

What each piece does:

- `read` waits for me to type something and puts it in a variable
- `-s` silent, nothing shows on screen and it doesn't go into shell history
- `-p "Token: "` shows a prompt so I know it's waiting
- `export` makes the variable visible to `tofu` and `curl`
- `echo` just moves to a new line when it's done

Paste is **Ctrl+Shift+V** in the Fedora terminal. Nothing shows when you paste. That's normal.

Check it loaded without printing it:

```bash
echo ${#CLOUDFLARE_API_TOKEN}
```

`${#...}` prints the length, not the value. Mine is `53`. If it says `0`, the paste didn't land.

Check the token works:

```bash
curl -s https://api.cloudflare.com/client/v4/user/tokens/verify -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

```
{"result":{"id":"<token-id>","status":"active"},"success":true,"errors":[],"messages":[{"code":10000,"message":"This API Token is valid and active","type":null}]}
```

### What broke here

First try I ran `read -s CLOUDFLARE_API_TOKEN` with no prompt. It looked like nothing happened, because it was silently waiting. I pasted with Ctrl+V, which doesn't paste in this terminal, so the variable stayed empty. The verify call came back with:

```
{"success":false,"errors":[{"code":6003,"message":"Invalid request headers","error_chain":[{"code":6111,"message":"Invalid format for Authorization header"}]}],...}
```

`echo ${#CLOUDFLARE_API_TOKEN}` printed `0`. Fix: the `-p "Token: "` prompt version and Ctrl+Shift+V.

---

## 6. Get the repo ready

```bash
ls ~/Developer/homelab-kubecraft
cd ~/Developer/homelab-kubecraft
git status
```

`git status` showed `main` up to date plus some untracked files that aren't part of this: `cnpg_for_specific_namespace.yaml`, `kube-plugin.deb`, `kube-plugin.rpm`, `operator.yaml`, `sre-coding-practice/`. Because of those, I never use `git add .` in this repo. I add paths by name.

"Up to date" only means up to date with the last time this machine checked GitHub, so:

```bash
git pull
```

It printed some Renovate branches being updated (`forced update`) and then `Already up to date.` for `main`. Nothing to do about the Renovate lines.

```bash
mkdir opentofu
cd opentofu
```

---

## 7. `.gitignore`

First file, before anything else exists, so state and secrets can never get committed by accident.

```bash
nvim .gitignore
```

```
# Provider plugins downloaded by `tofu init`. Rebuilt on every init
.terraform/

# State: OpenTofu's record of every managed resource, with real values.
# Never in Git, especially in a public repo
*.tfstate
*.tfstate.*
.terraform.tfstate.lock.info

# Saved plans also contain resolved values
*.tfplan

# Variable files hold environment-specific and sensitive values
*.tfvars
*.tfvars.json
!terraform.tfvars.example

# Local-only overrides, never shared
override.tf
override.tf.json
*_override.tf
*_override.tf.json

# Crash logs can include sensitive data from the run
crash.log
crash.*.log

# Per-user CLI config (credentials, plugin cache paths)
.terraformrc
terraform.rc
```

My first version only had `.terraform/`, the state files and `*.tfvars`. I checked it against GitHub's Terraform template and added the rest: the lock info file, saved plans, JSON tfvars, override files, crash logs and CLI config.

- `!terraform.tfvars.example`: the `!` is an exception, so a blank template can still be committed
- `.terraform.lock.hcl` is **not** in here on purpose. That file gets committed.

---

## 8. `versions.tf` and the first `tofu init`

```bash
nvim versions.tf
```

```hcl
terraform {
  required_version = "~> 1.11"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.29"
    }
  }
}

# Auth comes from the CLOUDFLARE_API_TOKEN environment variable.
# No credentials live in this folder.
provider "cloudflare" {}
```

- The block is called `terraform { }` even in OpenTofu. Not a typo.
- `~> 1.11` allows 1.11 and newer 1.x. Older refuses to run.
- `~> 5.29` allows newer 5.x, never 6.x. Major versions rename things (v5 renamed the DNS resource), so I don't want that happening on its own.
- `provider "cloudflare" {}` is empty because the provider reads `CLOUDFLARE_API_TOKEN` itself.

Prove it works before writing more:

```bash
tofu init
```

```
- Finding cloudflare/cloudflare versions matching "~> 5.29"...
- Installing cloudflare/cloudflare v5.29.0...
- Installed cloudflare/cloudflare v5.29.0 (signed, key ID C76001609EE3B136)
...
OpenTofu has created a lock file .terraform.lock.hcl to record the provider
selections it made above.
...
OpenTofu has been successfully initialized!
```

"Signed" means OpenTofu checked the provider really came from Cloudflare.

```bash
ls -a
git status --ignored .
```

`.terraform/` showed under **Ignored files**. `.terraform.lock.hcl` was untracked, which is what I want, because it gets committed.

---

## 9. `variables.tf`

```bash
nvim variables.tf
```

```hcl
variable "zone_id" {
  description = "Cloudflare zone ID for projectpattie.com"
  type        = string
}

variable "tunnel_id" {
  description = "ID of the homelab-kubecraft Cloudflare tunnel"
  type        = string
}

variable "node_ip" {
  description = "LAN IP Traefik answers on, used by local-only records"
  type        = string
  default     = "10.0.0.106"
}
```

- `zone_id`: every record needs to know which domain it belongs to
- `tunnel_id`: every tunnel record points at `<tunnel-id>.cfargotunnel.com`, so I write it once here instead of fifteen times
- `node_ip` has a default because it isn't secret and rarely changes

---

## 10. Look up the zone, the tunnel and every record

The token could look up the zone ID for me, so I didn't have to hunt through the new dashboard.

```bash
curl -s "https://api.cloudflare.com/client/v4/zones?name=projectpattie.com" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -m json.tool | grep -m1 '"id"'
```

```
            "id": "<zone-id>",
```

- `?name=projectpattie.com` asks for just that zone
- `python3 -m json.tool` makes the JSON readable
- `grep -m1 '"id"'` shows only the first `"id"` line

Then every record in the zone, one per line:

```bash
ZONE_ID=<zone-id>

curl -s "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records?per_page=100" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c 'import sys,json; [print(r["name"], r["type"], r["content"], r["proxied"], r["ttl"]) for r in json.load(sys.stdin)["result"]]'
```

That prints name, type, where it points, proxied or not, and TTL. It also proved the zone ID was right and showed me the tunnel ID inside the CNAME targets.

---

## 11. Decide what OpenTofu manages

What was in the zone:

| Group | Records | Belongs to |
|---|---|---|
| KubeCraft tunnel apps | abs, auth, feeds, home, links, me, mealie, n8n, ntfy, paperless, pgadmin, up, uptime, vault, zentao (15) | homelab-kubecraft |
| Local-only | grafana, lds, A records to 10.0.0.106, grey cloud | homelab-kubecraft |
| `links.lab` | CNAME to the same tunnel | Leftover from my old `.lab` naming, not in the ConfigMap anymore |
| `musica` | CNAME to a **different** tunnel | Brasil lab |
| Blog | `projectpattie.com`, `www` → Cloudflare Pages | projectpattie site |
| Email | 5 MX + 1 SPF TXT | Registrar email forwarding |

My tunnel ID was the one 16 records pointed at, including all 15 in my ConfigMap.

I picked **17 records**: the 15 tunnel apps and the 2 local-only ones. Left out on purpose:

- `musica`: belongs to the Brasil repo
- blog and email: not homelab, and a mistake there takes down my site or my mail
- `links.lab`: leaving it alone for now, cleanup later

OpenTofu only touches records it knows about. Everything else stays exactly as it is.

---

## 12. `terraform.tfvars` and the example file

Two files, two jobs.

Real values, stays on the machine:

```bash
nvim terraform.tfvars
```

```hcl
zone_id   = "<zone-id>"
tunnel_id = "<tunnel-id>"
```

Blank template, goes to GitHub:

```bash
nvim terraform.tfvars.example
```

```hcl
# Copy to terraform.tfvars (gitignored) and fill in.
zone_id   = "<zone-id>"
tunnel_id = "<tunnel-id>"
```

OpenTofu reads `terraform.tfvars` on its own, no flag needed.

```bash
git status --ignored .
```

`terraform.tfvars` under **Ignored files**. The `.example` file not there, so it gets committed.

---

## 13. `dns.tf`

```bash
nvim dns.tf
```

```hcl
locals {
  # Hostnames served through the homelab-kubecraft tunnel.
  # Must match the ingress list in apps/base/cloudflared/configmap.yaml.
  tunnel_hosts = toset([
    "abs",
    "auth",
    "feeds",
    "home",
    "links",
    "me",
    "mealie",
    "n8n",
    "ntfy",
    "paperless",
    "pgadmin",
    "up",
    "uptime",
    "vault",
    "zentao",
  ])

  # Local-only hostnames: grey cloud, point straight at the node.
  local_hosts = toset([
    "grafana",
    "lds",
  ])
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = local.tunnel_hosts

  zone_id = var.zone_id
  name    = "${each.key}.projectpattie.com"
  type    = "CNAME"
  content = "${var.tunnel_id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "local" {
  for_each = local.local_hosts

  zone_id = var.zone_id
  name    = "${each.key}.projectpattie.com"
  type    = "A"
  content = var.node_ip
  proxied = false
  ttl     = 1
}
```

- `for_each` makes one record per name in the list. A new app is one more word.
- `each.key` is the current name in the loop, so `n8n` becomes `n8n.projectpattie.com`
- `proxied` and `ttl = 1` (auto) are copied from what the API listing showed, so the code describes the records exactly as they already are

I didn't run `tofu plan` here. Without the import file it would have tried to create 17 duplicates.

---

## 14. `imports.tf`

This file tells OpenTofu "these 17 already exist, adopt them, don't create them." It needs each record's ID. This one-liner printed them already formatted for the file, and only for my 17:

```bash
curl -s "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records?per_page=100" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c '
import sys, json
tunnel = {"abs","auth","feeds","home","links","me","mealie","n8n","ntfy","paperless","pgadmin","up","uptime","vault","zentao"}
local = {"grafana","lds"}
recs = {r["name"].removesuffix(".projectpattie.com"): r["id"] for r in json.load(sys.stdin)["result"]}
print("# tunnel")
for n in sorted(tunnel): print(f"    {n} = \"{recs[n]}\"")
print("# local")
for n in sorted(local): print(f"    {n} = \"{recs[n]}\"")
'
```

```
# tunnel
    abs = "<record-id>"
    auth = "<record-id>"
    ...
# local
    grafana = "<record-id>"
    lds = "<record-id>"
```

```bash
nvim imports.tf
```

```hcl
# One-time file: adopts the 17 existing records into OpenTofu.
# Delete after the import apply succeeds.

locals {
  tunnel_record_ids = {
    abs       = "<record-id>"
    auth      = "<record-id>"
    # ...all 15
  }

  local_record_ids = {
    grafana = "<record-id>"
    lds     = "<record-id>"
  }
}

import {
  for_each = local.tunnel_record_ids
  to       = cloudflare_dns_record.tunnel[each.key]
  id       = "${var.zone_id}/${each.value}"
}

import {
  for_each = local.local_record_ids
  to       = cloudflare_dns_record.local[each.key]
  id       = "${var.zone_id}/${each.value}"
}
```

- `import { }`: "the record with this ID in Cloudflare is the one my code calls `tunnel["n8n"]`"
- `id = "${var.zone_id}/${each.value}"`: the provider wants import IDs as `<zone_id>/<record_id>`

### What broke here

The first time, I saved the skeleton with the placeholder comments still in it and no IDs. Second time, the IDs were in but the bottom of the file was gone: no closing `}` for `locals` and no `import` blocks. The file just ended after `lds`. I went to the end with `G`, opened a new line with `o`, and pasted the closing brace and both import blocks.

```bash
tofu fmt
tofu validate
```

```
imports.tf
Success! The configuration is valid.
```

`tofu fmt` printed `imports.tf` because it fixed the spacing in that file.

---

## 15. Plan, import, prove there's no drift

```bash
tofu plan
```

```
Plan: 17 to import, 0 to add, 0 to change, 0 to destroy.
```

That's the line that matters:

- **17 to import**: all of them get adopted
- **0 to add**: no duplicates
- **0 to change**: my code matches the real records exactly
- **0 to destroy**: nothing goes offline

It also printed a note about not using `-out`. That's fine here. `apply` plans again and shows me before doing anything.

```bash
tofu apply
```

Typed `yes` (only the full word works).

```
Apply complete! Resources: 17 imported, 0 added, 0 changed, 0 destroyed.
```

Nothing changed in Cloudflare. This only wrote the notebook, `terraform.tfstate`.

The import file has done its job, the state remembers the IDs now:

```bash
rm imports.tf
tofu plan
```

```
No changes. Your infrastructure matches the configuration.
```

That one line is the whole point. Git and Cloudflare agree.

```bash
tofu state list
```

17 lines, like `cloudflare_dns_record.tunnel["n8n"]` and `cloudflare_dns_record.local["grafana"]`.

```bash
git status --ignored .
```

`.terraform/`, `terraform.tfstate` and `terraform.tfvars` all under **Ignored files**.

---

## 16. Commit and push

From the repo root, only the `opentofu/` folder:

```bash
cd ~/Developer/homelab-kubecraft
git add opentofu/
git status
```

Before committing I checked **Changes to be committed** listed exactly these 6:

- `opentofu/.gitignore`
- `opentofu/.terraform.lock.hcl`
- `opentofu/dns.tf`
- `opentofu/terraform.tfvars.example`
- `opentofu/variables.tf`
- `opentofu/versions.tf`

No state, no tfvars.

```bash
git commit -m "Add: OpenTofu managing Cloudflare DNS (17 records imported)"
git push
```

```
[main 60630dd] Add: OpenTofu managing Cloudflare DNS (17 records imported)
 6 files changed, 130 insertions(+)
```

---

## 17. Drift drill

Drift is when the real thing doesn't match the code anymore, usually because someone changed it by hand. This drill proves OpenTofu notices.

**Step 1: change something by hand.** Cloudflare's docs link to the records page as `dash.cloudflare.com/?to=/:account/:zone/dns/records`. I picked projectpattie.com, searched `links`, chose the plain `links` CNAME (not `links.lab`), clicked **Edit**, typed `drift test` in **Comment**, and saved.

**Step 2: let OpenTofu find it.**

```bash
tofu plan
```

### What broke here (twice)

First run failed on every record with:

```
Error: failed to make http request
...
400 Bad Request {"success":false,"errors":[{"code":9106,"message":"Missing
X-Auth-Key, X-Auth-Email or Authorization headers"}],...}
```

I was in a terminal where `CLOUDFLARE_API_TOKEN` wasn't set anymore, so OpenTofu sent the requests with no token. Nothing changed in Cloudflare, `plan` only reads. I reloaded the token (section 5) and ran it again, but from the repo root:

```
Error: No configuration files
```

OpenTofu only reads the folder you're standing in. `cd opentofu` and it worked.

```
  # cloudflare_dns_record.tunnel["links"] will be updated in-place
  ~ resource "cloudflare_dns_record" "tunnel" {
      - comment = "drift test" -> null
    ...
Plan: 0 to add, 1 to change, 0 to destroy.
```

- `~` means update in place, the record stays, one field changes
- `- comment = "drift test" -> null` means Cloudflare has my comment, my code doesn't, so it would remove it
- Everything without a symbol already matches

**Step 3: put it back.**

```bash
tofu apply
```

```
Apply complete! Resources: 0 added, 1 changed, 0 destroyed.
```

Comment gone in the dashboard.

```bash
tofu plan
```

```
No changes. Your infrastructure matches the configuration.
```

The lesson: Flux would have fixed that on its own. OpenTofu only told me because I ran `plan`.

---

## 18. A namespace for the state, through Flux

The state file was only on Tupac. If I ran `tofu` from another machine it would have no notebook and think nothing existed. I decided to keep it in my own cluster as a Secret, in its own namespace.

First, check Tupac can reach the cluster:

```bash
kubectl get nodes
kubectl config current-context
```

biggie-smalls showed `NotReady` the first time. It had been logged out. Once it was back, all three were `Ready`. Context: `homelab`, same name as on my Mac.

The namespace goes through Flux like everything else, not `kubectl create`. It sits next to Renovate under `infrastructure/controllers/`, which the Flux Kustomization `infrastructure-controllers` already reconciles from `./infrastructure/controllers/staging`. No new Flux file needed.

`infrastructure/controllers/base/opentofu/namespace.yaml`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: opentofu
```

`infrastructure/controllers/base/opentofu/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
```

And one line in `infrastructure/controllers/staging/kustomization.yaml`, lined up under `- renovate`:

```yaml
   resources:
     - renovate
     - ../base/opentofu
```

That file is indented 3 spaces. The new line has to match. Staging points straight at base because there's nothing to change for a namespace, same as `apps/staging` does with `../base/homepage`.

### What broke here

I opened nvim from inside `opentofu/`, so all three files went into `opentofu/infrastructure/...` instead of the repo root. `kubectl kustomize` only showed Renovate's resources, and `git status` showed `opentofu/infrastructure/` as untracked. Fix, from the repo root:

```bash
mv opentofu/infrastructure/controllers/base/opentofu infrastructure/controllers/base/
nvim infrastructure/controllers/staging/kustomization.yaml   # add the line to the real file
rm -r opentofu/infrastructure                                 # only the duplicate staging file was left
```

Check before committing:

```bash
kubectl kustomize infrastructure/controllers/staging | grep -A3 "kind: Namespace"
```

Both `opentofu` and `renovate` namespaces showed up.

```bash
git add infrastructure/controllers/base/opentofu infrastructure/controllers/staging/kustomization.yaml
git commit -m "Add: opentofu namespace for OpenTofu state"
git push
flux reconcile kustomization infrastructure-controllers --with-source
kubectl get namespace opentofu
```

```
NAME       STATUS   AGE
opentofu   Active   58s
```

`--with-source` makes Flux pull from GitHub first instead of waiting for its next check.

---

## 19. Encrypt the state

A Secret is only base64, not encryption. So before the state left my laptop, I encrypted it with OpenTofu's built-in state encryption.

**Passphrase:**

```bash
openssl rand -base64 24
```

32 random characters (the docs say at least 16). Saved in Vaultwarden. Without it the state can't be read.

**Load it**, same trick as the token:

```bash
read -s -p "Passphrase: " TF_VAR_state_passphrase && export TF_VAR_state_passphrase && echo
echo ${#TF_VAR_state_passphrase}
```

`32`. Nothing in that command gets replaced. It asks for the passphrase at the prompt.

`TF_VAR_` is a naming rule: OpenTofu turns any environment variable starting with `TF_VAR_` into a variable, so this fills `var.state_passphrase` without writing it to a file.

**`encryption.tf`, first version, with a one-time fallback:**

```hcl
variable "state_passphrase" {
  description = "Passphrase that encrypts the OpenTofu state"
  type        = string
  sensitive   = true
}

terraform {
  encryption {
    method "unencrypted" "migrate" {}

    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method = method.aes_gcm.state

      fallback {
        method = method.unencrypted.migrate
      }
    }
  }
}
```

- `sensitive = true`: never printed in output
- `pbkdf2` turns the passphrase into a key, `aes_gcm` does the encrypting with it
- The `fallback` with `unencrypted` lets OpenTofu read the old plain-text state once so it can rewrite it encrypted. OpenTofu refuses to read plain text otherwise.

A new file starts empty in nvim. `i`, paste, Esc, `:wq`.

```bash
tofu validate
tofu apply
```

```
No changes. Your infrastructure matches the configuration.
...
Apply complete! Resources: 0 added, 0 changed, 0 destroyed.
```

```bash
head -c 300 terraform.tfstate; echo
```

Scrambled text with `"encrypted_data"`. No record names.

**Remove the fallback and lock it in.** `encryption.tf` became:

```hcl
terraform {
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}
```

(The `variable` block at the top stays.) `enforced = true` means plain text can never be written again.

```bash
tofu validate
tofu plan
```

No changes, so it still reads the encrypted state.

### What I almost missed

```bash
ls
head -c 200 terraform.tfstate.backup; echo
```

```
{"version":4,"terraform_version":"1.11.5","serial":1,...,"resources":[{"mode":"managed","type":"cloudflare_dns_record","name":"local",...
```

The backup was the old **plain-text** copy with my records in it.

```bash
rm terraform.tfstate.backup
```

---

## 20. Move the state into the cluster

Where kubectl's config lives on Tupac:

```bash
echo $KUBECONFIG
ls ~/.kube/config
```

`$KUBECONFIG` was empty, `~/.kube/config` exists. So that's the path.

`backend.tf`:

```hcl
terraform {
  backend "kubernetes" {
    secret_suffix  = "state"
    namespace      = "opentofu"
    config_path    = "~/.kube/config"
    config_context = "homelab"
  }
}
```

- `secret_suffix = "state"` → the Secret is named `tfstate-default-state`
- `namespace` is the one Flux just made
- `config_context = "homelab"` is the same name on Tupac and my Mac, so this file works on both

### What broke here

I was inside `opentofu/` and ran `nvim opentofu/backend.tf`. The file landed in `opentofu/opentofu/backend.tf`. `tofu init -migrate-state` ran fine but never asked to copy the state, because it never saw the backend. Fix:

```bash
ls opentofu            # backend.tf was in there
mv opentofu/backend.tf .
rmdir opentofu         # only removes empty folders, can't delete anything by accident
ls
cat backend.tf
```

Then the real migration:

```bash
tofu init -migrate-state
```

```
Do you want to copy existing state to the new backend?
  Pre-existing state was found while migrating the previous "local" backend to the
  newly configured "kubernetes" backend. No existing state was found in the newly
  configured "kubernetes" backend. ...
  Enter a value: yes

Successfully configured the backend "kubernetes"! ...
```

**Prove it:**

```bash
kubectl get secret -n opentofu
```

```
NAME                    TYPE     DATA   AGE
tfstate-default-state   Opaque   1      79s
```

```bash
kubectl get secret tfstate-default-state -n opentofu -o jsonpath='{.data.tfstate}' | base64 -d | gunzip | head -c 150; echo
```

This pulls the data out of the Secret, undoes the base64, unzips it and shows the start:

```
{"serial":3,"lineage":"...","meta":{"key_provider.pbkdf2.state":"eyJzYWx0Ijoi...
```

Key info and scrambled data, no record names. Even someone who can read the Secret can't read my state without the passphrase.

```bash
tofu plan
```

No changes, reading from the cluster now.

Clean up the old local copy and prove it isn't needed:

```bash
rm terraform.tfstate
tofu plan
```

Still no changes.

```bash
cd ~/Developer/homelab-kubecraft
git add opentofu/encryption.tf opentofu/backend.tf
git status
git commit -m "OpenTofu: encrypted state in the cluster (kubernetes backend)"
git push
```

Commit `8ae0dab`.

---

## 21. Secrets from Vaultwarden with `bw`

Pasting the token and passphrase in every new terminal was the last annoying part, and it's exactly what broke my drift drill. So both secrets now come from my own Vaultwarden.

My Vaultwarden at `vault.projectpattie.com` is **not** behind Authentik, so the CLI can reach it.

The Bitwarden docs offer a native download, npm, or snap. The native download is x64 only and Tupac is ARM, so npm:

```bash
node -v
npm install -g --prefix ~/.local @bitwarden/cli
```

Node was already there (`v26.13.0`). `--prefix ~/.local` installs into my home folder so it doesn't need sudo. It printed a deprecation warning and a note about a `core-js` install script, both safe to ignore.

```bash
bw config server https://vault.projectpattie.com
bw config server
bw login
```

`Saved setting config.` and then the URL printed back.

### What broke here

The first time I ran `bw config server` with extra words pasted in after the URL and got `error: too many arguments for 'config'. Expected 2 arguments but got 7.` The command takes exactly one URL.

Then the vault was empty. Nothing in the web vault, nothing in the CLI. The token and passphrase weren't saved there yet. I added both in the web vault: **+ Add** → **Login**, name and password only.

- `opentofu-dns` → password is the Cloudflare token
- `opentofu-state-passphrase` → password is the state passphrase

They go in the **Password** field because `bw get password` reads that field.

Check they're there, names only:

```bash
export BW_SESSION=$(bw unlock --raw)
bw sync
bw list items | python3 -c 'import sys,json; [print(i["name"]) for i in json.load(sys.stdin)]'
```

```
opentofu-dns
opentofu-state-passphrase
```

- `bw unlock --raw` asks for the master password and prints only the session key, which goes straight into `BW_SESSION`
- `bw sync` pulls items added in the browser down to the CLI

**Prove the values are right** by letting OpenTofu use them. A wrong token can't reach Cloudflare, a wrong passphrase can't decrypt the state:

```bash
export CLOUDFLARE_API_TOKEN=$(bw get password opentofu-dns)
export TF_VAR_state_passphrase=$(bw get password opentofu-state-passphrase)
echo ${#CLOUDFLARE_API_TOKEN} ${#TF_VAR_state_passphrase}
cd opentofu
tofu plan
```

`53 32`, then **No changes**.

---

## 22. `tofu-env`: one command per session

Remember the pattern, not the letters:

| Line | In plain words |
|---|---|
| `export BW_SESSION=$(bw unlock --raw)` | Open the vault |
| `export CLOUDFLARE_API_TOKEN=$(bw get password opentofu-dns)` | Take out the token |
| `export TF_VAR_state_passphrase=$(bw get password opentofu-state-passphrase)` | Take out the passphrase |

- `export NAME=...` puts something in a box called NAME that other programs can see
- `$( ... )` runs a command and uses whatever it prints
- The box names come from who reads them: the Cloudflare provider looks for `CLOUDFLARE_API_TOKEN`, OpenTofu turns `TF_VAR_state_passphrase` into `var.state_passphrase`

Then I stopped needing to remember it. In `~/.bashrc`:

```bash
nvim ~/.bashrc     # G to the bottom, o for a new line, paste
```

```bash
tofu-env() {
  export BW_SESSION=$(bw unlock --raw)
  export CLOUDFLARE_API_TOKEN=$(bw get password opentofu-dns)
  export TF_VAR_state_passphrase=$(bw get password opentofu-state-passphrase)
  echo "token: ${#CLOUDFLARE_API_TOKEN} chars, passphrase: ${#TF_VAR_state_passphrase} chars"
}
```

```bash
source ~/.bashrc
tofu-env
```

`source` reloads the file so the new command works in the window I'm already in.

Every session now:

```bash
tofu-env
cd ~/Developer/homelab-kubecraft/opentofu
tofu plan
# ...
bw lock
```

`tofu-env` only lives on Tupac. It isn't in Git yet.

---

## 23. nvim commands I used

| Command | What it does | Why I needed it |
|---|---|---|
| `nvim <file>` | Opens the file, or creates it if it doesn't exist | Every file in this build |
| `i` | Insert mode, so typing goes into the file | A new file opens empty in normal mode. Typing without `i` runs commands instead of writing text |
| Esc | Back to normal mode | Needed before any `:` command |
| `:w` | Save, stay in the file | Saving mid-edit to check something |
| `:wq` | Save and quit | Done with a file |
| `G` | Jump to the last line | Fixing the bottom of `imports.tf`, adding `tofu-env` to the end of `~/.bashrc` |
| `o` | New line below the cursor, already in insert mode | Pasting the missing import blocks and the function |
| Ctrl+Shift+V | Paste in the Fedora terminal | Ctrl+V doesn't paste here |

**The big one:** nvim creates the file relative to the folder you're in. Twice I opened nvim from inside `opentofu/` with a path that already started with a folder name, and the file landed one level too deep (`opentofu/infrastructure/...` and `opentofu/opentofu/backend.tf`). Look at the prompt before typing the path. If it ends in `/opentofu$`, paths are from there.

---

## 24. Shortcuts

- `echo ${#VAR}` checks a secret loaded without printing it. `0` means empty.
- `read -s -p "Prompt: " VAR` loads a secret with nothing on screen and nothing in history
- The API + `python3 -c` one-liners: let the API give me IDs and formatted lines instead of copying from the dashboard
- `git status --ignored .` shows exactly what's ignored in this folder. The fastest way to prove secrets won't get committed
- `tofu fmt` before `tofu validate` fixes spacing so I don't have to care about alignment
- `flux reconcile kustomization <name> --with-source` skips the wait for Flux's next pull
- `kubectl kustomize <path> | grep -A3 "kind: Namespace"` checks the build without scrolling through everything
- `head -c 200 <file>` peeks at a file without dumping all of it. That's how I caught the plain-text backup
- `tofu-env` for both secrets in one command

---

## 25. Things to look out for

- **The dashboard path for API tokens changed.** There's no My Profile in the profile menu. Go straight to `dash.cloudflare.com/profile/api-tokens/`
- **Paste is Ctrl+Shift+V** in the Fedora terminal, and `read -s` shows nothing while it waits
- **Secrets only live in the window you set them in.** A new terminal has none. Symptom: `Missing X-Auth-Key, X-Auth-Email or Authorization headers`
- **Run `tofu` from inside `opentofu/`.** From anywhere else: `No configuration files`
- **Check the folder before `nvim <path>`.** Files landing one level too deep cost me two fixes
- **Never `git add .` at the repo root.** Add paths by name
- **Look for `terraform.tfstate.backup`** after turning on encryption. Mine was the old plain-text copy
- **Don't run `tofu plan` with `dns.tf` but no import file** the first time. It would plan 17 duplicates
- **Lose the passphrase and the state is unreadable.** It's in Vaultwarden now
- **Docs placeholders aren't part of the command.** `bw config server` takes one URL, nothing after it

---

## 26. Verification commands

```bash
tofu-env                                             # both secrets loaded: 53 and 32
tofu plan                                            # No changes = Git, state and Cloudflare agree
tofu state list                                      # 17 managed records
kubectl get secret -n opentofu                       # tfstate-default-state
kubectl get secret tfstate-default-state -n opentofu -o jsonpath='{.data.tfstate}' | base64 -d | gunzip | head -c 150; echo   # encrypted
git status --ignored opentofu/                       # .terraform/ and terraform.tfvars ignored
```

---

## 27. Rollback

- **Never run `tofu destroy` in this folder.** It deletes every managed record, which takes every public app offline at once
- **Stop managing a record but keep it in Cloudflare:** `tofu state rm 'cloudflare_dns_record.tunnel["<name>"]'`, then take the name out of `dns.tf`
- **Remove a record for real:** take the name out of `dns.tf`, `tofu plan` (check it says `1 to destroy` and it's the right one), `tofu apply`
- **Undo a hand change:** `tofu plan` shows it, `tofu apply` puts it back (the drift drill)
- **Records themselves never depend on the state.** If the state were lost, the records keep working. I'd rebuild `imports.tf` (section 14) and import again

---

## 28. Decisions

| Decision | Why |
|---|---|
| OpenTofu 1.11.5 from Fedora's repo | Has everything this needs, no fighting the package manager |
| Token: DNS Edit, one zone, no expiration, no IP filter | Smallest access that works. IP filter would break when my home IP changes |
| Token and passphrase never in files | Env vars only, loaded from Vaultwarden |
| `opentofu/` at the repo root | Flux ignores it. It sits next to the apps whose DNS it holds |
| 17 records, not the whole zone | Homelab only. Blog, email and Brasil records stay out |
| Import, not recreate | Adopting live records means zero downtime |
| `for_each` over a list of names | Every tunnel record is the same except the name |
| `.terraform.lock.hcl` committed | Pins the exact provider, same as pinning image tags |
| State encrypted with pbkdf2 + aes_gcm, `enforced = true` | A Secret is only base64 |
| State in a Secret in my cluster | Any machine with kube access can use it, with a Lease as the lock |
| `opentofu` namespace through Flux | In Git like everything else, not hand-applied |
| `bw` from npm into `~/.local` | Native build is x64 only, Tupac is ARM. No sudo needed |
