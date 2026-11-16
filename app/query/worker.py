"""Durable export workers may run inside ASGI or as a separate process."""

import asyncio
import logging

from app.query.exports import generate


async def consume(store, slots):
    while True:
        try:
            job = await asyncio.to_thread(store.next_job)
            if job is None:
                await asyncio.sleep(1)
                continue
            try:
                payload = job["payload"]
                async with slots:
                    reports = await asyncio.to_thread(
                        generate,
                        payload["data"],
                        payload["charts"],
                        payload["formats"],
                        payload["receipt"],
                    )
            except Exception:
                logging.getLogger(__name__).exception("export_job_failed")
                await asyncio.to_thread(store.finish_job, job)
                continue
            await asyncio.to_thread(store.finish_job, job, reports)
        except asyncio.CancelledError:
            # The expiring lease allows another worker to recover after restart.
            raise
        except Exception as exc:  # noqa: BLE001 - keep retries alive without logging private storage values
            # Keep the consumer alive; unfinished jobs recover through their leases.
            logging.getLogger(__name__).warning(
                "export_storage_retry error=%s", type(exc).__name__
            )
            await asyncio.sleep(1)


async def maintain(store, retention_days):
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(store.prune, retention_days)
        except Exception as exc:  # noqa: BLE001 - keep cleanup alive after temporary storage failures
            logging.getLogger(__name__).warning(
                "cleanup_storage_retry error=%s", type(exc).__name__
            )


async def main():
    from app.config import Settings
    from app.diagnostics import configure_logging
    from app.query.store import Store

    settings = Settings()
    configure_logging(settings.application_log_file)
    store = Store(
        settings.database_file,
        settings.conversation_database_url.get_secret_value(),
        settings.conversation_retention_days,
    )
    try:
        slots = asyncio.Semaphore(settings.max_export_workers)
        await asyncio.gather(
            *(consume(store, slots) for _ in range(settings.max_export_workers))
        )
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
