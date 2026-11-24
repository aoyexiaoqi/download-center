#!/usr/bin/env python3
"""Upload Release assets with explicit authentication and SHA-256 round-trip checks.

python3 tools/release_upload.py --repo OWNER/REPO --tag TAG --publish FILE...
Add --diagnose to check files, hosts, credentials and the target without writing.
Authentication: GH_TOKEN, GITHUB_TOKEN, or an existing `gh auth login` session.
Existing assets are verified and reused; different bytes are never overwritten.
"""
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class UploadError(Exception):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise UploadError("Refusing a redirect to a non-HTTPS destination")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


OPENER = urllib.request.build_opener(SafeRedirect())


def token_from_environment():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise UploadError("No GitHub credential. Run `gh auth login`, or set GH_TOKEN with Contents: write for this repository. Never paste tokens into chat.")


def open_request(url, token=None, method="GET", data=None, content_type=None, size=None, accept="application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": "download-center-release-upload", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        if urllib.parse.urlsplit(url).hostname not in ("api.github.com", "uploads.github.com"):
            raise UploadError("Refusing to send GitHub credentials to another host")
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    if size is not None:
        headers["Content-Length"] = str(size)
    return OPENER.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=300)


def api(url, token, method="GET", body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    with open_request(url, token, method, payload, "application/json" if payload else None) as response:
        return json.load(response)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(url, expected_sha, expected_size, token=None, attempts=1):
    for attempt in range(attempts):
        try:
            digest = hashlib.sha256()
            size = 0
            with open_request(url, token, accept="application/octet-stream") as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size or digest.hexdigest() != expected_sha:
                raise UploadError("Downloaded bytes do not match the source SHA-256 and size")
            return
        except urllib.error.HTTPError as error:
            if error.code not in (404, 429, 502, 503, 504) or attempt + 1 == attempts:
                raise
            time.sleep(min(2 ** attempt, 8))


def diagnose(args, files):
    """Check every independent prerequisite, even if an earlier check fails."""
    failures = []

    def fail(label, message):
        failures.append(label)
        print(f"[失败] {label} · {message}")

    for path in files:
        try:
            if not path.is_file():
                raise ValueError("没有找到普通文件")
            with path.open("rb") as source:
                if not source.read(1):
                    raise ValueError("文件为空")
            print(f"[通过] {path} · {path.stat().st_size} 字节")
        except (OSError, ValueError) as error:
            fail(str(path), error)
    if len({p.name for p in files}) != len(files):
        fail("附件名称", "多个文件使用相同的附件名")

    reachable = set()
    for host in ("api.github.com", "uploads.github.com"):
        try:
            request = urllib.request.Request("https://" + host + "/", method="HEAD")
            with OPENER.open(request, timeout=10):
                pass
        except urllib.error.HTTPError as error:
            # The uploads host may return 404 at its root; it still answered HTTPS.
            if error.code == 407 or error.code >= 500:
                fail(host, f"HTTP {error.code}")
                continue
        except (OSError, UploadError) as error:
            fail(host, str(error))
            continue
        reachable.add(host)
        print(f"[通过] {host} · HTTPS 可达")

    try:
        token = token_from_environment()
    except UploadError:
        token = None
        fail("上传凭据", "未取得 GH_TOKEN、GITHUB_TOKEN 或已登录的 gh 凭据")
    if token and "api.github.com" in reachable:
        base = "https://api.github.com/repos/" + args.repo
        try:
            repo = api(base, token)
            print(f"[通过] 仓库可读 · {repo['full_name']}")
            if args.release_id:
                release = api(base + "/releases/" + str(args.release_id), token)
                if release["tag_name"] != args.tag:
                    fail("Release", f"实际标签为 {release['tag_name']}，预期为 {args.tag}")
                else:
                    state = "草稿" if release["draft"] else "已发布"
                    print(f"[通过] Release · {state}，{len(release['assets'])} 个附件")
        except urllib.error.HTTPError as error:
            hint = {401: "GitHub 拒绝了当前凭据，请在此机器更新登录",
                    403: "检查仓库授权、限流和组织策略",
                    404: "检查仓库、草稿 ID 及凭据的访问权限"}
            fail("GitHub API", f"HTTP {error.code} · " + hint.get(error.code, "请求失败"))
        except (OSError, ValueError, KeyError, UploadError) as error:
            fail("GitHub API", str(error))
    else:
        print("[跳过] 仓库和草稿读取需要可用的 API 和凭据")
    print("只读诊断完成。附件写入权限和下载链接由实际上传验证。")
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title")
    parser.add_argument("--diagnose", action="store_true", help="Check prerequisites without uploading or publishing")
    parser.add_argument("--release-id", type=int, help="Resume an existing draft by its numeric release ID")
    parser.add_argument("--publish", action="store_true", help="Publish only after every asset passes its download check")
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--verify-public", action="store_true", help="Also download without credentials after publishing (public repositories only)")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise UploadError("--repo must be OWNER/REPO")
    files = [Path(item).expanduser().resolve() for item in args.files]
    if args.diagnose:
        return diagnose(args, files)
    if args.verify_public and not args.publish:
        raise UploadError("--verify-public requires --publish")
    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            raise UploadError("Missing or empty source file: " + str(path))
    if len({p.name for p in files}) != len(files):
        raise UploadError("Asset basenames must be unique")
    checks = [(path, path.stat().st_size, file_digest(path)) for path in files]
    token = token_from_environment()
    base = "https://api.github.com/repos/" + args.repo
    repository = api(base, token)
    if args.verify_public and repository.get("private"):
        raise UploadError("Public download verification is unavailable for a private repository")
    if args.release_id:
        release = api(base + "/releases/" + str(args.release_id), token)
        if release["tag_name"] != args.tag:
            raise UploadError("Existing release tag differs from --tag")
    else:
        release = None
        page = 1
        while True:
            candidates = api(base + "/releases?per_page=100&page=" + str(page), token)
            release = next((item for item in candidates if item["tag_name"] == args.tag), None)
            if release is not None or len(candidates) < 100:
                break
            page += 1
        if release is None:
            release = api(base + "/releases", token, "POST", {
                "tag_name": args.tag, "target_commitish": repository["default_branch"],
                "name": args.title or args.tag, "draft": True, "prerelease": args.prerelease,
                "body": "Assets uploaded with explicit authentication and verified by a SHA-256 download check.",
            })
    release_api = base + "/releases/" + str(release["id"])
    assets = {item["name"]: item for item in release.get("assets", [])}
    verified = []
    for path, size, sha in checks:
        asset = assets.get(path.name)
        if asset is None:
            upload_base = release["upload_url"].split("{", 1)[0]
            upload_url = upload_base + "?" + urllib.parse.urlencode({"name": path.name})
            with path.open("rb") as source:
                with open_request(upload_url, token, "POST", source, mimetypes.guess_type(path.name)[0] or "application/octet-stream", size) as response:
                    asset = json.load(response)
        if asset.get("state") != "uploaded" or asset["size"] != size:
            raise UploadError("Existing or uploaded asset size/state differs: " + path.name + "; no asset was deleted")
        if asset.get("digest") and asset["digest"] != "sha256:" + sha:
            raise UploadError("Asset SHA-256 differs: " + path.name + "; no asset was overwritten")
        verify_download(asset["url"], sha, size, token)
        verified.append((asset, size, sha))
        print("VERIFIED " + path.name + " SHA256=" + sha)
    if args.publish and release["draft"]:
        release = api(release_api, token, "PATCH", {"draft": False, "make_latest": "false"})
    if args.verify_public:
        # Draft upload responses can contain an untagged URL. Publication changes
        # the download path, so resolve the authoritative asset URLs again.
        release = api(release_api, token)
        published_assets = {item["id"]: item for item in release["assets"]}
        for asset, size, sha in verified:
            asset = published_assets[asset["id"]]
            verify_download(asset["browser_download_url"], sha, size, attempts=6)
            print("PUBLIC_DOWNLOAD_VERIFIED " + asset["browser_download_url"])
    elif not args.publish and release["draft"]:
        print("Draft verified. Public download links become available after --publish.")
    print("RELEASE " + release["html_url"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as error:
        hints = {401: "Invalid or expired credential: refresh gh login or GH_TOKEN.",
                 403: "Check repository access, Contents: write, rate limits and organization policy.",
                 404: "Check repository/release identity and draft access.",
                 422: "Check tag and asset conflicts; existing files were not overwritten."}
        print("ERROR HTTP " + str(error.code) + ": " + hints.get(error.code, "GitHub API request failed."), file=sys.stderr)
        sys.exit(1)
    except (UploadError, OSError, ValueError, KeyError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
