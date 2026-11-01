from __future__ import annotations

import socket
import sys

import pytest

from locks import cli, services
from locks.orchestrate import Options, run
from locks.store import home

from .test_orchestrate import Script, kind, ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server(port: int) -> str:
    return f"{sys.executable} -m http.server {port} --bind 127.0.0.1@{port}"


def test_a_service_is_a_command_and_a_port():
    assert services.parse("npm run dev@4200") == services.Service("npm run dev", 4200)
    for bad in ("npm run dev", "@4200", "npm run dev@0", "npm@99999"):
        with pytest.raises(services.ServiceError, match="no service"):
            services.parse(bad)


def test_a_service_starts_listens_and_stops_with_the_run(tmp_path):
    port = free_port()
    with services.running([services.parse(server(port))], tmp_path, tmp_path / "log") as up:
        assert services.listening(port) and up == [f"`{server(port).rsplit('@', 1)[0]}` on localhost:{port}: "
                                                    "started by locks for this task"]
    assert not services.listening(port)


def test_a_port_already_up_is_left_alone(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with services.running([services.Service("whatever", port)], tmp_path, tmp_path / "log") as up:
            assert up[0].endswith("already up, left as it is")


def test_a_service_that_exits_never_listens_or_cannot_start(tmp_path):
    port = free_port()
    with pytest.raises(services.ServiceError, match=r"exited \(3\) before it listened"), services.running(
            [services.Service(f"{sys.executable} -c 'raise SystemExit(3)'", port)], tmp_path, tmp_path / "log"):
        pass
    with pytest.raises(services.ServiceError, match="did not listen within 0s"), services.running(
            [services.Service(f"{sys.executable} -c 'import time; time.sleep(30)'", port)], tmp_path,
            tmp_path / "log", wait_s=0.2):
        pass
    with pytest.raises(services.ServiceError, match="could not start"), services.running(
            [services.Service("no-such-program-for-locks", port)], tmp_path, tmp_path / "log"):
        pass


def test_a_service_that_ignores_sigterm_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "STOP_S", 0.5)
    port = free_port()
    code = ("import signal, socket, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); s = socket.socket(); "
            f"s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)")
    with services.running([services.Service(f'{sys.executable} -c "{code}"', port)], tmp_path, tmp_path / "log"):
        assert services.listening(port)
    assert not services.listening(port)


def test_a_run_with_its_servers_up_and_one_whose_server_never_came(git_repo, store):
    port = free_port()
    script = Script(ok())
    report = run(f"check the page at localhost:{port}", git_repo, store, Options(services=(server(port),)),
                 registry={}, launcher=script, classifier=kind("ui"))
    assert report.status == "done" and "Servers up for this task" in script.specs[0].prompt
    assert not any("needs running servers" in r for r in report.plan.reasons)
    assert not services.listening(port)
    script = Script(ok())
    report = run("check the page", git_repo, store,
                 Options(services=(f"{sys.executable} -c 'raise SystemExit(1)'@{free_port()}",)),
                 registry={}, launcher=script, classifier=kind("ui"))
    assert report.status == "blocked" and report.stop.cause == "environment" and script.specs == []
    assert (home() / "work").is_dir()


def test_the_flag_is_checked_where_it_is_read(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "x", "--service", "npm run dev"])
    assert "no service" in capsys.readouterr().err
    args = cli.build_parser().parse_args(["queue", "add", "x", "--service", "npm run dev@4200", "--carry",
                                          "--add-dir", "/tmp"])
    assert args.service == ["npm run dev@4200"] and args.carry and args.add_dir == ["/tmp"]
