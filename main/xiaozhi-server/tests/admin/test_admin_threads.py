"""后台的同步操作用自己的线程，不和语音识别抢默认线程池。"""
import asyncio
import threading

import core.admin.admin_handler as ah


def test_blocking_runs_on_the_admin_threads():
    name = asyncio.run(ah.AdminHandler._blocking(lambda: threading.current_thread().name))
    assert name.startswith("admin")
