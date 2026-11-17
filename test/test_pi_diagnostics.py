"""Pi 环境验收：临时 runtime、受控进程结果及本机 Node，不读取 Hub data。

    python -B test/test_pi_diagnostics.py -v
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SERVER = Path(__file__).resolve().parents[1] / "main" / "xiaozhi-server"
spec = importlib.util.spec_from_file_location(
    "pi_diagnostics_under_test", SERVER / "core/providers/llm/pi/diagnostics.py"
)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)

DEPENDENCIES = {"@earendil-works/pi-agent-core": "1.5.2", "@earendil-works/pi-ai": "1.5.2"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class PiDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-diagnostics-")
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(diagnostics, "RUNTIME", self.runtime).start()
        self.node = patch.object(diagnostics, "_node_path", return_value="node-test").start()
        self.run = patch.object(diagnostics.subprocess, "run").start()
        self.run.side_effect = [self.process("v22.19.0\n"), self.process('{"ok":true}')]
        write_json(self.runtime / "package.json", {"dependencies": DEPENDENCIES})
        self.lock = {"packages": {f"node_modules/{name}": {"version": version}
                                  for name, version in DEPENDENCIES.items()}}
        self.lock["packages"][""] = {"dependencies": dict(DEPENDENCIES)}
        write_json(self.runtime / "package-lock.json", self.lock)
        for name, version in DEPENDENCIES.items():
            write_json(self.runtime / "node_modules" / name / "package.json", {"version": version})

    @staticmethod
    def process(stdout="", code=0, stderr=""):
        return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)

    def assert_error(self, expected):
        level, detail = diagnostics.check_runtime()
        self.assertEqual(level, "error")
        self.assertIn(expected, detail)
        return detail

    def test_missing_manifest_does_not_start_node(self):
        (self.runtime / "package.json").unlink()
        self.assert_error("清单或锁文件无法读取")
        self.run.assert_not_called()

    def test_lock_drift_is_reported_before_loading(self):
        self.lock["packages"]["node_modules/@earendil-works/pi-ai"]["version"] = "1.5.1"
        write_json(self.runtime / "package-lock.json", self.lock)
        self.assert_error("清单与锁文件不一致")
        self.run.assert_not_called()

    def test_lock_root_must_preserve_manifest_dependency_declarations(self):
        for root in (None, {}, {"dependencies": {**DEPENDENCIES, "@earendil-works/pi-ai": "1.5.1"}}):
            with self.subTest(root=root):
                self.lock["packages"][""] = root
                write_json(self.runtime / "package-lock.json", self.lock)
                self.assert_error("清单与锁文件不一致")
        self.run.assert_not_called()

    def test_optional_node_dependencies_remain_optional(self):
        optional = {"node-linux-x64": "22.23.5"}
        write_json(self.runtime / "package.json", {"dependencies": DEPENDENCIES, "optionalDependencies": optional})
        self.lock["packages"][""]["optionalDependencies"] = optional
        write_json(self.runtime / "package-lock.json", self.lock)
        self.assertEqual(diagnostics.check_runtime()[0], "ok")
        self.lock["packages"][""]["optionalDependencies"] = {"node-linux-x64": "22.22.0"}
        write_json(self.runtime / "package-lock.json", self.lock)
        self.assert_error("清单与锁文件不一致")

    def test_missing_node_reports_installation_action(self):
        self.node.return_value = None
        detail = self.assert_error("未找到 Node 22.19+")
        self.assertIn("npm ci --omit=dev", detail)
        self.run.assert_not_called()

    def test_old_node_is_rejected_without_importing_packages(self):
        self.run.side_effect = [self.process("v22.18.1\n")]
        self.assert_error("版本过低")
        self.assertEqual(self.run.call_count, 1)

    def test_node_failure_and_unrecognized_version_are_errors(self):
        for response in (self.process("v24.1.0", 1), self.process("broken-version")):
            with self.subTest(response=response):
                self.run.side_effect = [response]
                self.assert_error("无法识别 Node 版本")

    def test_node_execution_error_is_actionable(self):
        self.run.side_effect = PermissionError("private-path")
        self.assert_error("Node 无法执行")

    def test_node_version_probe_is_bounded(self):
        self.run.side_effect = subprocess.TimeoutExpired("node-test", 3)
        self.assert_error("Node 版本检查超时")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 3)

    def test_missing_installed_package_never_reaches_import_probe(self):
        (self.runtime / "node_modules/@earendil-works/pi-ai/package.json").unlink()
        self.assert_error("缺少依赖 @earendil-works/pi-ai")
        self.assertEqual(self.run.call_count, 1)

    def test_wrong_installed_version_never_reaches_import_probe(self):
        write_json(self.runtime / "node_modules/@earendil-works/pi-ai/package.json", {"version": "1.6.0"})
        self.assert_error("与锁定版本 1.5.2 不一致")
        self.assertEqual(self.run.call_count, 1)

    def test_import_failure_does_not_echo_process_output(self):
        self.run.side_effect = [self.process("v24.1.0"), self.process(
            '{"ok":false,"module":"private-output"}', 1, "private-stderr")]
        detail = self.assert_error("依赖无法加载")
        self.assertNotIn("private", detail)

    def test_import_timeout_is_bounded_and_recoverable(self):
        self.run.side_effect = [self.process("v24.1.0"), subprocess.TimeoutExpired("node-test", 10)]
        self.assert_error("加载检查超时")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 10)
        self.run.side_effect = [self.process("v24.1.0"), self.process('{"ok":true}')]
        self.assertEqual(diagnostics.check_runtime()[0], "ok")

    def test_import_process_execution_error_is_reported(self):
        self.run.side_effect = [self.process("v24.1.0"), OSError("private-error")]
        self.assert_error("加载检查无法运行")

    def test_import_protocol_requires_valid_object_true_and_zero_exit(self):
        cases = [("not-json", 0), ("[]", 0), ('{"ok":1}', 0), ('{"ok":true}', 1)]
        for stdout, code in cases:
            with self.subTest(stdout=stdout, code=code):
                self.run.side_effect = [self.process("v24.1.0"), self.process(stdout, code)]
                self.assertEqual(diagnostics.check_runtime()[0], "error")

    def test_success_uses_runtime_cwd_and_excludes_model_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-key", "NODE_OPTIONS": "--inspect"}):
            level, detail = diagnostics.check_runtime()
        self.assertEqual(level, "ok")
        self.assertIn("Node 22.19.0", detail)
        self.assertIn("pi-agent-core 1.5.2", detail)
        self.assertIn("pi-ai 1.5.2", detail)
        args, kwargs = self.run.call_args
        self.assertEqual(args[0][:3], ["node-test", "--input-type=module", "-e"])
        self.assertEqual(kwargs["cwd"], self.runtime)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("NODE_OPTIONS", kwargs["env"])
        self.assertNotIn("shell", kwargs)


class NodeSelectionTest(unittest.TestCase):
    def test_linux_prefers_bundled_architecture_without_system_lookup(self):
        with tempfile.TemporaryDirectory(prefix="pi-node-") as tmp:
            runtime = Path(tmp)
            bundled = runtime / "node_modules/node-linux-arm64/bin/node"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with patch.object(diagnostics.sys, "platform", "linux"), \
                    patch.object(diagnostics.platform, "machine", return_value="aarch64"), \
                    patch.object(diagnostics.shutil, "which") as lookup:
                self.assertEqual(diagnostics._node_path(runtime), str(bundled))
                lookup.assert_not_called()

    def test_other_platforms_and_missing_bundle_use_system_node(self):
        with tempfile.TemporaryDirectory(prefix="pi-node-") as tmp:
            for system, arch in (("linux", "x86_64"), ("win32", "AMD64"), ("darwin", "arm64")):
                with self.subTest(system=system), patch.object(diagnostics.sys, "platform", system), \
                        patch.object(diagnostics.platform, "machine", return_value=arch), \
                        patch.object(diagnostics.shutil, "which", return_value="system-node"):
                    self.assertEqual(diagnostics._node_path(Path(tmp)), "system-node")


class RealNodeProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("本机未安装 Node")

    def test_real_esm_imports_and_missing_export_are_distinguished(self):
        with tempfile.TemporaryDirectory(prefix="pi-esm-") as tmp:
            runtime = Path(tmp)
            core = runtime / "node_modules/@earendil-works/pi-agent-core"
            ai = runtime / "node_modules/@earendil-works/pi-ai"
            write_json(core / "package.json", {"type": "module", "exports": "./index.js"})
            (core / "index.js").write_text("export function runAgentLoopContinue() {}", encoding="utf-8")
            names = {"openai-completions": "openAICompletionsApi", "openai-responses": "openAIResponsesApi",
                     "anthropic-messages": "anthropicMessagesApi", "google-generative-ai": "googleGenerativeAIApi"}
            exports = {".": "./index.js", **{f"./api/{name}.lazy": f"./{name}.js" for name in names}}
            write_json(ai / "package.json", {"type": "module", "exports": exports})
            (ai / "index.js").write_text("export function createAssistantMessageEventStream() {}", encoding="utf-8")
            for name, symbol in names.items():
                (ai / f"{name}.js").write_text(f"export function {symbol}() {{}}", encoding="utf-8")
            command = [self.node, "--input-type=module", "-e", diagnostics.IMPORT_PROBE]
            success = subprocess.run(command, cwd=runtime, capture_output=True, text=True, timeout=10)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(json.loads(success.stdout), {"ok": True})
            (core / "index.js").write_text("export const renamedExport = 1;", encoding="utf-8")
            broken = subprocess.run(command, cwd=runtime, capture_output=True, text=True, timeout=10)
            self.assertEqual(broken.returncode, 1)
            self.assertFalse(json.loads(broken.stdout)["ok"])


class StatusIntegrationTest(unittest.TestCase):
    def test_status_includes_shared_pi_check_once(self):
        config_store = types.ModuleType("core.admin.config_store")
        config_store.SECTIONS = ()
        config_store.SECRET_FIELD = diagnostics.re.compile("api_key")
        config_store.entry_type = lambda *_: None
        deps = types.ModuleType("core.admin.deps")
        pages = types.ModuleType("core.admin.pages")
        pages.asr_model_check = lambda _: None
        util = types.ModuleType("core.utils.util")
        util.get_local_ip = lambda: "127.0.0.1"
        stubs = {"core.admin.config_store": config_store, "core.admin.deps": deps,
                 "core.admin.pages": pages, "core.utils.util": util,
                 "core.providers.llm.pi.diagnostics": diagnostics,
                 "opuslib_next": types.ModuleType("opuslib_next")}
        for name in ("core", "core.admin", "core.providers", "core.providers.llm", "core.providers.llm.pi", "core.utils"):
            package = types.ModuleType(name)
            package.__path__ = []
            stubs[name] = package
        stubs["core.admin"].config_store = config_store
        stubs["core.admin"].deps = deps
        stubs["core.admin"].pages = pages
        status_spec = importlib.util.spec_from_file_location("pi_status_under_test", SERVER / "core/admin/status.py")
        status = importlib.util.module_from_spec(status_spec)
        with patch.object(sys, "path", [str(SERVER), *sys.path]), patch.dict(sys.modules, stubs):
            status_spec.loader.exec_module(status)
        with patch.object(status, "check_runtime", return_value=("error", "Pi 假定缺包")) as check, \
                patch.object(status, "_plugin_setting_checks", return_value=[]), \
                patch.object(status.ctypes.util, "find_library", return_value="opus"), \
                patch.dict(sys.modules, stubs):
            result = status.collect_status({}, {})
        check.assert_called_once_with()
        self.assertEqual([entry for entry in result["checks"] if entry["name"] == "Pi 引擎"],
                         [{"name": "Pi 引擎", "level": "error", "detail": "Pi 假定缺包"}])


if __name__ == "__main__":
    unittest.main()
