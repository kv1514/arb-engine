#!/usr/bin/env python3
"""Static self-check for the Chrome MV3 extension in extension/ (stdlib only).

Checks: manifest is valid JSON (no BOM), required keys, permission names are in Chrome's
MV3 list, every referenced file exists, host/match patterns look right, JS files parse
(macOS jsc if present, else node, else skipped), HTML files reference existing scripts.
Prints PASS or FAIL and exits non-zero on FAIL.  Usage: python3 scripts/check_extension.py [dir]
"""
import json
import os
import re
import shutil
import subprocess
import sys

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"

# chrome.permissions accepted by Chrome for Manifest V3 (developer.chrome.com/docs/extensions/reference/permissions-list)
MV3_PERMISSIONS = {
    "accessibilityFeatures.modify", "accessibilityFeatures.read", "activeTab", "alarms", "audio",
    "background", "bookmarks", "browsingData", "certificateProvider", "clipboardRead",
    "clipboardWrite", "contentSettings", "contextMenus", "cookies", "debugger",
    "declarativeContent", "declarativeNetRequest", "declarativeNetRequestWithHostAccess",
    "declarativeNetRequestFeedback", "desktopCapture", "dns", "documentScan", "downloads",
    "downloads.open", "downloads.ui", "enterprise.deviceAttributes", "enterprise.hardwarePlatform",
    "enterprise.networkingAttributes", "enterprise.platformKeys", "favicon", "fileBrowserHandler",
    "fileSystemProvider", "fontSettings", "gcm", "geolocation", "history", "identity",
    "identity.email", "idle", "loginState", "management", "nativeMessaging", "notifications",
    "offscreen", "pageCapture", "platformKeys", "power", "printerProvider", "printing",
    "printingMetrics", "privacy", "processes", "proxy", "readingList", "runtime", "scripting",
    "search", "sessions", "sidePanel", "storage", "system.cpu", "system.display", "system.memory",
    "system.storage", "tabCapture", "tabGroups", "tabs", "topSites", "tts", "ttsEngine",
    "unlimitedStorage", "userScripts", "vpnProvider", "wallpaper", "webAuthenticationProxy",
    "webNavigation", "webRequest", "webRequestBlocking", "webRequestAuthProvider",
}
RUN_AT = {"document_start", "document_end", "document_idle"}
MATCH_RE = re.compile(r"^(\*|https?|file|ftp|chrome-extension|urn):(//)?(\*|\*\.[^/*]+|[^/*]+)?(/.*)?$|^<all_urls>$")

errors, warnings = [], []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def exists(ext_dir, rel, what):
    p = os.path.join(ext_dir, rel)
    if rel.startswith("/") or ".." in rel.split("/"):
        err(f"{what} {rel!r} must be a relative path inside the extension folder")
    elif not os.path.isfile(p):
        err(f"{what} {rel!r} referenced by manifest.json does not exist")
    return p


def has_bom(path):
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


def check_match(pat, where):
    if not MATCH_RE.match(pat):
        err(f"{where}: {pat!r} is not a valid match pattern")
    elif "://" in pat and not pat.endswith("*") and not pat.startswith("<"):
        warn(f"{where}: {pat!r} has no path wildcard; it matches one exact URL")


def js_parser():
    """Return (name, fn) where fn(path) -> error string or None."""
    if os.access(JSC, os.X_OK):
        def run(path):
            code = "var s=readFile(%s);try{new Function(s);print('OK')}catch(e){print('ERR '+e)}" % json.dumps(path)
            out = subprocess.run([JSC, "-e", code], capture_output=True, text=True)
            txt = (out.stdout + out.stderr).strip()
            return None if txt == "OK" else txt or f"jsc exited {out.returncode}"
        return "jsc", run
    node = shutil.which("node")
    if node:
        def run(path):
            code = "const s=require('fs').readFileSync(process.argv[1],'utf8');try{new Function(s);console.log('OK')}catch(e){console.log('ERR '+e)}"
            out = subprocess.run([node, "-e", code, path], capture_output=True, text=True)
            txt = (out.stdout + out.stderr).strip()
            return None if txt == "OK" else txt or f"node exited {out.returncode}"
        return "node", run
    return None, None


def main():
    ext_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "extension"))
    print(f"check_extension: {ext_dir}")
    mpath = os.path.join(ext_dir, "manifest.json")
    if not os.path.isfile(mpath):
        err("manifest.json not found (Chrome: 'Manifest file is missing or unreadable')")
        return finish()
    if has_bom(mpath):
        err("manifest.json starts with a UTF-8 BOM; Chrome rejects it")
    try:
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        err(f"manifest.json is not valid JSON: {e}")
        return finish()

    if m.get("manifest_version") != 3:
        err(f"manifest_version must be 3 (got {m.get('manifest_version')!r})")
    for key in ("name", "version"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            err(f"manifest '{key}' must be a non-empty string")
    if isinstance(m.get("version"), str) and not re.fullmatch(r"\d+(\.\d+){0,3}", m["version"]):
        err(f"version {m['version']!r} must be 1-4 dot-separated integers")
    if isinstance(m.get("name"), str) and len(m["name"]) > 75:
        err("name longer than 75 characters")
    if isinstance(m.get("description"), str) and len(m["description"]) > 132:
        warn("description longer than 132 characters (Web Store limit)")

    for p in m.get("permissions", []):
        if p not in MV3_PERMISSIONS:
            err(f"unknown MV3 permission {p!r} (host patterns belong in host_permissions)")
    for p in m.get("optional_permissions", []):
        if p not in MV3_PERMISSIONS:
            err(f"unknown MV3 optional permission {p!r}")
    for h in m.get("host_permissions", []):
        check_match(h, "host_permissions")
    if "declarativeNetRequestWithHostAccess" in m.get("permissions", []) and not m.get("host_permissions"):
        err("declarativeNetRequestWithHostAccess needs host_permissions")

    bg = m.get("background")
    if bg is not None:
        if not isinstance(bg, dict) or "service_worker" not in bg:
            err("MV3 'background' must be {\"service_worker\": \"file.js\"} (no 'scripts'/'page')")
        else:
            exists(ext_dir, bg["service_worker"], "background.service_worker")
            if bg.get("type") not in (None, "module"):
                err("background.type must be omitted or 'module'")
    if "browser_action" in m or "page_action" in m:
        err("browser_action/page_action are MV2; use 'action'")
    action = m.get("action") or {}
    if action.get("default_popup"):
        exists(ext_dir, action["default_popup"], "action.default_popup")
    for k, v in (action.get("default_icon") or {}).items() if isinstance(action.get("default_icon"), dict) else []:
        exists(ext_dir, v, f"action.default_icon[{k}]")
    for k, v in (m.get("icons") or {}).items():
        exists(ext_dir, v, f"icons[{k}]")

    for i, cs in enumerate(m.get("content_scripts", [])):
        where = f"content_scripts[{i}]"
        if not cs.get("matches"):
            err(f"{where}: 'matches' is required")
        for pat in cs.get("matches", []):
            check_match(pat, where)
        for f in cs.get("js", []):
            exists(ext_dir, f, f"{where}.js")
        for f in cs.get("css", []):
            exists(ext_dir, f, f"{where}.css")
        if cs.get("run_at", "document_idle") not in RUN_AT:
            err(f"{where}: bad run_at {cs.get('run_at')!r}")

    war = m.get("web_accessible_resources")
    if war is not None:
        if not isinstance(war, list) or any(not isinstance(x, dict) for x in war):
            err("MV3 web_accessible_resources must be a list of {resources, matches|extension_ids} objects")
        else:
            for i, x in enumerate(war):
                if not x.get("resources"):
                    err(f"web_accessible_resources[{i}] needs 'resources'")
                if not (x.get("matches") or x.get("extension_ids")):
                    err(f"web_accessible_resources[{i}] needs 'matches' or 'extension_ids'")
                for pat in x.get("matches", []):
                    check_match(pat, f"web_accessible_resources[{i}]")
                for r in x.get("resources", []):
                    if "*" not in r:
                        exists(ext_dir, r, f"web_accessible_resources[{i}]")
    if m.get("content_security_policy") and not isinstance(m["content_security_policy"], dict):
        err("MV3 content_security_policy must be an object {extension_pages, sandbox}")

    # Every file in the folder: no BOMs, JS parses, HTML references exist, no stray junk.
    name, parse = js_parser()
    print(f"JS syntax check via: {name or 'SKIPPED (no jsc or node)'}")
    for root, dirs, files in os.walk(ext_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for fn in files:
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ext_dir)
            if fn == ".DS_Store":
                warn(f"{rel}: stray macOS file inside the extension folder (harmless, but delete it)")
                continue
            if fn.startswith("."):
                continue
            if fn.endswith((".js", ".json", ".html", ".css")) and has_bom(path):
                err(f"{rel}: starts with a UTF-8 BOM")
            if fn.endswith(".json"):
                try:
                    with open(path, encoding="utf-8") as f:
                        json.load(f)
                except ValueError as e:
                    err(f"{rel}: invalid JSON: {e}")
            if fn.endswith(".js") and parse:
                problem = parse(path)
                if problem:
                    err(f"{rel}: does not parse: {problem}")
            if fn.endswith(".html"):
                with open(path, encoding="utf-8", errors="replace") as f:
                    html = f.read()
                if re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html):
                    err(f"{rel}: inline <script> is blocked by the MV3 CSP")
                for ref in re.findall(r"""<(?:script|link)[^>]*(?:src|href)=["']([^"']+)["']""", html):
                    if not re.match(r"^(https?:|//|#|data:)", ref):
                        exists(os.path.dirname(path), ref, f"{rel} -> ")
                    else:
                        err(f"{rel}: remote resource {ref!r} is blocked by the MV3 CSP")
    return finish()


def finish():
    for w in warnings:
        print(f"  warn: {w}")
    for e in errors:
        print(f"  FAIL: {e}")
    if errors:
        print(f"FAIL ({len(errors)} error(s), {len(warnings)} warning(s))")
        return 1
    print(f"PASS ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
