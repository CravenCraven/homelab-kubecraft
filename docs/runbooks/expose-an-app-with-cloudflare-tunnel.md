# Exposing an app with Cloudflare Tunnel

Getting an app from "port-forward only" to "live on the internet over HTTPS",
without opening a single port on the router.

What the traffic does:

    internet -> Cloudflare edge -> tunnel -> ClusterIP Service -> pod

The tunnel is `homelab-kubecraft`, UUID `d0923ef7-de9d-4f31-a53a-9458d64586e5`.
Its manifests live in `apps/base/cloudflared/`.

---

# Part 1: first-time setup

Done once. Everything here already exists, so this part is for rebuilding or
for setting up a second cluster.

## Where to run these

On the Mac. Two of these commands need kubectl and a browser on the same
machine, and craventhegreat boots headless with no browser on it.

Check the Mac can reach the cluster before starting:

    kubectl cluster-info

Should print `https://10.0.0.106:6443`. If it prints `127.0.0.1` I am on the
server, not the Mac.

Also check which context I am pointed at, because the Mac has more than one and
a secret created in the wrong cluster fails silently:

    kubectl config current-context

## 1. Install cloudflared

    brew install cloudflared
    cloudflared --version

## 2. Log in to Cloudflare

    cloudflared tunnel login

A browser window opens with a list of zones. Click the zone. The terminal sits
there waiting the whole time, so if I wander off it times out and writes
nothing. That is what happened the first time I tried it.

On success it writes `~/.cloudflared/cert.pem`.

## 3. Create the tunnel

    cloudflared tunnel create homelab-kubecraft

Prints the UUID and writes `~/.cloudflared/<UUID>.json`.

That JSON is a credential. Anyone holding it can run my tunnel and serve
traffic on my domain. It does not go in Git.

## 4. Put the credentials into the cluster

    kubectl create namespace cloudflared
    kubectl create secret generic tunnel-credentials \
      --namespace cloudflared \
      --from-file=credentials.json=$HOME/.cloudflared/<UUID>.json

The name on the left of the `=` becomes the filename once it is mounted, which
is why the config points at `/etc/cloudflared/creds/credentials.json`.

Check it landed:

    kubectl get secret -n cloudflared

This step breaks my own rule. The README says if it is not in main, it is not
in the cluster. This secret exists only because I typed it. Rebuild from the
repo and it does not come back. Sealed Secrets is on the roadmap and this is
exactly why.

## 5. Write the cloudflared manifests

Four files in `apps/base/cloudflared/`.

`namespace.yaml`:

    apiVersion: v1
    kind: Namespace
    metadata:
      name: cloudflared

`configmap.yaml`:

    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: cloudflared
      namespace: cloudflared
    data:
      config.yaml: |
        tunnel: homelab-kubecraft
        credentials-file: /etc/cloudflared/creds/credentials.json
        metrics: 0.0.0.0:2000
        no-autoupdate: true
        ingress:
        - hostname: links.projectpattie.com
          service: http://linkding.linkding.svc.cluster.local:9090
        - service: http_status:404

`deployment.yaml`:

    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: cloudflared
      namespace: cloudflared
    spec:
      replicas: 2
      selector:
        matchLabels:
          app: cloudflared
      template:
        metadata:
          labels:
            app: cloudflared
        spec:
          containers:
          - name: cloudflared
            image: cloudflare/cloudflared:2026.10.0
            args:
            - tunnel
            - --config
            - /etc/cloudflared/config/config.yaml
            - run
            livenessProbe:
              httpGet:
                path: /ready
                port: 2000
              failureThreshold: 1
              initialDelaySeconds: 10
              periodSeconds: 10
            volumeMounts:
            - name: config
              mountPath: /etc/cloudflared/config
              readOnly: true
            - name: creds
              mountPath: /etc/cloudflared/creds
              readOnly: true
          volumes:
          - name: creds
            secret:
              secretName: tunnel-credentials
          - name: config
            configMap:
              name: cloudflared
              items:
              - key: config.yaml
                path: config.yaml

`kustomization.yaml`:

    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources:
      - namespace.yaml
      - configmap.yaml
      - deployment.yaml

Two places I went off the course on purpose:

- The image tag is pinned. The guide uses `:latest`, which means a pod restart
  can quietly change versions.
- cloudflared gets its own namespace instead of living in the app's namespace,
  because this tunnel is meant to serve more than one app.

## 6. Register it with the overlay

Flux reads `clusters/dev/apps.yaml`, which points at `./apps/staging`, which
pulls in whatever is listed in `apps/staging/kustomization.yaml`. Nothing in
`apps/base` gets applied unless the overlay names it.

    echo "  - ../base/cloudflared" >> apps/staging/kustomization.yaml

## 7. Push

    git add apps/base/cloudflared apps/staging/kustomization.yaml
    git commit -m "feat(cloudflared): tunnel deployment"
    git push origin main

Then wait a minute and check:

    kubectl get pods -n cloudflared

## 8. Turn on Always Use HTTPS

Cloudflare dashboard, pick the domain, SSL/TLS, Edge Certificates, Always Use
HTTPS. It is off by default, and until it is on, plain HTTP gets served instead
of redirecting.

While on that screen, SSL/TLS Overview should be Full (strict).

---

# Part 2: adding an app

This is the part I do every time.

## 1. Point DNS at the tunnel

    cloudflared tunnel route dns homelab-kubecraft <app>.projectpattie.com

This creates the CNAME for me, so I never have to touch existing records in the
dashboard and risk editing the wrong one.

## 2. Give the app a Service if it does not have one

Linkding did not have one. It had only ever been reached with port-forward,
which works against a pod IP and breaks every time the pod is replaced.

    apiVersion: v1
    kind: Service
    metadata:
      name: <app>
      namespace: <app-namespace>
    spec:
      type: ClusterIP
      selector:
        app: <app>
      ports:
        - port: <port>
          targetPort: <port>

The selector is the part to get right. A Service finds pods by label, not by
name. ClusterIP is correct here because cloudflared is also inside the cluster,
so nothing needs exposing on the home network.

Add the file to that folder's `kustomization.yaml`, push, then check:

    kubectl get svc -n <app-namespace>
    kubectl get endpoints -n <app-namespace>

Endpoints is the real check. A Service can exist and point at nothing if its
selector does not match the pod labels. If endpoints is empty the tunnel
connects to a dead end, and the error looks like a tunnel problem when it is
not.

## 3. Add the ingress rule

In `apps/base/cloudflared/configmap.yaml`, above the catch-all:

    - hostname: <app>.projectpattie.com
      service: http://<svc>.<namespace>.svc.cluster.local:<port>

Rules are read top to bottom and the first match wins.

## 4. Push, restart, check

    git add apps/base/cloudflared/configmap.yaml
    git commit -m "feat(cloudflared): route <app>"
    git push origin main

Wait a minute for Flux, confirm the cluster has the new config:

    kubectl get configmap cloudflared -n cloudflared -o yaml | grep hostname

Then restart:

    kubectl rollout restart deployment/cloudflared -n cloudflared
    kubectl get pods -n cloudflared

Both pods should come back 1/1. That matters more than it looks: the readiness
probe hits cloudflared's `/ready` endpoint, which only returns 200 when it has
a live connection to Cloudflare's edge. 1/1 means the tunnel is up.

Then load `https://<app>.projectpattie.com`.

---

# Things that have already bitten me

**Subdomain depth.** I picked `links.lab.projectpattie.com` first and got
ERR_SSL_VERSION_OR_CIPHER_MISMATCH. Universal SSL covers the apex and one level
of subdomain, nothing deeper, so there was no certificate for it and the
handshake died before anything reached my cluster. The cluster was fine the
whole time. I flattened it to `links.projectpattie.com` and it worked
immediately. One level deep unless I pay for Advanced Certificate Manager.

**Short service names do not cross namespaces.** cloudflared lives in its own
namespace, so `http://linkding:9090` resolves to nothing. It needs the full
`http://linkding.linkding.svc.cluster.local:9090`. The course does not hit this
because everything there sits in one namespace.

**Changing the ConfigMap does not restart the pods.** Flux updates the
ConfigMap, the running pods keep whatever they mounted at startup, and nothing
appears to happen. Always `rollout restart` after a config change. The real fix
is Kustomize's `configMapGenerator`, which hashes the name so any change forces
a rollout on its own. Not done yet.

**The catch-all rule is not optional.** cloudflared refuses to start without a
final rule that has no hostname on it.

    - service: http_status:404

**Everything needs `-n`.** My context has no default namespace, so any command
without `-n` looks in `default` and finds nothing. The course never types it
because his context defaults to the namespace his app is in.

---

# Backing it out

Pull the rule from the ConfigMap, push, `rollout restart` cloudflared, and
delete the DNS record. The app goes back to being internal only.
