"""Pi 的本地环境自检；不加载 Hub 配置、不启动对话、不请求模型。"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


RUNTIME = Path(__file__).parent / "runtime"
MIN_NODE = (22, 19, 0)

# 与 bridge.mjs 的入口保持一致；只验证导出可用，不创建模型请求。
IMPORT_PROBE = """
const modules = [
  ['@earendil-works/pi-agent-core', 'runAgentLoopContinue'],
  ['@earendil-works/pi-ai', 'createAssistantMessageEventStream'],
  ['@earendil-works/pi-ai/api/openai-completions.lazy', 'openAICompletionsApi'],
  ['@earendil-works/pi-ai/api/openai-responses.lazy', 'openAIResponsesApi'],
  ['@earendil-works/pi-ai/api/anthropic-messages.lazy', 'anthropicMessagesApi'],
  ['@earendil-works/pi-ai/api/google-generative-ai.lazy', 'googleGenerativeAIApi'],
];
let current = '';
try {
  for (const [name, symbol] of modules) {
    current = name;
    const imported = await import(name);
    if (typeof imported[symbol] !== 'function') throw new Error('missing export');
  }
  console.log(JSON.stringify({ok: true}));
} catch {
  console.log(JSON.stringify({ok: false, module: current}));
  process.exitCode = 1;
}
"""


def _node_path(runtime: Path):
    # 与 pi.node_binary 一样，Linux 优先使用 runtime 自带的 Node。
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    bundled = runtime / "node_modules" / f"node-linux-{arch}" / "bin" / "node"
    if sys.platform == "linux" and arch and bundled.is_file():
        return str(bundled)
    return shutil.which("node")


def check_runtime():
    """返回 (level, detail)，供命令行 doctor 和后台状态页共用。"""
    runtime = RUNTIME
    reinstall = f'请运行 npm ci --omit=dev --prefix "{runtime}"'
    try:
        manifest = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((runtime / "package-lock.json").read_text(encoding="utf-8"))
        dependencies = manifest["dependencies"]
        locked = lock["packages"]
        if not isinstance(dependencies, dict) or not dependencies or not isinstance(locked, dict):
            raise ValueError("invalid dependency manifest")
        root = locked.get("")
        if not isinstance(root, dict) or any(
            manifest.get(kind, {}) != root.get(kind, {})
            for kind in ("dependencies", "optionalDependencies")
        ):
            return "error", "Pi 依赖清单与锁文件不一致，请恢复同一版本的 package.json 和 package-lock.json"
        for name, version in dependencies.items():
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("dependency version is not pinned")
            if locked.get(f"node_modules/{name}", {}).get("version") != version:
                return "error", "Pi 依赖清单与锁文件不一致，请恢复同一版本的 package.json 和 package-lock.json"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "error", "Pi 依赖清单或锁文件无法读取，请恢复 runtime 目录后重新安装"

    node = _node_path(runtime)
    if not node:
        return "error", f"未找到 Node 22.19+。Linux x64/arm64 可通过安装 Pi 依赖获取；其他系统请安装 Node。{reinstall}"

    # 不把模型凭证、NODE_OPTIONS 或其他用户环境带入诊断进程。
    # 此白名单与真正的 Pi bridge 启动环境一致。
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "NODE_EXTRA_CA_CERTS", "SYSTEMROOT",
    )}
    try:
        version_result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=3, env=env,
        )
    except subprocess.TimeoutExpired:
        return "error", "Node 版本检查超时，请检查 Node 是否可正常运行"
    except OSError:
        return "error", f"Node 无法执行，请检查安装和执行权限。{reinstall}"
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version_result.stdout.strip())
    if version_result.returncode or not match:
        return "error", "无法识别 Node 版本，请安装 Node 22.19+"
    version = tuple(int(n) for n in match.groups())
    if version < MIN_NODE:
        return "error", f"Node {'.'.join(match.groups())} 版本过低，需要 22.19+"

    for name, expected in dependencies.items():
        try:
            installed = json.loads((runtime / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "error", f"Pi 缺少依赖 {name}，或依赖文件损坏。{reinstall}"
        if not isinstance(installed, dict) or installed.get("version") != expected:
            return "error", f"Pi 依赖 {name} 与锁定版本 {expected} 不一致。{reinstall}"

    try:
        probe = subprocess.run(
            [node, "--input-type=module", "-e", IMPORT_PROBE], cwd=runtime,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, env=env,
        )
    except subprocess.TimeoutExpired:
        return "error", f"Pi 依赖加载检查超时。{reinstall}"
    except OSError:
        return "error", f"Pi 依赖加载检查无法运行。{reinstall}"
    try:
        result = json.loads(probe.stdout)
    except ValueError:
        return "error", f"Pi 依赖加载检查未返回有效结果。{reinstall}"
    if probe.returncode or not isinstance(result, dict) or result.get("ok") is not True:
        return "error", f"Pi 依赖无法加载，或缺少 bridge 所需接口。{reinstall}"
    versions = "、".join(f"{name.rsplit('/', 1)[-1]} {version}" for name, version in dependencies.items())
    return "ok", f"Node {'.'.join(match.groups())}；{versions} 已加载"
