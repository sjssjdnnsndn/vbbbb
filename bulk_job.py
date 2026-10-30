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
from bulk_email import EmailTarget, OtpTimeoutError, read_otp_baseline, wait_for_new_otp

@dataclass
class BulkItem:
    index: int
    target: EmailTarget
    zip_path: Path
    session_path: Path
    status: str = 'uploaded'
    detail: str = ''
    user_id: int | None = None
    phone_number: str = ''
    twofa_eligible: bool = False
    twofa_status: str = 'not_started'
    twofa_detail: str = ''

async def _cleanup_conversion_files(item: BulkItem) -> None:
    item.zip_path.unlink(missing_ok=True)
    shutil.rmtree(item.zip_path.parent / 'extracted', ignore_errors=True)

def _cleanup_session(item: BulkItem) -> None:
    item.session_path.unlink(missing_ok=True)
    item.session_path.with_suffix('.session-journal').unlink(missing_ok=True)
STATUS_LABELS = {'uploaded': '已上传', 'converted': '已转换', 'conversion_failed': '转换失败', 'check_failed': '检查失败', 'duplicate': '重复账号', 'already_set': '已设置', 'pending': '待处理', 'success': '成功', 'email_failed': '邮箱更改失败', 'otp_timeout': '验证码超时'}

def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)

async def _notify_batch(batch: list[BulkItem], batch_number: int, notify) -> None:
    success = sum((item.status == 'success' for item in batch))
    failed = len(batch) - success
    first = batch[0].index
    last = batch[-1].index
    if failed == 0:
        await notify(f'批次 {batch_number}（{first}-{last}）：全部 {success} 个账号的绑定邮箱均已成功更改。')
        return
    failed_labels = ', '.join((f'账号 {item.index}（{_status_label(item.status)})' for item in batch if item.status != 'success'))
    await notify(f'批次 {batch_number}（{first}-{last}）：成功={success}，失败={failed}。\n失败：{failed_labels}')

async def run_bulk_job(*, items: list[BulkItem], convert_func: Callable[[Path, Path], Awaitable[None]], check_func: Callable[[Path], Awaitable[dict]], client_factory, notify: Callable[[str], Awaitable[None]]) -> dict[str, int]:
    """Run conversion, status checks, then authorized email changes in sequence."""
    total = len(items)
    await notify(f'批量流程已开始：{total} 个项目。开始逐个转换。')
    for item in items:
        try:
            await convert_func(item.zip_path, item.session_path)
            item.status = 'converted'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.status = 'conversion_failed'
            item.detail = type(exc).__name__
            _cleanup_session(item)
        finally:
            await _cleanup_conversion_files(item)
    converted = [item for item in items if item.status == 'converted']
    conversion_failed = total - len(converted)
    await notify(f'转换完成：成功={len(converted)}，失败={conversion_failed}。开始逐个检查绑定邮箱。')
    seen_user_ids: set[int] = set()
    for item in converted:
        try:
            status = await check_func(item.session_path)
            if not status.get('session_authorized'):
                item.status = 'check_failed'
                item.detail = 'unauthorized'
            else:
                item.user_id = status.get('user_id')
                item.phone_number = str(status.get('phone_number') or '')
                if item.user_id in seen_user_ids:
                    item.status = 'duplicate'
                else:
                    if item.user_id is not None:
                        seen_user_ids.add(item.user_id)
                    item.twofa_eligible = True
                    item.status = 'already_set' if status.get('verified_login_email_set') else 'pending'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.status = 'check_failed'
            item.detail = type(exc).__name__
        if not item.twofa_eligible:
            _cleanup_session(item)
    pending = [item for item in items if item.status == 'pending']
    already_set = sum((item.status == 'already_set' for item in items))
    duplicates = sum((item.status == 'duplicate' for item in items))
    check_failed = sum((item.status == 'check_failed' for item in items))
    await notify(f'检查完成：已设置绑定邮箱={already_set}，未设置={len(pending)}，重复={duplicates}，检查失败={check_failed}.')
    if not pending:
        await notify('没有需要更改绑定邮箱的账号。绑定邮箱阶段完成。')
        return {'total': total, 'converted': len(converted), 'conversion_failed': conversion_failed, 'already_set': already_set, 'pending': 0, 'success': 0, 'failed': 0}
    await notify(f'{len(pending)} 个账号将逐个更改绑定邮箱。账号之间随机间隔 3–5 分钟，验证码最多等待 2 分钟。')
    async with aiohttp.ClientSession() as http_session:
        batch: list[BulkItem] = []
        batch_number = 0
        for position, item in enumerate(pending, start=1):
            if position > 1:
                await asyncio.sleep(180 + secrets.randbelow(121))
            verification_code = ''
            client = None
            try:
                baseline = await read_otp_baseline(http_session, item.target.inbox_url)
                client = client_factory(str(item.session_path))
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError('会话未授权')
                sent = await client(functions.account.SendVerifyEmailCodeRequest(purpose=types.EmailVerifyPurposeLoginChange(), email=item.target.email))
                verification_code = await wait_for_new_otp(http_session, item.target.inbox_url, baseline, sent.length)
                await client(functions.account.VerifyEmailRequest(purpose=types.EmailVerifyPurposeLoginChange(), verification=types.EmailVerificationCode(code=verification_code)))
                verification_code = ''
                password_info = await client(functions.account.GetPasswordRequest())
                if not password_info.login_email_pattern:
                    item.status = 'email_failed'
                    item.detail = 'post_check_unset'
                else:
                    item.status = 'success'
            except asyncio.CancelledError:
                raise
            except OtpTimeoutError:
                item.status = 'otp_timeout'
            except Exception as exc:
                item.status = 'email_failed'
                item.detail = type(exc).__name__
            finally:
                verification_code = ''
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            batch.append(item)
            if len(batch) == 5:
                batch_number += 1
                await _notify_batch(batch, batch_number, notify)
                batch = []
        if batch:
            batch_number += 1
            await _notify_batch(batch, batch_number, notify)
    success = sum((item.status == 'success' for item in pending))
    failed = len(pending) - success
    await notify(f'批量绑定邮箱阶段完成：邮箱成功={success}，邮箱失败/超时={failed}，已设置={already_set}，转换失败={conversion_failed}，检查失败={check_failed}，重复={duplicates}.')
    return {'total': total, 'converted': len(converted), 'conversion_failed': conversion_failed, 'already_set': already_set, 'pending': len(pending), 'success': success, 'failed': failed}
