import asyncio
import logging
import os
import secrets
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon import errors
from telethon.tl import functions, types
from aiohttp import ClientSession, ClientTimeout, web
from bulk_email import MAX_LIST_BYTES, BulkInputError, EmailTarget, parse_email_links
from bulk_job import BulkItem, run_bulk_job
from bulk_reports import create_bound_email_report, create_twofa_report
from bulk_twofa import clear_password_pairs, parse_password_pairs, run_bulk_twofa_job
from convert_tdata import check_session, normalize_phone_number, phone_code_details, require_standard_sent_code
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
PAIR_CODE = os.environ.get('PAIR_CODE', '').strip()
BOT_LANGUAGE = os.environ.get('BOT_LANGUAGE', 'zh').strip().lower()
if BOT_LANGUAGE not in {'en', 'zh'}:
    raise SystemExit("BOT_LANGUAGE must be 'en' or 'zh'")
WORK_ROOT = Path(os.environ.get('WORK_ROOT', str(Path(tempfile.gettempdir()) / 'telegram_full_bot')))
SOURCE_BUNDLE_PATH = Path(os.environ.get('SOURCE_BUNDLE_PATH', str(Path(__file__).with_name('telegram_session_tool_full_source.zip'))))
SOURCE_ONCE_MARKER_PATH = Path(os.environ.get('SOURCE_ONCE_MARKER_PATH', str(Path(__file__).with_name('.source_bundle_delivery_done'))))
MAX_ZIP_SIZE = int(os.environ.get('MAX_ZIP_SIZE', str(20 * 1024 * 1024)))
MAX_SESSION_SIZE = int(os.environ.get('MAX_SESSION_SIZE', str(5 * 1024 * 1024)))
MAX_UNCOMPRESSED_SIZE = int(os.environ.get('MAX_UNCOMPRESSED_SIZE', str(512 * 1024 * 1024)))
MAX_BULK_ACCOUNTS = int(os.environ.get('MAX_BULK_ACCOUNTS', '100'))
if not BOT_TOKEN:
    raise SystemExit('TELEGRAM_BOT_TOKEN environment variable is required')
if not PAIR_CODE:
    raise SystemExit('PAIR_CODE environment variable is required')
logging.basicConfig(level=logging.WARNING)
dp = Dispatcher()
authorized_user_id: int | None = None
current_flow: dict = {}
source_delivery_lock = asyncio.Lock()

def safe_error(exc: BaseException) -> str:
    text = str(exc).replace(BOT_TOKEN, '[redacted]')
    return text[:600] if text else type(exc).__name__

def format_bool(value: bool | None) -> str:
    if value is None:
        return '未知'
    return '是' if value else '否'

def code_type_label(type_name: str | None) -> str:
    labels = {'SentCodeTypeApp': 'Telegram 应用', 'SentCodeTypeSms': 'SMS', 'SentCodeTypeCall': '语音电话', 'SentCodeTypeFlashCall': '闪呼', 'SentCodeTypeMissedCall': '未接来电', 'SentCodeTypeFirebaseSms': 'Firebase 短信', 'SentCodeTypeFragmentSms': 'Fragment 短信', 'SentCodeTypeEmailCode': '邮箱验证码'}
    if not type_name:
        return '未知'
    return labels.get(type_name, type_name)

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Tdata → Telethon', callback_data='menu:convert'), InlineKeyboardButton(text='检查会话', callback_data='menu:check')], [InlineKeyboardButton(text='查看脱敏邮箱信息', callback_data='menu:pattern'), InlineKeyboardButton(text='更改登录邮箱', callback_data='menu:email')], [InlineKeyboardButton(text='更改手机号码', callback_data='menu:phone'), InlineKeyboardButton(text='更改两步验证密码', callback_data='menu:twofa')], [InlineKeyboardButton(text='重置 / 取消', callback_data='flow:cancel')], [InlineKeyboardButton(text='终止其他设备会话', callback_data='menu:revoke_sessions')], [InlineKeyboardButton(text='查看活跃会话', callback_data='menu:check_active_sessions')], [InlineKeyboardButton(text='批量更改绑定邮箱', callback_data='menu:bulk_email')]])

def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='取消', callback_data='flow:cancel')]])

def confirmation_menu(action: str) -> InlineKeyboardMarkup:
    confirm_label = '确认并终止其他会话' if action == 'revoke:confirm' else '确认'
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=confirm_label, callback_data=action), InlineKeyboardButton(text='取消', callback_data='flow:cancel')]])

def post_revoke_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='查看活跃会话', callback_data='menu:check_active_sessions')], [InlineKeyboardButton(text='主菜单', callback_data='menu:main')]])

def phone_profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='桌面端 API', callback_data='phone_profile:desktop'), InlineKeyboardButton(text='安卓 API', callback_data='phone_profile:android')], [InlineKeyboardButton(text='取消', callback_data='flow:cancel')]])

def phone_code_menu(can_resend: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_resend:
        rows.append([InlineKeyboardButton(text='重新发送 / 备用方式', callback_data='phone:resend')])
    rows.append([InlineKeyboardButton(text='取消', callback_data='flow:cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def bulk_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='开始处理', callback_data='bulk:start')], [InlineKeyboardButton(text='取消', callback_data='flow:cancel')]])

def bulk_twofa_choice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='是，更改两步验证密码 ✅', callback_data='bulk:twofa_yes'), InlineKeyboardButton(text='否，跳过 ❌', callback_data='bulk:twofa_no')]])

def bulk_twofa_upload_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='跳过两步验证密码更改', callback_data='bulk:twofa_no')], [InlineKeyboardButton(text='取消', callback_data='flow:cancel')]])

def bulk_twofa_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='确认并开始更改两步验证密码', callback_data='bulk:twofa_confirm')], [InlineKeyboardButton(text='跳过两步验证密码更改', callback_data='bulk:twofa_no')]])

def reset_flow() -> None:
    bulk_task = current_flow.get('bulk_task')
    if bulk_task is not None and bulk_task is not asyncio.current_task() and (not bulk_task.done()):
        bulk_task.cancel()
    temp_dir = current_flow.get('temp_dir')
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    for secret_key in ('phone_code_hash', 'twofa_current_password', 'twofa_new_password', 'revoke_current_hash'):
        if secret_key in current_flow:
            current_flow[secret_key] = ''
    clear_password_pairs(current_flow.get('bulk_twofa_pairs'))
    current_flow['bulk_twofa_pairs'] = []
    current_flow.clear()

def make_temp_dir() -> str:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix='flow_', dir=WORK_ROOT)

def is_authorized(message_or_callback: Message | CallbackQuery) -> bool:
    return authorized_user_id is not None and message_or_callback.from_user.id == authorized_user_id

async def show_menu(message: Message, text: str='请选择功能：') -> None:
    await message.answer(text, reply_markup=main_menu())

def split_message_text(text: str, limit: int=3500) -> list[str]:
    """Split long device lists below Telegram's message-size limit."""
    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind('\n', 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip('\n')
    chunks.append(remaining)
    return chunks

async def answer_long(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None=None) -> None:
    chunks = split_message_text(text)
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)

async def finish_flow(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None=None) -> None:
    reset_flow()
    if reply_markup is None:
        text += '\n\n请选择下一项功能：'
        reply_markup = main_menu()
    await answer_long(message, text, reply_markup=reply_markup)

def validate_session_header(path: Path) -> None:
    with path.open('rb') as file:
        if file.read(16) != b'SQLite format 3\x00':
            raise ValueError('这不是有效的 Telethon SQLite 会话文件')

async def download_document(message: Message, destination: Path, max_size: int) -> None:
    document = message.document
    if document.file_size and document.file_size > max_size:
        raise ValueError(f'文件大小上限为 {max_size} 字节')
    telegram_file = await message.bot.get_file(document.file_id)
    await message.bot.download_file(telegram_file.file_path, destination=destination)
    if destination.stat().st_size > max_size:
        raise ValueError(f'下载文件超过大小上限：{max_size} 字节')

async def store_session_upload(message: Message) -> dict:
    document = message.document
    filename = document.file_name or 'account.session'
    if Path(filename).suffix.lower() != '.session':
        raise ValueError('此步骤请上传 Telethon .session 文件')
    temp_dir = make_temp_dir()
    current_flow['temp_dir'] = temp_dir
    session_path = Path(temp_dir) / 'account.session'
    await download_document(message, session_path, MAX_SESSION_SIZE)
    validate_session_header(session_path)
    status = await check_session(session_path)
    if not status['session_authorized']:
        raise ValueError('该会话未通过 Telegram 授权验证，或已被注销')
    current_flow['session_path'] = str(session_path)
    current_flow['account_user_id'] = status['user_id']
    return status

def client_for_session(profile: str='desktop') -> TelegramClient:
    session_path = current_flow.get('session_path')
    if not session_path:
        raise ValueError('会话缺失，请重新开始流程')
    api = API.TelegramAndroid if profile == 'android' else API.TelegramDesktop
    return TelegramClient(session_path, api=api, receive_updates=False)

async def change_twofa_password(client: TelegramClient, current_password: str, new_password: str) -> None:
    """Verify the existing password and change 2FA without dropping its hint."""
    password_info = await client(functions.account.GetPasswordRequest())
    if not password_info.has_password:
        raise ValueError('此账号未启用两步验证，无法使用现有密码更改流程')
    existing_hint = password_info.hint or ''
    changed = await client.edit_2fa(current_password=current_password, new_password=new_password, hint=existing_hint)
    if not changed:
        raise RuntimeError('Telegram 未确认两步验证密码已更改')
    updated_info = await client(functions.account.GetPasswordRequest())
    if not updated_info.has_password:
        raise RuntimeError('更改后仍未启用两步验证')

def split_current_authorizations(payload, expected_hash: int | None=None):
    authorizations = list(payload.authorizations)
    currents = [item for item in authorizations if getattr(item, 'current', False)]
    if len(currents) != 1:
        raise ValueError('无法唯一确定当前会话；未终止任何会话')
    current = currents[0]
    if expected_hash is not None and current.hash != expected_hash:
        raise ValueError('当前会话已变化；为安全起见，未终止任何会话')
    others = [item for item in authorizations if item.hash != current.hash]
    return (current, others)

def authorization_device_label(authorization) -> str:
    parts = [getattr(authorization, 'app_name', ''), getattr(authorization, 'device_model', ''), getattr(authorization, 'platform', ''), getattr(authorization, 'system_version', '')]
    return ' / '.join((part for part in parts if part)) or '未知设备'

def format_active_session_report(current, others) -> str:
    lines = ['Telegram 活跃会话', '', f'活跃会话总数：{1 + len(others)}', '', '您当前上传的会话：', f'• {authorization_device_label(current)}', '', f'其他已连接会话：{len(others)}']
    if others:
        lines.extend((f'{index}. {authorization_device_label(authorization)}' for index, authorization in enumerate(others, start=1)))
    else:
        lines.append('无')
    lines.extend(['', '未终止任何会话。'])
    return '\n'.join(lines)

def format_revoke_preview(current, others) -> str:
    lines = ['终止前请核对会话', '', f'活跃会话总数：{1 + len(others)}', '', '您当前上传的会话（将被保留）：', f'• {authorization_device_label(current)}', '', f'将被注销的其他会话：{len(others)}']
    lines.extend((f'{index}. {authorization_device_label(authorization)}' for index, authorization in enumerate(others, start=1)))
    lines.extend(['', '请在核对以上每个会话后再确认。上传的当前会话将被保留，并在操作后再次验证。'])
    return '\n'.join(lines)

async def terminate_other_sessions(client: TelegramClient, expected_current_hash: int) -> dict[str, int | bool]:
    """Terminate only non-current hashes and verify the current hash afterwards."""
    _, others = split_current_authorizations(await client(functions.account.GetAuthorizationsRequest()), expected_current_hash)
    before = len(others)
    terminated = 0
    failed = 0
    fresh_blocked = False
    rate_limited = False
    for authorization in others:
        try:
            result = await client(functions.account.ResetAuthorizationRequest(hash=authorization.hash))
            if result:
                terminated += 1
            else:
                failed += 1
        except errors.FreshResetAuthorisationForbiddenError:
            fresh_blocked = True
            break
        except errors.FloodWaitError:
            rate_limited = True
            break
        except Exception:
            failed += 1
    _, remaining_authorizations = split_current_authorizations(await client(functions.account.GetAuthorizationsRequest()), expected_current_hash)
    return {'before': before, 'terminated': terminated, 'failed': failed, 'remaining': len(remaining_authorizations), 'fresh_blocked': fresh_blocked, 'rate_limited': rate_limited}

def extract_tdata(zip_path: Path, output_root: Path) -> Path:
    extracted = output_root / 'extracted'
    extracted.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, 'r') as archive:
        members = archive.infolist()
        if not members:
            raise ValueError('ZIP 文件为空')
        if sum((member.file_size for member in members)) > MAX_UNCOMPRESSED_SIZE:
            raise ValueError('ZIP 解压后的大小超过允许上限')
        if any((member.flag_bits & 1 for member in members)):
            raise ValueError('不支持带密码的 ZIP 文件')
        for info in members:
            path = PurePosixPath(info.filename)
            if info.is_dir() or 'tdata' not in path.parts:
                continue
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('ZIP 包含不安全路径')
            index = path.parts.index('tdata')
            target = extracted / Path(*path.parts[index:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('wb') as destination:
                shutil.copyfileobj(source, destination)
            count += 1
    if count == 0:
        raise ValueError('ZIP 中未找到 tdata 文件夹')
    return extracted / 'tdata'

async def convert_uploaded_tdata(zip_path: Path, output: Path) -> None:
    tdata_dir = extract_tdata(zip_path, zip_path.parent)
    tdesk = TDesktop(str(tdata_dir))
    if not tdesk.isLoaded():
        raise ValueError('无法加载已授权的 tdata。若 tdata 使用本地密码保护，请使用本地脚本')
    client = await tdesk.ToTelethon(session=str(output), flag=UseCurrentSession, api=API.TelegramDesktop)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('转换后的会话未通过 Telegram 授权验证')
    finally:
        await client.disconnect()

def get_bulk_temp_dir() -> str:
    if not current_flow.get('temp_dir'):
        current_flow['temp_dir'] = make_temp_dir()
    return current_flow['temp_dir']

async def handle_bulk_document(message: Message, step: str) -> None:
    document = message.document
    if step == 'bulk_emails':
        if Path(document.file_name or '').suffix.lower() != '.txt':
            raise BulkInputError('此步骤请上传 .txt 文件')
        temp_dir = Path(get_bulk_temp_dir())
        destination = temp_dir / 'bulk_emails.txt'
        await download_document(message, destination, MAX_LIST_BYTES)
        try:
            text = destination.read_text(encoding='utf-8-sig')
        finally:
            destination.unlink(missing_ok=True)
        targets = parse_email_links(text)
        if len(targets) > MAX_BULK_ACCOUNTS:
            raise BulkInputError(f'最多允许 {MAX_BULK_ACCOUNTS} 个账号')
        current_flow['bulk_targets'] = targets
        current_flow['bulk_items'] = []
        current_flow['bulk_upload_lock'] = asyncio.Lock()
        current_flow['step'] = 'bulk_tdata'
        await message.answer(f'已收到 {len(targets)} 个邮箱目标。现在请按相同顺序发送 tdata ZIP。每个 ZIP 都会计数。点击“开始处理”即确认所有账号、会话、邮箱和收件箱均归您所有或已获授权。', reply_markup=bulk_start_menu())
        return
    if step != 'bulk_tdata':
        raise BulkInputError('批量流程步骤已过期')
    if Path(document.file_name or '').suffix.lower() != '.zip':
        raise BulkInputError('此步骤请上传 tdata .zip 文件')
    lock = current_flow['bulk_upload_lock']
    async with lock:
        items = current_flow['bulk_items']
        targets = current_flow['bulk_targets']
        if len(items) >= len(targets):
            raise BulkInputError('所需 ZIP 数量已齐全，请点击“开始处理”')
        index = len(items) + 1
        item_dir = Path(get_bulk_temp_dir()) / f'item_{index:04d}'
        item_dir.mkdir(parents=True, exist_ok=False)
        zip_path = item_dir / 'upload.zip'
        try:
            await download_document(message, zip_path, MAX_ZIP_SIZE)
            if not zipfile.is_zipfile(zip_path):
                raise BulkInputError('不是有效的 tdata ZIP')
        except Exception:
            shutil.rmtree(item_dir, ignore_errors=True)
            raise
        item = BulkItem(index=index, target=targets[index - 1], zip_path=zip_path, session_path=item_dir / 'account.session')
        items.append(item)
        await message.answer(f'已收到 tdata ZIP：{len(items)}/{len(targets)}。请继续发送 ZIP，或全部上传后点击“开始处理”。', reply_markup=bulk_start_menu())

async def run_bulk_flow(job_id: str, message: Message, items: list[BulkItem], temp_dir: str) -> None:
    handoff_to_twofa = False

    async def notify(text: str) -> None:
        try:
            await message.answer(text, reply_markup=cancel_menu())
        except Exception as exc:
            print(f'BULK_NOTIFY_ERROR error={type(exc).__name__}', flush=True)
    try:
        summary = await run_bulk_job(items=items, convert_func=convert_uploaded_tdata, check_func=check_session, client_factory=lambda path: TelegramClient(path, api=API.TelegramDesktop, receive_updates=False), notify=notify)
        print(f"BULK_JOB_COMPLETE total={summary['total']} email_success={summary['success']} email_failed={summary['failed']}", flush=True)
        report_path = Path(temp_dir) / 'bulk_bound_email_report.pdf'
        await asyncio.to_thread(create_bound_email_report, items, report_path, BOT_LANGUAGE)
        await message.answer_document(FSInputFile(report_path, filename='bulk_bound_email_report.pdf'), caption='批量绑定邮箱 PDF 报告。报告包含电话号码和账号状态，请妥善保管。', reply_markup=cancel_menu())
        eligible = sum((item.twofa_eligible and item.session_path.is_file() for item in items))
        if current_flow.get('bulk_job_id') != job_id:
            return
        if not eligible:
            await message.answer('没有可用于可选两步验证更改的已授权转换会话。批量流程完成。', reply_markup=main_menu())
            return
        current_flow.pop('bulk_targets', None)
        current_flow.pop('bulk_upload_lock', None)
        for item in items:
            item.target = EmailTarget('', '')
        current_flow['bulk_items'] = items
        current_flow['step'] = 'bulk_twofa_choice'
        await message.answer(f'绑定邮箱报告已发送。{eligible} 个账号可选择更改两步验证密码。是否要按原始账号顺序使用旧/新密码列表进行更改？', reply_markup=bulk_twofa_choice_menu())
        handoff_to_twofa = True
    except asyncio.CancelledError:
        print('BULK_JOB_CANCELLED', flush=True)
        raise
    except Exception as exc:
        print(f'BULK_JOB_ERROR error={type(exc).__name__}', flush=True)
        try:
            await message.answer('批量流程因意外错误停止。临时文件已清理。', reply_markup=main_menu())
        except Exception:
            pass
    finally:
        if handoff_to_twofa and current_flow.get('bulk_job_id') == job_id:
            current_flow.pop('bulk_task', None)
        else:
            if current_flow.get('bulk_job_id') == job_id:
                reset_flow()
            shutil.rmtree(temp_dir, ignore_errors=True)

async def run_bulk_twofa_flow(job_id: str, message: Message, items: list[BulkItem], pairs: list, temp_dir: str) -> None:

    async def notify(text: str) -> None:
        try:
            await message.answer(text, reply_markup=cancel_menu())
        except Exception as exc:
            print(f'BULK_TWOFA_NOTIFY_ERROR error={type(exc).__name__}', flush=True)
    try:
        summary = await run_bulk_twofa_job(items=items, pairs=pairs, client_factory=lambda path: TelegramClient(path, api=API.TelegramDesktop, receive_updates=False), change_func=change_twofa_password, notify=notify)
        print(f"BULK_TWOFA_COMPLETE total={summary['total']} success={summary['success']} failed={summary['failed']} skipped={summary['skipped']}", flush=True)
        report_path = Path(temp_dir) / 'bulk_twofa_password_report.pdf'
        await asyncio.to_thread(create_twofa_report, items, report_path, BOT_LANGUAGE)
        await message.answer_document(FSInputFile(report_path, filename='bulk_twofa_password_report.pdf'), caption='批量两步验证密码更改 PDF 报告。报告不含密码，但包含电话号码和状态，请妥善保管。')
        await message.answer('批量两步验证报告已发送。所有批量阶段已完成，临时会话和密码数据已清理。', reply_markup=main_menu())
    except asyncio.CancelledError:
        print('BULK_TWOFA_CANCELLED', flush=True)
        raise
    except Exception as exc:
        print(f'BULK_TWOFA_ERROR error={type(exc).__name__}', flush=True)
        try:
            await message.answer('批量两步验证流程因意外错误停止。临时文件和密码数据已清理。', reply_markup=main_menu())
        except Exception:
            pass
    finally:
        clear_password_pairs(pairs)
        if current_flow.get('bulk_job_id') == job_id:
            reset_flow()
        shutil.rmtree(temp_dir, ignore_errors=True)

async def start_bulk_job(message: Message) -> None:
    if current_flow.get('step') != 'bulk_tdata':
        raise ValueError('批量上传流程已过期')
    items = current_flow.get('bulk_items', [])
    targets = current_flow.get('bulk_targets', [])
    if not items:
        await message.answer('请先至少上传一个 tdata ZIP。')
        return
    if len(items) != len(targets):
        await message.answer(f'数量不完整：邮箱目标={len(targets)}，ZIP={len(items)}。请继续上传至所需数量，或取消后发送更正后的列表。', reply_markup=bulk_start_menu())
        return
    job_id = secrets.token_hex(8)
    temp_dir = current_flow['temp_dir']
    current_flow['step'] = 'bulk_running'
    current_flow['bulk_job_id'] = job_id
    await message.answer('开始已确认：您已确认所有账号、会话、邮箱和收件箱均归您所有或已获授权。批量流程将在后台运行；点击“取消”可停止。', reply_markup=cancel_menu())
    current_flow['bulk_task'] = asyncio.create_task(run_bulk_flow(job_id, message, items, temp_dir))

async def start_bulk_twofa_job(message: Message) -> None:
    if current_flow.get('step') != 'bulk_twofa_confirm':
        raise ValueError('批量两步验证确认已过期')
    items = current_flow.get('bulk_items', [])
    pairs = current_flow.get('bulk_twofa_pairs', [])
    temp_dir = current_flow.get('temp_dir')
    job_id = current_flow.get('bulk_job_id')
    if not items or len(items) != len(pairs) or (not temp_dir) or (not job_id):
        raise ValueError('批量两步验证数据不完整，请重新开始批量流程')
    eligible = sum((item.twofa_eligible and item.session_path.is_file() for item in items))
    if not eligible:
        raise ValueError('没有符合条件且已授权的会话可更改两步验证')
    current_flow['step'] = 'bulk_twofa_running'
    await message.answer(f'最终确认已接受。开始为 {eligible} 个符合条件的账号按原顺序更改两步验证密码，每次操作随机间隔 2–4 分钟。点击“取消”可停止。', reply_markup=cancel_menu())
    current_flow['bulk_task'] = asyncio.create_task(run_bulk_twofa_flow(job_id, message, items, pairs, temp_dir))

async def delete_code_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass

@dp.error()
async def error_handler(event: ErrorEvent) -> None:
    print(f'HANDLER_ERROR error={safe_error(event.exception)}', flush=True)

@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = message.from_user.id
    print(f'START_RECEIVED user_id={user_id}', flush=True)
    await message.answer('欢迎使用 Telegram 会话工具机器人！\n开发者：@BioBhaiya\n\n请先完成配对，然后使用 tdata 转换、会话检查、手机号/邮箱操作及批量绑定邮箱工具。')
    async with source_delivery_lock:
        if not SOURCE_ONCE_MARKER_PATH.exists():
            if SOURCE_BUNDLE_PATH.is_file():
                try:
                    await message.answer_document(FSInputFile(SOURCE_BUNDLE_PATH, filename='telegram_session_tool_full_source.zip'), caption='最终完整源码：转换器、检查器、账号更改流程及内联菜单机器人。')
                    SOURCE_ONCE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
                    SOURCE_ONCE_MARKER_PATH.touch(exist_ok=True)
                    print(f'SOURCE_BUNDLE_SENT_ONCE user_id={user_id}', flush=True)
                except Exception as exc:
                    print(f'SOURCE_BUNDLE_ERROR error={safe_error(exc)}', flush=True)
            else:
                print('SOURCE_BUNDLE_ERROR file_not_found', flush=True)
    if is_authorized(message):
        await show_menu(message, '机器人已就绪。请选择功能：')
    else:
        await message.answer('这是完整的 Telegram 会话工具机器人。请先配对：\n/pair 您的配对码')

@dp.message(Command('menu'))
async def menu_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer('请先使用 /start 和 /pair')
        return
    await show_menu(message)

@dp.message(Command('cancel'))
async def cancel_handler(message: Message) -> None:
    if is_authorized(message):
        reset_flow()
    await show_menu(message, '流程已取消/重置。请选择功能：')

@dp.message(Command('pair'))
async def pair_handler(message: Message) -> None:
    global authorized_user_id
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) != 2 or not secrets.compare_digest(parts[1].strip(), PAIR_CODE):
        await message.answer('配对码不正确。格式：/pair 您的配对码')
        return
    if authorized_user_id is not None and authorized_user_id != message.from_user.id:
        await message.answer('机器人已与其他用户配对')
        return
    authorized_user_id = message.from_user.id
    reset_flow()
    print(f'PAIRED user_id={authorized_user_id}', flush=True)
    await show_menu(message, '配对成功。请选择功能：')

@dp.callback_query()
async def callback_handler(callback: CallbackQuery) -> None:
    if not is_authorized(callback):
        await callback.answer('未授权', show_alert=True)
        return
    await callback.answer()
    data = callback.data or ''
    message = callback.message
    try:
        if data == 'flow:cancel':
            await finish_flow(message, '流程已取消')
            return
        if data.startswith('menu:'):
            reset_flow()
            feature = data.split(':', 1)[1]
            if feature == 'main':
                await show_menu(message, '请选择功能：')
                return
            prompts = {'convert': ('convert_zip', '请上传完整的 tdata ZIP。机器人会将其转换并发送 .session 文件。'), 'check': ('check_session', '请上传 Telethon .session。将检查其有效性及邮箱/两步验证状态。'), 'pattern': ('pattern_session', '请上传 Telethon .session。仅检查 Telegram 脱敏邮箱模式。'), 'email': ('email_session', '请上传已授权的 Telethon .session 以更改登录邮箱。'), 'phone': ('phone_session', '请上传已授权的 Telethon .session 以更改手机号码。'), 'twofa': ('twofa_session', '请上传已授权的 Telethon .session，以验证现有两步验证密码并设置新密码。'), 'revoke_sessions': ('revoke_session', '请上传要保留其当前会话的已授权 Telethon .session。机器人会通过 Telegram 验证，显示会话总数、标明您的当前会话、列出将注销的所有其他会话，并在终止前要求按钮确认。'), 'check_active_sessions': ('check_active_sessions', '请上传已授权的 Telethon .session。机器人会通过 Telegram 验证，并显示活跃会话总数、您的当前会话及所有其他已连接会话。此检查不会终止任何会话。'), 'bulk_email': ('bulk_emails', '批量更改绑定邮箱时，请上传包含邮箱和验证码链接的 .txt 文件。邮箱目标与 tdata ZIP 的数量和顺序必须一致。逐个转换及更改邮箱后，机器人会发送电话号码/绑定邮箱 PDF，并提供可选择、需确认且可跳过的两步验证密码更改阶段。')}
            if feature not in prompts:
                raise ValueError('未知功能')
            step, prompt = prompts[feature]
            current_flow['step'] = step
            await message.answer(prompt, reply_markup=cancel_menu())
            return
        if data == 'bulk:start':
            await start_bulk_job(message)
            return
        if data == 'bulk:twofa_yes':
            if current_flow.get('step') != 'bulk_twofa_choice':
                raise ValueError('批量两步验证选择已过期')
            count = len(current_flow.get('bulk_items', []))
            current_flow['step'] = 'bulk_twofa_passwords'
            await message.answer(f'请上传 .txt 或 .csv 密码列表，必须恰好包含 {count} 行，顺序与原始账号一致。每行格式为 old_password,new_password（也支持制表符分隔及 CSV 引号）。不可用账号对应行会跳过。列表解析后会删除，密码不会出现在日志或 PDF 报告中。', reply_markup=bulk_twofa_upload_menu())
            return
        if data == 'bulk:twofa_no':
            if current_flow.get('step') not in {'bulk_twofa_choice', 'bulk_twofa_passwords', 'bulk_twofa_confirm'}:
                raise ValueError('批量两步验证跳过选择已过期')
            await finish_flow(message, '已跳过两步验证密码更改。绑定邮箱报告已完成，临时会话和密码数据已清理。')
            return
        if data == 'bulk:twofa_confirm':
            await start_bulk_twofa_job(message)
            return
        if data == 'email:confirm':
            if current_flow.get('step') != 'email_confirm':
                raise ValueError('邮箱流程已过期，请重新开始')
            new_email = current_flow['new_email']
            current_flow['step'] = 'busy'
            client = client_for_session()
            try:
                await client.connect()
                sent = await client(functions.account.SendVerifyEmailCodeRequest(purpose=types.EmailVerifyPurposeLoginChange(), email=new_email))
            finally:
                await client.disconnect()
            current_flow['step'] = 'email_code'
            await message.answer(f'验证码已发送到新邮箱，长度为 {sent.length} 位。请输入收到的验证码。', reply_markup=cancel_menu())
            return
        if data == 'twofa:confirm':
            if current_flow.get('step') != 'twofa_confirm':
                raise ValueError('两步验证流程已过期，请重新开始')
            current_password = current_flow.get('twofa_current_password', '')
            new_password = current_flow.get('twofa_new_password', '')
            if not current_password or not new_password:
                raise ValueError('两步验证密码信息缺失，请重新开始流程')
            current_flow['step'] = 'busy'
            client = client_for_session()
            result_error = None
            try:
                await client.connect()
                await change_twofa_password(client, current_password, new_password)
            except errors.PasswordHashInvalidError:
                result_error = '现有两步验证密码不正确。未做任何更改。'
            except errors.FloodWaitError as exc:
                result_error = f'请求过多。请等待 {exc.seconds} 秒后重试。'
            except ValueError as exc:
                result_error = safe_error(exc)
            except Exception as exc:
                print(f'TWOFA_CHANGE_ERROR type={type(exc).__name__}', flush=True)
                result_error = '两步验证密码更改失败。请稍后重试；密码未被记录。'
            finally:
                current_password = ''
                new_password = ''
                current_flow['twofa_current_password'] = ''
                current_flow['twofa_new_password'] = ''
                try:
                    await client.disconnect()
                except Exception:
                    pass
            if result_error:
                await finish_flow(message, result_error)
            else:
                await finish_flow(message, '两步验证密码更改成功。原有提示和恢复设置均已保留。')
            return
        if data == 'revoke:confirm':
            if current_flow.get('step') != 'revoke_confirm':
                raise ValueError('会话终止流程已过期，请重新开始')
            expected_hash = current_flow.get('revoke_current_hash')
            if not isinstance(expected_hash, int):
                raise ValueError('当前会话标识缺失，请重新开始流程')
            current_flow['step'] = 'busy'
            client = client_for_session()
            result = None
            result_error = None
            try:
                await client.connect()
                result = await terminate_other_sessions(client, expected_hash)
            except ValueError as exc:
                result_error = safe_error(exc)
            except Exception as exc:
                print(f'SESSION_REVOKE_ERROR type={type(exc).__name__}', flush=True)
                result_error = '终止会话时发生错误。请重新打开功能并检查剩余会话。'
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            if result_error:
                await finish_flow(message, result_error)
            else:
                complete = result['remaining'] == 0
                lines = ['会话清理完成。' if complete else '会话清理仅部分完成。', '上传的当前会话已保留：是', f"原有其他会话数：{result['before']}", f"成功终止：{result['terminated']}", f"终止失败：{result['failed']}", f"剩余其他会话：{result['remaining']}"]
                if result['fresh_blocked']:
                    lines.append('Telegram 限制：当前会话登录时间不足 24 小时。')
                if result['rate_limited']:
                    lines.append('已达到 Telegram 频率限制，请稍后重试。')
                await finish_flow(message, '\n'.join(lines), reply_markup=post_revoke_menu() if complete else None)
            return
        if data.startswith('phone_profile:'):
            if current_flow.get('step') != 'phone_profile':
                raise ValueError('手机号流程已过期，请重新开始')
            profile = data.split(':', 1)[1]
            if profile not in {'desktop', 'android'}:
                raise ValueError('未知 API 配置')
            profile_label = {'desktop': '桌面端', 'android': '安卓'}[profile]
            current_flow['api_profile'] = profile
            current_flow['step'] = 'phone_number'
            await message.answer(f'已选择 {profile_label} API 配置。请发送带国家代码的新手机号码，例如 +919876543210。', reply_markup=cancel_menu())
            return
        if data == 'phone:confirm':
            if current_flow.get('step') != 'phone_confirm':
                raise ValueError('手机号流程已过期，请重新开始')
            profile = current_flow.get('api_profile', 'desktop')
            new_phone = current_flow['new_phone']
            current_flow['step'] = 'busy'
            immediate_success = False
            immediate_phone = None
            client = client_for_session(profile)
            try:
                await client.connect()
                response = await client(functions.account.SendChangePhoneCodeRequest(phone_number=new_phone, settings=types.CodeSettings()))
                if isinstance(response, types.auth.SentCodeSuccess):
                    updated_me = await client.get_me()
                    immediate_success = True
                    immediate_phone = updated_me.phone
            finally:
                await client.disconnect()
            if immediate_success:
                matches = immediate_phone == new_phone
                print(f'PHONE_CHANGE_IMMEDIATE_SUCCESS matches={matches}', flush=True)
                if not matches:
                    raise ValueError('Telegram 返回立即成功，但账号手机号码不匹配')
                await finish_flow(message, '手机号码更改成功')
                return
            sent = require_standard_sent_code(response)
            delivery, next_type, resend_delay = phone_code_details(sent)
            delivery_label = code_type_label(delivery)
            next_type_label = code_type_label(next_type) if next_type else None
            current_flow['phone_code_hash'] = sent.phone_code_hash
            current_flow['phone_next_type'] = next_type
            current_flow['phone_resend_at'] = time.monotonic() + resend_delay if resend_delay else None
            current_flow['phone_resend_count'] = 0
            current_flow['step'] = 'phone_code'
            print(f"PHONE_CODE_REQUEST_SENT api_profile={profile} delivery={delivery} code_length={getattr(sent.type, 'length', None)} next_type={next_type} timeout={resend_delay}", flush=True)
            fallback_text = f' 备用方式 {next_type_label} 可在 {resend_delay} 秒后重新发送。' if next_type else ' Telegram 未提供此验证挑战的备用方式。'
            await message.answer(f"验证码请求已接受。发送方式：{delivery_label}；长度：{getattr(sent.type, 'length', '未知')}.{fallback_text} 请输入收到的验证码。", reply_markup=phone_code_menu(bool(next_type)))
            return
        if data == 'phone:resend':
            if current_flow.get('step') != 'phone_code':
                raise ValueError('手机验证码流程已过期，请重新开始')
            next_type = current_flow.get('phone_next_type')
            if not next_type:
                await message.answer('Telegram 未提供此验证挑战的备用方式。')
                return
            if current_flow.get('phone_resend_count', 0) >= 1:
                await message.answer('安全的一次重发上限已使用。')
                return
            remaining = max(0, int(current_flow.get('phone_resend_at', 0) - time.monotonic() + 0.999))
            if remaining:
                await message.answer(f'请再等待 {remaining} 秒后重新发送。')
                return
            profile = current_flow.get('api_profile', 'desktop')
            new_phone = current_flow['new_phone']
            current_flow['step'] = 'busy'
            immediate_success = False
            immediate_phone = None
            client = client_for_session(profile)
            try:
                await client.connect()
                response = await client(functions.auth.ResendCodeRequest(phone_number=new_phone, phone_code_hash=current_flow['phone_code_hash']))
                if isinstance(response, types.auth.SentCodeSuccess):
                    updated_me = await client.get_me()
                    immediate_success = True
                    immediate_phone = updated_me.phone
            finally:
                await client.disconnect()
            if immediate_success:
                matches = immediate_phone == new_phone
                print(f'PHONE_CHANGE_RESEND_SUCCESS matches={matches}', flush=True)
                if not matches:
                    raise ValueError('重发后返回立即成功，但手机号码不匹配')
                await finish_flow(message, '手机号码更改成功')
                return
            sent = require_standard_sent_code(response)
            delivery, new_next_type, resend_delay = phone_code_details(sent)
            delivery_label = code_type_label(delivery)
            current_flow['phone_code_hash'] = sent.phone_code_hash
            current_flow['phone_next_type'] = new_next_type
            current_flow['phone_resend_at'] = time.monotonic() + resend_delay if resend_delay else None
            current_flow['phone_resend_count'] = 1
            current_flow['step'] = 'phone_code'
            print(f"PHONE_CODE_RESENT api_profile={profile} delivery={delivery} code_length={getattr(sent.type, 'length', None)} next_type={new_next_type} timeout={resend_delay}", flush=True)
            await message.answer(f"收到新的验证挑战。发送方式：{delivery_label}；长度：{getattr(sent.type, 'length', '未知')}。请仅使用最新验证码。", reply_markup=phone_code_menu(False))
            return
        await message.answer('按钮未知或已过期。请从菜单重新选择。')
    except Exception as exc:
        print(f'CALLBACK_FLOW_ERROR data={data} error={safe_error(exc)}', flush=True)
        await finish_flow(message, f'流程失败：{safe_error(exc)}')

@dp.message(F.document)
async def document_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer('请先使用 /start 和 /pair')
        return
    step = current_flow.get('step')
    document = message.document
    try:
        if step == 'convert_zip':
            current_flow['step'] = 'busy'
            temp_dir = make_temp_dir()
            current_flow['temp_dir'] = temp_dir
            zip_path = Path(temp_dir) / 'upload.zip'
            await download_document(message, zip_path, MAX_ZIP_SIZE)
            output = Path(temp_dir) / 'converted.session'
            await convert_uploaded_tdata(zip_path, output)
            await message.answer_document(FSInputFile(output, filename='converted.session'), caption='已转换并通过 Telegram 验证的 Telethon 会话。请妥善保管此文件。')
            await finish_flow(message, 'Tdata 转换完成')
        elif step in {'revoke_session', 'check_active_sessions'}:
            current_flow['step'] = 'busy'
            await store_session_upload(message)
            client = client_for_session()
            try:
                await client.connect()
                payload = await client(functions.account.GetAuthorizationsRequest())
            finally:
                await client.disconnect()
            current, others = split_current_authorizations(payload)
            if step == 'check_active_sessions':
                await finish_flow(message, format_active_session_report(current, others))
                return
            current_flow['revoke_current_hash'] = current.hash
            if not others:
                await finish_flow(message, format_active_session_report(current, others))
                return
            current_flow['step'] = 'revoke_confirm'
            await answer_long(message, format_revoke_preview(current, others), reply_markup=confirmation_menu('revoke:confirm'))
        elif step in {'check_session', 'pattern_session'}:
            current_flow['step'] = 'busy'
            status = await store_session_upload(message)
            if step == 'check_session':
                text = f"会话已通过 Telegram 验证。\n\n已验证登录/绑定邮箱已设置：{format_bool(status['verified_login_email_set'])}\n两步验证密码已启用：{format_bool(status['two_fa_password_set'])}\n恢复方式已配置：{format_bool(status['recovery_method_configured'])}\n待确认的恢复邮箱：{format_bool(status['unconfirmed_recovery_email_pending'])}"
            else:
                client = client_for_session()
                try:
                    await client.connect()
                    info = await client(functions.account.GetPasswordRequest())
                finally:
                    await client.disconnect()
                text = f"Telegram 脱敏邮箱模式：\n\n登录邮箱：{info.login_email_pattern or '未设置'}\n未确认恢复邮箱：{info.email_unconfirmed_pattern or '未设置'}"
            await finish_flow(message, text)
        elif step == 'email_session':
            current_flow['step'] = 'busy'
            await store_session_upload(message)
            current_flow['step'] = 'email_address'
            await message.answer('会话有效。现在请发送新的登录邮箱地址。', reply_markup=cancel_menu())
        elif step == 'phone_session':
            current_flow['step'] = 'busy'
            await store_session_upload(message)
            current_flow['step'] = 'phone_profile'
            await message.answer('会话有效。请选择验证码请求使用的 API 配置。', reply_markup=phone_profile_menu())
        elif step == 'twofa_session':
            current_flow['step'] = 'busy'
            status = await store_session_upload(message)
            if not status.get('two_fa_password_set'):
                await finish_flow(message, '此账号未启用两步验证。此流程仅用于更改现有两步验证密码。')
                return
            current_flow['step'] = 'twofa_current'
            await message.answer('请发送现有两步验证密码。收到后会删除该消息；密码不会显示或记录。', reply_markup=cancel_menu())
        elif step == 'bulk_twofa_passwords':
            if Path(document.file_name or '').suffix.lower() not in {'.txt', '.csv'}:
                raise BulkInputError('请以 .txt 或 .csv 文件上传旧/新密码列表')
            temp_dir = Path(current_flow['temp_dir'])
            destination = temp_dir / 'bulk_twofa_passwords.txt'
            await download_document(message, destination, MAX_LIST_BYTES)
            try:
                text = destination.read_text(encoding='utf-8-sig')
            finally:
                destination.unlink(missing_ok=True)
                await delete_code_message(message)
            items = current_flow.get('bulk_items', [])
            pairs = parse_password_pairs(text, len(items))
            text = ''
            current_flow['bulk_twofa_pairs'] = pairs
            eligible = sum((item.twofa_eligible and item.session_path.is_file() for item in items))
            current_flow['step'] = 'bulk_twofa_confirm'
            await message.answer(f'密码列表已接受：{len(pairs)} 行；符合条件的账号={eligible}。未显示或记录任何密码。请确认按顺序以随机 2–4 分钟间隔更改两步验证密码，或选择跳过。将生成不含密码的电话号码/状态 PDF。', reply_markup=bulk_twofa_confirm_menu())
        elif step in {'bulk_emails', 'bulk_tdata'}:
            await handle_bulk_document(message, step)
        elif step in {'bulk_running', 'bulk_twofa_running'}:
            await message.answer('批量流程正在运行。点击“取消”可停止。')
        else:
            await message.answer('请先从菜单选择功能，再上传所需文件。', reply_markup=main_menu())
    except Exception as exc:
        if step and step.startswith('bulk'):
            print(f'BULK_INPUT_ERROR step={step} error={type(exc).__name__}', flush=True)
            detail = str(exc) if isinstance(exc, BulkInputError) else '批量上传/读取失败'
            if step == 'bulk_tdata':
                markup = bulk_start_menu()
            elif step == 'bulk_twofa_passwords':
                markup = bulk_twofa_upload_menu()
            else:
                markup = cancel_menu()
            await message.answer(f'{detail}。请重新发送更正后的文件，或点击“取消”。', reply_markup=markup)
            return
        print(f'DOCUMENT_FLOW_ERROR step={step} error={safe_error(exc)}', flush=True)
        await finish_flow(message, f'流程失败：{safe_error(exc)}')

@dp.message()
async def text_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer('请先使用 /start 和 /pair')
        return
    step = current_flow.get('step')
    raw_text = message.text or ''
    text = raw_text.strip()
    try:
        if step == 'twofa_current':
            await delete_code_message(message)
            if not raw_text:
                raise ValueError('现有两步验证密码不能为空')
            current_flow['twofa_current_password'] = raw_text
            current_flow['step'] = 'twofa_new'
            await message.answer('请发送新的两步验证密码。收到后会删除该消息；密码不会显示或记录。', reply_markup=cancel_menu())
        elif step == 'twofa_new':
            await delete_code_message(message)
            if not raw_text:
                raise ValueError('新的两步验证密码不能为空')
            if raw_text == current_flow.get('twofa_current_password'):
                raise ValueError('新密码必须与现有两步验证密码不同')
            current_flow['twofa_new_password'] = raw_text
            current_flow['step'] = 'twofa_confirm'
            await message.answer('两个密码均已收到且不会显示。确认设置新的两步验证密码吗？原有提示和恢复设置将被保留。', reply_markup=confirmation_menu('twofa:confirm'))
        elif step == 'email_address':
            local, separator, domain = text.partition('@')
            if not separator or not local or (not domain) or ('@' in domain):
                raise ValueError('请发送有效的新邮箱地址')
            current_flow['new_email'] = text
            current_flow['step'] = 'email_confirm'
            await message.answer('Telegram 将向此新邮箱发送验证码。请现在确认。', reply_markup=confirmation_menu('email:confirm'))
        elif step == 'email_code':
            if not text.isdigit():
                raise ValueError('验证码只能包含数字')
            await delete_code_message(message)
            current_flow['step'] = 'busy'
            client = client_for_session()
            try:
                await client.connect()
                await client(functions.account.VerifyEmailRequest(purpose=types.EmailVerifyPurposeLoginChange(), verification=types.EmailVerificationCode(code=text)))
                info = await client(functions.account.GetPasswordRequest())
            finally:
                await client.disconnect()
            await finish_flow(message, f"登录邮箱更新成功。\n更新后的脱敏模式：{info.login_email_pattern or '未设置'}")
        elif step == 'phone_number':
            current_flow['new_phone'] = normalize_phone_number(text)
            current_flow['step'] = 'phone_confirm'
            await message.answer('将为此新号码请求验证码。请现在确认。', reply_markup=confirmation_menu('phone:confirm'))
        elif step == 'phone_code':
            if not text.isdigit():
                raise ValueError('验证码只能包含数字')
            await delete_code_message(message)
            current_flow['step'] = 'busy'
            profile = current_flow.get('api_profile', 'desktop')
            client = client_for_session(profile)
            try:
                await client.connect()
                await client(functions.account.ChangePhoneRequest(phone_number=current_flow['new_phone'], phone_code_hash=current_flow['phone_code_hash'], phone_code=text))
                updated_me = await client.get_me()
            finally:
                await client.disconnect()
            expected = current_flow['new_phone']
            await finish_flow(message, f'手机号码更改请求完成。\n更新后号码与请求一致：{format_bool(updated_me.phone == expected)}')
        else:
            await show_menu(message, '请选择功能，或使用 /cancel：')
    except Exception as exc:
        print(f'TEXT_FLOW_ERROR step={step} error={safe_error(exc)}', flush=True)
        await finish_flow(message, f'流程失败：{safe_error(exc)}')

async def start_health_server() -> web.AppRunner | None:
    """Bind Render's web-service port without starting a second process."""
    port_text = os.environ.get('PORT', '').strip()
    if not port_text:
        print('HEALTH_SERVER_DISABLED PORT_not_set', flush=True)
        return None
    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError('PORT must be an integer') from exc
    if not 1 <= port <= 65535:
        raise RuntimeError('PORT must be between 1 and 65535')

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text='OK\n', content_type='text/plain')
    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=port)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    print(f'HEALTH_SERVER_START host=0.0.0.0 port={port} path=/health', flush=True)
    return runner

async def self_ping_loop(url: str, interval: float) -> None:
    """Periodically request the configured public health URL."""
    timeout = ClientTimeout(total=25)
    async with ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.get(url) as response:
                    print(f'SELF_PING_STATUS status={response.status}', flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'SELF_PING_ERROR type={type(exc).__name__}', flush=True)

def start_self_ping() -> asyncio.Task[None] | None:
    """Start optional keep-alive pings inside the existing bot process."""
    url = os.environ.get('SELF_PING_URL', '').strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None or (parts.password is not None) or parts.query or parts.fragment:
        raise RuntimeError('SELF_PING_URL must be a plain HTTP(S) public health URL without credentials, query, or fragment')
    interval_text = os.environ.get('SELF_PING_INTERVAL', '120').strip()
    try:
        interval = int(interval_text)
    except ValueError as exc:
        raise RuntimeError('SELF_PING_INTERVAL must be an integer') from exc
    if not 60 <= interval <= 900:
        raise RuntimeError('SELF_PING_INTERVAL must be between 60 and 900 seconds')
    print(f'SELF_PING_START interval={interval}', flush=True)
    return asyncio.create_task(self_ping_loop(url, float(interval)))

async def main() -> None:
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=45))
    health_runner: web.AppRunner | None = None
    self_ping_task: asyncio.Task[None] | None = None
    try:
        health_runner = await start_health_server()
        self_ping_task = start_self_ping()
        me = await bot.get_me()
        print(f'FULL_BOT_READY username=@{me.username} user_id={me.id}', flush=True)
        await bot.delete_webhook(drop_pending_updates=False)
        print('FULL_BOT_POLLING_START', flush=True)
        await dp.start_polling(bot, polling_timeout=20, allowed_updates=dp.resolve_used_update_types(), close_bot_session=True)
    finally:
        reset_flow()
        if self_ping_task is not None:
            self_ping_task.cancel()
            await asyncio.gather(self_ping_task, return_exceptions=True)
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
