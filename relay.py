"""Temporary relay (runs on a small web host). Everything secret comes from the host's environment, nothing from this
public file. MODE: start (start the Kaggle run once), status (print its status and log tail), fetch (download the
finished run's files, encrypt them and serve them), idle. It always ends by serving ./pub over HTTP."""
import os, sys, json, subprocess, glob, http.server, functools, time, threading

MODE = os.environ.get("MODE", "idle")
ACCEL = os.environ.get("ACCEL", "NvidiaTeslaT4")        # or NvidiaTeslaP100
KERNEL = os.environ.get("KERNEL", "")              # owner/slug
PW = os.environ.get("RELAY_PASS", "")
REPO_RAW = os.environ.get("BUNDLE_URL", "")
os.makedirs("pub", exist_ok=True)
_h = functools.partial(http.server.SimpleHTTPRequestHandler, directory="pub")
threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), _h).serve_forever,
                 daemon=True).start()


def k(args, timeout=900):
    r = subprocess.run(["kaggle"] + args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def status():
    rc, out = k(["kernels", "status", KERNEL], 120)
    print("STATUS", rc, out.strip()[-300:], flush=True)
    return rc, out


KERNEL_CODE = r'''
import os, subprocess, urllib.request
W = "/home/claude/two-gardens"
os.makedirs(W, exist_ok=True)
urllib.request.urlretrieve("%(url)s", "/tmp/b.enc")
os.environ["RP"] = "%(pw)s"
subprocess.run("openssl enc -d -aes-256-cbc -pbkdf2 -pass env:RP -in /tmp/b.enc | tar xz -C " + W, shell=True, check=True)
os.environ.pop("RP")
os.environ["LOCAL_OUT"] = "/kaggle/working/relay"
os.environ["JOB_FILE"] = W + "/kaggle/%(job)s"
os.makedirs("/kaggle/working/relay", exist_ok=True)
p = subprocess.Popen(["bash", W + "/kaggle/run.sh"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
for line in p.stdout:
    print(line, end="", flush=True)
print("JOB EXIT CODE", p.wait())
'''

def start():
    rc, out = status()
    if rc == 0 and "has status" in out and os.environ.get("FORCE") != "1":
        print("START skipped: this notebook already exists", flush=True)
        return
    os.makedirs("nb", exist_ok=True)
    slug = KERNEL.split("/")[1]
    json.dump({"id": KERNEL, "title": slug, "code_file": "run.py", "language": "python", "kernel_type": "script",
               "is_private": True, "enable_gpu": True, "enable_internet": True, "machine_shape": ACCEL,
               "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": []},
              open("nb/kernel-metadata.json", "w"))
    open("nb/run.py", "w").write(KERNEL_CODE % {"url": REPO_RAW, "pw": PW, "job": os.environ.get("JOB", "job.json")})
    rc, out = k(["kernels", "push", "-p", "nb", "--accelerator", ACCEL])
    os.remove("nb/run.py")
    print("START", rc, out.strip()[-400:].replace(PW, "***") if PW else out.strip()[-400:], flush=True)
    status()


try:
    if MODE == "start":
        start()
    elif MODE == "status":
        status()
        try:      # a running session's log only comes as a stream: read it for a while, keep what arrived
            r = subprocess.run(["kaggle", "kernels", "logs", KERNEL, "--follow"], capture_output=True, text=True, timeout=45)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout or b""
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        lines = []
        for line in out.splitlines():
            s = line.strip().lstrip(",").rstrip(",")
            try:
                lines.append(str(json.loads(s).get("data", "")).rstrip())
            except Exception:
                lines.append(line.rstrip())
        for x in [l for l in lines if l][-int(os.environ.get("TAIL", "40")):]:
            print("LOG", x[:400].replace(PW, "***"), flush=True)
    elif MODE == "cancel":            # cancel my own stuck, still-queued run (CANCEL = its slug)
        # the session id comes from the CLI's own search call (the SDK builds the request; a hand-made one got 404)
        from kaggle.api.kaggle_api_extended import KaggleApi
        from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest
        kapi = KaggleApi()
        kapi.authenticate()
        slug = os.environ.get("CANCEL", "")
        owner = KERNEL.split("/")[0]
        rc, st = k(["kernels", "status", owner + "/" + slug], 120)
        print("CANCEL status of", slug, st.strip()[-200:], flush=True)
        if "QUEUED" not in st:
            print("CANCEL skipped: not queued", flush=True)
        else:
            sid = None
            for q in (slug, slug.replace("-", " ")):
                try:
                    resp = kapi.search(query=q, document_types=["notebook"], mine=True, page_size=50)
                except Exception as e:
                    print("CANCEL search error", repr(e)[:300], flush=True)
                    continue
                for d in (resp.documents or []):
                    kd = d.kernel_document
                    s = kd.session_id if kd else None
                    print("CANCEL found", d.slug, "|", d.title, "| session", s, "|", d.update_time, flush=True)
                    if s and (d.slug or "").split("/")[-1] == slug or (s and d.title == slug):
                        sid = s
                if sid:
                    break
            if sid:
                with kapi.build_kaggle_client() as kc:
                    req = ApiCancelKernelSessionRequest()
                    req.kernel_session_id = int(sid)
                    r = kc.kernels.kernels_api_client.cancel_kernel_session(req)
                print("CANCEL", slug, "session", sid, "->", (getattr(r, "error_message", None) or "ok"), flush=True)
                time.sleep(20)
                print("CANCEL status now", k(["kernels", "status", owner + "/" + slug], 120)[1].strip()[-200:], flush=True)
            else:
                print("CANCEL no session id found for", slug, flush=True)
    elif MODE == "sessions":          # which of my notebooks have a run going (to see what fills the GPU places)
        from kaggle.api.kaggle_api_extended import KaggleApi
        kapi = KaggleApi()
        kapi.authenticate()
        ks = kapi.kernels_list(mine=True, page_size=100, sort_by="dateRun") or []
        print("SESSIONS", len(ks), "notebooks", flush=True)
        for kk in ks[:60]:
            rc, st = k(["kernels", "status", kk.ref], 90)
            print("SESS", kk.ref, "| last run", kk.last_run_time, "| gpu", kk.enable_gpu, "|", kk.machine_shape, "|",
                  st.strip()[-60:], flush=True)
        try:
            resp = kapi.search(query="relay", document_types=["notebook"], mine=True, page_size=100)
            for d in (resp.documents or []):
                print("SESS search", d.slug, "| session", d.kernel_document.session_id if d.kernel_document else None,
                      "|", d.update_time, flush=True)
        except Exception as e:
            print("SESS search error", repr(e)[:200], flush=True)
    elif MODE == "probe":             # raw answers about one of my notebooks (PROBE = slugs), to find a run's session id
        from kaggle.api.kaggle_api_extended import KaggleApi
        kapi = KaggleApi()
        kapi.authenticate()
        owner = KERNEL.split("/")[0]
        red = lambda s: (s or "").replace(PW, "***") if PW else (s or "")
        with kapi.build_kaggle_client() as kc:
            hc = kc._http_client          # (not 'http': that name is the http.server module below)
            hc._init_session()
            base = hc._endpoint
            for slug in os.environ.get("PROBE", "").split(","):
                for name, body in (("GetKernelSessionStatus", {"userName": owner, "kernelSlug": slug}),
                                   ("ListKernelSessionOutput", {"userName": owner, "kernelSlug": slug, "pageSize": 5}),
                                   ("GetKernel", {"userName": owner, "kernelSlug": slug})):
                    try:
                        r = hc._session.post("%s/v1/kernels.KernelsApiService/%s" % (base, name), json=body,
                                               headers=hc._session.headers, auth=hc._session.auth, timeout=60)
                        t = r.text
                        if name == "GetKernel":
                            try:
                                t = json.dumps(r.json().get("metadata", {}))
                            except Exception:
                                t = "(not json)"
                        print("PROBE", slug, name, r.status_code, red(t)[:700], flush=True)
                    except Exception as e:
                        print("PROBE", slug, name, "error", repr(e)[:200], flush=True)
                try:
                    r = hc._session.get("%s/v1/kernels/logs/stream/%s/%s" % (base, owner, slug), stream=True, timeout=20,
                                          headers={"Accept": "text/event-stream, */*",
                                                   "User-Agent": hc._session.headers.get("User-Agent", "")},
                                          auth=hc._session.auth)
                    hd = {kk: vv for kk, vv in r.headers.items() if kk.lower() not in ("set-cookie",)}
                    body = b""
                    try:
                        for ch in r.iter_content(512):
                            body += ch
                            if len(body) > 600:
                                break
                    except Exception:
                        pass
                    r.close()
                    print("PROBE", slug, "logs", r.status_code, json.dumps(hd)[:500], red(body.decode("utf-8", "replace"))[:600],
                          flush=True)
                except Exception as e:
                    print("PROBE", slug, "logs error", repr(e)[:200], flush=True)
    elif MODE == "delete":            # delete one of my own relay notebooks (DELETE = its slug), only when confirmed
        slug = os.environ.get("DELETE", "")
        owner = KERNEL.split("/")[0]
        if os.environ.get("CONFIRM_DELETE") != "yes" or not slug.startswith("tg-toy-relay-"):
            print("DELETE refused: needs CONFIRM_DELETE=yes and a tg-toy-relay-* slug", flush=True)
        else:
            rc, out = k(["kernels", "delete", owner + "/" + slug, "-y"], 180)
            print("DELETE", slug, rc, out.strip()[-300:], flush=True)
            time.sleep(15)
            print("DELETE status now", k(["kernels", "status", owner + "/" + slug], 120)[1].strip()[-200:], flush=True)
    elif MODE == "displace":          # a new, CPU-only version of my own stuck notebook (DISPLACE = its slug) that just
        slug = os.environ.get("DISPLACE", "")     # prints a line - in case a new version takes the stuck run's place
        owner = KERNEL.split("/")[0]
        if not slug.startswith("tg-toy-relay-"):
            print("DISPLACE refused: not a tg-toy-relay-* slug", flush=True)
        else:
            print("DISPLACE before", k(["kernels", "status", owner + "/" + slug], 120)[1].strip()[-120:], flush=True)
            os.makedirs("nbx", exist_ok=True)
            json.dump({"id": owner + "/" + slug, "title": slug, "code_file": "run.py", "language": "python",
                       "kernel_type": "script", "is_private": True, "enable_gpu": False, "enable_internet": False,
                       "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": []},
                      open("nbx/kernel-metadata.json", "w"))
            open("nbx/run.py", "w").write('print("this version replaces a run that was stuck in the queue")\n')
            rc, out = k(["kernels", "push", "-p", "nbx"])
            print("DISPLACE push", rc, out.strip()[-300:], flush=True)
            for _ in range(6):
                time.sleep(20)
                print("DISPLACE status", k(["kernels", "status", owner + "/" + slug], 120)[1].strip()[-120:], flush=True)
    elif MODE == "chain":             # wait for WAIT_FOR's run to end, then start KERNEL (JOB, ACCEL)
        wait = os.environ.get("WAIT_FOR", "")
        t0, st = time.time(), ""
        while time.time() - t0 < float(os.environ.get("WAIT_MIN", "40")) * 60:
            rc, st = k(["kernels", "status", wait], 120)
            print("CHAIN", time.strftime("%H:%M:%S"), wait, st.strip()[-60:], flush=True)
            if any(x in st for x in ("COMPLETE", "ERROR", "CANCEL")):
                break
            time.sleep(60)
        if any(x in st for x in ("COMPLETE", "ERROR", "CANCEL")):
            start()
        else:
            print("CHAIN gave up waiting", flush=True)
    elif MODE == "quota":
        rc, out = k(["quota"], 120)
        for line in out.strip().splitlines()[-30:]:
            print("QUOTA", line[:300], flush=True)
    elif MODE == "fetch":
        status()
        rc, out = k(["kernels", "output", KERNEL, "-p", "dl"], 3000)
        print("OUTPUT", rc, out.strip()[-300:], flush=True)
        n = 0
        for p in sorted(glob.glob("dl/**/*", recursive=True)):
            if not os.path.isfile(p):
                continue
            base = os.path.relpath(p, "dl").replace("/", "__")
            size = os.path.getsize(p)
            parts = [p]
            if size > 90 * 2 ** 20:          # GitHub refuses files over 100 MB: send big ones in pieces
                subprocess.run(["split", "-b", "85m", "-d", "-a", "2", p, p + ".part"], check=True)
                os.remove(p)
                parts = sorted(glob.glob(p + ".part*"))
            for q in parts:
                name = (base + q[len(p):]) + ".enc"
                subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:RELAY_PASS", "-in", q,
                                "-out", "pub/" + name], check=True)
                print("FILE", name, os.path.getsize(q), flush=True)
                os.remove(q)
                n += 1
        print("FETCH done:", n, "files", flush=True)
except Exception as e:
    print("RELAY ERROR", repr(e)[:500].replace(PW or "\0", "***"), flush=True)

while True:              # keep serving ./pub
    time.sleep(3600)
