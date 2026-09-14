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
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_server(status=200):
    handler = type("Handler", (QuietHandler,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/health"


def stop_server(server):
    server.shutdown()
    server.server_close()


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


def test_single_endpoint_history_has_no_previous_endpoints(tmp_path):
    server, url = start_server()
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": url}, count=2, interval=0, db_path=db)
    finally:
        stop_server(server)

    stats = availability(db)["svc"]
    assert stats["url"] == url
    assert stats["checks"] == 2
    assert stats["previous_endpoints"] == []
    assert url not in render_pulse_report(db)


def test_repointed_name_does_not_blend_endpoint_histories(tmp_path):
    old_server, old = start_server(200)
    new_server, new = start_server(503)
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": old}, count=1, interval=0, db_path=db)
        run_pulse({"svc": new}, count=1, interval=0, db_path=db)
    finally:
        stop_server(old_server)
        stop_server(new_server)

    stats = availability(db)["svc"]
    # The headline numbers describe the endpoint Pulse probed most recently.
    assert stats["url"] == new
    assert stats["checks"] == 1
    assert stats["up"] == 0
    assert stats["availability"] == 0.0
    assert stats["last_ok"] is False
    assert stats["median_latency_ms"] is None
    # The earlier endpoint keeps its own history instead of vanishing.
    assert [(e["url"], e["checks"], e["up"], e["availability"], e["last_ok"])
            for e in stats["previous_endpoints"]] == [(old, 1, 1, 100.0, True)]

    records = export_records(db)
    assert [r.result for r in records] == ["passed", "failed"]
    assert old in records[0].evidence[0]
    assert new in records[1].evidence[0]

    report = render_pulse_report(db)
    assert "50.0%" not in report
    assert "0.0% of 1 checks, now DOWN" in report
    assert new in report
    assert f"earlier endpoint {old}: 100.0% of 1 checks, last up" in report


def test_name_returning_to_an_endpoint_reports_that_endpoint(tmp_path):
    a_server, a = start_server(200)
    b_server, b = start_server(503)
    db = str(tmp_path / "pulse.db")
    try:
        run_pulse({"svc": a}, count=1, interval=0, db_path=db)
        run_pulse({"svc": b}, count=1, interval=0, db_path=db)
        run_pulse({"svc": a}, count=1, interval=0, db_path=db)
    finally:
        stop_server(a_server)
        stop_server(b_server)

    stats = availability(db)["svc"]
    assert stats["url"] == a
    assert (stats["checks"], stats["up"], stats["last_ok"]) == (2, 2, True)
    assert [(e["url"], e["checks"], e["last_ok"])
            for e in stats["previous_endpoints"]] == [(b, 1, False)]


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


def test_free_port_probe_fails_cleanly(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    db = str(tmp_path / "pulse.db")
    run_pulse({"gone": f"http://127.0.0.1:{dead_port}/"}, count=1,
              interval=0, db_path=db)
    assert availability(db)["gone"]["availability"] == 0.0
