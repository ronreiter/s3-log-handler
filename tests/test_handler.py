import logging

import pytest

from s3_log_handler import S3LogHandler


@pytest.fixture
def s3_handler():
    client_params = {
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
        "region_name": "us-east-1",
        "endpoint_url": "http://localhost:4566",  # For localstack testing
    }

    handler = S3LogHandler(
        client_params=client_params,
        bucket_name="test-bucket",
        batch_size=2,
        flush_interval=1,
    )
    yield handler
    handler.close()


def test_log_entry_format(s3_handler):
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Test message",
        args=(),
        exc_info=None,
    )

    entry = s3_handler.format_log_entry(record)

    assert entry["logger"] == "test_logger"
    assert entry["level"] == "INFO"
    assert entry["message"] == "Test message"
    assert "timestamp" in entry


# Add more tests for batching, compression, S3 upload, etc.
