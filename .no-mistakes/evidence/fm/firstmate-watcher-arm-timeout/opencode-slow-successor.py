#!/usr/bin/env python3
"""Real `opencode serve` + real Firstmate OpenCode plugins + real fm-watch-arm.sh /
fm-watch.sh, isolated lab and loopback provider (adapted from
tests/opencode-v2-live-fixture.py). After the first watcher is armed, every
SUCCESSOR watcher's singleton-lock acquisition is delayed DELAY seconds (PATH shim on
ln(1), once per watcher pid) - the busy-machine worker-close condition from
the incidents. Then a worker status change fires the watcher and we observe
whether the plugin restores a healthy successor or surfaces
"could not restore watcher continuity".
Usage: REPO=<checkout with bin/ and .opencode/plugins> DELAY=<s> opencode-slow-successor.py
"""
import base64, http.server, json, os, shutil, socket, subprocess, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(os.environ["REPO"]).resolve()
DELAY = int(os.environ.get("DELAY", "18"))

def run(lab):
    project, home, fmhome = lab / "project", lab / "home", lab / "fmhome"
    project.mkdir(); home.mkdir()
    (fmhome / "state").mkdir(parents=True); (fmhome / "config").mkdir()
    shutil.copytree(REPO / "bin", project / "bin")
    shutil.copytree(REPO / ".opencode/plugins", project / ".opencode/plugins", ignore=shutil.ignore_patterns("fm-busy-state.js"))
    (project / "AGENTS.md").write_text("# Isolated slow-successor fixture\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    shim = lab / "shim"; shim.mkdir()
    real_ln = shutil.which("ln")
    # Slow successor: stall the successor watcher's singleton-lock creation
    # (`ln -s <owner> state/.watch.lock`), once per watcher pid.
    (shim / "ln").write_text(f"""#!/bin/sh
for a in "$@"; do last="$a"; done
case "$last" in
  */.watch.lock)
    if [ -e "{lab}/delay-armed" ] && [ "$PPID" != "$(cat "{lab}/first-watcher")" ] && [ ! -e "{lab}/delayed.$PPID" ]; then
      : > "{lab}/delayed.$PPID"; echo "$(date +%s) delaying lock acquisition of watcher $PPID by {DELAY}s" >> "{lab}/shim.log"; sleep {DELAY}
    fi ;;
esac
exec {real_ln} "$@"
""")
    (shim / "ln").chmod(0o755)
    # The plugin launches the arm through `bash -lc` (macOS path_helper moves /usr/bin ahead of it)
    # but sources the lab home's config/x-mode.env first; prepend the shim there.
    (fmhome / "config/x-mode.env").write_text(f'export PATH="{shim}:$PATH"\n')

    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "fixture", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ACK"}, "finish_reason": None}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                chunk["choices"][0] = {"index": 0, "delta": {}, "finish_reason": "stop"}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass

    provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    config = {"model": "fixture/native", "agents": {"build": {"model": "fixture/native"}}, "snapshots": False,
              "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
              "providers": {"fixture": {"package": "@opencode/ai/providers/openai-compatible", "settings": {"baseURL": f"http://127.0.0.1:{provider.server_port}/v1", "apiKey": "isolated-test-not-a-secret"}, "models": {"native": {"capabilities": {"tools": True, "input": ["text"], "output": ["text"]}, "limit": {"context": 100000, "output": 1000}}}}}}
    (project / "opencode.json").write_text(json.dumps(config))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not (k.startswith(("OPENCODE_", "FM_", "XDG_")) or k in ("TMUX", "HERDR_ENV", "NO_MISTAKES_GATE"))}
    env.update(PATH=f"{shim}:{env['PATH']}", HOME=str(home), TMPDIR=str(lab), FM_HOME=str(fmhome), FM_ROOT_OVERRIDE=str(project), FM_BACKEND="tmux", FM_POLL="1", FM_SIGNAL_GRACE="0", FM_HEARTBEAT="600", TMUX_TMPDIR=str(lab), OPENCODE_DB=str(lab / "native.db"), OPENCODE_DISABLE_MODELS_FETCH="1", OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_LSP_DOWNLOAD="1")
    for kind in ("CONFIG", "DATA", "CACHE", "STATE"):
        p = home / kind.lower(); p.mkdir(); env[f"XDG_{kind}_HOME"] = str(p)
    logpath = lab / "server.log"; log = logpath.open("w")
    process = subprocess.Popen(["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)], cwd=project, env=env, stdout=log, stderr=log)
    headers = {}
    def api(path, body=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, method=method, headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = r.read(); return json.loads(d) if d else None
    def until(check, label, limit=30):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            v = check()
            if v: return v
            time.sleep(0.1)
        raise RuntimeError(f"Timed out: {label}")
    t0 = time.monotonic()
    ts = lambda: f"[t+{time.monotonic()-t0:5.1f}s]"
    try:
        password = until(lambda: next((l.removeprefix("server password ") for l in logpath.read_text().splitlines() if l.startswith("server password ")), None), "server readiness")
        headers["Authorization"] = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
        version = api("/api/info")["version"]
        q = urllib.parse.urlencode({"location[directory]": str(project)})
        until(lambda: (lambda ps: len(ps) == 5 and all(p["state"]["status"] == "active" for p in ps))([p for p in api("/api/plugin?" + q)["data"] if p.get("id", "").startswith("fm.primary.")]), "plugins")
        print(f"{ts()} OpenCode {version} serving; 5 Firstmate plugins active from {REPO}")
        model = until(lambda: next((a.get("model") for a in api("/api/agent?" + q)["data"] if a["id"] == "build"), None), "build model")
        sid = api("/api/session", {"location": {"directory": str(project)}, "agent": "build", "model": model})["data"]["id"]
        api(f"/api/session/{sid}/prompt", {"text": "smoke"})
        until(lambda: api(f"/api/session/{sid}")["data"].get("outcome") == "succeeded", "smoke prompt completion")
        context = lambda: api(f"/api/session/{sid}/context")["data"]
        (fmhome / "state/task.meta").write_text("project=fixture\n")
        (fmhome / "state/.lock").write_text(str(process.pid) + "\n")
        api(f"/api/session/{sid}/prompt", {"text": "arm the watcher"})
        lock = fmhome / "state/.watch.lock/pid"
        until(lock.exists, "watcher arming")
        first = lock.read_text().strip()
        until(lambda: (fmhome / "state/.last-watcher-beat").exists(), "first beat")
        print(f"{ts()} first watcher armed pid={first}")
        (lab / "first-watcher").write_text(first + "\n"); (lab / "delay-armed").write_text("")
        print(f"{ts()} busy machine from now: each successor watcher's lock acquisition delayed {DELAY}s")
        (fmhome / "state/task.status").write_text("done: worker finished\n")
        texts_now = lambda: [m.get("text", "") for m in context()]
        until(lambda: any("WATCHER FIRED" in t or "could not" in t for t in texts_now()), "watcher notification or failure", limit=150)
        print(f"{ts()} plugin delivered the worker-close wake to the session")
        def outcome():
            texts = [m.get("text", "") for m in context()]
            fail = next((t for t in texts if "could not restore watcher continuity" in t or "could not verify a ready successor" in t), None)
            if fail: return ("FAILED", fail)
            beat = fmhome / "state/.last-watcher-beat"
            pid = lock.read_text().strip() if lock.exists() else ""
            marker = lab / f"delayed.{pid}"
            # Healthy only once the successor itself has beaten: a beat newer than the end of its delayed first beat.
            if pid and pid != first and beat.exists() and marker.exists() and beat.stat().st_mtime >= marker.stat().st_mtime + DELAY - 0.5:
                try: os.kill(int(pid), 0); return ("HEALTHY", pid)
                except OSError: return None
            return None
        try:
            result = until(outcome, "successor outcome", limit=90)
        except RuntimeError:
            print("DIAG lock:", lock.read_text() if lock.exists() else None, "first:", first)
            b = fmhome / "state/.last-watcher-beat"; print("DIAG beat age:", time.time() - b.stat().st_mtime if b.exists() else None)
            print("DIAG shim:", (lab / "shim.log").read_text() if (lab / "shim.log").exists() else None)
            print("DIAG files:", sorted(p.name for p in lab.iterdir()))
            for m in context(): print("DIAG ctx:", m.get("type"), (m.get("text") or "")[:300].replace("\n"," | "))
            print(subprocess.run(["ps","-o","pid,ppid,command","-ax"],capture_output=True,text=True).stdout.count(str(lab)), "procs in lab")
            print("\n".join(l for l in subprocess.run(["ps","-o","pid,ppid,command","-ax"],capture_output=True,text=True).stdout.splitlines() if "fm-watch" in l and str(lab) in l))
            raise
        print(f"{ts()} outcome: {result[0]} {result[1].strip()[:600]}")
        if result[0] == "HEALTHY": print(f"{ts()} successor watcher {result[1]} holds the lock after a {DELAY}s-delayed lock acquisition, and has beaten since")
        if result[0] == "HEALTHY":
            time.sleep(3)
            texts = [m.get("text", "") for m in context()]
            assert not any("could not" in t and "watcher" in t for t in texts), "late failure surfaced"
            print(f"{ts()} still healthy 3s later; no continuity failure surfaced in the session")
        print("shim log:\n" + (lab / "shim.log").read_text())
        return result[0]
    finally:
        try: subprocess.run([str(project / "bin/fm-watch-arm.sh"), "--stop"], cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        finally:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            provider.shutdown(); provider.server_close(); log.close()

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fm-oc-slow-") as d:
        r = run(Path(d).resolve())
    print("lab cleaned"); raise SystemExit(0 if r == "HEALTHY" else 3)
