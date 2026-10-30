"""Confirmed, sequential bulk 2FA password changes with secret cleanup."""
from __future__ import annotations
import asyncio
import csv
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable
from bulk_email import BulkInputError
from bulk_job import BulkItem

@dataclass
class PasswordPair:
    old_password: str
    new_password: str

    def clear(self) -> None:
        self.old_password = ''
        self.new_password = ''

def _parse_line(raw_line: str, line_number: int) -> PasswordPair:
    delimiter = '\t' if '\t' in raw_line and ',' not in raw_line else ','
    try:
        fields = next(csv.reader([raw_line], delimiter=delimiter, strict=True))
    except (csv.Error, StopIteration):
        raise BulkInputError(f'密码列表第 {line_number} 行：CSV/TSV 格式无效') from None
    if len(fields) != 2:
        raise BulkInputError(f'密码列表第 {line_number} 行：应为 old_password,new_password')
    old_password, new_password = fields
    if not old_password or not new_password:
        raise BulkInputError(f'密码列表第 {line_number} 行：密码不能为空')
    if len(old_password) > 256 or len(new_password) > 256:
        raise BulkInputError(f'密码列表第 {line_number} 行：密码最长为 256 个字符')
    if any(('\x00' in value or '\r' in value or '\n' in value for value in fields)):
        raise BulkInputError(f'密码列表第 {line_number} 行：包含无效控制字符')
    if old_password == new_password:
        raise BulkInputError(f'密码列表第 {line_number} 行：旧密码与新密码必须不同')
    return PasswordPair(old_password, new_password)

def parse_password_pairs(text: str, expected_count: int) -> list[PasswordPair]:
    pairs: list[PasswordPair] = []
    try:
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if not pairs:
                lowered = raw_line.strip().lower().replace(' ', '_')
                if lowered in {'old_password,new_password', 'old_password\tnew_password'}:
                    continue
            pairs.append(_parse_line(raw_line, line_number))
        if len(pairs) != expected_count:
            raise BulkInputError(f'密码列表数量不匹配：应为 {expected_count} 行，且顺序与原始账号一致')
        return pairs
    except Exception:
        clear_password_pairs(pairs)
        raise

def clear_password_pairs(pairs: list[PasswordPair] | None) -> None:
    if not pairs:
        return
    for pair in pairs:
        pair.clear()
    pairs.clear()

async def _notify_batch(batch: list[BulkItem], batch_number: int, notify) -> None:
    success = sum((item.twofa_status == 'success' for item in batch))
    failed = sum((item.twofa_status == 'failed' for item in batch))
    first = batch[0].index
    last = batch[-1].index
    await notify(f'两步验证批次 {batch_number}（账号 {first}-{last}）：密码更改成功={success}，失败={failed}.')

async def run_bulk_twofa_job(*, items: list[BulkItem], pairs: list[PasswordPair], client_factory: Callable[[str], object], change_func: Callable[[object, str, str], Awaitable[None]], notify: Callable[[str], Awaitable[None]], sleep_func: Callable[[float], Awaitable[None]]=asyncio.sleep, gap_seconds: Callable[[], int] | None=None) -> dict[str, int]:
    if len(items) != len(pairs):
        raise ValueError('密码对数量与账号数量不匹配')
    gap_seconds = gap_seconds or (lambda: 120 + secrets.randbelow(121))
    eligible_total = sum((item.twofa_eligible for item in items))
    await notify(f'批量两步验证流程已开始：{eligible_total} 个符合条件的账号，共 {len(items)} 个账号。每次操作随机间隔 2–4 分钟。')
    attempted = 0
    batch: list[BulkItem] = []
    batch_number = 0
    for item, pair in zip(items, pairs):
        if not item.twofa_eligible or not item.session_path.is_file():
            item.twofa_status = 'skipped'
            item.twofa_detail = item.status
            pair.clear()
            continue
        attempted += 1
        if attempted > 1:
            await sleep_func(gap_seconds())
        item.twofa_status = 'running'
        client = None
        try:
            client = client_factory(str(item.session_path))
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError('会话未授权')
            await change_func(client, pair.old_password, pair.new_password)
            item.twofa_status = 'success'
            item.twofa_detail = '已确认'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.twofa_status = 'failed'
            item.twofa_detail = type(exc).__name__
        finally:
            pair.clear()
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
    success = sum((item.twofa_status == 'success' for item in items))
    failed = sum((item.twofa_status == 'failed' for item in items))
    skipped = sum((item.twofa_status == 'skipped' for item in items))
    await notify(f'批量两步验证流程完成：成功={success}，失败={failed}，跳过={skipped}.')
    return {'total': len(items), 'eligible': eligible_total, 'success': success, 'failed': failed, 'skipped': skipped}
