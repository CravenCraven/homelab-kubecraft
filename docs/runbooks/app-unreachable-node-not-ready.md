# When an app goes down and the replacement pod will not start

On 5 October I found Linkding down. One pod had been sitting in Terminating,
its replacement had been Pending for over five hours, and biggie-smalls was
NotReady. It looked like three separate problems. It was one.

Writing this down because I will hit it again, and because the fix turned out
to be embarrassingly simple once I understood what I was looking at.

---

## What I ran

**Where was the pod, and are all the nodes up?**

    kubectl get pods -n <namespace> -o wide
    kubectl get nodes

`-o wide` adds a NODE column. The stuck pod was on biggie-smalls, which was the
node showing NotReady. That is the first hint the two are related.

**Why will the new pod not schedule?**

    kubectl describe pod <pending-pod> -n <namespace>

The Events section at the bottom is where the answer lives. Mine said:

    0/3 nodes are available:
      1 node(s) had untolerated taint(s),
      2 node(s) didn't match PersistentVolume's node affinity.

That one line is the whole diagnosis. The scheduler tried all three nodes and
explained why each one failed. One node was tainted (biggie-smalls, because it
had gone quiet). The other two were rejected because of the volume.

**What is wrong with the node?**

    kubectl describe node <node>

Two things to read:

- Conditions. Mine were all `Unknown` with "Kubelet stopped posting node
  status". `Unknown` and `False` mean different things. `False` is a node that
  is talking and reporting a problem. `Unknown` is a node that stopped talking
  at all, so it is asleep, powered off, or off the network.
- Taints. `node.kubernetes.io/unreachable:NoSchedule` and `:NoExecute`.
  Kubernetes put those there by itself when the node went quiet. Nobody typed
  them. `NoSchedule` blocks new pods, `NoExecute` evicts what is already there.
- `RenewTime` under Lease is the last heartbeat. Mine was 11:32 UTC, which is
  07:32 my time, which is roughly when I would have shut the laptop.

---

## Why it was one fault and not three

1. biggie-smalls stopped reporting.
2. Five minutes later the pod's default tolerations ran out, so Kubernetes
   marked it for eviction and it went to Terminating.
3. It could not finish terminating. Deleting a pod means waiting for the
   kubelet on that node to confirm the container is actually gone, and there
   was no kubelet answering.
4. The Deployment made a replacement, which could not schedule anywhere because
   of where the volume lives.

---

## The bit I had not internalised

This cluster uses k3s local-path storage. "Local" is literal. The volume is a
folder on one specific machine's disk, under
`/var/lib/rancher/k3s/storage/<pvc>`. The PersistentVolume carries a node
affinity rule saying "only put pods using me on the node where my data sits".

So an app with a local-path PVC can only ever run on that one node. The other
two nodes being healthy and idle does not help at all. Node down means app
down.

This is the trade-off already written in my README under design decisions. I
just had not felt it yet.

---

## The fix

Wake the node up.

That is it. Once the kubelet checked in, Kubernetes sorted the rest out with no
input from me: the taints cleared, the Terminating pod finished deleting, and
the Pending pod got scheduled.

    kubectl get nodes
    kubectl get pods -n <namespace>

The replacement pod was still the same one that had been Pending all morning,
not a fresh one. The scheduler had been retrying for seven hours and placed it
the second it had somewhere to put it. That is the control loop doing exactly
what it says on the tin.

---

## Stopping it happening again

- biggie-smalls is a laptop and still sleeps on lid close. craventhegreat got
  the logind config and masked sleep targets treatment and biggie-smalls has
  not. That is the actual fix.
- Worth thinking about which apps should carry a nodeSelector so they only land
  on nodes that stay awake.
- Shared storage removes the single-node pinning entirely. On the roadmap,
  nowhere near done.

---

## Node impact

| Node | Sleeps? | What goes down with it |
|---|---|---|
| craventhegreat | no, sleep disabled | the API. Running pods keep going, nothing new schedules |
| dell-node | no | anything with a PVC on its disk |
| biggie-smalls | yes, not fixed yet | anything with a PVC on its disk, which includes linkding |
