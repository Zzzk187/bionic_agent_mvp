"""Install / uninstall the bionic-agent-panel DSH client plugin.

This registers the plugin into the *desktop* profile (the one that serves the
Web GUI at http://127.0.0.1:59647). It does two things:

  1. Symlinks  <desktop>/node_modules/@local/bionic-agent-panel  ->  this folder.
  2. Adds  "@local/bionic-agent-panel"  to the profile package.json
     `dependencies` and `dsh.profile.bundles`.

The plugin's own `cordis.patch.yml` is layered automatically by DSH at boot
(no manual edit of the profile patch needed).

After install you MUST fully quit and restart DeepSeek Harness (including the
tray icon). The sidebar then shows a "记忆/任务" entry.

Usage:
    python install.py            # install
    python install.py --uninstall
"""

import argparse
import json
import os
import sys
import shutil

# --- resolve paths ---------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_NAME = "@local/bionic-agent-panel"

# desktop profile is resolved from DSH_HOME (~/.dsh). Override with env if needed.
DSH_HOME = os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh"))
PROFILE_DIR = os.environ.get(
    "DSH_PROFILE_DIR",
    os.path.join(DSH_HOME, "profiles", "desktop"),
)
NODE_MODULES = os.path.join(PROFILE_DIR, "node_modules")
LINK_TARGET = os.path.join(NODE_MODULES, PKG_NAME.replace("/", os.sep))


def _backup(path):
    if os.path.exists(path):
        bak = path + ".bak-bionic-panel"
        shutil.copy2(path, bak)
        return bak
    return None


def install():
    if not os.path.isdir(PROFILE_DIR):
        sys.exit(f"[x] desktop profile not found: {PROFILE_DIR}\n"
                 f"    Set DSH_PROFILE_DIR to your desktop profile directory.")
    os.makedirs(NODE_MODULES, exist_ok=True)

    # 1) symlink the plugin folder into node_modules/@local/...
    if os.path.islink(LINK_TARGET) or os.path.exists(LINK_TARGET):
        print(f"[=] link already exists: {LINK_TARGET}")
    else:
        os.makedirs(os.path.dirname(LINK_TARGET), exist_ok=True)
        try:
            os.symlink(HERE, LINK_TARGET, target_is_directory=True)
        except NotImplementedError:
            # Windows: need admin for symlink; fall back to junction (dir link)
            os.system(f'mklink /J "{LINK_TARGET}" "{HERE}"')
        print(f"[+] linked {LINK_TARGET} -> {HERE}")

    # 2) edit profile package.json
    pkg_path = os.path.join(PROFILE_DIR, "package.json")
    if not os.path.isfile(pkg_path):
        sys.exit(f"[x] profile package.json missing: {pkg_path}")
    _backup(pkg_path)
    with open(pkg_path, encoding="utf-8") as f:
        pkg = json.load(f)

    deps = pkg.setdefault("dependencies", {})
    if PKG_NAME not in deps:
        deps[PKG_NAME] = "0.1.0"
        print(f"[+] added {PKG_NAME} to dependencies")

    bundles = pkg.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    if PKG_NAME not in bundles:
        bundles.append(PKG_NAME)
        print(f"[+] added {PKG_NAME} to dsh.profile.bundles")

    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("\n[ok] plugin registered. Now just:")
    print("   FULLY quit DeepSeek Harness (tray icon too), then restart it.")
    print("   The sidebar shows a '记忆/任务' entry; the host half starts the")
    print("   FastAPI panel by itself — no manual `bionic-agent-admin` needed.")


def uninstall():
    # 1) remove symlink
    if os.path.islink(LINK_TARGET):
        os.unlink(LINK_TARGET)
        print(f"[-] removed link {LINK_TARGET}")
    elif os.path.exists(LINK_TARGET):
        print(f"[!] {LINK_TARGET} is not a symlink; leaving it alone")

    # 2) edit profile package.json
    pkg_path = os.path.join(PROFILE_DIR, "package.json")
    if os.path.isfile(pkg_path):
        _backup(pkg_path)
        with open(pkg_path, encoding="utf-8") as f:
            pkg = json.load(f)
        pkg.get("dependencies", {}).pop(PKG_NAME, None)
        bundles = pkg.get("dsh", {}).get("profile", {}).get("bundles", [])
        if PKG_NAME in bundles:
            bundles.remove(PKG_NAME)
        with open(pkg_path, "w", encoding="utf-8") as f:
            json.dump(pkg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"[-] removed {PKG_NAME} from profile package.json")
    print("\n[ok] uninstalled. Restart DeepSeek Harness to apply.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    if args.uninstall:
        uninstall()
    else:
        install()
