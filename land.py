"""The land action: run the owner's test commands under land's recorder, carrying every command whose recorded reads
are unchanged since main's last green run. Any doubt (no record yet, a recorder that could not see everything, a
command that touched the network) means the command runs."""
import json, os, re, subprocess, sys

T = os.environ.get("RUNNER_TEMP", "/tmp")
STATE, BASE, UNITS, OUT = (os.path.join(T, n) for n in ("land-state", "land-base", "land-units.json", "land-out.json"))
WS = os.environ.get("GITHUB_WORKSPACE", os.getcwd())


def sh(*a, check=True, **k):
    return subprocess.run(a, check=check, text=True, **k)


def out(k, v):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{k}={v}\n")


def prepare():
    # the units: one per line, `name: command` or a bare command
    units = {}
    for i, line in enumerate(l.strip() for l in os.environ["RUN_LINES"].splitlines()):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+):\s+(.+)$", line)
        name, cmd = (m.group(1), m.group(2)) if m else (f"cmd{len(units) + 1}", line)
        wd = os.environ.get("WD", ".")
        units[name] = cmd if wd in ("", ".") else f"cd {wd} && {cmd}"
    if not units:
        sys.exit("land: `run` names no command")
    json.dump(units, open(UNITS, "w"))
    os.makedirs(STATE, exist_ok=True)
    # the base: the PR's base, or the commit this push came after; a first run checks against itself
    base = os.environ.get("BASE_PR") or os.environ.get("BASE_PUSH") or ""
    if not base or set(base) == {"0"}:
        base = sh("git", "-C", WS, "rev-parse", "HEAD", capture_output=True).stdout.strip()
    if sh("git", "-C", WS, "cat-file", "-e", base + "^{commit}", check=False, capture_output=True).returncode:
        sh("git", "-C", WS, "fetch", "--no-tags", "--depth=1", "origin", base)
    sh("git", "-C", WS, "worktree", "add", "--detach", "-f", BASE, base, capture_output=True)
    print(f"land: {len(units)} command(s), checked against {base[:12]}")


def check():
    binp = os.environ["LAND_BIN"]
    os.chmod(binp, 0o755)
    cmd = ["sudo", "-E", "env", f"PATH={os.environ['PATH']}", binp, "check", "--base", BASE, "--cand", WS,
           "--state", STATE, "--units-file", UNITS, "--sensor", "bpf", "--json"]
    p = subprocess.run(cmd, text=True, capture_output=True)
    sys.stderr.write(p.stderr[-20000:])
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
    summary(said, p.returncode)
    branch = os.environ.get("GITHUB_REF_NAME", "")
    on_main = os.environ.get("GITHUB_EVENT_NAME") == "push" and branch == os.environ.get("LAND_MAIN")
    out("save", "true" if (on_main and p.returncode == 0) else "false")
    if p.returncode not in (0, 1):
        print(p.stdout[-4000:])
    sys.exit(p.returncode)


def summary(said, rc):
    tests = said.get("tests") or {}
    ran, carried = said.get("ran"), said.get("carried")
    out("ran", ran if ran is not None else "")
    out("carried", carried if carried is not None else "")
    lines = ["## land", ""]
    head = {0: "Green", 1: "A command newly fails"}.get(rc, "land could not check this run, so it ran nothing it could vouch for")
    lines.append(f"**{head}.** " + (f"{ran} ran, {carried} carried from main without running." if ran is not None else ""))
    lines += ["", "| command | result |", "|---|---|"]
    for name, v in sorted((said.get("verdicts") or said.get("units") or {}).items()):
        lines.append(f"| `{name}` | {v if isinstance(v, str) else json.dumps(v)[:120]} |")
    lines += ["", "<details><summary>land's answer</summary>", "", "```json", json.dumps(said, indent=1)[:60000], "```", "</details>"]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(lines) + "\n")


{"prepare": prepare, "check": check}[sys.argv[1]]()
