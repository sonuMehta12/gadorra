"""The JSON log file: its shape, its 7-day retention, and what gets written to it."""
import json
import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from app.config import settings
from app.logging_setup import JsonFormatter, mask_mobile, setup_logging


@pytest.fixture
def log_dir(tmp_path):
    setup_logging(str(tmp_path), retention_days=7)
    yield tmp_path
    setup_logging(settings.log_dir, settings.log_retention_days)  # back to the test default


def _file_handler() -> TimedRotatingFileHandler:
    [h] = [h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)]
    return h


def _lines(log_dir) -> list[dict]:
    _file_handler().flush()
    return [json.loads(line) for line in (log_dir / "app.log").read_text().splitlines()]


def test_each_line_is_one_json_object_with_its_extra_fields():
    record = logging.LogRecord("payments", logging.INFO, __file__, 1, "registration paid", None, None)
    record.ack = "GPET26/UP02/31457"
    line = json.loads(JsonFormatter().format(record))
    assert line["level"] == "INFO" and line["logger"] == "payments"
    assert line["msg"] == "registration paid"
    assert line["ack"] == "GPET26/UP02/31457"
    assert line["ts"].endswith("+05:30")  # Indian time, so it matches what students report


def test_an_exception_is_written_with_its_traceback():
    try:
        raise RuntimeError("razorpay down")
    except RuntimeError:
        import sys
        record = logging.LogRecord("api", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    line = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: razorpay down" in line["error"]


def test_the_file_rolls_daily_and_keeps_seven_days(log_dir):
    handler = _file_handler()
    assert handler.when == "MIDNIGHT" and handler.backupCount == 7
    for day in range(1, 11):  # ten old days already on disk
        (log_dir / f"app.log.2026-09-{day:02d}").write_text("{}\n")
    handler.doRollover()
    kept = sorted(p.name for p in log_dir.glob("app.log.*"))
    assert len(kept) == 7
    assert "app.log.2026-09-01" not in kept  # the oldest went first


def test_without_a_log_dir_nothing_is_written_to_disk(tmp_path):
    setup_logging("", 7)
    assert not any(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers)
    assert not list(tmp_path.iterdir())


def test_setting_up_twice_does_not_double_every_line(log_dir):
    setup_logging(str(log_dir), 7)
    logging.getLogger("x").info("once")
    assert [l["msg"] for l in _lines(log_dir)].count("once") == 1


def test_every_api_request_is_logged_with_status_time_and_ip(client, log_dir):
    client.get("/api/v1/masters/classes", headers={"X-Forwarded-For": "103.21.4.9, 10.10.1.4"})
    [req] = [l for l in _lines(log_dir) if l["logger"] == "request"]
    assert req["method"] == "GET" and req["path"] == "/api/v1/masters/classes"
    assert req["status"] == 200 and isinstance(req["ms"], int)
    assert req["ip"] == "103.21.4.9"  # the student, not the gateway


def test_failed_requests_are_logged_too(client, log_dir):
    client.post("/api/v1/auth/login/send-otp", json={"mobile": "9866700001"})
    statuses = [l["status"] for l in _lines(log_dir) if l["logger"] == "request"]
    assert statuses == [404]


def test_a_healthy_probe_is_not_logged(client, log_dir):
    client.get("/health")
    assert not [l for l in _lines(log_dir) if l["logger"] == "request"]


def test_a_payment_leaves_a_trail_from_form_to_acknowledgement(client, log_dir, paid):
    _, _, mobile, result = paid
    events = {l["msg"]: l for l in _lines(log_dir)}
    assert "registration created" in events
    assert events["registration paid"]["ack"] == result.acknowledgement.number
    assert "whatsapp sent" in events


def test_mobiles_are_masked_in_the_log(client, log_dir):
    client.post("/api/v1/auth/login/send-otp", json={"mobile": "9866700002"})
    text = (log_dir / "app.log").read_text()
    assert "9866700002" not in text
    assert "98****0002" in text


def test_mask_mobile():
    assert mask_mobile("9935846948") == "99****6948"
    assert mask_mobile("919935846948") == "91******6948"
    assert mask_mobile(None) is None


def test_a_mobile_or_email_inside_an_error_message_is_masked(log_dir):
    try:
        raise ValueError('value "9935846948" rejected for asha.verma@example.com, retry 919935846948')
    except ValueError:
        logging.getLogger("api").exception("insert failed")
    [line] = [l for l in _lines(log_dir) if l["msg"] == "insert failed"]
    assert "9935846948" not in line["error"] and "asha.verma" not in line["error"]
    assert "99****6948" in line["error"] and "a***@example.com" in line["error"]


def test_ids_and_acknowledgement_numbers_are_left_alone():
    from app.logging_setup import scrub
    text = "ack GPET26/UP02/31457 order order_Tj2pQ6ZRtZW4CC reg 6f1c2a9e-1b2c-4d5e-8f90-1234567890ab took 1234 ms"
    assert scrub(text) == text


def test_database_errors_never_print_their_values():
    from sqlalchemy import text
    from app.database import engine  # the app's engine, not the test fixture's
    try:
        with engine.connect() as conn:
            conn.execute(text("select cast(:name as int)"), {"name": "Asha Verma"})
    except Exception as exc:
        assert "Asha Verma" not in str(exc).split("[SQL:")[1]
        assert "hidden" in str(exc)
    else:
        raise AssertionError("expected a database error")
