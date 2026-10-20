# -*- coding: utf-8 -*-
"""四列集成测试专用实例，不读取业务配置或依赖其他未提交功能。"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

aiomysql = pytest.importorskip("aiomysql")


def _stop(process, binary, port):
    """只关闭当前 fixture 创建的进程，不扫描或终止其他数据库。"""
    admin = binary.with_name(
        "mysqladmin.exe" if os.name == "nt" else "mysqladmin",
    )
    if process.poll() is None and admin.is_file():
        result = subprocess.run(
            [
                str(admin),
                "--no-defaults",
                "--protocol=tcp",
                "--host=127.0.0.1",
                f"--port={port}",
                "--user=root",
                "shutdown",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode == 0:
            process.wait(timeout=20)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=20)


@pytest.fixture(scope="module")
def private_mysql(tmp_path_factory):
    """仅监听回环地址，临时数据目录由 pytest 管理。"""
    if os.environ.get("CRON_TEST_PRIVATE_MYSQL") != "1":
        pytest.skip("需要显式允许启动本机临时 MySQL")
    client = shutil.which("mysql")
    if not client:
        pytest.skip("本机没有 MySQL 工具")
    binary = Path(client).with_name(
        "mysqld.exe" if os.name == "nt" else "mysqld",
    )
    if not binary.is_file():
        pytest.skip("本机没有 mysqld")
    root = tmp_path_factory.mktemp("cron_profile_mysql")
    args = [
        str(binary),
        "--no-defaults",
        f"--basedir={binary.parent.parent}",
        f"--datadir={root / 'data'}",
    ]
    result = subprocess.run(
        [*args, "--initialize-insecure", "--console"],
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (root / "server.log").open("wb") as log:
        with subprocess.Popen(
            [
                *args,
                "--bind-address=127.0.0.1",
                f"--port={port}",
                "--mysqlx=OFF",
                "--skip-log-bin",
                "--console",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        ) as process:
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", port),
                            timeout=0.2,
                        ):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    pytest.fail(
                        "本机临时 MySQL 启动失败：" + str(root / "server.log"),
                    )
                yield {
                    "host": "127.0.0.1",
                    "port": port,
                    "user": "root",
                    "password": "",
                    "charset": "utf8mb4",
                    "autocommit": True,
                }
            finally:
                _stop(process, binary, port)


class ProfileDatabase:
    """真实池连接适配器，用于执行生产事务与人工 SQL。"""

    is_connected = True

    def __init__(self, pool):
        self.pool = pool

    def acquire(self):
        return self.pool.acquire()

    async def execute(self, query, params=None):
        async with self.pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, params)
                return cursor.rowcount

    async def fetch_all(self, query, params=None):
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall()

    async def fetch_one(self, query, params=None):
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None


@pytest.fixture
async def mysql_db(private_mysql):
    """每个测试使用新实例内的随机库，不建其他功能的表。"""
    connection = await aiomysql.connect(**private_mysql)
    name = "cron_profile_test_" + uuid4().hex
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin",
            )
    finally:
        connection.close()
    pool = await aiomysql.create_pool(
        **private_mysql,
        db=name,
        minsize=2,
        maxsize=4,
    )
    try:
        yield ProfileDatabase(pool)
    finally:
        pool.close()
        await pool.wait_closed()
