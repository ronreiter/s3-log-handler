# S3 Log Handler

A Python logging handler that asynchronously uploads logs to Amazon S3 in batches.

## Features

- Asynchronous upload to S3 using aioboto3
- Batching of log messages for efficient uploading
- Automatic compression of logs using gzip
- Configurable batch size and flush intervals
- Thread-safe operation
- Graceful shutdown handling with proper cleanup

## Installation

```bash
pip install s3-log-handler
```

## Usage

```python
import logging
from s3_log_handler import S3LogHandler

# Configure AWS credentials
client_params = {
    "aws_access_key_id": "your_access_key",
    "aws_secret_access_key": "your_secret_key",
    "region_name": "your_region"
}

# Create and configure the handler
handler = S3LogHandler(
    client_params=client_params,
    bucket_name="your-bucket-name",
    log_prefix="application-logs",
    batch_size=100,
    flush_interval=300
)

# Add handler to logger
logger = logging.getLogger()
logger.addHandler(handler)

# Log messages will now be uploaded to S3
logger.info("Hello, S3!")
```

## Configuration

- `client_params`: Dictionary of AWS client parameters
- `bucket_name`: Name of the S3 bucket
- `log_prefix`: Prefix for S3 keys (default: "application-logs")
- `batch_size`: Number of logs to batch before upload (default: 100)
- `flush_interval`: Maximum seconds between uploads (default: 300)

## License

MIT License - see LICENSE file for details


