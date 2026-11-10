"""Makes room in a DigitalOcean container registry before a new swingbot version is copied in (run by the
Docker image workflow, not part of the image).

    python docker/do_cleanup.py <registry>/<repository> <new version> [keep]

The free (Starter) registry holds 500 MB, a few versions of the image. This deletes every version tag but
the newest `keep` (default 1: the previous version stays, so `latest` keeps working until the new one is in),
then runs garbage collection, which frees the space, and waits for it to finish (the registry is read-only
meanwhile). Needs DO_TOKEN in the environment: a DigitalOcean API token with registry read, update and delete.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.digitalocean.com/v2/registry"


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + os.environ["DO_TOKEN"].strip(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def vkey(tag):
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def main():
    target, new = sys.argv[1], sys.argv[2].lstrip("v")
    keep = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    registry, repo = target.split("/", 1)
    code, data = call("GET", f"/{registry}/repositories/{repo}/tags?per_page=200")
    if code == 404:
        print("No swingbot images in the registry yet: nothing to clean")
        return
    if code != 200:
        sys.exit(f"Couldn't list the registry's tags (HTTP {code}: {data.get('message', '')}). "
                 "The DigitalOcean token needs registry read, update and delete.")
    versions = sorted({t["tag"] for t in data.get("tags", []) if re.fullmatch(r"v\d+(\.\d+)*", t["tag"])
                       and t["tag"] != "v" + new}, key=vkey)
    old = versions[:-keep] if keep else versions
    print(f"In the registry: {', '.join(versions) or 'no versions'}; keeping {', '.join(versions[-keep:]) if keep else 'none'}")
    for tag in old:
        code, data = call("DELETE", f"/{registry}/repositories/{repo}/tags/{tag}")
        print(f"  deleted {tag}" if code in (200, 204) else f"  couldn't delete {tag}: HTTP {code} {data.get('message', '')}")
        if code == 403:
            sys.exit("The DigitalOcean token can't delete tags: give it registry delete (and update) rights.")
    # garbage collection frees the space; untagged manifests (old builds of replaced tags) go too
    code, data = call("POST", f"/{registry}/garbage-collection", {"type": "untagged manifests and unreferenced blobs"})
    if code == 409:
        print("A garbage collection is already running: waiting for it")
    elif code not in (200, 201):
        sys.exit(f"Couldn't start garbage collection (HTTP {code}: {data.get('message', '')}). "
                 "The DigitalOcean token needs registry update rights.")
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        code, data = call("GET", f"/{registry}/garbage-collection")
        gc = data.get("garbage_collection") or {}
        if code == 404 or not gc or gc.get("status") in ("succeeded", "failed", "cancelled"):
            if gc.get("status") == "failed":
                sys.exit("Garbage collection failed; see the registry in DigitalOcean")
            freed = gc.get("freed_bytes")
            print("Garbage collection done" + (f": {freed / 1e6:.0f} MB freed" if freed else ""))
            return
        print(f"  garbage collection: {gc.get('status')}")
        time.sleep(15)
    sys.exit("Garbage collection didn't finish within 20 minutes")


if __name__ == "__main__":
    main()
