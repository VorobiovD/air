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


@pytest.mark.xfail(reason="accepted defense-in-depth gap: a raw-object-SHA read has no "
                          "path to deny-glob; backstop is the clean-clone assumption",
                   strict=False)
def test_bash_raw_sha_blob_read_known_gap(repo):
    # Documents the disclosed limit (tool_exec docstring): `git show <blob-sha>` reads a
    # blob by SHA with no path the deny-glob can screen. xfail keeps the gap visible so
    # accidental hardening doesn't silently mask it (and flips to pass if ever closed).
    import subprocess as _sp
    sha = _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD:.env"],
                  capture_output=True, text=True).stdout.strip()
    s = Sandbox(str(repo))
    with pytest.raises(ToolError):
        s.bash(f"git show {sha}")


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
