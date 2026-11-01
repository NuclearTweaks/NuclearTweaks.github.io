#!/usr/bin/env python3
"""Rebuild the NuclearTweaks APT/Cydia/Sileo repo served by GitHub Pages.

Source of truth = GitHub Releases of org repos tagged with `nucleartweaks-tweak`
(only the newest release per repo; prereleases land in the `beta` component).
Emits the legacy component layout that Cydia, Sileo, Zebra and Installer all read:

  pool/<component>/<pkg>_<ver>_<arch>.deb
  dists/nucleartweaks/<component>/binary-iphoneos-arm64/Packages{,.gz,.xz}
  dists/nucleartweaks/Release          (unsigned: GPG deferred — see repo README)
  index.html                           (install links + package list)

Requires `gh` authenticated (GH_TOKEN) to see private tweak repos.
"""
import argparse, gzip, hashlib, html, io, json, lzma, os, re, shutil, subprocess, sys, tarfile
from email.utils import formatdate

ORG     = os.environ.get("APT_ORG",   "NuclearTweaks")
REPO    = os.environ.get("APT_REPO",  "NuclearTweaks.github.io")
TOPIC   = os.environ.get("APT_TOPIC", "nucleartweaks-tweak")
DIST    = os.environ.get("APT_DIST",  "nucleartweaks")
ARCH    = "iphoneos-arm64"
APT_URL = os.environ.get("APT_URL",   f"https://{ORG.lower()}.github.io/")

def gh(*args, check=True):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=check)

def list_tweak_repos():
    r = gh(f"/orgs/{ORG}/repos?per_page=100", "--paginate",
           "--jq", f'.[] | select(.topics // [] | index("{TOPIC}")) | .name')
    return sorted(set(r.stdout.split()))

def latest_release(repo):
    r = gh(f"repos/{repo}/releases?per_page=1", check=False)
    if r.returncode:
        return None
    rel = json.loads(r.stdout)          # newest first, includes prereleases
    return rel[0] if rel else None

def download_deb(repo, tag, asset, dest_dir):
    subprocess.run(["gh", "release", "download", tag, "-R", repo,
                    "-n", asset, "-D", dest_dir, "--clobber"], check=True)
    return os.path.join(dest_dir, asset)

def ar_entries(path):
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ValueError("not a .deb (no ar magic)")
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                return
            name = hdr[0:16].decode(errors="replace").strip().rstrip("/")
            size = int(hdr[48:58].decode().strip())
            data = f.read(size)
            f.read(size % 2)
            yield name, data

def control_from_deb(path):
    for name, data in ar_entries(path):
        if name.startswith("control.tar"):
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r")
            member = "./control" if "./control" in tf.getnames() else "control"
            m = tf.extractfile(member)
            if m is None:
                raise ValueError("control.tar has no control member")
            return m.read().decode("utf-8")
    raise ValueError("no control.tar in deb")

FIELD_RE = re.compile(r"(?m)^(?=[A-Za-z][A-Za-z0-9-]*:)")

def split_fields(control):
    """-> [(field, raw value incl. multi-line description)] preserving order."""
    out = []
    for block in FIELD_RE.split(control):
        block = block.rstrip("\n")
        if not block.strip():
            continue
        name, _, rest = block.partition(":")
        out.append((name.strip(), rest.strip(" \n")))
    return out

def hashes(path):
    h = {a: hashlib.new(a) for a in ("md5", "sha1", "sha256")}
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for x in h.values():
                x.update(chunk)
    return h, os.path.getsize(path)

def collect(tmp, out):
    components = {"release": [], "beta": []}
    seen = {}
    repos = list_tweak_repos()
    print(f"tweak repos ({len(repos)}): {', '.join(repos) or 'NONE'}")
    for repo in repos:
        rel = latest_release(repo)
        if not rel:
            print(f"  {repo}: no GitHub Release yet — skipped")
            continue
        debs = [a["name"] for a in rel["assets"] if a["name"].endswith(".deb")]
        if not debs:
            print(f"  {repo}: release {rel['tag_name']} has no .deb asset — skipped")
            continue
        preferred = [d for d in debs if f"_{ARCH}.deb" in d] or debs
        comp = "beta" if rel.get("prerelease") else "release"
        path = download_deb(repo, rel["tag_name"], preferred[0], tmp)
        fields = split_fields(control_from_deb(path))
        fdict = dict(fields)
        pkg, ver = fdict.get("Package"), fdict.get("Version")
        if not pkg or not ver:
            print(f"  !! {repo}: deb missing Package/Version — skipped")
            continue
        if pkg in seen:
            print(f"  !! {repo}: Package {pkg} already provided by {seen[pkg]} — skipped (Package ids are forever; fix one repo)")
            continue
        seen[pkg] = repo
        if fdict.get("Architecture") != ARCH:
            print(f"  !! {repo}: Architecture '{fdict.get('Architecture')}' != {ARCH} (Sileo will refuse it)")
        h, size = hashes(path)
        pool_rel = f"pool/{comp}/{os.path.basename(path)}"
        dst = os.path.join(out, pool_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst) or os.path.getsize(dst) != size:
            shutil.copyfile(path, dst)
        stanza = list(fields) + [
            ("Filename", APT_URL.rstrip("/") + "/" + pool_rel),
            ("Size", str(size)), ("MD5sum", h["md5"].hexdigest()),
            ("SHA1", h["sha1"].hexdigest()), ("SHA256", h["sha256"].hexdigest()),
        ]
        text = "\n".join(f"{n}: {v}" for n, v in stanza)
        components[comp].append((pkg.lower(), text, fdict))
        print(f"  + {repo} {pkg} {ver} -> {comp}")
    return components

def write_dists(out, components, dry):
    distdir = os.path.join(out, "dists", DIST)
    present = []
    files = []  # (relpath, bytes) for Release checksums
    for comp in ("release", "beta"):
        entries = components[comp]
        cdir = os.path.join(distdir, comp, f"binary-{ARCH}")
        if not entries:
            if os.path.isdir(cdir) and not dry:
                shutil.rmtree(os.path.join(distdir, comp))
            continue
        present.append(comp)
        body = ("\n".join(t for _, t, _ in sorted(entries)) + "\n\n").encode()
        raw = [("Packages", body),
               ("Packages.gz", gzip.compress(body, 9)),
               ("Packages.xz", lzma.compress(body))]
        for fn, data in raw:
            files.append((f"{comp}/binary-{ARCH}/{fn}", data))
            if not dry:
                os.makedirs(cdir, exist_ok=True)
                with open(os.path.join(cdir, fn), "wb") as f:
                    f.write(data)
    comp_files = [f for f in files if f[0].endswith("/Packages")]  # checksum all variants
    def block(label, algo):
        lines = [f"{label}:"]
        for relp, data in sorted(files):
            hh = hashlib.new(algo, data).hexdigest()
            lines.append(f" {hh} {len(data)} {relp}")
        return "\n".join(lines)
    release = "\n".join([
        "Origin: NuclearTweaks",
        "Label: NuclearTweaks",
        f"Suite: {DIST}",
        f"Codename: {DIST}",
        "Version: 1.0",
        f"Date: {formatdate(usegmt=True)}",
        f"Architectures: {ARCH}",
        f"Components: {' '.join(present)}",
        "Description: NuclearTweaks - jailbreak tweaks for iOS 27 (rootless). Unsigned beta repo: enable untrusted in Sileo.",
        block("MD5Sum", "md5"), block("SHA1", "sha1"), block("SHA256", "sha256"), "",
    ]).encode()
    if not dry:
        os.makedirs(distdir, exist_ok=True)
        with open(os.path.join(distdir, "Release"), "wb") as f:
            f.write(release)
    return present

def write_index(out, components, present, dry):
    cards = []
    for comp in present:
        for _, _, f in sorted(components[comp]):
            dep = f.get("Depiction") or (APT_URL.rstrip("/") + "/depictions/" + f.get("Package", ""))
            cards.append(
                "<div class=c><div class=t>{n} <span class=v>{v}</span></div>"
                "<div class=d>{desc}</div><div class=m>by {au} &middot; {comp}</div></div>".format(
                    n=html.escape(f.get("Name", f.get("Package", "?"))),
                    v=html.escape(f.get("Version", "?")),
                    desc=html.escape((f.get("Description", "") or "").split("\n")[0]),
                    au=html.escape(f.get("Author", "NuclearTweaks")),
                    comp=comp))
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>NuclearTweaks repo</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#0d0d10;color:#e8e8ea;margin:0;padding:2rem}}
.wrap{{max-width:720px;margin:auto}}h1{{margin:0 0 .25rem}}.sub{{color:#9a9aa2;margin-bottom:1.5rem}}
.btn{{display:inline-block;background:#2f6fed;color:#fff;padding:.55rem 1rem;border-radius:10px;text-decoration:none;margin:.2rem .4rem .2rem 0}}
.c{{background:#17171c;border:1px solid #26262e;border-radius:12px;padding:1rem;margin:.6rem 0}}
.t{{font-weight:600}}.v{{color:#8f8f98;font-weight:400;font-size:.9em}}
.d{{color:#c2c2ca;margin:.35rem 0;font-size:.95em}}.m{{color:#77777f;font-size:.85em}}
.note{{color:#b58a2e;font-size:.85em;margin:1rem 0}}</style></head><body><div class=wrap>
<h1>NuclearTweaks</h1><div class=sub>Jailbreak tweaks for iOS 27 &mdash; rootless (jailbreak required to install)</div>
<a class=btn href="sileo://source/{APT_URL}">Add in Sileo</a>
<a class=btn href="cydia://url/https://cydia.saurik.com/api/#add?source={APT_URL}">Add in Cydia</a>
<a class=btn href="https://github.com/{ORG}" style="background:#333">GitHub</a>
<div class=note>Unsigned repository (beta) &mdash; Sileo: enable &ldquo;Unknown Sources&rdquo;/trust prompt; Cydia: source will show a warning. GPG signing lands with the keys milestone.</div>
{''.join(cards) or '<div class=c>No published tweaks yet.</div>'}
<p class=note>Sileo source URL: <code>{APT_URL}</code> &middot; built {formatdate(usegmt=True)}</p>
</div></body></html>
"""
    if not dry:
        with open(os.path.join(out, "index.html"), "w") as f:
            f.write(page)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    tmp = os.path.join(out, ".rebuild-tmp")
    os.makedirs(tmp, exist_ok=True)
    try:
        components = collect(tmp, out if not args.dry_run else tmp)
        present = write_dists(out, components, args.dry_run)
        write_index(out, components, present, args.dry_run)
        print(f"components: {', '.join(present) or 'none'} (dry={args.dry_run})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
