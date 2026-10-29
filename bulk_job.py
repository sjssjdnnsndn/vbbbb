"""Sequential, cancellation-friendly bulk login-email job runner."""

from __future__ import annotations

import asyncio
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from telethon.tl import functions, types

from bulk_email import (
    EmailTarget,
    OtpTimeoutError,
    read_otp_baseline,
    wait_for_new_otp,
)


@dataclass
class BulkItem:
    index: int
    target: EmailTarget
    zip_path: Path
    session_path: Path
    status: str = "uploaded"
    detail: str = ""
    user_id: int | None = None


async def _cleanup_conversion_files(item: BulkItem) -> None:
    item.zip_path.unlink(missing_ok=True)
    shutil.rmtree(item.zip_path.parent / "extracted", ignore_errors=True)


def _cleanup_session(item: BulkItem) -> None:
    item.session_path.unlink(missing_ok=True)
    item.session_path.with_suffix(".session-journal").unlink(missing_ok=True)


async def _notify_batch(batch: list[BulkItem], batch_number: int, notify) -> None:
    success = sum(item.status == "success" for item in batch)
    failed = len(batch) - success
    first = batch[0].index
    last = batch[-1].index
    if failed == 0:
        await notify(
            f"Batch {batch_number} ({first}-{last}): sabhi {success} accounts ka "
            "bound mail change successful."
        )
        return
    failed_labels = ", ".join(
        f"Account {item.index} ({item.status})"
        for item in batch
        if item.status != "success"
    )
    await notify(
        f"Batch {batch_number} ({first}-{last}): success={success}, failed={failed}.\n"
        f"Failed: {failed_labels}"
    )


async def run_bulk_job(
    *,
    items: list[BulkItem],
    convert_func: Callable[[Path, Path], Awaitable[None]],
    check_func: Callable[[Path], Awaitable[dict]],
    client_factory,
    notify: Callable[[str], Awaitable[None]],
) -> dict[str, int]:
    """Run conversion, status checks, then authorized email changes in sequence."""
    total = len(items)
    await notify(f"Bulk process started: {total} items. Conversion sequence start ho rahi hai.")

    for item in items:
        try:
            await convert_func(item.zip_path, item.session_path)
            item.status = "converted"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.status = "conversion_failed"
            item.detail = type(exc).__name__
            _cleanup_session(item)
        finally:
            await _cleanup_conversion_files(item)

    converted = [item for item in items if item.status == "converted"]
    conversion_failed = total - len(converted)
    await notify(
        f"Conversion complete: success={len(converted)}, failed={conversion_failed}. "
        "Ab bound-mail checking sequence start ho rahi hai."
    )

    seen_user_ids: set[int] = set()
    for item in converted:
        try:
            status = await check_func(item.session_path)
            if not status.get("session_authorized"):
                item.status = "check_failed"
                item.detail = "unauthorized"
            else:
                item.user_id = status.get("user_id")
                if item.user_id in seen_user_ids:
                    item.status = "duplicate"
                else:
                    if item.user_id is not None:
                        seen_user_ids.add(item.user_id)
                    item.status = (
                        "already_set" if status.get("verified_login_email_set") else "pending"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.status = "check_failed"
            item.detail = type(exc).__name__
        if item.status != "pending":
            _cleanup_session(item)

    pending = [item for item in items if item.status == "pending"]
    already_set = sum(item.status == "already_set" for item in items)
    duplicates = sum(item.status == "duplicate" for item in items)
    check_failed = sum(item.status == "check_failed" for item in items)
    await notify(
        "Checking complete: "
        f"bound mail already set={already_set}, set nahi={len(pending)}, "
        f"duplicates={duplicates}, check failed={check_failed}."
    )

    if not pending:
        await notify("Kisi account ka bound mail change pending nahi hai. Bulk process complete.")
        return {
            "total": total,
            "converted": len(converted),
            "conversion_failed": conversion_failed,
            "already_set": already_set,
            "pending": 0,
            "success": 0,
            "failed": 0,
        }

    await notify(
        f"{len(pending)} accounts ka bound mail change sequential hoga. "
        "Har account ke beech random 3-5 minute gap aur OTP wait max 2 minutes hai."
    )

    async with aiohttp.ClientSession() as http_session:
        batch: list[BulkItem] = []
        batch_number = 0
        for position, item in enumerate(pending, start=1):
            if position > 1:
                await asyncio.sleep(180 + secrets.randbelow(121))

            verification_code = ""
            client = None
            try:
                # Read the inbox before sending anything so an old OTP cannot be reused.
                baseline = await read_otp_baseline(http_session, item.target.inbox_url)
                client = client_factory(str(item.session_path))
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Session unauthorized")

                sent = await client(
                    functions.account.SendVerifyEmailCodeRequest(
                        purpose=types.EmailVerifyPurposeLoginChange(),
                        email=item.target.email,
                    )
                )
                verification_code = await wait_for_new_otp(
                    http_session,
                    item.target.inbox_url,
                    baseline,
                    sent.length,
                )
                await client(
                    functions.account.VerifyEmailRequest(
                        purpose=types.EmailVerifyPurposeLoginChange(),
                        verification=types.EmailVerificationCode(code=verification_code),
                    )
                )
                verification_code = ""
                password_info = await client(functions.account.GetPasswordRequest())
                if not password_info.login_email_pattern:
                    item.status = "email_failed"
                    item.detail = "post_check_unset"
                else:
                    item.status = "success"
            except asyncio.CancelledError:
                raise
            except OtpTimeoutError:
                item.status = "otp_timeout"
            except Exception as exc:
                item.status = "email_failed"
                item.detail = type(exc).__name__
            finally:
                verification_code = ""
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                _cleanup_session(item)

            batch.append(item)
            if len(batch) == 5:
                batch_number += 1
                await _notify_batch(batch, batch_number, notify)
                batch = []

        if batch:
            batch_number += 1
            await _notify_batch(batch, batch_number, notify)

    success = sum(item.status == "success" for item in pending)
    failed = len(pending) - success
    await notify(
        "Bulk process complete: "
        f"email success={success}, email failed/timeout={failed}, "
        f"already set={already_set}, conversion failed={conversion_failed}, "
        f"check failed={check_failed}, duplicates={duplicates}."
    )
    return {
        "total": total,
        "converted": len(converted),
        "conversion_failed": conversion_failed,
        "already_set": already_set,
        "pending": len(pending),
        "success": success,
        "failed": failed,
    }
