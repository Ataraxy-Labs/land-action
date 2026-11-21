"""The land action: run the owner's test commands under land's recorder, carrying every command whose recorded reads
are unchanged since main's last green run. Any doubt (no record yet, a recorder that could not see everything, a
command that touched the network) means the command runs."""
import json, os, re, subprocess, sys

GITLAB = bool(os.environ.get("GITLAB_CI"))
T = os.environ.get("RUNNER_TEMP", "/tmp")
STATE, BASE, UNITS, OUT = (os.path.join(T, n) for n in ("land-state", "land-base", "land-units.json", "land-out.json"))
WS = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR") or os.getcwd()
CACHE = os.path.join(WS, ".land-cache")


def sh(*a, check=True, **k):
    return subprocess.run(a, check=check, text=True, **k)


def out(k, v):
    # GitHub reads step outputs from GITHUB_OUTPUT; on GitLab the job's exit code is the only output
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"{k}={v}\n")


def gitlab_user():
    """GitLab jobs run as root. Tests run as root write into system folders (Python's __pycache__ under /usr/local)
    and the next command reads what the last one wrote, so nothing could be carried. Run them as an ordinary user
    that owns the checkout, as GitHub's runner user does: it can read every system package, and write only its own."""
    import pwd
    try:
        u = pwd.getpwnam("land")
    except KeyError:
        sh("useradd", "-m", "-s", "/bin/bash", "land", capture_output=True)
        u = pwd.getpwnam("land")
    return u


def prepare():
    # the units: one per line, `name: command` or a bare command
    units = {}
    for i, line in enumerate(l.strip() for l in os.environ["RUN_LINES"].splitlines()):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+):\s+(.+)$", line)
        name, cmd = (m.group(1), m.group(2)) if m else (f"cmd{len(units) + 1}", line)
        wd = os.environ.get("WD", ".")
        cmd = cmd if wd in ("", ".") else f"cd {wd} && {cmd}"
        # the job's own toolchain: the commands see what the workflow's earlier steps installed
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "PYTHONPATH", "VIRTUAL_ENV", "pythonLocation", "GOPATH", "GOROOT",
               "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME", "NODE_PATH", "CI") if os.environ.get(k)}
        if GITLAB:
            env["HOME"] = "/home/land"
        units[name] = {"command": cmd, "env": env}
    if not units:
        sys.exit("land: `run` names no command")
    json.dump(units, open(UNITS, "w"))
    os.makedirs(STATE, exist_ok=True)
    if GITLAB and os.path.isdir(CACHE):
        # GitLab restores its cache inside the project; the record itself lives outside the tree under test
        sh("cp", "-a", CACHE + "/.", STATE)
    # the base: the PR's base, or the commit this push came after; a first run checks against itself
    base = (os.environ.get("BASE_PR") or os.environ.get("BASE_PUSH")
            or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or os.environ.get("CI_COMMIT_BEFORE_SHA") or "")
    if not base or set(base) == {"0"}:
        base = sh("git", "-C", WS, "rev-parse", "HEAD", capture_output=True).stdout.strip()
    if sh("git", "-C", WS, "cat-file", "-e", base + "^{commit}", check=False, capture_output=True).returncode:
        sh("git", "-C", WS, "fetch", "--no-tags", "--depth=1", "origin", base)
    sh("git", "-C", WS, "worktree", "add", "--detach", "-f", BASE, base, capture_output=True)
    if GITLAB:
        # root keeps using git in a checkout it no longer owns
        sh("git", "config", "--global", "--add", "safe.directory", "*")
        u = gitlab_user()
        for d in (WS, BASE):
            sh("chown", "-R", f"{u.pw_uid}:{u.pw_gid}", d)
    print(f"land: {len(units)} command(s), checked against {base[:12]}")


def check():
    binp = os.environ["LAND_BIN"]
    os.chmod(binp, 0o755)
    keep = [f"{k}={os.environ[k]}" for k in ("PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV", "GOPATH", "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME") if os.environ.get(k)]
    # GitHub: the recorder needs root (sudo) and the commands run as the runner user. GitLab: the job is already
    # root in a privileged container, and the commands run as the job does
    pre = [] if GITLAB else ["sudo", "env", *keep]
    if GITLAB:
        u = gitlab_user()
        uid, gid = u.pw_uid, u.pw_gid
    else:
        uid, gid = os.getuid(), os.getgid()
    wrap = ["--wrap", f"setpriv --reuid={uid} --regid={gid} --init-groups bash -c"]
    cmd = [*pre, binp, "check", "--base", BASE, "--cand", WS,
           "--state", STATE, "--units-file", UNITS, "--sensor", "bpf", "--json", *wrap]
    p = subprocess.run(cmd, text=True, capture_output=True)
    sys.stderr.write(p.stderr[-20000:])
    if not GITLAB:
        sh("sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", STATE, check=False)
    said = {}
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("{"):
            try:
                said = json.loads(line)
                break
            except ValueError:
                pass
    open(OUT, "w").write(json.dumps(said, indent=1))
    failing = sorted(n for n, v in (said.get("verdicts") or {}).items() if v == "failed")
    rc = p.returncode if p.returncode not in (0,) else (1 if failing else 0)
    summary(said, rc)
    if GITLAB:
        on_main = os.environ.get("CI_PIPELINE_SOURCE") == "push" and os.environ.get("CI_COMMIT_BRANCH") == os.environ.get("CI_DEFAULT_BRANCH")
    else:
        on_main = os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("GITHUB_REF_NAME", "") == os.environ.get("LAND_MAIN")
    out("save", "true" if (on_main and rc == 0) else "false")
    if GITLAB and on_main and rc == 0:
        # only a green run on the default branch writes the record the next pipelines carry from
        sh("rm", "-rf", CACHE)
        sh("cp", "-a", STATE, CACHE)
    if rc not in (0, 1):
        print(p.stdout[-4000:])
    for n in failing if not GITLAB else []:
        print(f"::error title=land: {n} failed::" + (said.get("failed_text") or {}).get(n, "")[-1500:].replace("\n", "%0A"))
    sys.exit(rc)


def summary(said, rc):
    ran, carried = said.get("ran"), said.get("carried")
    out("ran", "" if ran is None else ran)
    out("carried", "" if carried is None else carried)
    why = {}
    for w in said.get("why") or []:
        n, _, r = w.partition(": ")
        why[n.replace("suite::", "")] = r
    verdicts = {n.replace("suite::", ""): v for n, v in (said.get("verdicts") or {}).items()}
    chosen = {n.replace("suite::", "") for n in said.get("chosen") or []}
    head = {0: "Green", 1: "A command failed"}.get(rc, "land could not check this run")
    lines = ["## land", "", f"**{head}.** " + (f"{ran} ran, {carried} carried from main without running." if ran is not None else ""), "",
             "| command | | result | why |", "|---|---|---|---|"]
    for n in sorted(verdicts):
        mark = "ran" if n in chosen else "carried"
        icon = "✅" if verdicts[n] == "passed" else "❌"
        lines.append(f"| `{n}` | {mark} | {icon} {verdicts[n]} | {why.get(n, '')} |")
    text = said.get("failed_text") or {}
    if any("No module named" in t for t in text.values()) and os.path.isdir(os.path.expanduser("~/.local")):
        lines += ["", "> Packages installed with `pip install --user` (in your home directory) are not visible to land's recorder. "
                  "Install them with `actions/setup-python`, or `sudo python3 -m pip install`, before this step."]
    for n, t in sorted(text.items()):
        lines += ["", f"<details><summary>{n.replace('suite::', '')} output</summary>", "", "```", t[-6000:], "```", "</details>"]
    lines += ["", "Commands are carried only when every file they read on main's last green run is unchanged. "
              "[How land decides](https://docs.useland.dev/fast)"]
    # GitHub shows the step summary on the run; GitLab keeps land-summary.md as the job's artifact
    dest = os.environ.get("GITHUB_STEP_SUMMARY") or os.path.join(WS, "land-summary.md")
    with open(dest, "a") as f:
        f.write("\n".join(lines) + "\n")
    print(f"land: {head}. ran {ran}, carried {carried}")
    for n in sorted(verdicts):
        print(f"  {n}: {'ran' if n in chosen else 'carried'}, {verdicts[n]}  ({why.get(n, '')})")


{"prepare": prepare, "check": check}[sys.argv[1]]()
