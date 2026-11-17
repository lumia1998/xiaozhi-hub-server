# Hub 测试

Python 测试使用独立依赖清单，默认从仓库示例配置和 `test/fixtures/config.yaml` 加载配置。可用 Python 3.10–3.13 运行。

在项目目录创建环境并安装依赖。

```sh
python -m venv test/.venv
test/.venv/bin/python -m pip install -r test/requirements.txt
PYTHONDONTWRITEBYTECODE=1 test/.venv/bin/python -m pytest -c test/pytest.ini -q
node --test test/test_models_page_state.cjs
```

Windows 下使用 `test/.venv/Scripts/python.exe -B -X utf8` 替换上述 Python 命令。

Pi 桥接集成测试使用本地模拟模型服务。将 `main/xiaozhi-server/core/providers/llm/pi/runtime/` 中的 `bridge.mjs`、`package.json` 和 `package-lock.json` 复制到 `test/.local/pi-runtime/`，执行 `npm ci --omit=dev --omit=optional --prefix test/.local/pi-runtime`，并安装 Node.js 22.19 或以上版本。

真实云端 API 测试由 `RUN_LIVE_API_TESTS=1` 启用，并可用 `PYTEST_CONFIG_FILE` 指定私有测试配置。音频解码集成测试使用系统 ffmpeg 和 libmpg123，POSIX 权限断言在 Linux 上执行。

测试依赖、缓存、日志和临时 Pi runtime 分别位于 `test/.venv/`、`test/.pytest_cache/` 和 `test/.local/`，这些目录由 Git 忽略。
