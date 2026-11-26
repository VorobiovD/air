"""Parser-grade + adversarial tests for tool_exec.Sandbox — the headless mode's
read-only trust boundary. Every refusal here is a security property: a
prompt-injected PR diff must not be able to escape the checkout, read a secret,
or run anything but read-only git."""
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import tool_exec  # noqa: E402
from tool_exec import Sandbox, ToolError  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A real git checkout with a normal file, a nested source file, and an
    in-tree secret (.env) to attack."""
    r = tmp_path / "checkout"
    r.mkdir()
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def login(user):\n    return user\n# TODO auth\n")
    (r / ".env").write_text("SECRET_TOKEN=sk-super-secret-value\n")
    (r / "config").mkdir()
    (r / "config" / "secrets.yml").write_text("db_password: hunter2\n")
    env = {**tool_exec._GIT_HARDENED_ENV, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=r, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, env=env, check=True)
    return r


# ---- the happy path (must WORK) ------------------------------------------

def test_read_normal_file(repo):
    s = Sandbox(str(repo))
    out = s.read("src/app.py")
    assert "def login" in out and "\t" in out  # numbered


def test_glob_and_grep_jailed_to_checkout(repo):
    s = Sandbox(str(repo))
    assert "src/app.py" in s.glob("**/*.py")
    hits = s.grep("login", glob="*.py")
    assert "src/app.py:1:" in hits


def test_bash_git_blame_allowed(repo):
    s = Sandbox(str(repo))
    out = s.bash("git blame src/app.py")
    assert "def login" in out


def test_bash_git_log_allowed(repo):
    s = Sandbox(str(repo))
    assert "init" in s.bash("git log --oneline")


# ---- path-jail escapes (must REFUSE) -------------------------------------

def test_read_parent_traversal_refused(repo):
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read("../../../etc/passwd")


def test_read_absolute_outside_refused(repo):
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read("/etc/passwd")


def test_symlink_escape_refused(repo):
    s = Sandbox(str(repo))
    (repo / "evil").symlink_to("/etc")
    with pytest.raises(ToolError):
        s.read("evil/passwd")


# ---- in-tree secret reads (must REFUSE via deny-glob) --------------------

def test_read_dotenv_refused(repo):
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read(".env")


def test_read_secrets_yaml_refused(repo):
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read("config/secrets.yml")   # matches *secret*


def test_grep_skips_secret_files(repo):
    s = Sandbox(str(repo))
    out = s.grep("SECRET_TOKEN")        # the value lives in .env
    assert "sk-super-secret-value" not in out


# ---- the git dispatcher (the adversary's flag/refspec surface) -----------

def test_bash_non_git_refused(repo):
    s = Sandbox(str(repo))
    for cmd in ["rm -rf /", "cat /etc/passwd", "curl http://evil", "python -c 'x'"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_disallowed_git_verb_refused(repo):
    s = Sandbox(str(repo))
    for cmd in ["git push origin main", "git config core.pager x", "git clone http://e",
                "git fetch", "git checkout -- .", "git commit -m x"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_global_option_before_verb_refused(repo):
    # `git -c core.pager=<exec>` is the classic config-exec vector.
    s = Sandbox(str(repo))
    for cmd in ["git -c core.pager=touch\\ pwned blame src/app.py",
                "git -C /etc log", "git --exec-path=/tmp blame src/app.py"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_deny_flag_refused(repo):
    s = Sandbox(str(repo))
    for cmd in ["git log --output=/tmp/pwned", "git diff -O/tmp/x",
                "git log --upload-pack=/bin/sh"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_show_refspec_secret_refused(repo):
    # `git show HEAD:.env` reads the secret from the object DB, bypassing an
    # FS-only jail — the refspec path must still be deny-globbed.
    s = Sandbox(str(repo))
    for cmd in ["git show HEAD:.env", "git show HEAD:config/secrets.yml",
                "git cat-file -p HEAD:.env"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_show_refspec_normal_allowed(repo):
    s = Sandbox(str(repo))
    assert "def login" in s.bash("git show HEAD:src/app.py")


def test_bash_pathspec_secret_refused(repo):
    # A plain pathspec (NOT a `ref:path` refspec) must also be deny-globbed:
    # `git log -p -- .env` / `git diff -- config/secrets.yml` would otherwise dump
    # the in-tree secret's content straight from history, bypassing the deny-glob.
    s = Sandbox(str(repo))
    for cmd in ["git log -p -- .env", "git diff HEAD -- .env", "git show HEAD -- .env",
                "git log --oneline -- config/secrets.yml", "git log -p .env"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_pathspec_normal_allowed(repo):
    # A non-sensitive pathspec is fine — refs / SHAs / `--` / normal paths never
    # match a deny-glob, so legitimate path-scoped history reads still work.
    s = Sandbox(str(repo))
    assert "init" in s.bash("git log --oneline -- src/app.py")


def test_bash_pathspec_magic_refused(repo):
    # Literal-text deny-globbing is defeated by git pathspec MAGIC: `:(glob).e??`
    # and `:(icase).ENV` expand to match `.env`, and `:.env` (staged form) names the
    # blob directly — none match a deny-glob as literal text. Refuse any leading-':'
    # token outright (a read-only review never needs one).
    s = Sandbox(str(repo))
    for cmd in ["git log -p -- :(glob).e??", "git show :(icase).ENV", "git show :.env",
                "git log -p -- :(top).env", "git diff -- :!src"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_wildcard_pathspec_refused(repo):
    # A glob-wildcard pathspec expands git-side to match files the literal token
    # never equals: `.e??` matches `.env`, `*.key` matches a private key, `[se]*`
    # matches `secrets.yml`. The literal deny-glob check can't see the expansion,
    # so refuse any pathspec carrying *, ?, or [ outright.
    s = Sandbox(str(repo))
    for cmd in ["git log -p -- .e??", "git log -p -- *.key", "git diff -- '[se]*'",
                "git show HEAD -- .en?", "git log -- 'config/*.yml'"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_no_index_refused(repo):
    # `git diff --no-index <a> <b>` turns git into a general two-path file differ that
    # reads ARBITRARY filesystem paths (/proc/<ppid>/environ → the bot PAT + API key),
    # bypassing the realpath jail. --no-index must be in the deny-flag screen.
    s = Sandbox(str(repo))
    for cmd in ["git diff --no-index /etc/hosts /dev/null",
                "git diff --no-index .env /dev/null"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_absolute_path_arg_refused(repo):
    # Defense-in-depth: no git arg may name an absolute path outside the checkout.
    # (Commit ranges like HEAD~2..HEAD don't start with '/', so they're unaffected.)
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.bash("git log -- /etc/passwd")


def test_grep_does_not_scan_under_secret_directory(repo):
    # grep must screen the full rel path, not just basename: fnmatch lets `*` cross
    # `/`, so `*secret*` matches `secrets/token.txt` whose basename is innocuous.
    # Without the rel check grep reads a file read()/glob() would refuse.
    (repo / "secrets").mkdir()
    (repo / "secrets" / "token.txt").write_text("API_KEY=sk-leak-me\n")
    s = Sandbox(str(repo))
    assert "sk-leak-me" not in s.grep("API_KEY")


def test_bash_contents_flag_refused(repo):
    # git blame --contents=<file> reads an ARBITRARY filesystem path (the --no-index
    # sibling: leaks /proc/<ppid>/environ → the bot PAT). Refused in every form.
    s = Sandbox(str(repo))
    for cmd in ["git blame --contents=/etc/hosts src/app.py",
                "git blame --contents /etc/hosts src/app.py",
                "git blame --contents=.env src/app.py"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_bash_flag_allowlist_default_deny(repo):
    # Default-deny: path-valued / exec / object-enumerating flags are refused even
    # though they're not in the explicit deny-regex; the small safe set still works.
    s = Sandbox(str(repo))
    for bad in ["git blame -S/tmp/revs src/app.py", "git diff --anchored=/etc/passwd",
                "git log --textconv", "git cat-file --batch", "git diff --ext-diff=/bin/sh"]:
        with pytest.raises(ToolError):
            s.bash(bad)
    assert "def login" in s.bash("git blame -L 1,2 src/app.py")
    assert "init" in s.bash("git log -1 --format=%s")
    assert "def login" in s.bash("git log -p -- src/app.py")


def test_bash_pathless_dump_of_committed_secret_refused(repo):
    # `git log -p` / `git show HEAD` carry no path token, so the path deny-glob never
    # engages. Two layers cover it: git is told to exclude deny-globbed paths (the
    # output simply omits them), and the output scan refuses any diff header that
    # still names one (exercised here via an explicit `-- .`, which skips the
    # git-side exclusion). The fixture commits .env + config/secrets.yml.
    s = Sandbox(str(repo))
    out = s.bash("git show HEAD")
    assert "sk-super-secret-value" not in out and "hunter2" not in out
    assert "src/app.py" in out  # the allowed file is still shown
    for cmd in ["git log -p", "git log -p -- .", "git show HEAD -- ."]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    assert "init" in s.bash("git log --oneline")  # no content dump → allowed


def test_deny_glob_case_insensitive(repo):
    # On a case-insensitive FS (macOS/Windows) `.ENV` opens `.env`; a case-sensitive
    # fnmatch let the spelling slip past the deny-glob. The check now folds case, so
    # case-variant spellings of a deny-globbed secret are refused on EVERY platform
    # (the match is in code, before the file open).
    (repo / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read(".ENV")
    with pytest.raises(ToolError):
        s.read(".Env")
    with pytest.raises(ToolError):
        s.read("server.PEM")
    with pytest.raises(ToolError):
        s.grep("SECRET", path=".ENV")


def test_bash_dash_L_path_smuggle_refused(repo):
    # git log -L<range>:<file> / -L:<funcname>:<file> embeds a file path in the -L flag
    # value, which skips the path screens. Refuse the colon (path-bearing) form; the
    # normal range form (file as a separate arg) still works.
    s = Sandbox(str(repo))
    for cmd in ["git log -L1,1:.env", "git log -L:.*:config/secrets.yml", "git show -L1,1:.env"]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    assert "def login" in s.bash("git blame -L1,2 src/app.py")  # plain range still works


def test_bash_traversal_refused(repo):
    s = Sandbox(str(repo))
    for cmd in ["git log -- ../../../etc/passwd", "git log -- a/../../etc/passwd"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_git_internals_refused(repo):
    # .git/ holds the actions/checkout-persisted bot PAT (extraheader) + hooks/refs.
    # Read/Grep/Glob must refuse .git/* but still allow .gitignore (no slash → not matched).
    (repo / ".gitignore").write_text("*.log\n")
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.read(".git/config")
    assert "config" not in s.glob(".git/**/*")
    assert "repositoryformatversion" not in s.grep("repositoryformatversion")  # .git/config not scanned
    assert "*.log" in s.read(".gitignore")  # legit dotfile still readable


def test_bash_raw_sha_blob_read_refused(repo):
    # A blob read by bare object id has no path the deny-glob can screen, so it is
    # refused outright (`<rev>:<path>` keeps the path screen).
    import subprocess as _sp
    sha = _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD:.env"],
                  capture_output=True, text=True).stdout.strip()
    s = Sandbox(str(repo))
    for cmd in [f"git show {sha}", f"git cat-file -p {sha}", f"git cat-file blob {sha}"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_glob_does_not_surface_secrets(repo):
    # glob() must filter deny-globbed paths like read/grep do — surfacing even the
    # NAME points a prompt-injected agent straight at the secret to read next.
    s = Sandbox(str(repo))
    out = s.glob("**/*")
    assert ".env" not in out and "secrets.yml" not in out
    assert "src/app.py" in out   # normal files still listed


def test_bash_shell_metachars_inert(repo, tmp_path):
    # The security property: NO shell runs, so `;`/`&&`/`|` cannot chain a second
    # command. Either the split makes argv[1] a non-verb (refused), or the
    # metachars become literal args to a read verb (git errors) — never executed.
    s = Sandbox(str(repo))
    marker = tmp_path / "pwned"
    with pytest.raises(ToolError):
        s.bash(f"git log; touch {marker}")     # 'log;' is not an allowed verb
    try:
        s.bash(f"git log && touch {marker}")   # args passed literally to `git log`
    except ToolError:
        pass
    assert not marker.exists()                  # the key property: touch never ran


# ---- dispatch contract (never raises into the loop) ----------------------

def test_dispatch_returns_error_tuple_not_raise(repo):
    s = Sandbox(str(repo))
    txt, is_err = s.dispatch("Read", {"file_path": ".env"})
    assert is_err and "refused" in txt
    txt, is_err = s.dispatch("Bash", {"command": "rm -rf /"})
    assert is_err and "git" in txt
    txt, is_err = s.dispatch("Read", {"file_path": "src/app.py"})
    assert not is_err and "def login" in txt
    txt, is_err = s.dispatch("Nonexistent", {})
    assert is_err


# ---- output guard: header forms, flags, raw objects ------------------------
# A throwaway history in which fake secret files were committed and later removed,
# in a root path, a spaced folder, a Unicode folder, a merge resolution and as a
# tagged blob. Every probe must either be refused or return zero FAKE_SECRET bytes.

def _run_git(cwd, *args):
    env = {**tool_exec._GIT_HARDENED_ENV, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture
def protected_repo(tmp_path):
    r = tmp_path / "protected_repo"
    r.mkdir()
    _run_git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def login(user):\n    return user\n")
    (r / ".env").write_text("TOKEN=FAKE_SECRET_ROOT\n")
    (r / "my dir").mkdir()
    (r / "my dir" / ".env").write_text("TOKEN=FAKE_SECRET_SPACE\n")
    (r / "café").mkdir()
    (r / "café" / ".env").write_text("TOKEN=FAKE_SECRET_UNICODE\n")
    (r / "merge").mkdir()
    (r / "merge" / ".env").write_text("TOKEN=FAKE_SECRET_BASE\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "add files")
    blob = _run_git(r, "rev-parse", "HEAD:.env").stdout.strip()
    _run_git(r, "tag", "-a", "blobtag", blob, "-m", "tagged blob")
    base = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    # two branches editing merge/.env differently -> a conflicted, hand-resolved merge
    _run_git(r, "checkout", "-q", "-b", "side")
    (r / "merge" / ".env").write_text("TOKEN=FAKE_SECRET_SIDE\n")
    _run_git(r, "commit", "-q", "-am", "side edit")
    _run_git(r, "checkout", "-q", "main")
    (r / "merge" / ".env").write_text("TOKEN=FAKE_SECRET_MAIN\n")
    _run_git(r, "commit", "-q", "-am", "main edit")
    _run_git(r, "merge", "side")           # conflicts on merge/.env
    (r / "merge" / ".env").write_text("TOKEN=FAKE_SECRET_MERGED\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "merge side")
    merge = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    for p in (".env", "my dir/.env", "café/.env", "merge/.env"):
        (r / p).unlink()
    (r / "src" / "app.py").write_text("def login(user):\n    return user\n# touched\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "remove files")
    head = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    # sanity: the fixture really produces a combined-diff header for the merge
    assert "diff --cc" in _run_git(r, "show", merge).stdout
    # side branch: a deny-globbed file renamed to an innocent name and back (100% renames)
    _run_git(r, "checkout", "-q", "-b", "renames", base)
    _run_git(r, "mv", ".env", "other.txt")
    _run_git(r, "commit", "-q", "-m", "rename out")
    out_c = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    _run_git(r, "mv", "other.txt", ".env")
    _run_git(r, "commit", "-q", "-m", "rename back")
    back_c = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    _run_git(r, "checkout", "-q", "main")
    assert _run_git(r, "rev-parse", "HEAD").stdout.strip() == head
    return {"root": r, "blob": blob, "base": base, "merge": merge,
            "rename_out": out_c, "rename_back": back_c, "head": head}


def _assert_no_protected_content(sandbox, cmd):
    try:
        out = sandbox.bash(cmd)
    except ToolError:
        return  # refused is fine
    assert "FAKE_SECRET" not in out, f"protected content via: {cmd}"


def test_guard_probes_never_return_protected_content(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    merge, blob, base = protected_repo["merge"], protected_repo["blob"], protected_repo["base"]
    probes = [
        "git log -p", "git log -p --all", "git log -p --graph", "git log --graph -p",
        "git log -p --color=always", "git log -p --graph --color=always",
        "git log -p --color", "git log -p --color=auto", "git log -p -U3 --graph",
        "git log -p -- .", "git log -p -- 'my dir'", "git log -p -- café",
        f"git show {merge}", f"git show {base}", "git show HEAD~1", "git show HEAD~2",
        f"git show {merge} --graph", f"git show {merge} --color=always",
        f"git show {merge} -- .", f"git diff {base} {merge}", f"git diff {base}..{merge}",
        f"git show {blob}", f"git cat-file -p {blob}", f"git cat-file blob {blob}",
        "git show blobtag", "git cat-file -p blobtag", "git show blobtag^{}",
        f"git cat-file -p {blob[:10]}", f"git diff {blob} {blob}",
        "git log -p -- .env", f"git show {base}:.env", f"git show {base}:'my dir/.env'",
        # `--` before the object id must not skip the raw-blob refusal
        f"git cat-file -p -- {blob}", "git cat-file blob -- blobtag",
        "git cat-file -p -- blobtag^{}", f"git cat-file -p -- {blob[:10]}",
        # renames across the deny-glob boundary (the excluded side un-pairs the rename)
        "git log -p renames", "git log -p --all", "git log -p -1 renames",
        "git log -p -z renames", "git log -p --format=format:X renames",
        "git log --format=%B -p renames", f"git show {protected_repo['rename_out']}",
        f"git show {protected_repo['rename_back']}", "git log -p -C -C -1 renames",
        f"git diff {base} {protected_repo['rename_out']}", f"git diff {protected_repo['rename_out']}^ {protected_repo['rename_out']}",
    ]
    for cmd in probes:
        _assert_no_protected_content(s, cmd)


def test_guard_scan_layer_refuses_each_header_form(protected_repo):
    # `-- .` skips the git-side exclusion, so only the output scan stands in the way:
    # plain, spaced-folder, Unicode-folder (quoted) and combined-merge headers.
    s = Sandbox(str(protected_repo["root"]))
    for cmd in ["git log -p -- .", f"git show {protected_repo['merge']} -- .",
                f"git show {protected_repo['base']} -- .", "git log -p -- 'my dir'", "git log -p -- café"]:
        with pytest.raises(ToolError):
            s.bash(cmd)


def test_guard_excludes_deny_globbed_paths_git_side(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    out = s.bash(f"git show {protected_repo['base']}")
    assert "src/app.py" in out and "def login" in out
    assert ".env" not in out
    out = s.bash(f"git show {protected_repo['merge']}")      # merge touching only .env: nothing to show
    assert "FAKE_SECRET" not in out
    with pytest.raises(ToolError, match="git show"):
        s.bash("git log -p")      # log is refused, never silently filtered


def test_guard_graph_and_color_flags(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    for cmd in ["git log -p --graph", "git log --graph -U2", "git log --graph --patch",
                "git show --graph HEAD", "git log -p --color=always", "git log -p --color",
                "git log --oneline --color=auto", "git log --oneline --color-words"]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    # harmless forms keep working
    assert "add files" in s.bash("git log --oneline --graph")
    assert "add files" in s.bash("git log --oneline --color=never")
    assert "add files" in s.bash("git log --oneline --no-color")


def test_guard_raw_blob_and_tag_refused_but_commits_trees_ok(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    for cmd in [f"git show {protected_repo['blob']}", f"git cat-file -p {protected_repo['blob']}",
                "git show blobtag", "git cat-file -p blobtag"]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    assert "add files" in s.bash(f"git cat-file -p {protected_repo['base']}")        # a commit
    assert "src" in s.bash(f"git cat-file -p {protected_repo['base']}^{{tree}}")     # a tree listing
    assert "def login" in s.bash(f"git show {protected_repo['base']}:src/app.py")    # rev:path


def test_guard_ordinary_calls_still_work(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    base = protected_repo["base"]
    assert "add files" in s.bash("git log --oneline")
    assert "def login" in s.bash("git blame -L 1,2 src/app.py")
    assert "def login" in s.bash(f"git show {base}:src/app.py")
    assert "def login" in s.bash("git log -p -- src/app.py")
    assert "def login" in s.bash("git log -p src/app.py")          # path without `--`
    assert "# touched" in s.bash(f"git diff {base}..HEAD -- src/app.py")
    assert "# touched" in s.bash(f"git diff {base}..HEAD")          # path-less, exclusions added
    assert "src/app.py" in s.bash(f"git show --stat {base}")
    assert "add files" in s.bash("git log --format=%s -n 5")


def test_guard_cat_file_dashdash_blob_refused(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    for cmd in [f"git cat-file -p -- {protected_repo['blob']}", "git cat-file blob -- blobtag"]:
        with pytest.raises(ToolError, match="raw blob"):
            s.bash(cmd)
    assert "add files" in s.bash(f"git cat-file -p -- {protected_repo['base']}")  # a commit is still fine


def test_guard_rename_across_deny_boundary_refused(protected_repo):
    s = Sandbox(str(protected_repo["root"]))
    with pytest.raises(ToolError, match="protected path"):
        s.bash("git log -p renames")
    for cmd in [f"git show {protected_repo['rename_out']}", f"git show {protected_repo['rename_back']}",
                f"git diff {protected_repo['base']} {protected_repo['rename_out']}"]:
        with pytest.raises(ToolError, match="deny-globbed"):
            s.bash(cmd)


def test_guard_count_flag_keeps_commit_selection(protected_repo):
    """`log -p -1` must show HEAD (not skip it via exclude-pathspec history simplification);
    HEAD edits a normal file but also deleted .env files, so the scan refuses it rather
    than silently showing a different commit."""
    s = Sandbox(str(protected_repo["root"]))
    try:
        out = s.bash("git log -p -1")
    except ToolError:
        return
    assert protected_repo["head"][:7] in out
    assert "FAKE_SECRET" not in out


# ---- object checks fail closed; commit identity; shared time budget ----------

@pytest.fixture
def plain(tmp_path):
    """Harmless code, an ordinary empty commit, a commit touching only a protected file,
    and a commit mixing a protected and an ordinary file."""
    r = tmp_path / "plain"
    r.mkdir()
    _run_git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def login(user):\n    return user\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "init")
    _run_git(r, "commit", "-q", "--allow-empty", "-m", "deploy checkpoint")
    empty = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    (r / ".env").write_text("TOKEN=FAKE_SECRET_ONLY\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "protected only")
    only = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    (r / ".env").write_text("TOKEN=FAKE_SECRET_MIXED\n")
    (r / "src" / "app.py").write_text("def login(user):\n    return user\n# edited\n")
    _run_git(r, "commit", "-q", "-am", "mixed")
    mixed = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    return {"root": r, "empty": empty, "only": only, "mixed": mixed}


def _spy_run(monkeypatch, fail_type_lookup=None):
    """Wrap the git runner. `fail_type_lookup(argv)` may raise or return a fake process
    for the object-type lookups; every other call is real and recorded."""
    real = subprocess.run
    calls = []

    def fake(argv, *a, **kw):
        calls.append((list(argv), kw.get("timeout")))
        if fail_type_lookup and "cat-file" in argv and "-t" in argv:
            return fail_type_lookup(argv)
        return real(argv, *a, **kw)
    monkeypatch.setattr(tool_exec.subprocess, "run", fake)
    return calls


def _content_calls(calls):
    return [c for c, _ in calls
            if not (("cat-file" in c and "-t" in c) or "rev-parse" in c or "--name-status" in c)
            and any(v in c for v in ("show", "log", "diff", "cat-file"))]


def _failure_modes():
    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    def oserr(argv):
        raise OSError("no git")

    def nonzero(argv):
        return subprocess.CompletedProcess(argv, 1, "", "")

    def odd_stderr(argv):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: something unexpected")

    def odd_stdout(argv):
        return subprocess.CompletedProcess(argv, 0, "weird\n", "")
    return [timeout, oserr, nonzero, odd_stderr, odd_stdout]


def test_guard_indeterminate_object_type_refuses_content_read(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    mixed = plain["mixed"]
    cmds = [f"git show {mixed}", f"git cat-file -p {mixed}", f"git cat-file -p -- {mixed}",
            f"git log -p {mixed}", f"git diff {mixed}~1 {mixed}", f"git show -s {mixed}",
            "git show HEAD", f"git log --format=%H {mixed}"]
    for failing in _failure_modes():
        calls = _spy_run(monkeypatch, failing)
        for cmd in cmds:
            with pytest.raises(ToolError):
                s.bash(cmd)
        assert _content_calls(calls) == [], f"content ran after a failed check ({failing.__name__})"
        monkeypatch.undo()


def test_guard_unknown_name_is_not_read(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    calls = _spy_run(monkeypatch)
    for cmd in ["git show nosuchref", "git log -p nosuch..HEAD", "git cat-file -p nosuch",
                "git show deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    assert _content_calls(calls) == []
    # an existing in-tree path without `--` is still a valid operand
    assert "def login" in s.bash("git log -p src/app.py")


def test_guard_empty_commit_keeps_its_identity(plain):
    s = Sandbox(str(plain["root"]))
    empty = plain["empty"]
    assert empty in s.bash(f"git show {empty}")
    assert s.bash(f"git show -s --format=%H {empty}").strip() == empty
    assert s.bash(f"git log --format=%H -1 {empty}").strip() == empty
    assert empty in s.bash(f"git log --format=%H {empty}")
    # history with nothing protected is not rewritten by a pathspec: the empty commit stays
    assert "deploy checkpoint" in s.bash(f"git log -p {empty}")
    assert _OMIT not in s.bash(f"git show {empty}")              # nothing was filtered


_OMIT = "[air: content of protected paths omitted]"


def test_guard_filtered_commit_keeps_header_and_notes_omission(plain):
    s = Sandbox(str(plain["root"]))
    only, mixed = plain["only"], plain["mixed"]
    out = s.bash(f"git show {only}")
    assert only in out and "protected only" in out and _OMIT in out
    assert "FAKE_SECRET" not in out and "diff --git" not in out
    out = s.bash(f"git show {mixed}")
    assert mixed in out and "# edited" in out and _OMIT in out and "FAKE_SECRET" not in out
    assert s.bash(f"git show --oneline {mixed}").startswith(mixed[:7])
    assert _OMIT in s.bash(f"git diff {only}~1 {mixed}")
    # a revision that does not exist still fails, it is not reported as an omission
    with pytest.raises(ToolError):
        s.bash("git show 0123456789abcdef0123456789abcdef01234567")


def test_guard_log_patch_is_refused_not_filtered(plain):
    """`log` is never path-limited: a range whose patch would show a protected path is
    refused whole (no commit is silently dropped); harmless history and a pathspec on
    code keep working, empty commits included."""
    s = Sandbox(str(plain["root"]))
    with pytest.raises(ToolError, match="git show"):
        s.bash("git log -p")
    with pytest.raises(ToolError, match="git show"):
        s.bash(f"git log -p {plain['only']}")
    out = s.bash(f"git log -p {plain['empty']}")           # harmless history incl. empty commit
    assert "deploy checkpoint" in out and "init" in out and "def login" in out
    out = s.bash("git log -p -- src/app.py")               # a code path still works
    assert "# edited" in out and "FAKE_SECRET" not in out
    assert "deploy checkpoint" not in out                  # path-limiting is the caller's choice
    # metadata-only log still lists every commit, empty and protected-only included
    listing = s.bash("git log --format=%s")
    assert "deploy checkpoint" in listing and "protected only" in listing


def test_guard_show_several_revisions_keeps_each_patch_under_its_header(plain):
    s = Sandbox(str(plain["root"]))
    mixed, empty, only = plain["mixed"], plain["empty"], plain["only"]
    out = s.bash(f"git show {mixed} {empty} {only}")
    assert "FAKE_SECRET" not in out
    i_mixed, i_empty, i_only = (out.index(f"commit {r}") for r in (mixed, empty, only))
    assert i_mixed < i_empty < i_only
    assert "# edited" in out[i_mixed:i_empty]                # patch sits under its own header
    assert "# edited" not in out[i_empty:]                   # the empty commit has none
    assert "diff --git" not in out[i_empty:i_only]
    assert out.count(_OMIT) == 1                             # one trailing note, outside any cut
    assert out.rstrip().endswith(_OMIT)
    # a different order keeps the same pairing
    out = s.bash(f"git show {empty} {mixed}")
    assert out.index(f"commit {empty}") < out.index(f"commit {mixed}") < out.index("# edited")


def test_guard_metadata_only_requests_are_not_filtered(plain):
    s = Sandbox(str(plain["root"]))
    only = plain["only"]
    assert s.bash(f"git show -s --format=%H {only}").strip() == only
    assert "protected only" in s.bash(f"git show --no-patch {only}")
    assert s.bash(f"git log --format=%H -3").split()[1] == only
    assert ".env" in s.bash(f"git show --stat --format= {only}")
    assert ".env" in s.bash(f"git log --name-only --format=%H -1 {only}")
    for cmd in [f"git show -s {only}", f"git log --stat -1 {only}"]:
        assert _OMIT not in s.bash(cmd) and "FAKE_SECRET" not in s.bash(cmd)


def test_guard_time_budget_is_shared_across_subprocesses(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    mixed = plain["mixed"]
    calls = _spy_run(monkeypatch)
    s.bash(f"git show {mixed}")        # type check, pre-pass, header and patch calls
    timeouts = [t for _, t in calls]
    assert len(timeouts) >= 4 and all(t is not None and t <= tool_exec._TOOL_BUDGET_S
                                      for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)   # draws down one allowance


def test_guard_time_budget_exhaustion_refuses(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    mixed = plain["mixed"]
    # a clock that moves 10 s per reading: the 30 s budget runs out mid-call
    ticks = iter(range(0, 10_000, 10))
    monkeypatch.setattr(tool_exec, "_clock", lambda: float(next(ticks)))
    calls = _spy_run(monkeypatch)
    with pytest.raises(ToolError, match="time budget"):
        s.bash(f"git show {mixed}")
    # the filtered patch read (the call carrying the exclusion pathspecs) never started
    assert not [c for c, _ in calls if any(a.startswith(":(exclude") for a in c)]
    monkeypatch.undo()
    # a zero budget refuses before any subprocess starts
    monkeypatch.setattr(tool_exec, "_TOOL_BUDGET_S", 0)
    calls = _spy_run(monkeypatch)
    with pytest.raises(ToolError, match="time budget"):
        s.bash("git log --oneline")
    assert calls == []


def test_guard_refspec_path_split_matches_git(tmp_path):
    # git splits <rev>:<path> at the first ':' outside a {...} group; the screen must
    # check the same path git reads.
    r = tmp_path / "r"
    r.mkdir()
    _run_git(r, "init", "-q")
    (r / "app.py").write_text("print(1)\n")
    (r / ".npmrc").write_text("token=FAKE_SECRET_NPMRC\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "fix: a:b")
    s = Sandbox(str(r))
    for cmd in ["git show HEAD^{/:}:.npmrc", "git cat-file -p HEAD^{/:}:.npmrc",
                "git show HEAD^{/fix:}:.npmrc", "git show HEAD:.npmrc"]:
        out, is_err = s.dispatch("Bash", {"command": cmd})
        assert "FAKE_SECRET" not in out, cmd
        assert is_err, cmd
    out, is_err = s.dispatch("Bash", {"command": "git show HEAD^{/:}:app.py"})
    assert not is_err and "print(1)" in out


# ---- commit selection stays git's; coverage limits are visible; operand coupling ----

@pytest.fixture
def forked(tmp_path):
    """Harmless history with a side branch merged in (so first-parent differs from the
    full walk), then 51 more small commits. Nothing in it is protected."""
    r = tmp_path / "forked"
    r.mkdir()
    _run_git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("v0\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "c0")
    base = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    _run_git(r, "checkout", "-q", "-b", "side")
    (r / "src" / "side.py").write_text("side\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "side work")
    _run_git(r, "checkout", "-q", "main")
    (r / "src" / "app.py").write_text("v1\n")
    _run_git(r, "commit", "-q", "-am", "main work")
    _run_git(r, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    mid = _run_git(r, "rev-parse", "HEAD").stdout.strip()
    for n in range(51):
        (r / "src" / "app.py").write_text(f"n{n}\n")
        _run_git(r, "commit", "-q", "-am", f"step {n}")
    return {"root": r, "base": base, "mid": mid}


def _native(root, *args):
    return _run_git(root, *args).stdout


def test_show_empty_range_shows_nothing_like_git(forked):
    s = Sandbox(str(forked["root"]))
    assert _native(forked["root"], "show", "HEAD..HEAD") == ""
    out = s.bash("git show HEAD..HEAD")
    assert "commit " not in out and out.strip() in ("", "(no output)")


def test_show_first_parent_range_matches_git(forked):
    s = Sandbox(str(forked["root"]))
    mid, base = forked["mid"], forked["base"]
    for cmd in (f"git show --first-parent {base}..{mid}", f"git show --reverse {base}..{mid}",
                f"git show {base}..{mid}", f"git show -n 2 {base}..{mid}",
                f"git show --max-count=1 {base}..{mid}"):
        assert s.bash(cmd) == _native(forked["root"], *cmd.split()[1:]), cmd
    out = s.bash(f"git show --first-parent {base}..{mid}")
    assert "side work" not in out                    # a side-branch commit is not shown
    assert "side work" in s.bash(f"git show {base}..{mid}")


def test_show_range_touching_protected_path_is_refused_with_hint(plain):
    s = Sandbox(str(plain["root"]))
    empty, mixed = plain["empty"], plain["mixed"]
    cmds = [f"git show {empty}~1..HEAD", f"git show {empty}..HEAD", f"git show {empty}...HEAD",
            f"git show ^{empty} HEAD", f"git show {mixed}^!", f"git show {mixed}^@",
            f"git show {mixed}^-", "git show --first-parent HEAD", "git show -n 1 HEAD",
            "git show -1 HEAD", "git show --max-count=1 HEAD", "git show --since=2000-01-01 HEAD",
            "git show --until=2030-01-01 HEAD", "git show --after=2000-01-01 HEAD",
            "git show --before=2030-01-01 HEAD", f"git show --reverse {empty}..HEAD",
            "git show --all"]
    for cmd in cmds:
        with pytest.raises(ToolError, match="one commit at a time"):
            s.bash(cmd)
    # one commit at a time, and a pathspec, both still work
    assert "# edited" in s.bash(f"git show {mixed}")
    assert "# edited" in s.bash(f"git show {empty}..HEAD -- src/app.py")
    # a walk that selects nothing protected goes to git unchanged
    harmless = f"git show {empty}~1..{empty}"
    assert s.bash(harmless) == _native(plain["root"], *harmless.split()[1:])


def test_show_range_of_many_harmless_commits_passes_through(forked):
    s = Sandbox(str(forked["root"]))
    cmd = "git show HEAD~51..HEAD"
    out = s.bash(cmd)
    assert out == _native(forked["root"], *cmd.split()[1:])
    assert out.count("\ncommit ") + out.startswith("commit ") == 51


def test_show_several_revisions_at_the_cap_reports_k_of_n(tmp_path, monkeypatch):
    r = tmp_path / "big"
    r.mkdir()
    _run_git(r, "init", "-q", "-b", "main")
    shas = []
    for n in range(3):
        (r / f"f{n}.py").write_text("".join(f"line {n} {i}\n" for i in range(90)))
        (r / ".env").write_text(f"TOKEN=FAKE_SECRET_{n}\n")
        _run_git(r, "add", "-A")
        _run_git(r, "commit", "-q", "-m", f"big {n}")
        shas.append(_run_git(r, "rev-parse", "HEAD").stdout.strip())
    s = Sandbox(str(r))
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 3000)
    out = s.bash("git show " + " ".join(shas))
    assert len(out) <= 3000 and "FAKE_SECRET" not in out
    assert f"commit {shas[0]}" in out and f"commit {shas[1]}" in out   # headers kept
    assert f"commit {shas[2]}" not in out
    assert "showing 2 of 3 requested commits" in out
    # everything fits: no coverage note
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 60_000)
    out = s.bash("git show " + " ".join(shas))
    assert "requested commits" not in out and "truncated" not in out
    assert all(f"commit {x}" in out for x in shas)
    # one commit alone larger than the cap: cut visibly, still under its own header
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 700)
    out = s.bash(f"git show {shas[0]}")
    assert len(out) <= 700 and f"commit {shas[0]}" in out
    assert "[air: output truncated at 700 characters]" in out
    out = s.bash(f"git show {shas[0]} {shas[1]}")
    assert f"commit {shas[0]}" in out and "truncated at 700" in out
    assert "showing 1 of 2 requested commits" in out


def test_output_cap_is_visible_for_diff_log_and_show(forked, monkeypatch):
    s = Sandbox(str(forked["root"]))
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 250)
    for cmd in ("git log -p", f"git diff {forked['base']}..HEAD -U20 --stat -p",
                f"git show {forked['base']}..HEAD", "git log --format=%H%n%B"):
        out = s.bash(cmd)
        assert len(out) <= 250 and "[air: output truncated at 250 characters]" in out, cmd
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 60_000)
    assert "truncated" not in s.bash("git log -p")


def test_omission_and_truncation_notes_both_survive(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 150)
    out = s.bash(f"git diff {plain['only']}~1 {plain['mixed']} -U100")
    assert len(out) <= 150 and _OMIT in out and "output truncated at 150" in out


def test_colon_operands_without_a_screened_path(plain, tmp_path, monkeypatch):
    s = Sandbox(str(plain["root"]))
    # `<rev>:` names a tree; a verified non-blob object may be read
    assert "src" in s.bash("git show HEAD:")
    assert "src" in s.bash("git cat-file -p HEAD:")
    assert "src" in s.bash("git ls-files")                  # sanity: plain call still fine
    # an unknown name with a trailing colon, or a colon-only spelling, is refused
    for cmd in ["git show nosuch:", "git cat-file -p nosuch:", "git show nosuch^{/:}",
                "git show deadbeefdeadbeefdeadbeefdeadbeefdeadbeef:"]:
        with pytest.raises(ToolError):
            s.bash(cmd)
    # a failed lookup never reaches content execution
    cmds = ["git show HEAD:", "git cat-file -p HEAD:", "git show HEAD:src", "git show HEAD^{/x:}"]
    for failing in _failure_modes():
        calls = _spy_run(monkeypatch, failing)
        for cmd in cmds[:2]:
            with pytest.raises(ToolError):
                s.bash(cmd)
        assert _content_calls(calls) == [], failing.__name__
        monkeypatch.undo()


def test_verify_operands_couples_colon_exemption_to_the_screen(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    budget = tool_exec._Budget()
    looked = []
    real = s._lookup_type
    monkeypatch.setattr(s, "_lookup_type", lambda sp, b: looked.append(sp) or real(sp, b))
    # a colon token the screen produced a path for needs no object lookup
    assert s._verify_operands("show", ["HEAD:src/app.py"], budget, {"HEAD:src/app.py"}) is False
    assert looked == []
    # the same token with NO screened path is verified like any other operand
    s._verify_operands("show", ["HEAD:"], budget, set())
    assert looked == ["HEAD:"]
    # ... and a blob verdict (or a failure) refuses it
    monkeypatch.setattr(s, "_lookup_type", lambda sp, b: "blob")
    with pytest.raises(ToolError, match="raw blob"):
        s._verify_operands("show", ["HEAD:"], budget, set())

    def boom(sp, b):
        raise ToolError("refused: could not verify")
    monkeypatch.setattr(s, "_lookup_type", boom)
    with pytest.raises(ToolError):
        s._verify_operands("show", ["x:"], budget, set())


def test_colon_inside_braces_is_verified_not_assumed(tmp_path):
    r = tmp_path / "r2"
    r.mkdir()
    _run_git(r, "init", "-q")
    (r / "app.py").write_text("print(1)\n")
    _run_git(r, "add", "-A")
    _run_git(r, "commit", "-q", "-m", "fix: a:b")
    s = Sandbox(str(r))
    out = s.bash("git show -s HEAD^{/:}")
    assert "fix: a:b" in out
    with pytest.raises(ToolError):
        s.bash("git show HEAD^{/nomatch:}")


def test_range_with_a_refspec_end_does_not_exempt_the_other_side(plain):
    r = plain["root"]
    blob = _run_git(r, "rev-parse", "HEAD:.env").stdout.strip()
    _run_git(r, "tag", "blobtag", blob)
    s = Sandbox(str(r))
    for cmd in (f"git diff blobtag..HEAD:src/app.py", f"git diff HEAD:src/app.py..blobtag",
                f"git diff {blob[:12]}..HEAD:src/app.py", f"git diff blobtag...HEAD:src/app.py",
                f"git show blobtag..HEAD:src/app.py"):
        with pytest.raises(ToolError):
            s.bash(cmd)
    # a plain refspec and separate operands keep working
    assert "login" in s.bash("git show HEAD:src/app.py")
    assert "src/app.py" in s.bash("git diff HEAD~1 HEAD -- src/app.py")


def test_show_omission_note_survives_truncation(plain, monkeypatch):
    s = Sandbox(str(plain["root"]))
    monkeypatch.setattr(tool_exec, "_MAX_OUTPUT", 200)
    out = s.bash(f"git show {plain['mixed']} -U100")
    assert len(out) <= 200 and "FAKE_SECRET" not in out
    assert _OMIT in out and "output truncated at 200" in out
    out = s.bash(f"git show {plain['empty']} {plain['mixed']} -U100")   # only the first fits
    assert "showing 1 of 2" in out and "FAKE_SECRET" not in out


def test_multi_revision_show_matches_git_format(plain):
    s = Sandbox(str(plain["root"]))
    empty = plain["empty"]
    one = _run_git(plain["root"], "rev-parse", f"{empty}~1").stdout.strip()
    assert s.bash(f"git show {one} {empty}") == _native(plain["root"], "show", one, empty)
    out = s.bash(f"git show {empty} {empty}")                  # a repeated revision shows once
    assert out == _native(plain["root"], "show", empty)


def test_show_of_a_tree_lists_it_once(plain):
    s = Sandbox(str(plain["root"]))
    out = s.bash("git show HEAD^{tree}")
    assert out.count("src/") == 1 and "FAKE_SECRET" not in out


def test_show_aliases_and_annotated_tag_print_once(plain):
    s = Sandbox(str(plain["root"]))
    mixed = plain["mixed"]
    _run_git(plain["root"], "tag", "-a", "rel", "-m", "release", mixed)
    out = s.bash(f"git show HEAD main {mixed}")      # three names, one commit
    assert out.count(f"commit {mixed}") == 1
    out = s.bash("git show rel")                      # annotated tag on a filtered commit
    assert out.count("tag rel") == 1 and out.count(f"commit {mixed}") == 1
    assert "FAKE_SECRET" not in out and "# edited" in out


def test_walk_refusal_wording_holds_for_one_selected_commit(plain):
    s = Sandbox(str(plain["root"]))
    out, is_err = s.dispatch("Bash", {"command": f"git show -1 {plain['only']}"})
    assert is_err and "walks history" in out and "several commits" not in out
