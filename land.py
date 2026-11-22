"""The land action: run the owner's test commands under land's recorder, carrying every command whose recorded reads
are unchanged since main's last green run. Any doubt (no record yet, a recorder that could not see everything, a
command that touched the network) means the command runs."""
import json, os, re, subprocess, sys

GITLAB = bool(os.environ.get("GITLAB_CI"))
T = os.environ.get("RUNNER_TEMP", "/tmp")
STATE, BASE, UNITS, OUT = (os.path.join(T, n) for n in ("land-state", "land-base", "land-units.json", "land-out.json"))
WS = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR") or os.getcwd()
CACHE = os.path.join(WS, ".land-cache")
ORIG = os.path.join(T, "land-cache-orig")


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


TESTS = {
    # the runner, how to spot it in a command, and which files are its tests
    "pytest": (re.compile(r"(^|[\s/])(py\.test|pytest)(\s|$)"), re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")),
    "vitest": (re.compile(r"(^|[\s/])(vitest|jest)(\s|$)"), re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")),
}


def tracked(paths):
    """The repository's files under `paths` (the whole tree when none), as git sees them."""
    r = sh("git", "-C", WS, "ls-files", "--", *(paths or ["."]), capture_output=True, check=False)
    return [l for l in r.stdout.splitlines() if l]


def slug(path):
    """A file's unit name: its path without the extension, in the letters land's recorder accepts."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", re.sub(r"\.[^./]+$", "", path)).strip("-") or "root"


def split_files(name, cmd):
    """One command per test file (land's own split, by the runner's convention). A file that only names its own path is
    carried or rerun on its own, so a change reruns the files that can see it. Returns None when the command is not one
    this knows how to split: it then runs whole, as written."""
    words = cmd.split()
    for kind, (spot, test) in TESTS.items():
        if not spot.search(cmd):
            continue
        # the paths the command already names (directories or files); every other word is a flag or the runner itself
        named = [w for w in words if not w.startswith("-") and os.path.exists(os.path.join(WS, w)) and w not in ("python", "python3")]
        if any(os.path.isfile(os.path.join(WS, w)) for w in named):
            return None  # it already names its files: it runs them, whole
        files = sorted(f for f in tracked(named) if test.search(f) and "node_modules/" not in f)
        if not files:
            return None
        base = " ".join(w for w in words if w not in named)
        # pytest says 5 when a file holds no tests: a file with nothing to run passes
        tail = "; rc=$?; [ $rc -eq 5 ] && exit 0; exit $rc" if kind == "pytest" else ""
        return {slug(f): f"{base} {f}{tail}" for f in files}
    if re.search(r"(^|\s)go\s+test(\s|$)", cmd) and "./..." in words:
        pkgs = sorted({os.path.dirname(f) or "." for f in tracked([]) if f.endswith("_test.go")})
        if not pkgs:
            return None
        return {slug(p): cmd.replace("./...", "./" + p if p != "." else ".") for p in pkgs}
    return None


def prepare():
    if GITLAB:
        # root keeps using git in the checkout after handing it to the land user
        sh("git", "config", "--global", "--add", "safe.directory", "*")
    # the units: one per line, `name: command` or a bare command
    units = {}
    for i, line in enumerate(l.strip() for l in os.environ["RUN_LINES"].splitlines()):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+):\s+(.+)$", line)
        name, cmd = (m.group(1), m.group(2)) if m else (f"cmd{len(units) + 1}", line)
        wd = os.environ.get("WD", ".")
        parts = (split_files(name, cmd) if os.environ.get("SPLIT", "none") == "files" and wd in ("", ".") else None) or {name: cmd}
        # the job's own toolchain: the commands see what the workflow's earlier steps installed
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "PYTHONPATH", "VIRTUAL_ENV", "pythonLocation", "GOPATH", "GOROOT",
               "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME", "NODE_PATH", "CI") if os.environ.get(k)}
        if GITLAB:
            env["HOME"] = "/home/land"
        # Python caches compiled files beside its sources, including in a toolchain the job installed (setup-python's
        # folder is writable): one command would read what the one before it wrote, and nothing could be carried
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for n, c in parts.items():
            n = n if n not in units else f"{name}-{n}"
            units[n] = {"command": c if wd in ("", ".") else f"cd {wd} && {c}", "env": env}
    if not units:
        sys.exit("land: `run` names no command")
    json.dump(units, open(UNITS, "w"))
    os.makedirs(STATE, exist_ok=True)
    if GITLAB and os.path.isdir(CACHE):
        # GitLab restores its cache inside the project; the record itself lives outside the tree under test
        sh("cp", "-a", CACHE + "/.", STATE)
        sh("cp", "-a", CACHE, ORIG)
        # and out of the checkout: a test that lists the project root must see the same entries as on main
        sh("rm", "-rf", CACHE)
    # the base: the PR's base, or the commit this push came after; a first run checks against itself
    base = (os.environ.get("BASE_PR") or os.environ.get("BASE_PUSH")
            or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or os.environ.get("CI_COMMIT_BEFORE_SHA") or "")
    if not base or set(base) == {"0"}:
        base = sh("git", "-C", WS, "rev-parse", "HEAD", capture_output=True).stdout.strip()
    if sh("git", "-C", WS, "cat-file", "-e", base + "^{commit}", check=False, capture_output=True).returncode:
        sh("git", "-C", WS, "fetch", "--no-tags", "--depth=1", "origin", base)
    # a clone, not a worktree: a worktree's .git is a file, the checkout's a folder, so the root would never list alike
    # it borrows the checkout's objects (the checkout is often shallow, which a clone refuses)
    sh("git", "init", "-q", BASE)
    with open(os.path.join(BASE, ".git", "objects", "info", "alternates"), "w") as f:
        f.write(os.path.join(WS, ".git", "objects") + "\n")
    if os.path.exists(os.path.join(WS, ".git", "shallow")):
        sh("cp", os.path.join(WS, ".git", "shallow"), os.path.join(BASE, ".git", "shallow"))
    sh("git", "-C", BASE, "checkout", "-q", "--detach", base)
    if GITLAB:
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
    # GitLab: the recorder runs as root and makes each command's fresh HOME and TMPDIR; with umask 0 the land user
    # can write them, so a test's temp files stay in its own private folders instead of falling back to /tmp
    p = subprocess.run(cmd, text=True, capture_output=True, preexec_fn=(lambda: os.umask(0)) if GITLAB else None)
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
    # main's record moves only on green. A pull request keeps its own record of whatever this run could check, so its
    # next push carries what this one proved; it lives in the pull request's cache scope, which main never reads
    on_pr = not GITLAB and os.environ.get("GITHUB_EVENT_NAME") in ("pull_request", "pull_request_target")
    out("save", "true" if (on_main and rc == 0) or (on_pr and rc in (0, 1)) else "false")
    if GITLAB:
        # only a green run on the default branch writes the record the next pipelines carry from; every other job
        # hands GitLab back exactly the record it restored
        sh("rm", "-rf", CACHE)
        if on_main and rc == 0:
            sh("cp", "-a", STATE, CACHE)
        elif os.path.isdir(ORIG):
            sh("cp", "-a", ORIG, CACHE)
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
