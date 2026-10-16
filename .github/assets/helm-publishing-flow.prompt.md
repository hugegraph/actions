# Helm pipeline illustration

Generated with the built-in image generation tool. Final asset: `helm-publishing-flow.png`.

## Original prompt

```text
Use case: infographic-diagram
Asset type: a generated illustration embedded in an open-source GitHub README.
Primary request: Create a crisp, polished horizontal infographic explaining the actual HugeGraph Helm validation and publication pipeline. Wide landscape, about 3:1 aspect ratio, readable when embedded 1100 pixels wide. White or very light neutral background, dark navy typography, restrained teal and blue accents, simple line illustrations of source code, a chart package, Kubernetes pods, an evidence folder and two registry endpoints. Consistent clean spacing and arrows. No brand logos or watermarks.
Composition: a left-to-right main flow with six numbered cards. Card 4 can be taller with compact test details. Card 6 branches into two equal destination pills but has a shared publisher card above the split. Include only the exact short text specified below, no extra prose.
Text (verbatim):
Title: "HugeGraph Helm: test once, publish the same package"
Card 1 heading: "1  Fix source"
Card 1 subtext: "Apache commit"
Card 2 heading: "2  Chart checks"
Card 2 subtext: "Isolated regression"
Card 3 heading: "3  Package once"
Card 3 subtext: "One .tgz"
Card 4 heading: "4  GitHub kind"
Card 4 subtext lines: "1 PD + 1 Store + 1 Server" / "Schema · CRUD · index · rollback" / "PVC · auth · independent reads" / "Restart pods · read back · clean up"
Card 5 heading: "5  Retain evidence"
Card 5 subtext: "Exact .tgz + SHA256"
Card 6 heading: "6  Publish"
Card 6 subtext lines: "One version lock" / "Preflight both registries"
Destination pill 1: "GHCR"
Destination pill 2: "Docker Hub"
Shared text below destination pills: "Authenticated + anonymous pulls"
Footer small note: "Partial failure: retry with the retained package"
Accuracy constraints: Show a single immutable .tgz flowing through tests and publishing. Do not suggest high availability, a 3+3+3 topology, automatic rollback, or a shared atomic transaction between registries. The restart applies to Store and Server pods. The six cards must be legible and correctly ordered, with clearly connected arrows. Spell HugeGraph, SHA256 and GHCR exactly.
```

## Correction prompt

```text
Use case: precise-object-edit
Edit target: the supplied HugeGraph Helm pipeline infographic.
Change only the bottom dashed arrow line and its two arrowheads: remove this entire dashed path, including the arrowhead below "Fix source" and arrowhead below "Docker Hub". Preserve the exact footer text "Partial failure: retry with the retained package" centered underneath as plain text without any arrow or connector. This avoids implying recovery rebuilds the source.
Keep every other pixel, card, text, icon, color, spacing and main forward arrow unchanged. Do not add any other elements.
```
