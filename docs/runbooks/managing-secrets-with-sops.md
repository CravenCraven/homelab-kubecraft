# When a secret only exists because I typed it

On 5 October I created `tunnel-credentials` by hand with `kubectl create
secret`, and wrote in my own runbook that it broke my rule: if it is not in
main, it is not in the cluster. Rebuild from the repo and the tunnel does not
come back.

On 6 and 7 October I fixed that. Every secret now lives in the repo, encrypted,
and Flux unlocks it on the way in. I also used the same setup to make Linkding
create its own admin user, so I never have to `exec` into the pod and run
`createsuperuser` again.

Writing this down because the next app I add will need a secret, and because
most of what I got wrong here was something I was sure about.

How it works, the way I think about it:

    my Mac (plain file) -> sops locks it -> GitHub -> Flux pulls it -> unlocks with sops-age -> Secret in cluster -> pod

There are two keys. The public one is in `.sops.yaml` and can only lock
things, so it is fine in Git. The private one is the `sops-age` secret in
`flux-system`, and it is the only thing that can unlock. That one never goes
near Git.

What is managed this way so far:

| Secret | Namespace | File | How the pod gets it |
|---|---|---|---|
| tunnel-credentials | cloudflared | `apps/base/cloudflared/secret.yaml` | mounted as a file |
| linkding-superuser | linkding | `apps/base/linkding/secret.yaml` | environment variables |

---

# Part 1: the setup I did once

All of this already exists. It is here for a rebuild or a second cluster.

## Where to run these

On the Mac, in `~/Developer/homelab-kubecraft`. SOPS finds `.sops.yaml` by
looking in the folder I am standing in, so if I am in the wrong folder it has
no rules and refuses to encrypt.

## 1. Tools

    brew install sops age

## 2. Make the key pair

    age-keygen -o <path-to-age-key>

It prints the public key, which starts with `age1`. The file it writes is the
private key. It stays on the Mac and does not go in the repo.

## 3. The rules file

`.sops.yaml` at the root of the repo:

    creation_rules:
      - path_regex: .*\.yaml
        encrypted_regex: ^(data|stringData)$
        age: <age-public-key>

`path_regex` is which files this applies to, so any YAML. `encrypted_regex` is
which fields get locked, so only the values under `data` and `stringData`.
`kind`, `name` and `namespace` stay readable on purpose. Flux has to know what
the object is before it can unlock it.

## 4. Give the cluster the private key

    kubectl create secret generic sops-age -n flux-system --from-file=age.agekey=<path-to-age-key>

This is still a hand-made secret. It has to be. It is the key to everything
else, so something has to exist before Git can take over.

    kubectl get secret sops-age -n flux-system

## 5. Tell Flux to unlock

In `clusters/dev/apps.yaml`, on the `apps` Kustomization:

    decryption:
      provider: sops
      secretRef:
        name: sops-age

I had to work out which Kustomization this goes on, because I have three and
they are chained across two repos:

| Kustomization | Repo | What it applies |
|---|---|---|
| flux-system | homelab (Brazil) | the pointer file |
| flux-system-kubecraft | homelab-kubecraft | `./clusters/dev` |
| apps | homelab-kubecraft | `./apps/staging`, so every app folder |

Decryption is not inherited. A parent having it does not give it to the
children. It has to sit on the one that actually applies the secret files, and
all of mine are under `apps/`, so it is `apps`. The course puts it on
`flux-system` because his repo only has one.

To check it is there:

    kubectl get kustomization apps -n flux-system -o yaml | grep -A4 decryption

Nothing printed means it is not.

---

# Part 2: adding a secret

This is the part I do every time. The setup above is once. Encrypting is for
every single new secret file. I got that wrong on day two.

## 1. Find the app's folder

    grep -rl <app> apps

My first instinct was to put the secret at the root of the repo so it would
apply to everything. That is not how secrets work. A Secret belongs to one
namespace and only pods in that namespace can read it. It goes in the app's
own folder.

## 2. Write it in plain text

`apps/base/<app>/secret.yaml`:

    apiVersion: v1
    kind: Secret
    metadata:
      name: <secret-name>
      namespace: <app-namespace>
    type: Opaque
    stringData:
      <KEY_NAME>: <value>
      <PASSWORD_KEY>: "<password>"

- The name is lowercase, letters, numbers and hyphens. I tried one with a space
  in it first. Kubernetes will not take it.
- The namespace has to match the app's, or the app cannot see the secret.
- `stringData` lets me type plain values. `data` would mean base64-ing
  everything myself.
- Quote anything with a `:` or `#` in it.

To check it without the password on my screen:

    grep -v PASSWORD apps/base/<app>/secret.yaml

**Not committing yet.** Right now it is a password in a text file.

## 3. Lock it

    sops --encrypt --in-place apps/base/<app>/secret.yaml

No output means it worked. Then:

    grep -c "ENC\[" apps/base/<app>/secret.yaml

That counts the locked lines. It should be the number of values plus one,
because SOPS adds its own seal to catch tampering. Linkding has two values and
showed 3.

## 4. List it

Add `secret.yaml` under `resources:` in `apps/base/<app>/kustomization.yaml`.
Flux does not apply a file just because it is in the folder.

## 5. Point the app at it

Depends what the app wants.

Linkding wants **environment variables**. In the container, lined up with
`ports:`:

    envFrom:
      - secretRef:
          name: <secret-name>

Every key in the secret becomes a variable with the same name. I named mine
`LD_SUPERUSER_NAME` and `LD_SUPERUSER_PASSWORD` because that is exactly what
Linkding looks for, so one block covers both.

cloudflared wants **a file**. A volume from the secret, mounted into the
container:

    volumes:
    - name: <volume-name>
      secret:
        secretName: <secret-name>

The key name becomes the filename in the mount folder.

## 6. Check it builds

    kubectl kustomize apps/base/<app> | grep -A3 envFrom

This builds the folder the way Flux will, without touching the cluster. Bad
indentation fails here instead of in a pod. It sorts fields alphabetically, so
`envFrom` showing up above `image` is fine.

## 7. Commit only those files

    git status
    git add apps/base/<app>/secret.yaml apps/base/<app>/kustomization.yaml apps/base/<app>/deployment.yaml
    git commit -m "feat(<app>): <what this change actually does>"
    git push

By name, not `git add .`. And a message about this change, not the last one.

## 8. Apply and prove it

    flux reconcile kustomization apps --with-source

Then three checks:

    kubectl get pods -n <app-namespace>
    kubectl exec deploy/<app> -n <app-namespace> -- printenv <KEY_NAME>
    kubectl logs deploy/<app> -n <app-namespace>

New pod running, the variable made it into the container, and the app's own
log says what it did with it. I only ever `printenv` the username. Never the
password.

---

# Part 3: taking over a secret I made by hand

What I did with `tunnel-credentials`.

1. Write and lock a file with the **same name and namespace** as the one in
   the cluster, using Part 2.
2. Push and reconcile. Flux updates the existing secret in place.
3. Delete it and let Flux bring it back:

       kubectl delete secret <secret-name> -n <namespace>
       flux reconcile kustomization apps --with-source
       kubectl get secrets -n <namespace>

   Mine came back at 38 seconds old. That is Git rebuilding it with nothing
   from me.

4. Restart the pods:

       kubectl rollout restart deployment <app> -n <namespace>
       kubectl get pods -n <namespace>

   Running pods keep the copy they loaded when they started. Until they
   restart, I have not actually proven the new secret works.

5. Load the app in the browser.

---

# Changing a value later

    sops apps/base/<app>/secret.yaml

Opens it unlocked in my editor and locks it again when I save. Then commit,
push, reconcile, restart.

---

# How the course solution did it differently

Same end result in the cluster. Different ways of getting there. Writing down
the why for each so I know which to reach for.

## 1. Writing the secret file

The course generates it:

    kubectl create secret generic <name> -n <namespace> --from-literal=<KEY>=<value> --dry-run=client -o yaml > secret.yaml

I typed mine in vim.

- **How his works.** `--dry-run=client` builds the secret on my laptop and
  stops before sending it to the cluster. `-o yaml` prints it as YAML. `>`
  saves that into a file. kubectl fills in the form for me.
- **Why it matters.** A tool wrote it, so no typos and no broken indentation.
  That would have saved me the vim paste mess. Typing it myself is how I
  learned what each line is for.
- **The catch.** `--from-literal` puts the password on the command line, so it
  ends up in my shell history. Typing it into the file does not.
- **Because:** generating YAML avoids typing mistakes, writing it by hand
  taught me the structure, and I keep passwords off the command line.

This works for nearly anything, not just secrets. It is how to get a correct
skeleton for a deployment or configmap without typing it.

## 2. `data` vs `stringData`

kubectl writes the values under `data`, already base64. I used `stringData`,
plain text.

- **How it works.** `data` expects everything already encoded. `stringData`
  takes it as typed and Kubernetes does the encoding when it stores it. Once
  it is in the cluster they are identical.
- **Why it matters.** Nobody hand-types base64. When I write it myself,
  `stringData` is the practical one.
- **Because:** both store the same thing. `stringData` is for people typing,
  `data` is what tools produce.

## 3. Telling SOPS what to encrypt

The course puts the rules on the command every time:

    sops --age=<age-public-key> --encrypt --encrypted-regex '^(data|stringData)$' --in-place <file>

Mine are saved in `.sops.yaml`, so my command is only:

    sops --encrypt --in-place <file>

- **How it works.** `--age` is which key locks it. `--encrypted-regex` is which
  fields get locked. `.sops.yaml` holds the same two settings, and SOPS reads it
  when I run it from the repo folder.
- **Why it matters.** Same key, same fields, same result. His way means
  remembering a long key and a regex every time. Forget the regex once and SOPS
  locks the whole file, `kind` and `name` included, and Flux cannot tell it is a
  Secret anymore. Mine is written down once in the repo, so anyone who clones it
  encrypts the same way.
- **Because:** the rules live in `.sops.yaml` so every secret gets locked the
  same way and nobody has to remember the flags. That suits me, since
  remembering long commands is the hard part.

## 4. The `configMapRef` line

The course loads two things into Linkding:

    envFrom:
      - configMapRef:
          name: <configmap-name>
      - secretRef:
          name: <secret-name>

I only load the secret.

- **How it works.** A ConfigMap holds settings that are not sensitive, like a
  timezone. A Secret holds the ones that are. `envFrom` can load both, and every
  key becomes an environment variable.
- **Why it matters.** The course already had a Linkding ConfigMap from an
  earlier step. My Linkding folder does not have one, so there was nothing to
  load. Not a missed step.
- **Because:** ConfigMaps for normal settings, Secrets for sensitive ones. I
  only had sensitive ones.

## 5. Proving it worked

The course solution shows the files and commands. I also proved Git owns the
secret.

- **How.** Deleted it and watched it come back at 38 seconds old. Restarted the
  pods, because they keep the copy they loaded at startup. Checked `printenv`
  and the logs.
- **Why it matters.** "The file is in Git" and "Git is where the secret comes
  from" are not the same thing. Until I delete and rebuild, the old hand-made
  one could still be doing the work.
- **Because:** I do not trust a secret is Git-managed until I have deleted it
  and watched Flux bring it back.

## Why environment variables at all

The course links the twelve-factor app's page on config. The idea is that
anything that changes between environments, passwords, URLs, usernames, goes
in environment variables, not baked into the app or the image. Same image,
different secret, different environment. That is the reason `envFrom` exists.

---

# What I do not do

- Commit a secret file before it is locked.
- `cat` or paste a secret file that might be unlocked. `grep -v` or the
  `ENC[` count tells me what I need.
- `printenv` a password.
- Put the private key anywhere near Git.

---

# Things that have already bitten me

**I thought setting up SOPS covered every file.** The night before, I
encrypted the cloudflared secret. The next morning I wrote the Linkding one
and asked "didn't I do encryption yesterday?" I did. For a different file. One
lock, but every new box gets locked separately.

**The secret said it was 41 hours old after Flux applied it.** I expected it
to be new. Flux found one with the same name already there and updated it in
place, and AGE counts from when it was created, not when it last changed. Like
repainting a house: the year it was built stays the same. The only way to know
Git owned it was to delete it and watch it come back.

**My grep found nothing in a file I knew had a name in it.** I searched for
`^  name:`, two spaces. SOPS had rewritten the file with four. Now I search
`grep -nE "name:|namespace:"` with no spacing at all.

**`git log --file` is not `git log -- file`.** The lone `--` with a space means
"options are done, a file comes next". Without the space, git thinks it is an
option and gives up. I also typed `secert`.

**Pasting into vim wrecked the indentation.** `envFrom` landed under `ports`
and the next two lines slid to the left edge. `:set paste` only helps if it is
on before I paste, not after. What fixed it was `<<` and `>>` in command mode,
which move a whole line left or right without retyping it.

**Linkding would not create my user.** The log said "Skip creating initial
superuser, user already exists". It was right. Those variables only create
the user on a fresh install with an empty database. Changing the password in
the secret will not change my existing login.

**I reused last night's commit message.** The Linkding commit says "encrypt
tunnel credentials with age". It is already on main and I am not force-pushing
to fix a message. It stays.

**Still not explained.** When I restarted cloudflared, `get pods` showed no
pods at all for a moment. It has 2 replicas and no strategy set, so it should
swap them one at a time. It came back fine within a minute, but I want to read
the events next time instead of shrugging.

---

# Backing it out

Take `secret.yaml` out of the app's `kustomization.yaml` and push. `apps` has
`prune: true`, so Flux deletes the secret from the cluster. Take `envFrom` or
the volume out of the deployment in the same commit, or the pod will sit there
failing to start, looking for a secret that is gone.
