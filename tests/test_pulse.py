import http.server
import socket
import threading

from nandatown.cli import main
from nandatown.pulse import (
    availability,
    export_records,
    render_pulse_report,
    run_pulse,
)


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health"


def test_pulse_records_up_then_down(tmp_path):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    run_pulse({"svc": url}, count=2, interval=0.05, db_path=db)
    server.shutdown()
    server.server_close()
    run_pulse({"svc": url}, count=2, interval=0.05, db_path=db)

    stats = availability(db)["svc"]
    assert stats["checks"] == 4
    assert stats["up"] == 2
    assert stats["availability"] == 50.0
    assert stats["last_ok"] is False

    records = export_records(db)
    assert len(records) == 4
    assert [r.result for r in records] == ["passed", "passed", "failed",
                                           "failed"]
    assert all(r.observer == "town-pulse.v1" for r in records)

    report = render_pulse_report(db)
    assert "50.0%" in report
    assert "now DOWN" in report
    assert "History is the evidence" in report


def test_pulse_cli(tmp_path, capsys):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    try:
        assert main(["pulse", "--target", f"svc={url}", "--count", "2",
                     "--interval", "0.05", "--db", db]) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "100.0%" in out
    assert main(["pulse", "--records", "--db", db]) == 0
    assert "town-pulse.v1" in capsys.readouterr().out
    assert main(["pulse", "--db", db]) == 2


def start_counting_server():
    hits = []

    class CountingHandler(QuietHandler):
        def do_GET(self):
            hits.append(self.path)
            super().do_GET()

    server = http.server.HTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health", hits


def test_pulse_cli_refuses_reused_target_name_before_probing(tmp_path,
                                                             capsys):
    first, first_url, first_hits = start_counting_server()
    second, second_url, second_hits = start_counting_server()
    db = tmp_path / "pulse.db"
    try:
        code = main(["pulse", "--target", f"svc={first_url}",
                     "--target", f"svc={second_url}", "--count", "1",
                     "--interval", "0", "--db", str(db)])
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()
    out = capsys.readouterr().out
    assert code == 2
    assert "target name 'svc' is given more than once" in out
    assert first_hits == [] and second_hits == []
    assert not db.exists()


def test_free_port_probe_fails_cleanly(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    db = str(tmp_path / "pulse.db")
    run_pulse({"gone": f"http://127.0.0.1:{dead_port}/"}, count=1,
              interval=0, db_path=db)
    assert availability(db)["gone"]["availability"] == 0.0
