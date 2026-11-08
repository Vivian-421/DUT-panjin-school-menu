import base64
import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sync_menu import GitHub, MENU_PATHS, Sync, merge_text


class Repository:
    def __init__(self):
        self.sequence = 0
        self.trees = {}
        self.commits = {}
        self.blobs = {}
        self.refs = {}
        self.pulls = []
        files = {path: "# 菜单\n\n- 甲：10元\n\n## 另一窗口\n\n- 乙：20元\n" for path in MENU_PATHS}
        files.update({"README.md": "用户的README\n", "LICENSE": "CC0\n"})
        self.refs["main"] = self.new_commit(files, None)

    def identifier(self):
        self.sequence += 1
        return str(self.sequence)

    def new_commit(self, files, parent):
        tree = self.identifier()
        self.trees[tree] = copy.deepcopy(files)
        head = self.identifier()
        self.commits[head] = {"tree": {"sha": tree}, "parents": [] if parent is None else [parent]}
        return head

    def files(self, head=None):
        return self.trees[self.commits[head or self.refs["main"]]["tree"]["sha"]]

    def change(self, changes):
        files = dict(self.files())
        files.update(changes)
        self.refs["main"] = self.new_commit(files, self.refs["main"])

    def handle(self, method, url, data):
        path = unquote(urlsplit(url).path.split("/repos/test/menu", 1)[1])
        if method == "GET" and path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.refs[path.removeprefix("/git/ref/heads/")]}}
        if method == "GET" and path.startswith("/git/trees/"):
            reference = path.removeprefix("/git/trees/")
            files = self.files(reference) if reference in self.commits else self.trees[reference]
            entries = []
            for name, content in files.items():
                sha = self.identifier()
                self.blobs[sha] = content
                entries.append({"path": name, "sha": sha, "type": "blob", "mode": "100644"})
            return {"tree": entries}
        if method == "GET" and path.startswith("/git/blobs/"):
            return {"content": base64.b64encode(self.blobs[path.split("/")[-1]].encode()).decode()}
        if method == "GET" and path.startswith("/git/commits/"):
            return self.commits[path.split("/")[-1]]
        if method == "POST" and path == "/git/trees":
            files = dict(self.trees[data["base_tree"]])
            for entry in data["tree"]:
                if "content" in entry:
                    files[entry["path"]] = entry["content"]
                else:
                    files.pop(entry["path"], None)
            sha = self.identifier()
            self.trees[sha] = files
            return {"sha": sha}
        if method == "POST" and path == "/git/commits":
            sha = self.identifier()
            self.commits[sha] = {"tree": {"sha": data["tree"]}, "parents": data["parents"]}
            return {"sha": sha}
        if method == "PATCH" and path == "/git/refs/heads/main":
            if self.refs["main"] not in self.commits[data["sha"]]["parents"]:
                raise RuntimeError("non-fast-forward")
            self.refs["main"] = data["sha"]
            return {}
        if method == "POST" and path == "/git/refs":
            self.refs[data["ref"].removeprefix("refs/heads/")] = data["sha"]
            return {}
        if method == "GET" and path == "/pulls":
            return self.pulls
        if method == "POST" and path == "/pulls":
            pull = dict(data, number=len(self.pulls) + 1, html_url="https://example.test/review", state="open")
            self.pulls.append(pull)
            return pull
        if method == "GET" and path.startswith("/pulls/"):
            return self.pulls[int(path.split("/")[-1]) - 1]
        raise RuntimeError("不支持的测试请求：" + method + " " + path)


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = Repository()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self):
                payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                data = json.loads(payload) if payload else None
                if self.headers.get("Authorization") != "Bearer test-token":
                    self.send_error(401)
                    return
                result = cls.repository.handle(self.command, self.path, data)
                body = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PATCH = respond

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).repository = Repository()
        self.repo = self.repository
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in MENU_PATHS:
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_text(self.repo.files()[name], encoding="utf-8")
        self.private = self.root / "个人记录.md"
        self.private.write_text("私人备注与偏好不能公开\n", encoding="utf-8")
        self.state = self.root / "state.json"
        self.github = GitHub("test/menu", "test-token", f"http://127.0.0.1:{self.server.server_port}")
        self.sync().run(initialize=True)

    def tearDown(self):
        self.temporary.cleanup()

    def sync(self):
        return Sync(self.github, self.root, self.state)

    def change_local(self, old, new, name=MENU_PATHS[0]):
        path = self.root / name
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def test_unchanged_does_not_commit(self):
        head = self.repo.refs["main"]
        self.assertEqual(self.sync().run(), "synced")
        self.assertEqual(self.repo.refs["main"], head)

    def test_local_change_publishes_only_menu_and_preserves_readme(self):
        self.change_local("10元", "12元")
        self.sync().run()
        self.assertIn("12元", self.repo.files()[MENU_PATHS[0]])
        self.assertEqual(self.repo.files()["README.md"], "用户的README\n")
        self.assertNotIn("个人记录.md", self.repo.files())
        self.assertEqual(self.private.read_text(encoding="utf-8"), "私人备注与偏好不能公开\n")

    def test_remote_change_persists_locally_after_restart(self):
        self.repo.change({MENU_PATHS[0]: self.repo.files()[MENU_PATHS[0]].replace("10元", "13元")})
        self.sync().run()
        self.assertIn("13元", (self.root / MENU_PATHS[0]).read_text(encoding="utf-8"))
        head = self.repo.refs["main"]
        self.sync().run()
        self.assertEqual(self.repo.refs["main"], head)

    def test_disjoint_edits_in_same_file_merge(self):
        self.change_local("10元", "12元")
        self.repo.change({MENU_PATHS[0]: self.repo.files()[MENU_PATHS[0]].replace("20元", "23元")})
        self.sync().run()
        text = (self.root / MENU_PATHS[0]).read_text(encoding="utf-8")
        self.assertIn("12元", text)
        self.assertIn("23元", text)
        self.assertFalse(self.repo.pulls)

    def conflict(self):
        self.change_local("10元", "12元")
        self.repo.change({MENU_PATHS[0]: self.repo.files()[MENU_PATHS[0]].replace("10元", "13元")})
        remote_head = self.repo.refs["main"]
        self.assertEqual(self.sync().run(), "waiting")
        self.assertEqual(self.repo.refs["main"], remote_head)
        self.assertIn("12元", (self.root / MENU_PATHS[0]).read_text(encoding="utf-8"))
        self.assertIn("13元", self.repo.files()[MENU_PATHS[0]])
        return self.repo.pulls[0]

    def test_conflict_opens_one_pr_and_never_merges(self):
        self.conflict()
        self.sync().run()
        self.sync().run()
        self.assertEqual(len(self.repo.pulls), 1)
        self.assertEqual(self.repo.pulls[0]["state"], "open")

    def test_owner_merge_is_applied(self):
        pull = self.conflict()
        self.repo.change({MENU_PATHS[0]: self.repo.files(self.repo.refs[pull["head"]])[MENU_PATHS[0]]})
        pull["state"] = "closed"
        self.assertEqual(self.sync().run(), "synced")
        self.assertIn("12元", (self.root / MENU_PATHS[0]).read_text(encoding="utf-8"))

    def test_owner_reject_is_applied(self):
        pull = self.conflict()
        pull["state"] = "closed"
        self.assertEqual(self.sync().run(), "synced")
        self.assertIn("13元", (self.root / MENU_PATHS[0]).read_text(encoding="utf-8"))
        self.sync().run()
        self.assertEqual(len(self.repo.pulls), 1)

    def test_new_edit_while_waiting_survives_review(self):
        pull = self.conflict()
        self.change_local("20元", "25元")
        pull["state"] = "closed"
        self.sync().run()
        text = (self.root / MENU_PATHS[0]).read_text(encoding="utf-8")
        self.assertIn("13元", text)
        self.assertIn("25元", text)

    def test_multiple_conflict_regions_remain_private(self):
        base = "a\n" + "same\n" * 10 + "b\n"
        merged, conflict = merge_text(base.replace("a\n", "c\n").replace("b\n", "d\n"), base,
                                      base.replace("a\n", "e\n").replace("b\n", "f\n"))
        self.assertTrue(conflict)
        self.assertNotIn("<<<<<<<", merged)

    def test_local_change_during_remote_work_is_not_overwritten(self):
        original = self.sync().local_files()
        replacement = dict(original)
        replacement[MENU_PATHS[0]] = replacement[MENU_PATHS[0]].replace("10元", "13元")
        self.change_local("10元", "12元")
        self.sync().apply(original, replacement)
        self.assertIn("12元", (self.root / MENU_PATHS[0]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
