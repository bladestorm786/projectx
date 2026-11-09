"""Temporary relay (runs on a small web host). Everything secret comes from the host's environment, nothing from this
public file. MODE: start (start the Kaggle run once), status (print its status and log tail), fetch (download the
finished run's files, encrypt them and serve them), idle. It always ends by serving ./pub over HTTP."""
import os, sys, json, subprocess, glob, http.server, functools

MODE = os.environ.get("MODE", "idle")
KERNEL = os.environ.get("KERNEL", "")              # owner/slug
PW = os.environ.get("RELAY_PASS", "")
REPO_RAW = os.environ.get("BUNDLE_URL", "")
os.makedirs("pub", exist_ok=True)


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

try:
    if MODE == "start":
        rc, out = status()
        if rc == 0 and "has status" in out:
            print("START skipped: this notebook already exists", flush=True)
        else:
            os.makedirs("nb", exist_ok=True)
            slug = KERNEL.split("/")[1]
            json.dump({"id": KERNEL, "title": slug, "code_file": "run.py", "language": "python", "kernel_type": "script",
                       "is_private": True, "enable_gpu": True, "enable_internet": True, "machine_shape": "NvidiaTeslaT4",
                       "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": []},
                      open("nb/kernel-metadata.json", "w"))
            open("nb/run.py", "w").write(KERNEL_CODE % {"url": REPO_RAW, "pw": PW, "job": os.environ.get("JOB", "job.json")})
            rc, out = k(["kernels", "push", "-p", "nb", "--accelerator", "NvidiaTeslaT4"])
            os.remove("nb/run.py")
            print("START", rc, out.strip()[-400:].replace(PW, "***"), flush=True)
            status()
    elif MODE == "status":
        status()
        rc, out = k(["kernels", "logs", KERNEL], 300)
        lines = []
        for line in out.splitlines():
            s = line.strip().lstrip(",").rstrip(",")
            try:
                lines.append(str(json.loads(s).get("data", "")).rstrip())
            except Exception:
                lines.append(line.rstrip())
        for x in [l for l in lines if l][-int(os.environ.get("TAIL", "40")):]:
            print("LOG", x[:400].replace(PW, "***"), flush=True)
    elif MODE == "fetch":
        status()
        rc, out = k(["kernels", "output", KERNEL, "-p", "dl"], 3000)
        print("OUTPUT", rc, out.strip()[-300:], flush=True)
        n = 0
        for p in sorted(glob.glob("dl/**/*", recursive=True)):
            if os.path.isfile(p):
                name = os.path.relpath(p, "dl").replace("/", "__") + ".enc"
                subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:RELAY_PASS", "-in", p,
                                "-out", "pub/" + name], check=True)
                print("FILE", name, os.path.getsize(p), flush=True)
                os.remove(p)
                n += 1
        print("FETCH done:", n, "files", flush=True)
except Exception as e:
    print("RELAY ERROR", repr(e)[:500].replace(PW or "\0", "***"), flush=True)

h = functools.partial(http.server.SimpleHTTPRequestHandler, directory="pub")
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), h).serve_forever()
