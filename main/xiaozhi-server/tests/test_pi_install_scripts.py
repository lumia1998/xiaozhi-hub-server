"""离线执行 Pi 安装/更新边界；所有安装命令和仓库均限制在临时 fixture 内。

在仓库根目录独立运行，跳过会加载服务配置的全局 conftest：
    python -m pytest --noconftest main/xiaozhi-server/tests/test_pi_install_scripts.py -q
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
BASH = shutil.which("bash")
GIT = shutil.which("git")
POWERSHELL = shutil.which("powershell.exe")
RUNTIME_PATH = Path("main/xiaozhi-server/core/providers/llm/pi/runtime")
MARKER_PATH = Path("main/xiaozhi-server/data/.install-complete")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def shell(command, env, cwd):
    if not BASH:
        pytest.skip("需要 Bash")
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", command],
        cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )


def fake_tools(tmp_path, *, isolated=False):
    tools = tmp_path / "fake-bin"
    write(tools / "node", '#!/bin/bash\nexit "${NODE_EXIT:-0}"\n')
    write(tools / "npm", '''#!/bin/bash
if [[ "$1" == --version ]]; then
  printf '10.0.0\n'
  exit "${NPM_VERSION_EXIT:-0}"
fi
[[ "$1" == ci && "$2" == --omit=dev && "$3" == --prefix ]] || exit 98
IFS= read -r lock < "$4/package-lock.json"
printf 'npm:%s\n' "$lock" >> "$TRACE"
exit "${NPM_CI_EXIT:-0}"
''')
    env = os.environ.copy()
    env.update({
        "PATH": str(tools) if isolated else str(tools) + os.pathsep + env["PATH"],
        "TRACE": str(tmp_path / "trace.log"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
    })
    return tools, env


@pytest.mark.parametrize("failure", ["missing_node", "missing_npm", "node", "npm_version", "npm_ci", None])
def test_bash_installer_checks_tools_and_propagates_failure(tmp_path, failure):
    tools, env = fake_tools(tmp_path, isolated=True)
    runtime = tmp_path / "runtime with spaces"
    write(runtime / "package.json", '{}\n')
    write(runtime / "package-lock.json", '{"version":"fixture"}\n')
    if failure == "missing_node":
        (tools / "node").unlink()
    elif failure == "missing_npm":
        (tools / "npm").unlink()
    elif failure:
        env[{"node": "NODE_EXIT", "npm_version": "NPM_VERSION_EXIT", "npm_ci": "NPM_CI_EXIT"}[failure]] = "7"
    env["PI_HELPER"] = (SCRIPTS / "pi_runtime.sh").as_posix()
    env["PI_FIXTURE"] = runtime.as_posix()
    result = shell('source "$PI_HELPER"; hub_install_pi_runtime "$PI_FIXTURE"', env, tmp_path)
    assert (result.returncode == 0) == (failure is None), result.stdout + result.stderr
    trace = Path(env["TRACE"])
    assert trace.exists() == (failure in (None, "npm_ci"))
    if trace.exists():
        assert trace.read_text() == 'npm:{"version":"fixture"}\n'


def git(repo, env, *args):
    if not GIT:
        pytest.skip("需要 Git")
    # 不设置远端，禁止任何全局 hooks；调用只针对当前临时仓库。
    result = subprocess.run(
        [GIT, "-c", "core.hooksPath=/dev/null", "-c", "user.name=fixture",
         "-c", "user.email=fixture@example.invalid", *args],
        cwd=repo, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def commit(repo, env):
    git(repo, env, "add", ".")
    git(repo, env, "commit", "-qm", "fixture")
    return git(repo, env, "rev-parse", "HEAD")


def prepare_repo(tmp_path):
    _, env = fake_tools(tmp_path)
    repo = tmp_path / "fixture repo"
    repo.mkdir()
    # 后续 update.sh 会删完成标记、重置文件；先验证所有目标都在本次临时根内。
    assert repo.resolve().is_relative_to(tmp_path.resolve())
    git(repo, env, "init", "-q")
    write(repo / ".gitignore", "main/xiaozhi-server/data/\n")
    write(repo / RUNTIME_PATH / "package.json", '{}\n')
    write(repo / RUNTIME_PATH / "package-lock.json", '{"version":"old"}\n')
    write(repo / "scripts/autostart.sh", "#!/bin/bash\nexit 1\n")
    write(repo / MARKER_PATH, "")
    return repo, env


def current_updater(repo):
    for name in ("update.sh", "pi_runtime.sh"):
        write(repo / "scripts" / name, (SCRIPTS / name).read_text(encoding="utf-8"))


def run_update(repo, env, old):
    env = env | {"UPDATE_SCRIPT": (repo / "scripts/update.sh").as_posix(), "OLD_COMMIT": old}
    # --after-pull 直接进入已更新阶段，不经过 fetch，也没有配置任何 remote。
    return shell('bash "$UPDATE_SCRIPT" --after-pull "$OLD_COMMIT"', env, repo)


def test_only_pi_lock_change_triggers_install(tmp_path):
    repo, env = prepare_repo(tmp_path)
    current_updater(repo)
    write(repo / "scripts/install.sh", '''#!/bin/bash
printf 'install\n' >> "$TRACE"
: > main/xiaozhi-server/data/.install-complete
''')
    old = commit(repo, env)
    write(repo / RUNTIME_PATH / "package-lock.json", '{"version":"new"}\n')
    commit(repo, env)
    result = run_update(repo, env, old)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(env["TRACE"]).read_text() == "install\n"
    assert (repo / MARKER_PATH).exists()


@pytest.mark.parametrize("npm_exit", [0, 9])
def test_rollback_restores_old_pi_when_old_installer_has_no_pi_step(tmp_path, npm_exit):
    repo, env = prepare_repo(tmp_path)
    write(repo / "scripts/update.sh", "#!/bin/bash\nexit 0\n")
    write(repo / "scripts/install.sh", '''#!/bin/bash
printf 'old-install\n' >> "$TRACE"
: > main/xiaozhi-server/data/.install-complete
''')
    old = commit(repo, env)
    current_updater(repo)
    write(repo / RUNTIME_PATH / "package-lock.json", '{"version":"new"}\n')
    write(repo / "scripts/install.sh", "#!/bin/bash\nexit 8\n")
    commit(repo, env)
    env["NPM_CI_EXIT"] = str(npm_exit)
    result = run_update(repo, env, old)
    assert result.returncode != 0  # 更新本身失败，即使回滚成功也不能返回更新成功。
    assert git(repo, env, "rev-parse", "HEAD") == old, result.stdout + result.stderr
    expected = 'npm:{"version":"old"}\n' + ("old-install\n" if npm_exit == 0 else "")
    assert Path(env["TRACE"]).read_text() == expected
    assert (repo / MARKER_PATH).exists() == (npm_exit == 0)
    assert not (repo / "scripts/pi_runtime.sh").exists()


@pytest.mark.skipif(not POWERSHELL, reason="需要 Windows PowerShell 5.1")
@pytest.mark.parametrize("npm_exit", [0, 7])
def test_powershell_npm_stderr_is_not_failure_but_exit_code_is(tmp_path, npm_exit):
    runtime = tmp_path / "runtime with spaces"
    write(runtime / "package.json", '{}\n')
    write(runtime / "package-lock.json", '{}\n')
    tools = tmp_path / "fake-bin"
    tools.mkdir()
    (tools / "npm.cmd").write_text(
        '@echo off\r\nif "%~1"=="--version" exit /b 0\r\n'
        'echo fixture-warning 1>&2\r\necho %*>"%TRACE%"\r\n'
        f'exit /b {npm_exit}\r\n', encoding="ascii",
    )
    runner = tmp_path / "runner.ps1"
    runner.write_text('''$ErrorActionPreference = "Stop"
. $env:PI_HELPER
# 不调用系统 Node；仅模拟已通过版本检查，真正执行的是 fixture npm.cmd。
function node.exe { $global:LASTEXITCODE = 0 }
try {
    Install-HubPiRuntime $env:PI_FIXTURE
    if ($ErrorActionPreference -ne "Stop") { exit 99 }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if ($ErrorActionPreference -ne "Stop") { exit 99 }
    exit 1
}
''', encoding="utf-8-sig")
    env = os.environ | {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "PI_HELPER": str(SCRIPTS / "pi_runtime.ps1"),
        "PI_FIXTURE": str(runtime), "TRACE": str(tmp_path / "trace.log"),
    }
    result = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
        env=env, cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert result.returncode == (0 if npm_exit == 0 else 1), repr(result.stdout + result.stderr)
    assert b"fixture-warning" in result.stderr
    assert Path(env["TRACE"]).read_text().strip() == f'ci --omit=dev --prefix "{runtime}"'
