# Helm pipeline illustration

Generated with the built-in image generation tool. Final asset: `helm-publishing-flow.png`.

## Generation prompt

```text
Use case: infographic-diagram
Asset type: generated illustration for the HugeGraph open-source GitHub README.
Primary request: Replace the existing workflow diagram with a much simpler, polished horizontal overview. Only THREE main stages: "Package once" → "Kubernetes validation" → "Publish same package". No source steps, no chart-check steps, no evidence-storage stage. Wide landscape around 2.8:1, white background, navy typography, restrained teal/blue accents. Large readable English text, clean consistent spacing, minimal icons. Central validation stage is largest and is the visual focus.
Layout: three large cards left to right, connected by clear right-pointing arrows. Left card simple .tgz package illustration. Central card a small Kubernetes-style outline enclosing three pod icons labeled PD, Store, Server, and three compact check lines below. Right card the same .tgz package branching to two equal pill-shaped destinations GHCR and Docker Hub. No logo required. Avoid extra decoration and use fewer icons than the original.
Text (verbatim, do not invent other labels):
Title: "HugeGraph Helm"
Left card heading: "Package once"
Left card subtext: "One .tgz"
Center card heading: "Kubernetes validation"
Center card topology: "1 PD + 1 Store + 1 Server"
Center check lines:
"Schema · vertex/edge CRUD · index · rollback"
"PVC · auth · independent reads"
"Recreate Store/Server · verify persisted data · cleanup"
Right card heading: "Publish same package"
Right destinations: "GHCR" and "Docker Hub"
Right compact note: "Preflight both · verify pulls"
Footer optional, if room: "Retry with original package"
Accuracy: identical package is tested then distributed to both destinations. Do not imply high availability or an atomic transaction across registries. Do not include "Fix source", "Chart checks", or "Retain evidence". No loops or arrows pointing back to package creation. No watermark. All text must be spelled exactly and readable in a GitHub README.
```

## Final refinement prompt

```text
Use case: infographic-diagram
Edit target: the supplied three-stage HugeGraph Helm infographic. Refine only the labels and balance of supporting text to emphasize THREE proof claims. Preserve its three-card layout, white background, navy/teal/blue palette, package icons, forward arrows, PD/Store/Server icons and GHCR/Docker Hub destination branches. Keep all English text readable, crisp and unclipped. You may increase height slightly if needed but retain wide landscape approximately 2.7:1.
Replace the current labels with exactly these:
Overall title: "HugeGraph Helm"
Left card heading: "Package once"
Left card proof callout, prominent beneath icon: "SAME TESTED BYTES"
Left card supporting line: "One .tgz for install and push"
Center card heading: "Real Kubernetes validation"
Center topology line: "1 PD + 1 Store + 1 Server"
Center pod labels stay "PD", "Store", "Server"
Center proof callout, prominent below the pod diagram: "DATA SURVIVES POD RECREATION"
Center supporting lines (small but legible):
"Schema · vertex/edge CRUD · index · rollback"
"Recreate Store + Server → read back → cleanup"
Right card heading: "GHCR + Docker Hub"
Right destinations stay "GHCR" and "Docker Hub"
Right proof callout, prominent below destination branches: "BOTH REGISTRIES MATCH"
Right supporting lines:
"One version lock · preflight both"
"Authenticated + anonymous pulls · SHA256"
Footer: "Retry with original package"
Remove all superseded text, including the old detailed laundry list of tests and the old "Preflight both · verify pulls" line. The key three proof callouts should be visually stronger than supporting lines, while staying inside their card. No other new text. No recovery loops, no arrows back to package creation, no claim of atomic cross-registry transaction, no high-availability implication, no watermark.
```
