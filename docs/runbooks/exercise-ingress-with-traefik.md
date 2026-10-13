# Exercise - Ingress with Traefik

Tag: homelab-kubecraft

Getting Grafana onto `grafana.projectpattie.com` through Traefik, with the
Ingress built by the kube-prometheus-stack chart from my values instead of an
`ingress.yaml` I wrote. Then the bonus: a self-signed TLS certificate served
on that Ingress.

The exercise, from the course:

> Use the kube-prometheus-stack helm chart to create an ingress on the cluster.
> The goal is to be able to enter something like grafana.mydomain.com into the
> browser and access Grafana that way. Do not create an ingress yaml file
> yourself, but try to utilize `helm show values` to configure it yourself
> using the chart. Bonus points: create a self signed TLS certificate and serve
> that on the ingress.

What the traffic does:

    browser -> DNS (grafana.projectpattie.com -> 10.0.0.106) -> Traefik -> Ingress -> Grafana Service -> pod

Files this touches:

    infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml
    infrastructure/monitoring/controllers/staging/kube-prometheus-stack/kustomization.yaml
    infrastructure/monitoring/controllers/staging/kube-prometheus-stack/tls-secret.yaml

---

# Where to run these

On the Mac, in `~/Developer/homelab-kubecraft`, unless a step says any folder.

# Why there is no ingress.yaml

For Linkding I wrote the Ingress myself. Grafana comes from a Helm chart, and
I cannot edit a chart's files. The only way to change what it builds is to
give it values. The chart already knows how to make an Ingress. It ships
switched off. So the job is: find the setting names, then set them.

---

# Part 1: find the settings

## 1. Check the chart version I am running

    flux get helmreleases -A

Shows every Helm install Flux manages. kube-prometheus-stack was on `92.0.0`.
I need the settings for the version I run, because they can change between
versions.

## 2. Save the chart's settings to a file

Any folder:

    helm show values prometheus-community/kube-prometheus-stack --version 92.0.0 > /tmp/kps-values.yaml

Prints every setting the chart accepts, with defaults. Over six thousand
lines, so it goes to a file.

If it says the version is not found, see "Things that bit me" below.

## 3. Search for the ingress settings

    grep -n "ingress:" /tmp/kps-values.yaml

Six hits: 640, 1511, 3828, 3832, 4164, 5556. More than one because the chart
installs several apps and each has its own ingress switch.

## 4. Find which one is Grafana's

    grep -n "^[a-zA-Z]" /tmp/kps-values.yaml

Lists the top-level headings with line numbers. `grafana:` starts at 1396 and
the next heading, `kubernetesServiceMonitors:`, starts at 1732. The only
`ingress:` between those is line 1511.

## 5. Read Grafana's ingress section

    sed -n '1511,1560p' /tmp/kps-values.yaml

Prints lines 1511 to 1560. Lines starting with `#` are comments, examples
only. The three settings I need:

| Setting | Default | What I set |
|---|---|---|
| `enabled` | `false` | `true` |
| `ingressClassName` | not set (example says `nginx`) | `traefik` |
| `hosts` | `[]` | `grafana.projectpattie.com` |

`path` is already `/`. `tls` is for the bonus.

## 6. Get the ingress class name

    k get ingressclass

    NAME                CONTROLLER                      PARAMETERS   AGE
    traefik (default)   traefik.io/ingress-controller   <none>       87d

The name is `traefik`. `(default)` is not part of it.

---

# Part 2: set the values

## 1. Edit the HelmRelease

    vim infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml

There is already a `grafana:` section under `values:`. The ingress block goes
inside it, in the same column as `admin:`. Do not add a second `grafana:`
heading.

      values:
        grafana:
           admin:
             existingSecret: grafana-admin
           ingress:
             enabled: true
             ingressClassName: traefik
             hosts:
               - grafana.projectpattie.com

Spaces, not tabs.

## 2. Build, dry-run, diff

    kubectl kustomize infrastructure/monitoring/controllers/staging
    kubectl apply --dry-run=server -f infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml
    git diff

The dry run prints a warning about a missing `last-applied-configuration`
annotation. That is kubectl noticing Flux created this HelmRelease, not me.
Ignore it. I want `configured (server dry run)`.

The diff should show only the five new ingress lines.

The dry run cannot tell me if the chart understands my settings. Only the
real upgrade proves that.

## 3. Commit and push

    git add infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml
    git commit -m "Enable Grafana ingress via kube-prometheus-stack chart values"
    git push

## 4. Reconcile and check

    flux reconcile source git flux-system-kubecraft -n flux-system
    flux get helmreleases -n monitoring
    k get ingress -n monitoring

Wanted: `Helm upgrade succeeded`, and an Ingress named
`kube-prometheus-stack-grafana` with class `traefik` and my hostname.

    NAME                            CLASS     HOSTS                       ADDRESS                           PORTS   AGE
    kube-prometheus-stack-grafana   traefik   grafana.projectpattie.com   10.0.0.106,10.0.0.195,10.0.0.20   80      38s

---

# Part 3: DNS and test

## 1. Add the DNS record

Cloudflare dashboard, DNS, Records, Add record:

- Type: `A`
- Name: `grafana`
- IPv4 address: `10.0.0.106`
- Proxy status: DNS only (grey cloud)

DNS only because `10.0.0.106` is a private address. The orange cloud cannot
reach it. It also keeps Grafana on the home network only.

## 2. Test

    nslookup grafana.projectpattie.com
    curl -I http://grafana.projectpattie.com

`nslookup` should return `10.0.0.106`. `curl` should return `302 Found` with
`Location: /login`. Then open `http://grafana.projectpattie.com` in the
browser.

---

# Part 4: bonus, self-signed TLS

## 1. Create the certificate and key

Any folder:

    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout /tmp/grafana-tls.key -out /tmp/grafana-tls.crt -subj "/CN=grafana.projectpattie.com" -addext "subjectAltName=DNS:grafana.projectpattie.com"

| Part | What it does |
|---|---|
| `req -x509` | make a self-signed certificate |
| `-newkey rsa:2048` | make a new private key with it |
| `-nodes` | no password on the key, Traefik has to read it unattended |
| `-days 365` | valid for a year |
| `-keyout` / `-out` | where the key and certificate are saved |
| `-subj` / `-addext` | the hostname the certificate is for, must match the browser |

Both files go in `/tmp`, not the repo. The key is plain text until it is
encrypted.

    ls -l /tmp/grafana-tls.crt /tmp/grafana-tls.key

## 2. Create the Secret manifest and encrypt it

    k create secret tls grafana-tls -n monitoring --cert=/tmp/grafana-tls.crt --key=/tmp/grafana-tls.key --dry-run=client -o yaml > infrastructure/monitoring/controllers/staging/kube-prometheus-stack/tls-secret.yaml
    sops --encrypt --in-place infrastructure/monitoring/controllers/staging/kube-prometheus-stack/tls-secret.yaml

Run the second one straight after the first. In between, the key is in the
repo folder unencrypted. No output from `sops` means it worked.

The Secret has to be in the `monitoring` namespace. The chart's own comment
says so: "Secret must be manually created in the namespace".

Check it is encrypted:

    grep -c "ENC\[" infrastructure/monitoring/controllers/staging/kube-prometheus-stack/tls-secret.yaml

Should print `3`: `tls.crt`, `tls.key`, and the `mac` line SOPS adds. `0`
means it is still plain text. Do not commit.

## 3. Add the file to the kustomization

    vim infrastructure/monitoring/controllers/staging/kube-prometheus-stack/kustomization.yaml

    resources:
      - ../../base/kube-prometheus-stack/
      - secret.yaml
      - tls-secret.yaml

Flux only applies what is listed here. Then check it builds:

    kubectl kustomize infrastructure/monitoring/controllers/staging/kube-prometheus-stack | grep -n "name: grafana"

Should show both `grafana-admin` and `grafana-tls`.

## 4. Point the ingress at the Secret

    vim infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml

`tls:` goes in the same column as `hosts:`.

           ingress:
             enabled: true
             ingressClassName: traefik
             hosts:
               - grafana.projectpattie.com
             tls:
               - secretName: grafana-tls
                 hosts:
                   - grafana.projectpattie.com

## 5. Commit and push all three files

    git diff
    git add infrastructure/monitoring/controllers/base/kube-prometheus-stack/release.yaml infrastructure/monitoring/controllers/staging/kube-prometheus-stack/kustomization.yaml infrastructure/monitoring/controllers/staging/kube-prometheus-stack/tls-secret.yaml
    git status
    git commit -m "Add self-signed TLS to Grafana ingress"
    git push
    flux reconcile source git flux-system-kubecraft -n flux-system

`git diff` does not show `tls-secret.yaml` because it is a new file Git is
not tracking yet. `git status` does.

## 6. Check

    k get secret grafana-tls -n monitoring
    k get ingress -n monitoring
    curl -kI https://grafana.projectpattie.com

Wanted: the Secret exists with type `kubernetes.io/tls`, PORTS reads
`80, 443`, and curl returns `HTTP/2 302` with `location: /login`. `-k` tells
curl to accept a self-signed certificate.

To confirm it is my certificate and not Traefik's fallback one:

    curl -kv https://grafana.projectpattie.com 2>&1 | grep -i "subject:"

Should show `CN=grafana.projectpattie.com`. `TRAEFIK DEFAULT CERT` means the
Secret was not picked up.

## 7. Browser

Open `https://grafana.projectpattie.com`. Chrome shows "Your connection is
not private" with `NET::ERR_CERT_AUTHORITY_INVALID`. That is expected for a
self-signed certificate. Advanced, then Proceed.

## 8. Clean up

    rm /tmp/grafana-tls.key

The encrypted copy in Git is the one that counts.

---

# Things that bit me

**`helm show values` said version 92.0.0 was not found.** The Mac keeps its
own list of chart versions and mine was older than the release. The cluster
had it because Flux fetches its own list.

    helm repo update

Then run `helm show values` again.

**`grep` returned nothing.** I ran it right after the failed `helm show
values`, so `/tmp/kps-values.yaml` was empty. An empty result here means go
back and check the file got written.

**I put `ingress:` above `admin:` and one space to the left.** Because
`admin:` was then the next line and indented further, YAML would have read
`admin` as being inside `ingress` and broken the Grafana password setting.
Fix: `ingress:` goes below `existingSecret`, in exactly the same column as
`admin:`.

**I did not know what to put for `ingressClassName`.** It is the NAME column
of `k get ingressclass`. The chart's `nginx` is only their example.

**I left `enabled: false`.** I had the class and the hostname right and the
switch still off. The chart does not error on that. It builds nothing. Check
this line before committing.

**I left out the `tls:` heading.** I typed `- secretName:` directly under
`hosts:`, which makes the secret look like a second hostname. `tls:` needs
its own line in the same column as `hosts:`.

**A leftover line of spaces at the end of `release.yaml`.** Showed up as a
red block in vim. `dd` on that line.

**The browser warning looked like a failure.** It is not. `AUTHORITY_INVALID`
means the browser does not know who signed the certificate. If it had said
`COMMON_NAME_INVALID`, the hostname in the `openssl` command would have been
wrong.

**Do not run the dry-run command without `--dry-run`.** That would change the
cluster by hand and Flux would overwrite it from Git. The only real path is
commit and push.

---

# Backing it out

Remove the `tls:` block to go back to plain HTTP, or set `enabled: false` (or
remove the whole `ingress:` block) to remove the Ingress. If removing TLS,
also take `tls-secret.yaml` out of the kustomization and delete the file.
Push, reconcile, and delete the `grafana` A record in Cloudflare.
