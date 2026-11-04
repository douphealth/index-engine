"""Tiny in-memory GitHub API double: git refs (create-only, 422 on duplicate), matching-refs, issue comments."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.refs, self.comments, self.lock, self.down = {}, [], threading.Lock(), False


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _route(self, method):
            if st.down:
                return self._send(503, {"message": "down"})
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"message": "auth"})
            path, _, q = self.path.partition("?")
            page = int(dict(p.split("=") for p in q.split("&") if "=" in p).get("page", 1))
            parts = path.strip("/").split("/")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}") if method == "POST" else {}
            if parts[3:5] == ["git", "matching-refs"]:
                pre = "refs/" + "/".join(parts[5:])
                items = [{"ref": r, "object": {"sha": s}} for r, s in sorted(st.refs.items()) if r.startswith(pre)]
                return self._send(200, items[(page - 1) * 100: page * 100])
            if parts[3:5] == ["git", "ref"]:
                return self._send(200, {"object": {"sha": "a" * 40}})
            if parts[3:5] == ["git", "refs"] and method == "POST":
                with st.lock:
                    if body["ref"] in st.refs:
                        return self._send(422, {"message": "Reference already exists"})
                    st.refs[body["ref"]] = body["sha"]
                return self._send(201, {"ref": body["ref"]})
            if len(parts) == 6 and parts[3] == "issues" and parts[5] == "comments":
                if method == "POST":
                    st.comments.append({"body": body["body"], "user": {"login": "bot"}, "created_at": "2026-11-04T00:00:00Z"})
                    return self._send(201, {"id": len(st.comments)})
                return self._send(200, st.comments[(page - 1) * 100: page * 100])
            if len(parts) == 3:
                return self._send(200, {"default_branch": "main"})
            return self._send(404, {"message": "nf"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")
    return H


def start():
    st = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st, f"http://127.0.0.1:{srv.server_address[1]}"
