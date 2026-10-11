"""JSON logs keep what an operator needs to debug a failure."""
from __future__ import annotations

import json
import logging
import sys

from product.logging_config import JsonFormatter


def test_json_log_line_keeps_the_traceback():
    logger = logging.getLogger("oncall.test_json")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logger.makeRecord(
            logger.name, logging.ERROR, __file__, 1, "job failed", (), exc_info=sys.exc_info()
        )
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "job failed"
    assert "RuntimeError: boom" in line["exc"]
    assert "Traceback" in line["exc"]
