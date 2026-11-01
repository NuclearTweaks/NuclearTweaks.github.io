# NuclearTweaks APT repository

Served by GitHub Pages at **https://nucleartweaks.github.io/** — add that URL in
Sileo/Cydia on a jailbroken rootless iOS 15+ device.

- **Never hand-edit** `dists/`, `pool/`, or `index.html` — everything is generated
  by `scripts/rebuild.py` from GitHub Releases of org repos tagged
  `nucleartweaks-tweak` (newest release each; prerelease → `beta` component).
- Rebuild triggers: `repository_dispatch` from tweak publishes, daily at 04:17 UTC,
  or manual `workflow_dispatch`.
- Requires `GH_TOKEN` (secret on this repo) to read private tweak repos.
- **Unsigned** (no InRelease/GPG) by design for now — public-release milestone
  adds signing. Sileo shows an untrusted-source warning meanwhile.
- Layout is the legacy component layout (`dists/<dist>/<comp>/binary-<arch>/`)
  because it is the one format Cydia, Sileo, Zebra and Installer all read.
- `Release` declares `Architectures: iphoneos-arm64` — rootless only, by design.
