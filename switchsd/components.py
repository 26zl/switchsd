"""The component manifest: which homebrew goes on the card, from which release, verified how."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .common import CHUNK, Failure, Mismatch, Usage, as_invoking_user, cache_dir, log, sha256_file

DATA = Path(__file__).resolve().parent / "data"
MANIFEST = DATA / "components.json"
LOCK = DATA / "components.lock.json"
# socket.timeout is its own class on Python 3.9; a stalled read raises it there.
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, http.client.HTTPException)
CATEGORIES = ("core", "installer", "saves", "transfer", "updater", "cheats", "overlays", "controllers",
              "network", "themes", "utility", "games", "streaming", "remote", "payload")


class Changed(Mismatch):
    """A pinned file that GitHub no longer serves as it was pinned: its project changed the release afterwards.

    Projects do replace files under a release they already published. Nothing unchecked goes on a card,
    so setup leaves such a component as it is and sets up the rest; see cli.resolve_plan.
    """


@dataclass(frozen=True)
class Rule:
    asset: str  # regex for a release asset name, "archive" for a source snapshot, "" for `content`
    unzip: str | None = None  # regex prefix stripped from zip members; present means "extract"
    include: str | None = None
    exclude: str | None = None
    into: str = ""
    member: str | None = None  # regex picking one zip member to copy to `to`
    to: tuple[str, ...] = ()
    keep: str | None = None  # files matching this are installed only when missing
    content: str | None = None  # a small file written as is, for what a release lacks


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    category: str
    default: bool
    about: str
    repo: str
    archive: str | None
    files: tuple[Rule, ...]
    overrides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    prerelease: bool = False  # the project only publishes pre-releases, so take its newest one
    firmware: str | None = None  # a source file that names the newest firmware the release runs on


def _rule(raw: dict, where: str) -> Rule:
    known = {"asset", "unzip", "include", "exclude", "into", "member", "to", "keep", "content"}
    if set(raw) - known or ("asset" in raw) == ("content" in raw):
        raise Failure(f"{where}: unknown keys, or not exactly one of asset and content, in {sorted(raw)}")
    rule = Rule(raw.get("asset", ""), raw.get("unzip"), raw.get("include"), raw.get("exclude"),
                raw.get("into", ""), raw.get("member"), tuple(raw.get("to", ())), raw.get("keep"),
                raw.get("content"))
    if rule.content is not None and (not rule.to or rule.unzip is not None or rule.member):
        raise Failure(f"{where}: a content rule only takes `to`")
    extracting = rule.unzip is not None
    if extracting == bool(rule.to):
        raise Failure(f"{where}: a rule either unzips or copies to `to`, not both or neither")
    if rule.member and not rule.to:
        raise Failure(f"{where}: `member` needs `to`")
    for pattern in (rule.asset if rule.asset != "archive" else None, rule.unzip, rule.include, rule.exclude,
                    rule.member, rule.keep):
        if pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                raise Failure(f"{where}: bad pattern {pattern!r}: {e}") from None
    for dest in (*rule.to, rule.into):
        if dest and not safe_relpath(dest.rstrip("/")):
            raise Failure(f"{where}: unsafe destination {dest!r}")
    return rule


def load_manifest(path: Path = MANIFEST) -> list[Component]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    comps, seen = [], set()
    for entry in raw["components"]:
        cid = entry["id"]
        if cid in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cid):
            raise Failure(f"components.json: duplicate or invalid id {cid!r}")
        if entry["category"] not in CATEGORIES:
            raise Failure(f"components.json: {cid}: unknown category {entry['category']!r}")
        seen.add(cid)
        source = entry["source"]
        comps.append(Component(
            cid, entry["name"], entry["category"], bool(entry["default"]), entry.get("about", ""),
            source["repo"], source.get("archive"),
            tuple(_rule(r, f"components.json: {cid}") for r in entry["files"]),
            tuple(entry.get("overrides", ())), tuple(entry.get("requires", ())),
            bool(source.get("prerelease", False)), source.get("firmware")))
        if comps[-1].firmware and not safe_relpath(comps[-1].firmware):
            raise Failure(f"components.json: {cid}: unsafe firmware path {comps[-1].firmware!r}")
    ids = {c.id for c in comps}
    for c in comps:
        for other in (*c.overrides, *c.requires):
            if other not in ids:
                raise Failure(f"components.json: {c.id} names unknown component {other!r}")
    return comps


def select(comps: list[Component], with_ids: list[str], without_ids: list[str],
           only_ids: list[str]) -> list[Component]:
    ids = {c.id for c in comps}
    unknown = sorted(set(with_ids + without_ids + only_ids) - ids)
    if unknown:
        raise Usage(f"unknown component(s): {', '.join(unknown)}; `switchsd.py list` shows them all")
    chosen = (set(only_ids) if only_ids else {c.id for c in comps if c.default}) | set(with_ids)
    chosen -= set(without_ids)
    by_id = {c.id: c for c in comps}
    for cid in sorted(chosen):
        for need in by_id[cid].requires:
            if need not in chosen:
                if need in without_ids:
                    raise Usage(f"{cid} needs {need}, which --without leaves out")
                log(f"{cid} needs {need}; adding it")
                chosen.add(need)
    return [c for c in comps if c.id in chosen]


def safe_relpath(path: str) -> bool:
    """A relative, forward-slash path that stays inside the card and names what it seems to name.

    FAT drops a name's trailing dots and spaces, so `emuMMC./x` would land in `emuMMC/`.
    """
    if not path or path.startswith(("/", "\\")) or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        return False
    return all(part not in ("", ".", "..") and not part.endswith((".", " ")) for part in path.split("/"))


def load_lock(path: Path = LOCK) -> dict:
    if not path.exists():
        raise Failure(f"{path.name} is missing; run `switchsd.py lock` to create it")
    return json.loads(path.read_text(encoding="utf-8"))


def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _request(url: str, accept: str | None = None) -> urllib.request.Request:
    headers = {"User-Agent": f"switchsd/{__version__}"}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    token = github_token()
    if token and url.startswith("https://api.github.com/"):
        request.add_unredirected_header("Authorization", f"Bearer {token}")  # never passed on to another host
    return request


def github_api(path: str) -> Any:
    url = f"https://api.github.com/{path}"
    try:
        with urllib.request.urlopen(_request(url, "application/vnd.github+json"), timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        hint = " (rate limited: set GITHUB_TOKEN)" if e.code in (403, 429) else ""
        raise Failure(f"GitHub API {path}: HTTP {e.code}{hint}") from None
    except NETWORK_ERRORS as e:
        raise Failure(f"GitHub API {path}: {getattr(e, 'reason', e)}") from None


def download(url: str, dest: Path, sha256: str | None) -> Path:
    """Fetch `url` into `dest` unless a copy with the right checksum is already there."""
    if dest.exists() and (sha256 is None or sha256_file(dest) == sha256):
        return dest
    with as_invoking_user():
        return _download(url, dest, sha256)


def _download(url: str, dest: Path, sha256: str | None) -> Path:
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, 4):
        digest = hashlib.sha256()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(_request(url), timeout=60) as resp, open(part, "wb") as out:
                for chunk in iter(lambda: resp.read(CHUNK), b""):
                    digest.update(chunk)
                    out.write(chunk)
                announced = resp.headers.get("Content-Length", "")
                if announced.isdigit() and int(announced) != out.tell():
                    raise ConnectionError(f"the server sent {out.tell()} of {announced} bytes")
            break
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            if sha256 and e.code in (404, 410):
                raise Changed(f"{url} is no longer there (HTTP {e.code})") from None
            if attempt == 3:
                raise Failure(f"download failed: {url}: HTTP {e.code}") from None
            time.sleep(2 * attempt)
        except NETWORK_ERRORS as e:
            part.unlink(missing_ok=True)
            if attempt == 3:
                raise Failure(f"download failed: {url}: {getattr(e, 'reason', e)}") from None
            time.sleep(2 * attempt)
        except OSError as e:
            with contextlib.suppress(OSError):
                part.unlink()
            raise Failure(f"cannot write to the download cache {dest.parent}: {e.strerror or e}") from None
    if sha256 and digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise Changed(f"{url} is not the file that was pinned (SHA-256 {digest.hexdigest()}, pinned {sha256})")
    os.replace(part, dest)
    return dest


def cached_path(comp: Component, tag: str, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{comp.repo}/{tag}/{name}")
    return cache_dir() / safe


def match_asset(rule: Rule, names: list[str], where: str) -> str:
    if rule.asset == "archive":
        return "archive"
    found = [n for n in names if re.search(rule.asset, n)]
    if len(found) != 1:
        raise Failure(f"{where}: pattern {rule.asset!r} matches {found or 'no asset'} in {names}")
    return found[0]


def newest_release(comp: Component) -> dict:
    """GitHub's latest stable release, or the newest of any kind for projects that only make pre-releases."""
    if not comp.prerelease:
        return github_api(f"repos/{comp.repo}/releases/latest")
    releases = github_api(f"repos/{comp.repo}/releases?per_page=1")
    if not releases:
        raise Failure(f"{comp.id}: {comp.repo} has no releases")
    return releases[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def web_text(url: str) -> str:
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except NETWORK_ERRORS as e:
        raise Failure(f"{url}: {getattr(e, 'reason', e)}") from None


def web_redirect(url: str) -> str:
    """Where `url` redirects to, without following it."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(_request(url), timeout=60) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return e.headers["Location"]
        raise Failure(f"{url}: HTTP {e.code}") from None
    except NETWORK_ERRORS as e:
        raise Failure(f"{url}: {getattr(e, 'reason', e)}") from None


def newest_tag_web(comp: Component) -> str:
    """The same answer as the API gives, from github.com's redirect and feeds, which need no token."""
    base = f"https://github.com/{comp.repo}"
    if comp.archive:
        match = re.search(r"Grit::Commit/([0-9a-f]{40})<", web_text(f"{base}/commits/{comp.archive}.atom"))
    elif comp.prerelease:
        match = re.search(r'href="https://github\.com/[^"]+?/releases/tag/([^"]+)"', web_text(f"{base}/releases.atom"))
    else:
        match = re.search(r"/releases/tag/(.+)$", web_redirect(f"{base}/releases/latest"))
    if not match:
        raise Failure(f"{comp.id}: could not read the newest release of {comp.repo}")
    return urllib.parse.unquote(match.group(1))


def newest_tag(comp: Component) -> str:
    """The tag (or commit) `lock` would pin now, without downloading anything."""
    if not github_token():
        return newest_tag_web(comp)
    if comp.archive:
        return github_api(f"repos/{comp.repo}/commits/{comp.archive}")["sha"]
    return newest_release(comp)["tag_name"]


def verify_pin(comp: Component, pin: dict) -> tuple[list[str], int]:
    """A pin against what GitHub publishes for that release today: the differences, and how many of its
    files GitHub publishes no checksum for. Uses the API."""
    if comp.archive:
        return [], len(pin["assets"])  # a source snapshot has no published checksum
    try:
        release = github_api(f"repos/{comp.repo}/releases/tags/{pin['tag']}")
    except Failure as e:
        return [f"release {pin['tag']} cannot be read: {e}"], 0
    published = {a["name"]: (a.get("digest") or "").removeprefix("sha256:") for a in release.get("assets", [])}
    problems, unchecked = [], 0
    for name, asset in pin["assets"].items():
        if name not in published:
            problems.append(f"{name} is no longer part of release {pin['tag']}")
        elif not published[name]:
            unchecked += 1
        elif published[name] != asset["sha256"]:
            problems.append(f"{name}: pinned {asset['sha256'][:12]}…, GitHub now publishes {published[name][:12]}…")
    return problems, unchecked


def supported_firmware(comp: Component, tag: str) -> str:
    """The newest firmware a release runs on, as its own source says: Atmosphère's api version header."""
    text = web_text(f"https://raw.githubusercontent.com/{comp.repo}/{urllib.parse.quote(tag)}/{comp.firmware}")
    found = dict(re.findall(r"(?m)^#define\s+ATMOSPHERE_SUPPORTED_HOS_VERSION_(MAJOR|MINOR|MICRO)\s+(\d+)", text))
    if len(found) != 3:
        raise Failure(f"{comp.id}: {comp.firmware} at {tag} does not name a supported firmware")
    return f"{found['MAJOR']}.{found['MINOR']}.{found['MICRO']}"


def resolve_latest(comp: Component) -> dict:
    """Pin entry for the newest release (or branch head), checksums included."""
    if comp.archive:
        ref = github_api(f"repos/{comp.repo}/commits/{comp.archive}")["sha"]
        url = f"https://codeload.github.com/{comp.repo}/zip/{ref}"
        path = cached_path(comp, ref, "archive.zip")
        path.unlink(missing_ok=True)  # nothing vouches for a cached copy: fetch what becomes the pin afresh
        download(url, path, None)
        return {"tag": ref, "assets": {"archive": {"url": url, "sha256": sha256_file(path)}}}
    release = newest_release(comp)
    assets = {a["name"]: a for a in release["assets"]}
    pinned = {}
    for rule in (r for r in comp.files if r.content is None):
        name = match_asset(rule, list(assets), comp.id)
        asset = assets[name]
        published = (asset.get("digest") or "").removeprefix("sha256:")
        path = cached_path(comp, release["tag_name"], name)
        if not published:
            path.unlink(missing_ok=True)  # as above
        download(asset["browser_download_url"], path, None)
        digest = sha256_file(path)
        if published and published != digest:
            path.unlink(missing_ok=True)
            raise Failure(f"{comp.id}: {name} does not match the checksum GitHub publishes for it")
        pinned[name] = {"url": asset["browser_download_url"], "sha256": digest}
    pin = {"tag": release["tag_name"], "published": release.get("published_at", "")[:10], "assets": pinned}
    if comp.firmware:
        pin["firmware"] = supported_firmware(comp, release["tag_name"])
    return pin


def fetch(comp: Component, pin: dict) -> dict[str, Path]:
    """Download (or reuse) a component's pinned assets; returns asset name -> local file."""
    files = {}
    for name, asset in pin["assets"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", ""))):
            raise Failure(f"{comp.id}: the pin for {name} has no SHA-256, so the download cannot be verified")
        files[name] = download(asset["url"], cached_path(comp, pin["tag"], name), asset["sha256"])
    return files


def verified(path: Path, sha256: str, what: str) -> bytes:
    """A downloaded file's content, once it is certain that these are the bytes that were pinned."""
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Failure(f"{what} in the download cache no longer matches its pinned checksum: something changed "
                      f"{path} while it was in use. Run the command again to fetch it afresh.")
    return data
