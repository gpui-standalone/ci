#!/usr/bin/env python3
import argparse, json, os, subprocess, shutil, time, urllib.request
from pathlib import Path
import tomlkit

SUFFIX = "-standalone"
ZED_REPO = "https://github.com/zed-industries/zed"

def metadata(zed):
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=zed, capture_output=True, text=True, check=True,
    ).stdout
    return {p["name"]: p for p in json.loads(out)["packages"]}

def closure(pkgs, root):
    order, seen = [], set()

    def visit(name):
        if name in seen or name not in pkgs:
            return
        seen.add(name)
        for d in pkgs[name]["dependencies"]:
            if d["kind"] != "dev" and d.get("path") and d["name"] in pkgs:
                visit(d["name"])
        order.append(name)

    visit(root)
    return order

def renamed(name, root):
    return f"{root}{SUFFIX}" if name == root else f"{name}-{root}{SUFFIX}"

def index_versions(name):
    n = name.lower()
    d = {1: f"1/{n}", 2: f"2/{n}", 3: f"3/{n[0]}/{n}"}.get(len(n), f"{n[:2]}/{n[2:4]}/{n}")
    try:
        body = urllib.request.urlopen(f"https://index.crates.io/{d}", timeout=15).read().decode()
    except Exception:
        return []
    return [json.loads(l)["vers"] for l in body.splitlines() if l.strip()]

def dep_spec(d, internal, rename, version, local):
    spec = {}
    if d["name"] in internal:
        spec["package"] = rename[d["name"]]
        if local:
            spec["path"] = f"../{rename[d['name']]}"
        else:
            spec["version"] = version
    else:
        src = d.get("source") or ""
        if src.startswith("git") and d["req"] in ("*", ""):
            if d.get("optional"):
                return None
            raise SystemExit(f"required git dep without crates.io version: {d['name']}")
        spec["version"] = d["req"]
        if d.get("rename"):
            spec["package"] = d["name"]
    if not d.get("uses_default_features", True):
        spec["default-features"] = False
    if d.get("features"):
        spec["features"] = d["features"]
    if d.get("optional"):
        spec["optional"] = True
    if list(spec) == ["version"]:
        return spec["version"]
    t = tomlkit.inline_table()
    t.update(spec)
    return t

def build_manifest(pkg, internal, rename, version, local):
    doc = tomlkit.parse(Path(pkg["manifest_path"]).read_text())
    name = pkg["name"]

    p = doc["package"]
    p["name"] = rename[name]
    p["version"] = version
    p["edition"] = pkg["edition"]
    p["license"] = pkg.get("license") or "Apache-2.0"
    p["description"] = pkg.get("description") or f"Standalone mirror of Zed's {name} crate."
    p["repository"] = pkg.get("repository") or ZED_REPO
    if pkg.get("rust_version"):
        p["rust_version"] = pkg["rust_version"]
    p.pop("publish", None)

    lib = doc.get("lib", tomlkit.table())
    lib["name"] = name.replace("-", "_")
    doc["lib"] = lib

    for k in ("dependencies", "dev-dependencies", "build-dependencies",
              "target", "patch", "lints", "workspace"):
        doc.pop(k, None)

    dropped = []
    for d in pkg["dependencies"]:
        if d["kind"] == "dev":
            continue
        spec = dep_spec(d, internal, rename, version, local)
        key = d.get("rename") or d["name"]
        if spec is None:
            dropped.append(d["name"])
            continue
        table = {None: "dependencies", "build": "build-dependencies"}[d["kind"]]
        tgt = d.get("target")
        if tgt:
            doc.setdefault("target", tomlkit.table())
            doc["target"].setdefault(tgt, tomlkit.table())
            doc["target"][tgt].setdefault(table, tomlkit.table())[key] = spec
        else:
            doc.setdefault(table, tomlkit.table())[key] = spec

    if dropped and "features" in doc:
        scrub = {f"dep:{n}" for n in dropped} | set(dropped)
        for feat, reqs in list(doc["features"].items()):
            doc["features"][feat] = [
                r for r in reqs
                if r not in scrub and r.split("?/")[0].split("/")[0] not in dropped
            ]

    return doc

def rewrite(pkg, internal, rename, version, local, dest):
    shutil.copytree(Path(pkg["manifest_path"]).parent, dest, dirs_exist_ok=True)
    doc = build_manifest(pkg, internal, rename, version, local)
    (dest / "Cargo.toml").write_text(tomlkit.dumps(doc))

def publish(dest, dry):
    cmd = ["cargo", "publish", "--no-verify", "--allow-dirty",
           "--manifest-path", str(dest / "Cargo.toml")]
    if dry:
        cmd.append("--dry-run")
        if subprocess.run(cmd).returncode != 0:
            print(f"warn: dry-run could not verify {dest.name} "
                  f"(expected for crates with not-yet-published internal deps)")
        return
    for _ in range(4):
        if subprocess.run(cmd).returncode == 0:
            return
        time.sleep(20)
    raise SystemExit(f"publish failed: {dest.name}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zed", required=True)
    ap.add_argument("--root", default="gpui")
    ap.add_argument("--version")
    ap.add_argument("--crate")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    zed = Path(args.zed).resolve()
    pkgs = metadata(zed)
    order = closure(pkgs, args.root)
    rename = {n: renamed(n, args.root) for n in order}
    internal = {n: pkgs[n] for n in order}

    if args.list:
        print(json.dumps([
            {"name": n, "repo": n, "dir": rename[n],
             "path": os.path.relpath(Path(pkgs[n]["manifest_path"]).parent, zed)}
            for n in order
        ]))
        return

    version = args.version
    if args.manifest:
        print(tomlkit.dumps(build_manifest(pkgs[args.crate], internal, rename, version, args.local)))
        return

    out = Path(args.out).resolve() if args.out else zed.parent / "_standalone"
    for n in order:
        dest = out / rename[n]
        rewrite(pkgs[n], internal, rename, version, args.local, dest)
        if args.local:
            print(f"{rename[n]} -> {dest}")
            continue
        if version in index_versions(rename[n]):
            print(f"skip {rename[n]}@{version}")
            continue
        print(f"publish {rename[n]}@{version}")
        publish(dest, args.dry_run)


if __name__ == "__main__":
    main()
