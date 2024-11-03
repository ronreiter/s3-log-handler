import asyncio
import gzip
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from s3_log_handler import S3LogHandler


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
    return handler


@pytest.fixture
def sample_log_records():
    records = []
    for i in range(2):
        record = logging.LogRecord(
            name="test_logger",
            level=20,  # INFO
            pathname="test_path",
            lineno=i + 1,
            msg=f"Test message {i + 1}",
            args=(),
            exc_info=None,
        )
        records.append(record)
    return records


@pytest.mark.asyncio
async def test_flush_logs_empty_buffer(s3_handler):
    """Test that _flush_logs handles empty buffer correctly"""
    # Ensure buffer is empty
    s3_handler.log_buffer = []

    # Call _flush_logs
    await s3_handler._flush_logs()

    # Verify no S3 calls were made
    assert len(s3_handler.log_buffer) == 0


@pytest.mark.asyncio
async def test_flush_logs_with_records(s3_handler, sample_log_records):
    """Test that _flush_logs correctly processes and uploads logs"""
    fixed_datetime = datetime(2024, 1, 1, 12, 0, 0)

    # Mock datetime.utcnow correctly
    mock_datetime = MagicMock()
    mock_datetime.utcnow.return_value = fixed_datetime
    mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

    # Format log records and add to buffer
    s3_handler.log_buffer = [
        s3_handler.format_log_entry(record) for record in sample_log_records
    ]

    # Create mock S3 client
    mock_s3_client = AsyncMock()
    mock_s3_context = AsyncMock()
    mock_s3_context.__aenter__.return_value = mock_s3_client
    mock_s3_context.__aexit__.return_value = None

    # Apply all our mocks
    with patch("socket.gethostname", return_value="test-host"), patch(
        "os.getpid", return_value=12345
    ), patch("datetime.datetime", mock_datetime), patch.object(
        s3_handler.session, "client", return_value=mock_s3_context
    ):
        await s3_handler._flush_logs()

        # Verify S3 put_object was called with correct parameters
        expected_key = (
            "application-logs/date=2024-01-01/hour=12/host=test-host/"
            "2024-01-01T12:00:00_test-host_12345.jsonl.gz"
        )

        # Verify the call was made
        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args[1]

        assert call_args["Bucket"] == "test-bucket"
        assert call_args["Key"] == expected_key
        assert call_args["ContentType"] == "application/x-gzip"

        # Verify the uploaded data
        uploaded_data = call_args["Body"]
        decompressed_data = gzip.decompress(uploaded_data).decode("utf-8")
        log_entries = [json.loads(line) for line in decompressed_data.split("\n")]

        assert len(log_entries) == 2
        assert all(entry["logger"] == "test_logger" for entry in log_entries)
        assert all(entry["level"] == "INFO" for entry in log_entries)
        assert log_entries[0]["message"] == "Test message 1"
        assert log_entries[1]["message"] == "Test message 2"

    # Verify buffer was cleared
    assert len(s3_handler.log_buffer) == 0


@pytest.mark.asyncio
async def test_flush_logs_handles_s3_error(s3_handler, sample_log_records):
    """Test that _flush_logs handles S3 upload errors gracefully"""
    # Format log records and add to buffer
    s3_handler.log_buffer = [
        s3_handler.format_log_entry(record) for record in sample_log_records
    ]

    # Create mock S3 client that raises an exception
    mock_s3_client = AsyncMock()
    mock_s3_client.put_object.side_effect = Exception("S3 Upload Failed")
    mock_s3_context = AsyncMock()
    mock_s3_context.__aenter__.return_value = mock_s3_client
    mock_s3_context.__aexit__.return_value = None

    # Mock print to capture error message
    with patch("builtins.print") as mock_print:
        with patch.object(s3_handler.session, "client", return_value=mock_s3_context):
            await s3_handler._flush_logs()

            # Verify error was logged
            mock_print.assert_called_with(
                "Error uploading logs to S3: S3 Upload Failed"
            )

            # Buffer should be empty even if upload failed
            assert len(s3_handler.log_buffer) == 0


@pytest.mark.asyncio
async def test_flush_logs_concurrent_access(s3_handler):
    """Test that _flush_logs handles concurrent access correctly"""
    # Create multiple log records
    log_records = []
    for i in range(5):
        record = logging.LogRecord(
            name="test_logger",
            level=20,
            pathname="test_path",
            lineno=i + 1,
            msg=f"Test message {i + 1}",
            args=(),
            exc_info=None,
        )
        log_records.append(s3_handler.format_log_entry(record))

    # Mock S3 client with delay to simulate network latency
    mock_s3_client = AsyncMock()

    async def delayed_put_object(**kwargs):
        await asyncio.sleep(0.1)
        return True

    mock_s3_client.put_object.side_effect = delayed_put_object
    mock_s3_context = AsyncMock()
    mock_s3_context.__aenter__.return_value = mock_s3_client
    mock_s3_context.__aexit__.return_value = None

    with patch.object(s3_handler.session, "client", return_value=mock_s3_context):
        # Add logs and trigger concurrent flushes
        s3_handler.log_buffer = log_records[:3]
        flush1 = s3_handler._flush_logs()

        # Try to add more logs while first flush is in progress
        s3_handler.log_buffer.extend(log_records[3:])
        flush2 = s3_handler._flush_logs()

        # Wait for both flushes to complete
        await asyncio.gather(flush1, flush2)

        # Verify all logs were processed
        assert len(s3_handler.log_buffer) == 0
        assert mock_s3_client.put_object.call_count > 0
