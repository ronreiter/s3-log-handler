import asyncio
import atexit
import gzip
import json
import logging
import os
import signal
import threading
import weakref
from datetime import datetime
from logging import LogRecord
from queue import Queue
from typing import Any, Dict, List, Optional

import _queue
import aioboto3


class S3LogHandler(logging.Handler):
    _instances = weakref.WeakSet()

    def __init__(
        self,
        client_params: dict,
        bucket_name: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        log_prefix: str = "application-logs",
        batch_size: int = 100,
        flush_interval: int = 300,
    ):
        super().__init__()
        self.bucket_name = bucket_name
        self.log_prefix = log_prefix
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.client_params = client_params

        self.log_buffer: List[Dict[str, Any]] = []
        self.last_flush = datetime.utcnow()
        self.session = aioboto3.Session()
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._queue = Queue()
        self._shutdown = threading.Event()

        # Store the main application loop
        self.main_loop = loop

        # Create a new loop for the worker thread
        self._worker_loop = None

        # Start background worker
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

        # Register instance for cleanup
        self._instances.add(self)

        # Register signal handlers and atexit hook
        self._setup_shutdown_handlers()

    def _setup_shutdown_handlers(self) -> None:
        """
        Set up signal handlers and register atexit hook for graceful shutdown.
        """
        # Register signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            try:
                signal.signal(sig, self._signal_handler)
            except ValueError:
                # This can happen if not in main thread
                pass

        # Register atexit handler
        atexit.register(self.close)

    def _signal_handler(self, signum: int, frame) -> None:
        """
        Handle shutdown signals by initiating graceful cleanup.
        """
        print(f"Received signal {signum}. Initiating graceful shutdown...")
        self._teardown()

    def _teardown(self) -> None:
        """
        Perform graceful teardown operations.
        """
        # Set shutdown flag
        self._shutdown.set()

        # Ensure all remaining logs are flushed
        if self._worker_loop and self._worker_loop.is_running():
            try:
                # Schedule final flush in the worker loop
                asyncio.run_coroutine_threadsafe(
                    self._flush_logs(), self._worker_loop
                ).result(
                    timeout=30
                )  # Wait up to 30 seconds for final flush
            except Exception as e:
                print(f"Error during final flush in teardown: {str(e)}")

        # Close the handler
        self.close()

    def emit(self, record: LogRecord) -> None:
        """
        Emit a record.
        """
        try:
            log_entry = self.format_log_entry(record)
            self._queue.put(log_entry)
        except Exception:
            self.handleError(record)

    def _worker(self) -> None:
        """Background thread worker that processes the queue."""
        # Create a new event loop for this thread
        self._worker_loop = asyncio.new_event_loop()
        if self._worker_loop is None:
            return
        asyncio.set_event_loop(self._worker_loop)

        try:
            while not self._shutdown.is_set() or not self._queue.empty():
                try:
                    # Get all available items from queue
                    entries = []
                    try:
                        # Wait for items with timeout
                        entry = self._queue.get(timeout=1.0)
                        entries.append(entry)

                        # Get any additional items that are already in the queue
                        while len(entries) < self.batch_size:
                            try:
                                entry = self._queue.get_nowait()
                                entries.append(entry)
                            except _queue.Empty:
                                break
                    except _queue.Empty:
                        # No items available, check if we need to flush based on time
                        if (
                            datetime.utcnow() - self.last_flush
                        ).total_seconds() >= self.flush_interval:
                            self._worker_loop.run_until_complete(self._flush_logs())
                        continue

                    if entries:
                        # Process entries
                        self._worker_loop.run_until_complete(
                            self._process_entries(entries)
                        )

                except Exception as e:
                    print(f"Error in worker thread: {str(e)}")

        finally:
            # Final flush and cleanup
            try:
                if self.log_buffer:
                    self._worker_loop.run_until_complete(self._flush_logs())
            except Exception as e:
                print(f"Error during final flush: {str(e)}")

            self._worker_loop.close()

    async def _process_entries(self, entries: List[Dict[str, Any]]) -> None:
        async with self._async_lock:
            self.log_buffer.extend(entries)
            if len(self.log_buffer) >= self.batch_size:
                await self._flush_logs()

    def format_log_entry(self, record: LogRecord) -> Dict[str, Any]:
        """
        Format a LogRecord into a dictionary.
        """
        return {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line_number": record.lineno,
            "thread": record.thread,
            "thread_name": record.threadName,
            "process": record.process,
            "process_name": record.processName,
            "exc_info": record.exc_info,
            "extra": {
                key: value
                for key, value in record.__dict__.items()
                if key not in LogRecord.__dict__ and not key.startswith("_")
            },
        }

    async def _flush_logs(self) -> None:
        if not self.log_buffer:
            return

        async with self._async_lock:
            logs_to_flush = self.log_buffer.copy()
            self.log_buffer.clear()
            self.last_flush = datetime.utcnow()

        timestamp = datetime.utcnow().strftime("%Y-%m-%d")
        filename = f"{datetime.utcnow().isoformat()}-{os.getpid()}.jsonl.gz"
        s3_key = f"{self.log_prefix}/date={timestamp}/{filename}"

        compressed_data = self._compress_logs(logs_to_flush)

        try:
            async with self.session.client("s3", **self.client_params) as s3:
                await s3.put_object(
                    Bucket=self.bucket_name,
                    Key=s3_key,
                    Body=compressed_data,
                    ContentType="application/x-gzip",
                    # ContentEncoding="gzip"
                )
        except Exception as e:
            print(f"Error uploading logs to S3: {str(e)}")

    def _compress_logs(self, logs: list) -> bytes:
        jsonl_data = "\n".join(json.dumps(log) for log in logs)
        return gzip.compress(jsonl_data.encode("utf-8"))

    def close(self) -> None:
        """
        Clean up resources and ensure remaining logs are flushed
        """
        self._shutdown.set()
        self._worker_thread.join(
            timeout=30
        )  # Wait up to 30 seconds for worker to finish
        super().close()

    @classmethod
    def cleanup_all(cls) -> None:
        """
        Class method to properly cleanup all instances
        """
        for handler in cls._instances:
            handler.close()
