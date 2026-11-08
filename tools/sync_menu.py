#!/usr/bin/env python3
"""同步公开菜单；个人记录和仓库README不参与同步。"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MENU_PATHS = tuple(f"菜单/{name}.md" for name in ("八一", "八二", "美食广场", "第三食堂"))


def atomic_write(path, text, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(text)
        os.chmod(temporary, 0o644 if owner else 0o600)
        if owner and hasattr(os, "chown"):
            os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merge_text(local, base, remote):
    if local == remote or remote == base:
        return local, False
    if local == base:
        return remote, False
    if None in (local, base, remote):
        return local, True
    with tempfile.TemporaryDirectory(prefix="menu-merge-") as directory:
        paths = [Path(directory) / name for name in ("local", "base", "remote")]
        for path, text in zip(paths, (local, base, remote)):
            path.write_text(text, encoding="utf-8", newline="")
        result = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)], capture_output=True
        )
    if result.returncode < 0 or result.returncode >= 128:
        raise RuntimeError("菜单三方合并执行失败")
    if result.returncode:
        return local, True
    return result.stdout.decode("utf-8"), False


class GitHub:
    def __init__(self, repository, token, api_url="https://api.github.com"):
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(self, method, path, data=None):
        request = urllib.request.Request(
            self.api_url + "/repos/" + self.repository + path,
            data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "dut-panjin-menu-sync",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub {method} {path}: HTTP {error.code}") from None

    def head(self, branch="main"):
        branch = urllib.parse.quote(branch, safe="/")
        return self.request("GET", "/git/ref/heads/" + branch)["object"]["sha"]

    def files(self, head):
        tree = self.request("GET", "/git/trees/" + head + "?recursive=1")
        entries = {entry["path"]: entry for entry in tree["tree"]}
        result = {}
        for path in MENU_PATHS:
            entry = entries.get(path)
            if entry is None:
                result[path] = None
            else:
                blob = self.request("GET", "/git/blobs/" + entry["sha"])
                result[path] = base64.b64decode(blob["content"]).decode("utf-8")
        return result

    def commit(self, parent, old, new, message):
        tree = self.request("GET", "/git/commits/" + parent)["tree"]["sha"]
        entries = []
        for path in MENU_PATHS:
            if old[path] == new[path]:
                continue
            entry = {"path": path, "mode": "100644", "type": "blob"}
            if new[path] is None:
                entry["sha"] = None
            else:
                entry["content"] = new[path]
            entries.append(entry)
        if not entries:
            return parent
        tree = self.request("POST", "/git/trees", {"base_tree": tree, "tree": entries})["sha"]
        return self.request(
            "POST", "/git/commits", {"message": message, "tree": tree, "parents": [parent]}
        )["sha"]

    def advance(self, head):
        self.request("PATCH", "/git/refs/heads/main", {"sha": head, "force": False})

    def branch(self, name, head):
        self.request("POST", "/git/refs", {"ref": "refs/heads/" + name, "sha": head})

    def review(self, branch, conflicts):
        existing = self.request(
            "GET", "/pulls?state=all&head=" + urllib.parse.quote(
                self.repository.split("/")[0] + ":" + branch, safe=""
            )
        )
        if existing:
            return existing[0]
        return self.request("POST", "/pulls", {
            "title": "菜单同步冲突：请维护者审核",
            "head": branch,
            "base": "main",
            "body": "沙箱与线上菜单同时修改，以下文件存在文本冲突：\n\n"
            + "\n".join("- " + path for path in conflicts)
            + "\n\n此分支保留沙箱版本，main保留线上版本。请Vivian-421核对差异后处理；"
            "同步器不会自动合并此PR，待审核结束后恢复同步。个人记录不在本PR中。",
        })


class Sync:
    def __init__(self, github, root, state_path, owner=None, status_path=None):
        self.github = github
        self.root = Path(root)
        self.state_path = Path(state_path)
        self.owner = owner
        self.status_path = Path(status_path) if status_path else None
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}

    def save(self):
        atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def local_files(self):
        return {
            name: (self.root / name).read_text(encoding="utf-8") if (self.root / name).exists() else None
            for name in MENU_PATHS
        }

    def apply(self, original, new):
        for name in MENU_PATHS:
            path = self.root / name
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != original[name] or current == new[name]:
                continue
            if new[name] is None:
                path.unlink()
            else:
                atomic_write(path, new[name], self.owner)

    def status(self, kind, detail=""):
        if self.status_path:
            text = "# 菜单同步状态\n\n" + {
                "synced": "沙箱公开菜单与GitHub已同步。",
                "waiting": "同步已暂停，等待维护者处理冲突PR：" + detail,
                "error": "同步暂未完成：" + detail,
            }[kind] + "\n"
            if not self.status_path.exists() or self.status_path.read_text(encoding="utf-8") != text:
                atomic_write(self.status_path, text, self.owner)
        return kind

    def run(self, initialize=False):
        pending = self.state.get("pending")
        if pending:
            if not pending.get("number"):
                pull = self.github.review(pending["branch"], pending["conflicts"])
                pending.update(number=pull["number"], url=pull["html_url"])
                self.save()
            else:
                pull = self.github.request("GET", "/pulls/" + str(pending["number"]))
            if pull["state"] == "open":
                return self.status("waiting", pending["url"])
        head = self.github.head()
        if head == self.state.get("head"):
            remote = self.state["remote"]
        else:
            remote = self.github.files(head)
        local = self.local_files()
        if "base" not in self.state:
            if not initialize or local != remote:
                raise RuntimeError("首次启动须显式初始化，且公开菜单与远端一致")
            self.state = {"head": head, "base_head": head, "base": remote, "remote": remote}
            self.save()
            return self.status("synced")

        if pending:
            adopted = {
                name: remote[name] if local[name] == pending["local"][name] else local[name]
                for name in MENU_PATHS
            }
            self.apply(local, adopted)
            self.state.update(base=pending["local"], base_head=pending["commit"])
            del self.state["pending"]
            self.save()
            local = self.local_files()

        merged = {}
        conflicts = []
        for name in MENU_PATHS:
            merged[name], conflict = merge_text(local[name], self.state["base"][name], remote[name])
            if conflict:
                conflicts.append(name)
        if conflicts:
            branch = "sandbox-review-" + str(time.time_ns())
            commit = self.github.commit(
                self.state["base_head"], self.state["base"], local,
                "docs: 保留沙箱菜单冲突版本供维护者审核",
            )
            self.github.branch(branch, commit)
            self.state["pending"] = {
                "branch": branch, "commit": commit, "local": local, "conflicts": conflicts,
            }
            self.save()
            pull = self.github.review(branch, conflicts)
            self.state["pending"].update(number=pull["number"], url=pull["html_url"])
            self.save()
            return self.status("waiting", pull["html_url"])
        if merged != remote:
            head = self.github.commit(head, remote, merged, "docs: 同步沙箱公开菜单")
            self.github.advance(head)
        self.apply(local, merged)
        self.state.update(head=head, base_head=head, base=merged, remote=merged)
        self.state.pop("failures", None)
        self.state.pop("retry_after", None)
        self.save()
        return self.status("synced")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--repository", default="Vivian-421/DUT-panjin-school-menu")
    parser.add_argument("--status-file")
    parser.add_argument("--owner", help="沙箱文件属主，格式为uid:gid")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    owner = tuple(map(int, args.owner.split(":"))) if args.owner else None
    sync = Sync(GitHub(args.repository, token), args.root, args.state, owner, args.status_file)
    if time.time() < sync.state.get("retry_after", 0):
        print("backoff")
        return
    try:
        print(sync.run(args.initialize))
    except Exception as error:
        failures = sync.state.get("failures", 0) + 1
        sync.state.update(failures=failures, retry_after=time.time() + min(120 * 2 ** min(failures, 5), 3600))
        sync.save()
        detail = str(error).replace(token, "[凭据已隐藏]")
        sync.status("error", detail)
        raise SystemExit(detail) from None


if __name__ == "__main__":
    main()
