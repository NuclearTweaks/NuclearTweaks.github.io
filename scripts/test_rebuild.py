#!/usr/bin/env python3
"""Offline test for rebuild.py: fake tweak repos + real debs, no GitHub calls."""
import json, os, shutil, subprocess, sys, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebuild  # noqa: E402

WORK = os.environ.get("SCRATCH", "/tmp") + "/apt-rebuild-test"
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)

# --- build two synthetic device debs (one normal, one prerelease/beta) --------
def make_deb(pkg, name, version, desc, path):
    d = os.path.join(WORK, pkg.replace(".", "_"))
    os.makedirs(os.path.join(d, "var/jb/Library/MobileSubstrate/DynamicLibraries"), exist_ok=True)
    with open(os.path.join(d, "DEBIAN_control"), "w") as f:
        f.write(f"""Package: {pkg}
Name: {name}
Version: {version}
Architecture: iphoneos-arm64
Depends: mobilesubstrate, firmware (>= 15.0)
Maintainer: Spec
Author: Spec
Description: {desc}
 More detail line.
Section: Tweaks
""")
    shutil.copy("/bin/echo", os.path.join(d, "var/jb/Library/MobileSubstrate/DynamicLibraries/x.dylib"))
    layout = os.path.join(d, "stage")
    shutil.copytree(os.path.join(d, "var"), os.path.join(layout, "var"))
    os.makedirs(os.path.join(layout, "DEBIAN"))
    shutil.copy(os.path.join(d, "DEBIAN_control"), os.path.join(layout, "DEBIAN/control"))
    subprocess.run(["dpkg-deb", "-Zxz", "-b", layout, path], check=True, capture_output=True)

make_deb("com.nucleartweaks.jbhellotweak", "JBHelloTweak", "1.0.0",
         "Proof-of-life tweak.", os.path.join(WORK, "jb_1.0.0_iphoneos-arm64.deb"))
make_deb("com.nucleartweaks.betaonly", "BetaOnly", "0.2.0",
         "Beta thing.", os.path.join(WORK, "beta_0.2.0_iphoneos-arm64.deb"))

# --- monkeypatch the GitHub layer --------------------------------------------
releases = {
    "JBHelloTweak": [{"tag_name": "v1.0.0", "prerelease": False,
                      "assets": [{"name": "jb_1.0.0_iphoneos-arm64.deb"}]}],
    "BetaTweak":    [{"tag_name": "v0.2.0", "prerelease": True,
                      "assets": [{"name": "beta_0.2.0_iphoneos-arm64.deb"}]}],
    "EmptyTweak":   [],
}
rebuild.list_tweak_repos = lambda: ["JBHelloTweak", "BetaTweak", "EmptyTweak"]
rebuild.latest_release = lambda repo: (releases[repo][0] if releases.get(repo) else None)
def fake_dl(repo, tag, asset, dest):
    src = os.path.join(WORK, asset)
    shutil.copy(src, os.path.join(dest, asset))
    return os.path.join(dest, asset)
rebuild.download_deb = fake_dl

OUT = os.path.join(WORK, "repo")
os.makedirs(OUT)
tmp = os.path.join(OUT, ".rebuild-tmp"); os.makedirs(tmp)
comps = rebuild.collect(tmp, OUT)
shutil.rmtree(tmp, ignore_errors=True)
present = rebuild.write_dists(OUT, comps, dry=False)
rebuild.write_index(OUT, comps, present, dry=False)

# --- assertions ----------------------------------------------------------------
pkgfile = os.path.join(OUT, "dists/nucleartweaks/release/binary-iphoneos-arm64/Packages")
rel = os.path.join(OUT, "dists/nucleartweaks/Release")
beta = os.path.join(OUT, "dists/nucleartweaks/beta/binary-iphoneos-arm64/Packages")
text = open(pkgfile).read()
assert "Package: com.nucleartweaks.jbhellotweak" in text
assert "Filename: https://nucleartweaks.github.io/pool/release/jb_1.0.0_iphoneos-arm64.deb" in text
assert " More detail line." in text, "multiline description preserved"
assert "SHA256:" in text and "MD5sum:" in text
assert open(beta).read().count("Package:") == 1 and "betaonly" in open(beta).read()
assert os.path.exists(os.path.join(OUT, "pool/release/jb_1.0.0_iphoneos-arm64.deb"))
assert os.path.exists(os.path.join(OUT, "pool/beta/beta_0.2.0_iphoneos-arm64.deb"))
reltxt = open(rel).read()
assert "Architectures: iphoneos-arm64" in reltxt
assert "Components: release beta" in reltxt
assert " release/binary-iphoneos-arm64/Packages" in reltxt
# checksum verify against real bytes
import hashlib
for line in reltxt.split("SHA256:")[1].split("\n")[1:]:
    if not line.strip(): continue
    if line.startswith(" "):
        h, size, path = line.split()
        actual = hashlib.sha256(open(os.path.join(OUT, "dists/nucleartweaks", path), "rb").read()).hexdigest()
        assert actual == h, path
    else:
        break
idx = open(os.path.join(OUT, "index.html")).read()
assert "JBHelloTweak" in idx and "sileo://source" in idx
assert not os.path.exists(tmp)
print("ALL ASSERTIONS PASSED")
print("--- Packages (release) head ---")
print("\n".join(text.splitlines()[:14]))
