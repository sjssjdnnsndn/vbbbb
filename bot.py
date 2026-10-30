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
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon.tl import functions, types
from aiohttp import ClientSession, ClientTimeout, web

from bulk_email import MAX_LIST_BYTES, BulkInputError, parse_email_links
from bulk_job import BulkItem, run_bulk_job
from convert_tdata import (
    check_session,
    normalize_phone_number,
    phone_code_details,
    require_standard_sent_code,
)


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PAIR_CODE = os.environ.get("PAIR_CODE", "").strip()
WORK_ROOT = Path(
    os.environ.get(
        "WORK_ROOT",
        str(Path(tempfile.gettempdir()) / "telegram_full_bot"),
    )
)
SOURCE_BUNDLE_PATH = Path(
    os.environ.get(
        "SOURCE_BUNDLE_PATH",
        str(Path(__file__).with_name("telegram_session_tool_full_source.zip")),
    )
)
SOURCE_ONCE_MARKER_PATH = Path(
    os.environ.get(
        "SOURCE_ONCE_MARKER_PATH",
        str(Path(__file__).with_name(".source_bundle_delivery_done")),
    )
)
MAX_ZIP_SIZE = int(os.environ.get("MAX_ZIP_SIZE", str(20 * 1024 * 1024)))
MAX_SESSION_SIZE = int(os.environ.get("MAX_SESSION_SIZE", str(5 * 1024 * 1024)))
MAX_UNCOMPRESSED_SIZE = int(
    os.environ.get("MAX_UNCOMPRESSED_SIZE", str(512 * 1024 * 1024))
)
MAX_BULK_ACCOUNTS = int(os.environ.get("MAX_BULK_ACCOUNTS", "100"))

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required")
if not PAIR_CODE:
    raise SystemExit("PAIR_CODE environment variable is required")

logging.basicConfig(level=logging.WARNING)
dp = Dispatcher()
authorized_user_id: int | None = None
current_flow: dict = {}
source_delivery_lock = asyncio.Lock()


def safe_error(exc: BaseException) -> str:
    text = str(exc).replace(BOT_TOKEN, "[redacted]")
    return text[:600] if text else type(exc).__name__


def zh_bool(value: bool | None) -> str:
    if value is None:
        return "未知"
    return "是" if value else "否"


def code_type_label(type_name: str | None) -> str:
    labels = {
        "SentCodeTypeApp": "Telegram 应用",
        "SentCodeTypeSms": "短信",
        "SentCodeTypeCall": "语音电话",
        "SentCodeTypeFlashCall": "闪呼",
        "SentCodeTypeMissedCall": "未接来电",
        "SentCodeTypeFirebaseSms": "Firebase 短信",
        "SentCodeTypeFragmentSms": "Fragment 短信",
        "SentCodeTypeEmailCode": "邮箱验证码",
    }
    if not type_name:
        return "未知"
    return labels.get(type_name, type_name)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Tdata → Telethon", callback_data="menu:convert"),
                InlineKeyboardButton(text="检查会话", callback_data="menu:check"),
            ],
            [
                InlineKeyboardButton(text="掩码邮箱信息", callback_data="menu:pattern"),
                InlineKeyboardButton(text="更改登录邮箱", callback_data="menu:email"),
            ],
            [
                InlineKeyboardButton(text="更改手机号码", callback_data="menu:phone"),
                InlineKeyboardButton(text="重置 / 取消", callback_data="flow:cancel"),
            ],
            [
                InlineKeyboardButton(
                    text="批量更改绑定邮箱", callback_data="menu:bulk_email"
                )
            ],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="取消", callback_data="flow:cancel")]
        ]
    )


def confirmation_menu(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="确认", callback_data=action),
                InlineKeyboardButton(text="取消", callback_data="flow:cancel"),
            ]
        ]
    )


def phone_profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="桌面 API", callback_data="phone_profile:desktop"),
                InlineKeyboardButton(text="Android API", callback_data="phone_profile:android"),
            ],
            [InlineKeyboardButton(text="取消", callback_data="flow:cancel")],
        ]
    )


def phone_code_menu(can_resend: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_resend:
        rows.append(
            [InlineKeyboardButton(text="重新发送 / 备用方式", callback_data="phone:resend")]
        )
    rows.append([InlineKeyboardButton(text="取消", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bulk_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="开始处理", callback_data="bulk:start")],
            [InlineKeyboardButton(text="取消", callback_data="flow:cancel")],
        ]
    )


def reset_flow() -> None:
    bulk_task = current_flow.get("bulk_task")
    if (
        bulk_task is not None
        and bulk_task is not asyncio.current_task()
        and not bulk_task.done()
    ):
        bulk_task.cancel()
    temp_dir = current_flow.get("temp_dir")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    if "phone_code_hash" in current_flow:
        current_flow["phone_code_hash"] = ""
    current_flow.clear()


def make_temp_dir() -> str:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="flow_", dir=WORK_ROOT)


def is_authorized(message_or_callback: Message | CallbackQuery) -> bool:
    return authorized_user_id is not None and (
        message_or_callback.from_user.id == authorized_user_id
    )


async def show_menu(message: Message, text: str = "请选择功能：") -> None:
    await message.answer(text, reply_markup=main_menu())


async def finish_flow(message: Message, text: str) -> None:
    reset_flow()
    await show_menu(message, text + "\n\n请选择下一个功能：")


def validate_session_header(path: Path) -> None:
    with path.open("rb") as file:
        if file.read(16) != b"SQLite format 3\x00":
            raise ValueError("这不是有效的 Telethon SQLite 会话文件")


async def download_document(message: Message, destination: Path, max_size: int) -> None:
    document = message.document
    if document.file_size and document.file_size > max_size:
        raise ValueError(f"文件大小上限为 {max_size} 字节")
    telegram_file = await message.bot.get_file(document.file_id)
    await message.bot.download_file(telegram_file.file_path, destination=destination)
    if destination.stat().st_size > max_size:
        raise ValueError(f"下载的文件超过大小上限 {max_size} 字节")


async def store_session_upload(message: Message) -> dict:
    document = message.document
    filename = document.file_name or "account.session"
    if Path(filename).suffix.lower() != ".session":
        raise ValueError("此步骤请上传 Telethon .session 文档")

    temp_dir = make_temp_dir()
    current_flow["temp_dir"] = temp_dir
    session_path = Path(temp_dir) / "account.session"
    await download_document(message, session_path, MAX_SESSION_SIZE)
    validate_session_header(session_path)

    status = await check_session(session_path)
    if not status["session_authorized"]:
        raise ValueError("会话未在 Telegram 上获得授权或已被撤销")

    current_flow["session_path"] = str(session_path)
    current_flow["account_user_id"] = status["user_id"]
    return status


def client_for_session(profile: str = "desktop") -> TelegramClient:
    session_path = current_flow.get("session_path")
    if not session_path:
        raise ValueError("会话缺失；请重新开始流程")
    api = API.TelegramAndroid if profile == "android" else API.TelegramDesktop
    return TelegramClient(session_path, api=api, receive_updates=False)


def extract_tdata(zip_path: Path, output_root: Path) -> Path:
    extracted = output_root / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    count = 0

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if not members:
            raise ValueError("ZIP 为空")
        if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_SIZE:
            raise ValueError("ZIP 解压后的大小超过允许限制")
        if any(member.flag_bits & 0x1 for member in members):
            raise ValueError("不支持受密码保护的 ZIP")

        for info in members:
            path = PurePosixPath(info.filename)
            if info.is_dir() or "tdata" not in path.parts:
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("ZIP 包含不安全的路径")
            index = path.parts.index("tdata")
            target = extracted / Path(*path.parts[index:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            count += 1

    if count == 0:
        raise ValueError("ZIP 中未找到 tdata 文件夹")
    return extracted / "tdata"


async def convert_uploaded_tdata(zip_path: Path, output: Path) -> None:
    tdata_dir = extract_tdata(zip_path, zip_path.parent)
    tdesk = TDesktop(str(tdata_dir))
    if not tdesk.isLoaded():
        raise ValueError(
            "无法加载已授权的 tdata。对于受本地密码保护的 tdata，请使用本地脚本"
        )

    client = await tdesk.ToTelethon(
        session=str(output),
        flag=UseCurrentSession,
        api=API.TelegramDesktop,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("转换后的会话未在 Telegram 上获得授权")
    finally:
        await client.disconnect()


def get_bulk_temp_dir() -> str:
    if not current_flow.get("temp_dir"):
        current_flow["temp_dir"] = make_temp_dir()
    return current_flow["temp_dir"]


async def handle_bulk_document(message: Message, step: str) -> None:
    document = message.document
    if step == "bulk_emails":
        if Path(document.file_name or "").suffix.lower() != ".txt":
            raise BulkInputError("此步骤请上传 .txt 文档")
        temp_dir = Path(get_bulk_temp_dir())
        destination = temp_dir / "bulk_emails.txt"
        await download_document(message, destination, MAX_LIST_BYTES)
        try:
            text = destination.read_text(encoding="utf-8-sig")
        finally:
            destination.unlink(missing_ok=True)

        targets = parse_email_links(text)
        if len(targets) > MAX_BULK_ACCOUNTS:
            raise BulkInputError(f"最多允许 {MAX_BULK_ACCOUNTS} 个账号")
        current_flow["bulk_targets"] = targets
        current_flow["bulk_items"] = []
        current_flow["bulk_upload_lock"] = asyncio.Lock()
        current_flow["step"] = "bulk_tdata"
        await message.answer(
            f"已收到 {len(targets)} 个邮箱目标。现在请按相同顺序发送 tdata ZIP。"
            "每个 ZIP 都会计数。点击“开始处理”即表示你确认所有"
            "账号/会话/邮箱/收件箱均为本人所有或已获授权。",
            reply_markup=bulk_start_menu(),
        )
        return

    if step != "bulk_tdata":
        raise BulkInputError("批量步骤已过期")
    if Path(document.file_name or "").suffix.lower() != ".zip":
        raise BulkInputError("此步骤请上传 tdata .zip 文档")

    lock = current_flow["bulk_upload_lock"]
    async with lock:
        items = current_flow["bulk_items"]
        targets = current_flow["bulk_targets"]
        if len(items) >= len(targets):
            raise BulkInputError("所需 ZIP 数量已齐全；请使用“开始处理”")
        index = len(items) + 1
        item_dir = Path(get_bulk_temp_dir()) / f"item_{index:04d}"
        item_dir.mkdir(parents=True, exist_ok=False)
        zip_path = item_dir / "upload.zip"
        try:
            await download_document(message, zip_path, MAX_ZIP_SIZE)
            if not zipfile.is_zipfile(zip_path):
                raise BulkInputError("不是有效的 tdata ZIP")
        except Exception:
            shutil.rmtree(item_dir, ignore_errors=True)
            raise

        item = BulkItem(
            index=index,
            target=targets[index - 1],
            zip_path=zip_path,
            session_path=item_dir / "account.session",
        )
        items.append(item)
        await message.answer(
            f"已收到 tdata ZIP：{len(items)}/{len(targets)}。"
            "可以继续发送 ZIP，或全部接收后点击“开始处理”。",
            reply_markup=bulk_start_menu(),
        )


async def run_bulk_flow(job_id: str, message: Message, items: list[BulkItem], temp_dir: str) -> None:
    async def notify(text: str) -> None:
        final = text.startswith(("批量处理完成", "没有账号", "批量处理因意外错误"))
        try:
            await message.answer(text, reply_markup=main_menu() if final else cancel_menu())
        except Exception as exc:
            print(f"BULK_NOTIFY_ERROR error={type(exc).__name__}", flush=True)

    try:
        summary = await run_bulk_job(
            items=items,
            convert_func=convert_uploaded_tdata,
            check_func=check_session,
            client_factory=lambda path: TelegramClient(
                path, api=API.TelegramDesktop, receive_updates=False
            ),
            notify=notify,
        )
        print(
            f"BULK_JOB_COMPLETE total={summary['total']} "
            f"email_success={summary['success']} email_failed={summary['failed']}",
            flush=True,
        )
    except asyncio.CancelledError:
        print("BULK_JOB_CANCELLED", flush=True)
        raise
    except Exception as exc:
        print(f"BULK_JOB_ERROR error={type(exc).__name__}", flush=True)
        await notify("批量处理因意外错误停止。正在清理临时文件。")
    finally:
        if current_flow.get("bulk_job_id") == job_id:
            current_flow.clear()
        shutil.rmtree(temp_dir, ignore_errors=True)


async def start_bulk_job(message: Message) -> None:
    if current_flow.get("step") != "bulk_tdata":
        raise ValueError("批量上传流程已过期")
    items = current_flow.get("bulk_items", [])
    targets = current_flow.get("bulk_targets", [])
    if not items:
        await message.answer("请先至少上传一个 tdata ZIP。")
        return
    if len(items) != len(targets):
        await message.answer(
            f"数量不完整：邮箱目标={len(targets)}，ZIP={len(items)}。"
            "请上传至所需数量，或取消后重新发送更正后的列表。",
            reply_markup=bulk_start_menu(),
        )
        return

    job_id = secrets.token_hex(8)
    temp_dir = current_flow["temp_dir"]
    current_flow["step"] = "bulk_running"
    current_flow["bulk_job_id"] = job_id
    await message.answer(
        "已确认开始：你已确认所有账号、会话、邮箱和收件箱"
        "均为本人所有或已获授权。批量处理将在后台运行；"
        "可使用“取消”停止。",
        reply_markup=cancel_menu(),
    )
    current_flow["bulk_task"] = asyncio.create_task(
        run_bulk_flow(job_id, message, items, temp_dir)
    )


async def delete_code_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


@dp.error()
async def error_handler(event: ErrorEvent) -> None:
    print(f"HANDLER_ERROR error={safe_error(event.exception)}", flush=True)


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = message.from_user.id
    print(f"START_RECEIVED user_id={user_id}", flush=True)

    await message.answer(
        "欢迎使用 Telegram 会话工具机器人！\n"
        "开发者：@BioBhaiya\n\n"
        "请完成配对，以使用 Tdata 转换、会话检查、手机/邮箱流程和"
        "批量绑定邮箱工具。"
    )

    async with source_delivery_lock:
        if not SOURCE_ONCE_MARKER_PATH.exists():
            if SOURCE_BUNDLE_PATH.is_file():
                try:
                    await message.answer_document(
                        FSInputFile(
                            SOURCE_BUNDLE_PATH,
                            filename="telegram_session_tool_full_source.zip",
                        ),
                        caption="最终完整源码：转换器、检查器、账号更改流程和内联菜单机器人。",
                    )
                    SOURCE_ONCE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
                    SOURCE_ONCE_MARKER_PATH.touch(exist_ok=True)
                    print(f"SOURCE_BUNDLE_SENT_ONCE user_id={user_id}", flush=True)
                except Exception as exc:
                    print(f"SOURCE_BUNDLE_ERROR error={safe_error(exc)}", flush=True)
            else:
                print("SOURCE_BUNDLE_ERROR file_not_found", flush=True)

    if is_authorized(message):
        await show_menu(message, "机器人已就绪。请选择功能：")
    else:
        await message.answer(
            "这是完整的 Telegram 会话工具机器人。请先配对：\n"
            "/pair 你的配对码"
        )


@dp.message(Command("menu"))
async def menu_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("请先使用 /start 和 /pair")
        return
    await show_menu(message)


@dp.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    if is_authorized(message):
        reset_flow()
    await show_menu(message, "流程已取消/重置。请选择功能：")


@dp.message(Command("pair"))
async def pair_handler(message: Message) -> None:
    global authorized_user_id

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not secrets.compare_digest(parts[1].strip(), PAIR_CODE):
        await message.answer("配对码错误。格式：/pair 你的配对码")
        return

    if authorized_user_id is not None and authorized_user_id != message.from_user.id:
        await message.answer("机器人已与其他用户配对")
        return

    authorized_user_id = message.from_user.id
    reset_flow()
    print(f"PAIRED user_id={authorized_user_id}", flush=True)
    await show_menu(message, "配对成功。请选择功能：")


@dp.callback_query()
async def callback_handler(callback: CallbackQuery) -> None:
    if not is_authorized(callback):
        await callback.answer("未授权", show_alert=True)
        return

    await callback.answer()
    data = callback.data or ""
    message = callback.message

    try:
        if data == "flow:cancel":
            await finish_flow(message, "流程已取消")
            return

        if data.startswith("menu:"):
            reset_flow()
            feature = data.split(":", 1)[1]
            prompts = {
                "convert": (
                    "convert_zip",
                    "请上传完整的 tdata ZIP。机器人将转换并发送 .session 文档。",
                ),
                "check": (
                    "check_session",
                    "请上传 Telethon .session。将检查有效性以及邮箱/2FA 状态。",
                ),
                "pattern": (
                    "pattern_session",
                    "请上传 Telethon .session。仅检查 Telegram 的掩码邮箱模式。",
                ),
                "email": (
                    "email_session",
                    "请上传已授权的 Telethon .session，以更改登录邮箱。",
                ),
                "phone": (
                    "phone_session",
                    "请上传已授权的 Telethon .session，以更改手机号码。",
                ),
                "bulk_email": (
                    "bulk_emails",
                    "要批量更改绑定邮箱，请上传包含邮箱+OTP 链接的 .txt 文件。"
                    "邮箱目标与 tdata ZIP 的顺序和数量必须一致。",
                ),
            }
            if feature not in prompts:
                raise ValueError("未知功能")
            step, prompt = prompts[feature]
            current_flow["step"] = step
            await message.answer(prompt, reply_markup=cancel_menu())
            return

        if data == "bulk:start":
            await start_bulk_job(message)
            return

        if data == "email:confirm":
            if current_flow.get("step") != "email_confirm":
                raise ValueError("邮箱流程已过期；请重新开始")
            new_email = current_flow["new_email"]
            current_flow["step"] = "busy"
            client = client_for_session()
            try:
                await client.connect()
                sent = await client(
                    functions.account.SendVerifyEmailCodeRequest(
                        purpose=types.EmailVerifyPurposeLoginChange(),
                        email=new_email,
                    )
                )
            finally:
                await client.disconnect()

            current_flow["step"] = "email_code"
            await message.answer(
                f"已向新邮箱发送 {sent.length} 位验证码。"
                "请发送收到的验证码。",
                reply_markup=cancel_menu(),
            )
            return

        if data.startswith("phone_profile:"):
            if current_flow.get("step") != "phone_profile":
                raise ValueError("手机流程已过期；请重新开始")
            profile = data.split(":", 1)[1]
            if profile not in {"desktop", "android"}:
                raise ValueError("未知 API 配置")
            profile_label = {"desktop": "桌面", "android": "Android"}[profile]
            current_flow["api_profile"] = profile
            current_flow["step"] = "phone_number"
            await message.answer(
                f"已选择 {profile_label} API 配置。请发送带国家代码的新手机号码，例如 +919876543210。",
                reply_markup=cancel_menu(),
            )
            return

        if data == "phone:confirm":
            if current_flow.get("step") != "phone_confirm":
                raise ValueError("手机流程已过期；请重新开始")
            profile = current_flow.get("api_profile", "desktop")
            new_phone = current_flow["new_phone"]
            current_flow["step"] = "busy"
            immediate_success = False
            immediate_phone = None
            client = client_for_session(profile)
            try:
                await client.connect()
                response = await client(
                    functions.account.SendChangePhoneCodeRequest(
                        phone_number=new_phone,
                        settings=types.CodeSettings(),
                    )
                )
                if isinstance(response, types.auth.SentCodeSuccess):
                    updated_me = await client.get_me()
                    immediate_success = True
                    immediate_phone = updated_me.phone
            finally:
                await client.disconnect()

            if immediate_success:
                matches = immediate_phone == new_phone
                print(f"PHONE_CHANGE_IMMEDIATE_SUCCESS matches={matches}", flush=True)
                if not matches:
                    raise ValueError(
                        "Telegram 返回立即成功，但账号手机号码不匹配"
                    )
                await finish_flow(message, "手机号码已成功更改")
                return

            sent = require_standard_sent_code(response)
            delivery, next_type, resend_delay = phone_code_details(sent)
            delivery_label = code_type_label(delivery)
            next_type_label = code_type_label(next_type) if next_type else None
            current_flow["phone_code_hash"] = sent.phone_code_hash
            current_flow["phone_next_type"] = next_type
            current_flow["phone_resend_at"] = (
                time.monotonic() + resend_delay if resend_delay else None
            )
            current_flow["phone_resend_count"] = 0
            current_flow["step"] = "phone_code"
            print(
                f"PHONE_CODE_REQUEST_SENT api_profile={profile} delivery={delivery} "
                f"code_length={getattr(sent.type, 'length', None)} "
                f"next_type={next_type} timeout={resend_delay}",
                flush=True,
            )
            fallback_text = (
                f" 备用方式 {next_type_label} 可在 {resend_delay} 秒后重新发送。"
                if next_type
                else " Telegram 未为此验证挑战提供备用方式。"
            )
            await message.answer(
                f"验证码请求已接受。发送方式：{delivery_label}；"
                f"长度：{getattr(sent.type, 'length', '未知')}。"
                f"{fallback_text} 请发送收到的验证码。",
                reply_markup=phone_code_menu(bool(next_type)),
            )
            return

        if data == "phone:resend":
            if current_flow.get("step") != "phone_code":
                raise ValueError("手机验证码流程已过期；请重新开始")
            next_type = current_flow.get("phone_next_type")
            if not next_type:
                await message.answer("Telegram 未为此验证挑战提供备用方式。")
                return
            if current_flow.get("phone_resend_count", 0) >= 1:
                await message.answer("安全的一次重发机会已使用。")
                return
            remaining = max(
                0,
                int(current_flow.get("phone_resend_at", 0) - time.monotonic() + 0.999),
            )
            if remaining:
                await message.answer(f"请再等待 {remaining} 秒后重发。")
                return

            profile = current_flow.get("api_profile", "desktop")
            new_phone = current_flow["new_phone"]
            current_flow["step"] = "busy"
            immediate_success = False
            immediate_phone = None
            client = client_for_session(profile)
            try:
                await client.connect()
                response = await client(
                    functions.auth.ResendCodeRequest(
                        phone_number=new_phone,
                        phone_code_hash=current_flow["phone_code_hash"],
                    )
                )
                if isinstance(response, types.auth.SentCodeSuccess):
                    updated_me = await client.get_me()
                    immediate_success = True
                    immediate_phone = updated_me.phone
            finally:
                await client.disconnect()

            if immediate_success:
                matches = immediate_phone == new_phone
                print(f"PHONE_CHANGE_RESEND_SUCCESS matches={matches}", flush=True)
                if not matches:
                    raise ValueError(
                        "重发后返回立即成功，但手机号码不匹配"
                    )
                await finish_flow(message, "手机号码已成功更改")
                return

            sent = require_standard_sent_code(response)
            delivery, new_next_type, resend_delay = phone_code_details(sent)
            delivery_label = code_type_label(delivery)
            current_flow["phone_code_hash"] = sent.phone_code_hash
            current_flow["phone_next_type"] = new_next_type
            current_flow["phone_resend_at"] = (
                time.monotonic() + resend_delay if resend_delay else None
            )
            current_flow["phone_resend_count"] = 1
            current_flow["step"] = "phone_code"
            print(
                f"PHONE_CODE_RESENT api_profile={profile} delivery={delivery} "
                f"code_length={getattr(sent.type, 'length', None)} "
                f"next_type={new_next_type} timeout={resend_delay}",
                flush=True,
            )
            await message.answer(
                f"已获取新的验证挑战。发送方式：{delivery_label}；"
                f"长度：{getattr(sent.type, 'length', '未知')}。 "
                "请仅使用最新验证码。",
                reply_markup=phone_code_menu(False),
            )
            return

        await message.answer("按钮未知或已过期。请从菜单重新选择。")

    except Exception as exc:
        print(f"CALLBACK_FLOW_ERROR data={data} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"流程失败：{safe_error(exc)}")


@dp.message(F.document)
async def document_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("请先使用 /start 和 /pair")
        return

    step = current_flow.get("step")
    try:
        if step == "convert_zip":
            current_flow["step"] = "busy"
            temp_dir = make_temp_dir()
            current_flow["temp_dir"] = temp_dir
            zip_path = Path(temp_dir) / "upload.zip"
            await download_document(message, zip_path, MAX_ZIP_SIZE)
            output = Path(temp_dir) / "converted.session"
            await convert_uploaded_tdata(zip_path, output)
            await message.answer_document(
                FSInputFile(output, filename="converted.session"),
                caption="已转换并通过 Telegram 验证的 Telethon 会话。请妥善保管此文件，勿泄露。",
            )
            await finish_flow(message, "Tdata 转换完成")

        elif step in {"check_session", "pattern_session"}:
            current_flow["step"] = "busy"
            status = await store_session_upload(message)

            if step == "check_session":
                text = (
                    "会话在 Telegram 上有效。\n\n"
                    f"已设置验证登录/绑定邮箱：{zh_bool(status['verified_login_email_set'])}\n"
                    f"已启用 2FA 密码：{zh_bool(status['two_fa_password_set'])}\n"
                    f"已配置恢复方式：{zh_bool(status['recovery_method_configured'])}\n"
                    "有待确认的恢复邮箱："
                    f"{zh_bool(status['unconfirmed_recovery_email_pending'])}"
                )
            else:
                client = client_for_session()
                try:
                    await client.connect()
                    info = await client(functions.account.GetPasswordRequest())
                finally:
                    await client.disconnect()
                text = (
                    "Telegram 掩码邮箱模式：\n\n"
                    f"登录邮箱：{info.login_email_pattern or '未设置'}\n"
                    "未确认的恢复邮箱："
                    f"{info.email_unconfirmed_pattern or '未设置'}"
                )
            await finish_flow(message, text)

        elif step == "email_session":
            current_flow["step"] = "busy"
            await store_session_upload(message)
            current_flow["step"] = "email_address"
            await message.answer(
                "会话有效。现在请发送新的登录邮箱地址。",
                reply_markup=cancel_menu(),
            )

        elif step == "phone_session":
            current_flow["step"] = "busy"
            await store_session_upload(message)
            current_flow["step"] = "phone_profile"
            await message.answer(
                "会话有效。请选择用于 OTP 请求的 API 配置。",
                reply_markup=phone_profile_menu(),
            )

        elif step in {"bulk_emails", "bulk_tdata"}:
            await handle_bulk_document(message, step)

        elif step == "bulk_running":
            await message.answer("批量处理正在运行。使用“取消”停止。")

        else:
            await message.answer(
                "请先从菜单选择功能，再上传所需文件。",
                reply_markup=main_menu(),
            )

    except Exception as exc:
        if step and step.startswith("bulk"):
            print(
                f"BULK_INPUT_ERROR step={step} error={type(exc).__name__}",
                flush=True,
            )
            detail = str(exc) if isinstance(exc, BulkInputError) else "批量上传/读取失败"
            await message.answer(
                f"{detail}. 请重新发送更正后的文件，或使用“取消”。",
                reply_markup=bulk_start_menu() if step == "bulk_tdata" else cancel_menu(),
            )
            return
        print(f"DOCUMENT_FLOW_ERROR step={step} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"流程失败：{safe_error(exc)}")


@dp.message()
async def text_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("请先使用 /start 和 /pair")
        return

    step = current_flow.get("step")
    text = (message.text or "").strip()

    try:
        if step == "email_address":
            local, separator, domain = text.partition("@")
            if not separator or not local or not domain or "@" in domain:
                raise ValueError("请发送有效的新邮箱地址")
            current_flow["new_email"] = text
            current_flow["step"] = "email_confirm"
            await message.answer(
                "Telegram 将向此新邮箱发送验证码。请确认。",
                reply_markup=confirmation_menu("email:confirm"),
            )

        elif step == "email_code":
            if not text.isdigit():
                raise ValueError("验证码只能包含数字")
            await delete_code_message(message)
            current_flow["step"] = "busy"
            client = client_for_session()
            try:
                await client.connect()
                await client(
                    functions.account.VerifyEmailRequest(
                        purpose=types.EmailVerifyPurposeLoginChange(),
                        verification=types.EmailVerificationCode(code=text),
                    )
                )
                info = await client(functions.account.GetPasswordRequest())
            finally:
                await client.disconnect()
            await finish_flow(
                message,
                "登录邮箱已成功更新。\n"
                f"更新后的掩码模式：{info.login_email_pattern or '未设置'}",
            )

        elif step == "phone_number":
            current_flow["new_phone"] = normalize_phone_number(text)
            current_flow["step"] = "phone_confirm"
            await message.answer(
                "将向此新号码请求发送验证码。请确认。",
                reply_markup=confirmation_menu("phone:confirm"),
            )

        elif step == "phone_code":
            if not text.isdigit():
                raise ValueError("验证码只能包含数字")
            await delete_code_message(message)
            current_flow["step"] = "busy"
            profile = current_flow.get("api_profile", "desktop")
            client = client_for_session(profile)
            try:
                await client.connect()
                await client(
                    functions.account.ChangePhoneRequest(
                        phone_number=current_flow["new_phone"],
                        phone_code_hash=current_flow["phone_code_hash"],
                        phone_code=text,
                    )
                )
                updated_me = await client.get_me()
            finally:
                await client.disconnect()

            expected = current_flow["new_phone"]
            await finish_flow(
                message,
                "手机号码更改请求已完成。\n"
                f"更新后的号码与请求一致：{zh_bool(updated_me.phone == expected)}",
            )

        else:
            await show_menu(message, "请选择功能或使用 /cancel：")

    except Exception as exc:
        print(f"TEXT_FLOW_ERROR step={step} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"流程失败：{safe_error(exc)}")


async def start_health_server() -> web.AppRunner | None:
    """Bind Render's web-service port without starting a second process."""
    port_text = os.environ.get("PORT", "").strip()
    if not port_text:
        print("HEALTH_SERVER_DISABLED PORT_not_set", flush=True)
        return None

    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK\n", content_type="text/plain")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    print(f"HEALTH_SERVER_START host=0.0.0.0 port={port} path=/health", flush=True)
    return runner


async def self_ping_loop(url: str, interval: float) -> None:
    """Periodically request the configured public health URL."""
    timeout = ClientTimeout(total=25)
    async with ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.get(url) as response:
                    print(f"SELF_PING_STATUS status={response.status}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log the configured URL, headers, or network details.
                print(f"SELF_PING_ERROR type={type(exc).__name__}", flush=True)


def start_self_ping() -> asyncio.Task[None] | None:
    """Start optional keep-alive pings inside the existing bot process."""
    url = os.environ.get("SELF_PING_URL", "").strip()
    if not url:
        return None

    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError(
            "SELF_PING_URL must be a plain HTTP(S) public health URL without credentials, query, or fragment"
        )

    interval_text = os.environ.get("SELF_PING_INTERVAL", "120").strip()
    try:
        interval = int(interval_text)
    except ValueError as exc:
        raise RuntimeError("SELF_PING_INTERVAL must be an integer") from exc
    if not 60 <= interval <= 900:
        raise RuntimeError("SELF_PING_INTERVAL must be between 60 and 900 seconds")

    print(f"SELF_PING_START interval={interval}", flush=True)
    return asyncio.create_task(self_ping_loop(url, float(interval)))


async def main() -> None:
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=45))
    health_runner: web.AppRunner | None = None
    self_ping_task: asyncio.Task[None] | None = None

    try:
        # Bind before Telegram network calls so Render detects the port promptly.
        health_runner = await start_health_server()
        self_ping_task = start_self_ping()
        me = await bot.get_me()
        print(f"FULL_BOT_READY username=@{me.username} user_id={me.id}", flush=True)
        await bot.delete_webhook(drop_pending_updates=False)
        print("FULL_BOT_POLLING_START", flush=True)
        await dp.start_polling(
            bot,
            polling_timeout=20,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=True,
        )
    finally:
        reset_flow()
        if self_ping_task is not None:
            self_ping_task.cancel()
            await asyncio.gather(self_ping_task, return_exceptions=True)
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
