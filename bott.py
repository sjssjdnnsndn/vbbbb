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
from aiohttp import web

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


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Tdata → Telethon", callback_data="menu:convert"),
                InlineKeyboardButton(text="Check Session", callback_data="menu:check"),
            ],
            [
                InlineKeyboardButton(text="Masked Mail Info", callback_data="menu:pattern"),
                InlineKeyboardButton(text="Change Login Mail", callback_data="menu:email"),
            ],
            [
                InlineKeyboardButton(text="Change Phone Number", callback_data="menu:phone"),
                InlineKeyboardButton(text="Reset / Cancel", callback_data="flow:cancel"),
            ],
            [
                InlineKeyboardButton(
                    text="Bulk Bound Mail Change", callback_data="menu:bulk_email"
                )
            ],
        ]
    )


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Cancel", callback_data="flow:cancel")]
        ]
    )


def confirmation_menu(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm", callback_data=action),
                InlineKeyboardButton(text="Cancel", callback_data="flow:cancel"),
            ]
        ]
    )


def phone_profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Desktop API", callback_data="phone_profile:desktop"),
                InlineKeyboardButton(text="Android API", callback_data="phone_profile:android"),
            ],
            [InlineKeyboardButton(text="Cancel", callback_data="flow:cancel")],
        ]
    )


def phone_code_menu(can_resend: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_resend:
        rows.append(
            [InlineKeyboardButton(text="Resend / fallback", callback_data="phone:resend")]
        )
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bulk_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Start Process", callback_data="bulk:start")],
            [InlineKeyboardButton(text="Cancel", callback_data="flow:cancel")],
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


async def show_menu(message: Message, text: str = "Feature choose karo:") -> None:
    await message.answer(text, reply_markup=main_menu())


async def finish_flow(message: Message, text: str) -> None:
    reset_flow()
    await show_menu(message, text + "\n\nAgla feature choose karo:")


def validate_session_header(path: Path) -> None:
    with path.open("rb") as file:
        if file.read(16) != b"SQLite format 3\x00":
            raise ValueError("Ye valid Telethon SQLite session file nahi hai")


async def download_document(message: Message, destination: Path, max_size: int) -> None:
    document = message.document
    if document.file_size and document.file_size > max_size:
        raise ValueError(f"File size limit {max_size} bytes hai")
    telegram_file = await message.bot.get_file(document.file_id)
    await message.bot.download_file(telegram_file.file_path, destination=destination)
    if destination.stat().st_size > max_size:
        raise ValueError(f"Downloaded file size limit {max_size} bytes hai")


async def store_session_upload(message: Message) -> dict:
    document = message.document
    filename = document.file_name or "account.session"
    if Path(filename).suffix.lower() != ".session":
        raise ValueError("Is step mein Telethon .session document upload karo")

    temp_dir = make_temp_dir()
    current_flow["temp_dir"] = temp_dir
    session_path = Path(temp_dir) / "account.session"
    await download_document(message, session_path, MAX_SESSION_SIZE)
    validate_session_header(session_path)

    status = await check_session(session_path)
    if not status["session_authorized"]:
        raise ValueError("Session Telegram par authorized nahi hai ya revoked hai")

    current_flow["session_path"] = str(session_path)
    current_flow["account_user_id"] = status["user_id"]
    return status


def client_for_session(profile: str = "desktop") -> TelegramClient:
    session_path = current_flow.get("session_path")
    if not session_path:
        raise ValueError("Session missing hai; flow dobara start karo")
    api = API.TelegramAndroid if profile == "android" else API.TelegramDesktop
    return TelegramClient(session_path, api=api, receive_updates=False)


def extract_tdata(zip_path: Path, output_root: Path) -> Path:
    extracted = output_root / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    count = 0

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if not members:
            raise ValueError("ZIP khaali hai")
        if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_SIZE:
            raise ValueError("ZIP ka uncompressed size allowed limit se zyada hai")
        if any(member.flag_bits & 0x1 for member in members):
            raise ValueError("Password-protected ZIP supported nahi hai")

        for info in members:
            path = PurePosixPath(info.filename)
            if info.is_dir() or "tdata" not in path.parts:
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("ZIP mein unsafe path hai")
            index = path.parts.index("tdata")
            target = extracted / Path(*path.parts[index:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            count += 1

    if count == 0:
        raise ValueError("ZIP mein tdata folder nahi mila")
    return extracted / "tdata"


async def convert_uploaded_tdata(zip_path: Path, output: Path) -> None:
    tdata_dir = extract_tdata(zip_path, zip_path.parent)
    tdesk = TDesktop(str(tdata_dir))
    if not tdesk.isLoaded():
        raise ValueError(
            "Authorized tdata load nahi hua. Local-passcode-protected tdata ke liye local script use karo"
        )

    client = await tdesk.ToTelethon(
        session=str(output),
        flag=UseCurrentSession,
        api=API.TelegramDesktop,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Converted session Telegram par authorized nahi hai")
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
            raise BulkInputError("Is step mein .txt document upload karo")
        temp_dir = Path(get_bulk_temp_dir())
        destination = temp_dir / "bulk_emails.txt"
        await download_document(message, destination, MAX_LIST_BYTES)
        try:
            text = destination.read_text(encoding="utf-8-sig")
        finally:
            destination.unlink(missing_ok=True)

        targets = parse_email_links(text)
        if len(targets) > MAX_BULK_ACCOUNTS:
            raise BulkInputError(f"Maximum {MAX_BULK_ACCOUNTS} accounts allowed hain")
        current_flow["bulk_targets"] = targets
        current_flow["bulk_items"] = []
        current_flow["bulk_upload_lock"] = asyncio.Lock()
        current_flow["step"] = "bulk_tdata"
        await message.answer(
            f"{len(targets)} email targets received. Ab same sequence mein tdata ZIPs bhejo. "
            "Har ZIP ka count hoga. Start Process dabakar aap confirm karte ho ki saare "
            "accounts/sessions/emails/inboxes owned/authorized hain.",
            reply_markup=bulk_start_menu(),
        )
        return

    if step != "bulk_tdata":
        raise BulkInputError("Bulk step expired hai")
    if Path(document.file_name or "").suffix.lower() != ".zip":
        raise BulkInputError("Is step mein tdata .zip document upload karo")

    lock = current_flow["bulk_upload_lock"]
    async with lock:
        items = current_flow["bulk_items"]
        targets = current_flow["bulk_targets"]
        if len(items) >= len(targets):
            raise BulkInputError("Expected ZIP count already complete hai; Start Process use karo")
        index = len(items) + 1
        item_dir = Path(get_bulk_temp_dir()) / f"item_{index:04d}"
        item_dir.mkdir(parents=True, exist_ok=False)
        zip_path = item_dir / "upload.zip"
        try:
            await download_document(message, zip_path, MAX_ZIP_SIZE)
            if not zipfile.is_zipfile(zip_path):
                raise BulkInputError("Valid tdata ZIP nahi hai")
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
            f"tdata ZIP received: {len(items)}/{len(targets)}. "
            "Aur ZIPs bhej sakte ho ya sab receive hone ke baad Start Process dabao.",
            reply_markup=bulk_start_menu(),
        )


async def run_bulk_flow(job_id: str, message: Message, items: list[BulkItem], temp_dir: str) -> None:
    async def notify(text: str) -> None:
        final = text.startswith(("Bulk process complete", "Kisi account", "Bulk process unexpected"))
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
        await notify("Bulk process unexpected error se ruka. Temporary files clean ki ja rahi hain.")
    finally:
        if current_flow.get("bulk_job_id") == job_id:
            current_flow.clear()
        shutil.rmtree(temp_dir, ignore_errors=True)


async def start_bulk_job(message: Message) -> None:
    if current_flow.get("step") != "bulk_tdata":
        raise ValueError("Bulk upload flow expired hai")
    items = current_flow.get("bulk_items", [])
    targets = current_flow.get("bulk_targets", [])
    if not items:
        await message.answer("Pehle kam se kam ek tdata ZIP upload karo.")
        return
    if len(items) != len(targets):
        await message.answer(
            f"Count incomplete: email targets={len(targets)}, ZIPs={len(items)}. "
            "Expected count tak ZIPs upload karo ya Cancel karke corrected list do.",
            reply_markup=bulk_start_menu(),
        )
        return

    job_id = secrets.token_hex(8)
    temp_dir = current_flow["temp_dir"]
    current_flow["step"] = "bulk_running"
    current_flow["bulk_job_id"] = job_id
    await message.answer(
        "Start confirmed: aapne saare accounts/sessions/emails/inboxes ki "
        "ownership/authorization confirm ki. Bulk process background task mein start hoga; "
        "Cancel se stop kar sakte ho.",
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
        "Welcome to Telegram Session Tools Bot!\n"
        "Developer: @BioBhaiya\n\n"
        "Tdata conversion, session checks, phone/email flows aur bulk bound-mail "
        "tools ke liye pairing complete karo."
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
                        caption="Final full source delivery: converter, checker, account-change flows aur inline-menu bot.",
                    )
                    SOURCE_ONCE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
                    SOURCE_ONCE_MARKER_PATH.touch(exist_ok=True)
                    print(f"SOURCE_BUNDLE_SENT_ONCE user_id={user_id}", flush=True)
                except Exception as exc:
                    print(f"SOURCE_BUNDLE_ERROR error={safe_error(exc)}", flush=True)
            else:
                print("SOURCE_BUNDLE_ERROR file_not_found", flush=True)

    if is_authorized(message):
        await show_menu(message, "Bot ready hai. Feature choose karo:")
    else:
        await message.answer(
            "Full Telegram session tool bot hai. Pehle pairing karo:\n"
            "/pair YOUR_PAIRING_CODE"
        )


@dp.message(Command("menu"))
async def menu_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("Pehle /start aur /pair use karo")
        return
    await show_menu(message)


@dp.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    if is_authorized(message):
        reset_flow()
    await show_menu(message, "Flow cancel/reset ho gaya. Feature choose karo:")


@dp.message(Command("pair"))
async def pair_handler(message: Message) -> None:
    global authorized_user_id

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not secrets.compare_digest(parts[1].strip(), PAIR_CODE):
        await message.answer("Pairing code galat hai. Format: /pair YOUR_PAIRING_CODE")
        return

    if authorized_user_id is not None and authorized_user_id != message.from_user.id:
        await message.answer("Bot kisi aur user ke saath paired hai")
        return

    authorized_user_id = message.from_user.id
    reset_flow()
    print(f"PAIRED user_id={authorized_user_id}", flush=True)
    await show_menu(message, "Pairing successful. Feature choose karo:")


@dp.callback_query()
async def callback_handler(callback: CallbackQuery) -> None:
    if not is_authorized(callback):
        await callback.answer("Not authorized", show_alert=True)
        return

    await callback.answer()
    data = callback.data or ""
    message = callback.message

    try:
        if data == "flow:cancel":
            await finish_flow(message, "Flow cancel ho gaya")
            return

        if data.startswith("menu:"):
            reset_flow()
            feature = data.split(":", 1)[1]
            prompts = {
                "convert": (
                    "convert_zip",
                    "Complete tdata ZIP upload karo. Bot convert karke .session document bhejega.",
                ),
                "check": (
                    "check_session",
                    "Telethon .session upload karo. Validity aur mail/2FA status milega.",
                ),
                "pattern": (
                    "pattern_session",
                    "Telethon .session upload karo. Sirf Telegram ka masked email pattern check hoga.",
                ),
                "email": (
                    "email_session",
                    "Login email change ke liye authorized Telethon .session upload karo.",
                ),
                "phone": (
                    "phone_session",
                    "Phone number change ke liye authorized Telethon .session upload karo.",
                ),
                "bulk_email": (
                    "bulk_emails",
                    "Bulk bound mail change ke liye email+OTP links wali .txt upload karo. "
                    "Email targets aur tdata ZIPs ka sequence/count same hona chahiye.",
                ),
            }
            if feature not in prompts:
                raise ValueError("Unknown feature")
            step, prompt = prompts[feature]
            current_flow["step"] = step
            await message.answer(prompt, reply_markup=cancel_menu())
            return

        if data == "bulk:start":
            await start_bulk_job(message)
            return

        if data == "email:confirm":
            if current_flow.get("step") != "email_confirm":
                raise ValueError("Email flow expired hai; dobara start karo")
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
                f"New email par {sent.length}-digit code bheja gaya hai. "
                "Received code yahan bhejo.",
                reply_markup=cancel_menu(),
            )
            return

        if data.startswith("phone_profile:"):
            if current_flow.get("step") != "phone_profile":
                raise ValueError("Phone flow expired hai; dobara start karo")
            profile = data.split(":", 1)[1]
            if profile not in {"desktop", "android"}:
                raise ValueError("Unknown API profile")
            current_flow["api_profile"] = profile
            current_flow["step"] = "phone_number"
            await message.answer(
                f"{profile.title()} API profile selected. New phone number country code ke saath bhejo, jaise +919876543210.",
                reply_markup=cancel_menu(),
            )
            return

        if data == "phone:confirm":
            if current_flow.get("step") != "phone_confirm":
                raise ValueError("Phone flow expired hai; dobara start karo")
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
                        "Telegram ne immediate success diya, lekin account phone match nahi hua"
                    )
                await finish_flow(message, "Phone number successfully change ho gaya")
                return

            sent = require_standard_sent_code(response)
            delivery, next_type, resend_delay = phone_code_details(sent)
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
                f" Fallback {next_type} ke liye resend {resend_delay}s baad available hai."
                if next_type
                else " Telegram ne is challenge ke liye fallback advertise nahi kiya."
            )
            await message.answer(
                f"Verification code request accept hui. Delivery: {delivery}; "
                f"length: {getattr(sent.type, 'length', 'unknown')}."
                f"{fallback_text} Received code yahan bhejo.",
                reply_markup=phone_code_menu(bool(next_type)),
            )
            return

        if data == "phone:resend":
            if current_flow.get("step") != "phone_code":
                raise ValueError("Phone-code flow expired hai; dobara start karo")
            next_type = current_flow.get("phone_next_type")
            if not next_type:
                await message.answer("Telegram ne is challenge ke liye fallback advertise nahi kiya.")
                return
            if current_flow.get("phone_resend_count", 0) >= 1:
                await message.answer("Safe one-resend limit already use ho chuki hai.")
                return
            remaining = max(
                0,
                int(current_flow.get("phone_resend_at", 0) - time.monotonic() + 0.999),
            )
            if remaining:
                await message.answer(f"Resend se pehle {remaining}s aur wait karo.")
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
                        "Resend ke baad immediate success mila, lekin phone match nahi hua"
                    )
                await finish_flow(message, "Phone number successfully change ho gaya")
                return

            sent = require_standard_sent_code(response)
            delivery, new_next_type, resend_delay = phone_code_details(sent)
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
                f"Naya verification challenge mila. Delivery: {delivery}; "
                f"length: {getattr(sent.type, 'length', 'unknown')}. "
                "Ab sirf latest code use karo.",
                reply_markup=phone_code_menu(False),
            )
            return

        await message.answer("Unknown/expired button. Menu se dobara choose karo.")

    except Exception as exc:
        print(f"CALLBACK_FLOW_ERROR data={data} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"Flow failed: {safe_error(exc)}")


@dp.message(F.document)
async def document_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("Pehle /start aur /pair use karo")
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
                caption="Converted aur Telegram-validated Telethon session. Is file ko private rakho.",
            )
            await finish_flow(message, "Tdata conversion complete")

        elif step in {"check_session", "pattern_session"}:
            current_flow["step"] = "busy"
            status = await store_session_upload(message)

            if step == "check_session":
                text = (
                    "Session Telegram par valid hai.\n\n"
                    f"Verified login/bound email set: {status['verified_login_email_set']}\n"
                    f"2FA password enabled: {status['two_fa_password_set']}\n"
                    f"Recovery method configured: {status['recovery_method_configured']}\n"
                    "Unconfirmed recovery email pending: "
                    f"{status['unconfirmed_recovery_email_pending']}"
                )
            else:
                client = client_for_session()
                try:
                    await client.connect()
                    info = await client(functions.account.GetPasswordRequest())
                finally:
                    await client.disconnect()
                text = (
                    "Telegram ke masked email patterns:\n\n"
                    f"Login email: {info.login_email_pattern or 'NOT SET'}\n"
                    "Unconfirmed recovery email: "
                    f"{info.email_unconfirmed_pattern or 'NOT SET'}"
                )
            await finish_flow(message, text)

        elif step == "email_session":
            current_flow["step"] = "busy"
            await store_session_upload(message)
            current_flow["step"] = "email_address"
            await message.answer(
                "Session valid hai. Ab new login email address bhejo.",
                reply_markup=cancel_menu(),
            )

        elif step == "phone_session":
            current_flow["step"] = "busy"
            await store_session_upload(message)
            current_flow["step"] = "phone_profile"
            await message.answer(
                "Session valid hai. OTP request ke liye API profile choose karo.",
                reply_markup=phone_profile_menu(),
            )

        elif step in {"bulk_emails", "bulk_tdata"}:
            await handle_bulk_document(message, step)

        elif step == "bulk_running":
            await message.answer("Bulk process running hai. Stop karne ke liye Cancel use karo.")

        else:
            await message.answer(
                "Pehle menu se feature choose karo, phir required file upload karo.",
                reply_markup=main_menu(),
            )

    except Exception as exc:
        if step and step.startswith("bulk"):
            print(
                f"BULK_INPUT_ERROR step={step} error={type(exc).__name__}",
                flush=True,
            )
            detail = str(exc) if isinstance(exc, BulkInputError) else "Bulk upload/read failed"
            await message.answer(
                f"{detail}. Corrected file dobara bhejo ya Cancel use karo.",
                reply_markup=bulk_start_menu() if step == "bulk_tdata" else cancel_menu(),
            )
            return
        print(f"DOCUMENT_FLOW_ERROR step={step} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"Flow failed: {safe_error(exc)}")


@dp.message()
async def text_handler(message: Message) -> None:
    if not is_authorized(message):
        await message.answer("Pehle /start aur /pair use karo")
        return

    step = current_flow.get("step")
    text = (message.text or "").strip()

    try:
        if step == "email_address":
            local, separator, domain = text.partition("@")
            if not separator or not local or not domain or "@" in domain:
                raise ValueError("Valid new email address bhejo")
            current_flow["new_email"] = text
            current_flow["step"] = "email_confirm"
            await message.answer(
                "Is new email par Telegram verification code bheja jayega. Confirm karo.",
                reply_markup=confirmation_menu("email:confirm"),
            )

        elif step == "email_code":
            if not text.isdigit():
                raise ValueError("Verification code sirf digits mein bhejo")
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
                "Login email successfully update ho gayi.\n"
                f"Updated masked pattern: {info.login_email_pattern or 'NOT SET'}",
            )

        elif step == "phone_number":
            current_flow["new_phone"] = normalize_phone_number(text)
            current_flow["step"] = "phone_confirm"
            await message.answer(
                "Is new number par verification code request hogi. Confirm karo.",
                reply_markup=confirmation_menu("phone:confirm"),
            )

        elif step == "phone_code":
            if not text.isdigit():
                raise ValueError("Verification code sirf digits mein bhejo")
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
                "Phone number change request complete.\n"
                f"Updated phone matches requested: {updated_me.phone == expected}",
            )

        else:
            await show_menu(message, "Feature choose karo ya /cancel use karo:")

    except Exception as exc:
        print(f"TEXT_FLOW_ERROR step={step} error={safe_error(exc)}", flush=True)
        await finish_flow(message, f"Flow failed: {safe_error(exc)}")


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


async def main() -> None:
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=45))
    health_runner: web.AppRunner | None = None

    try:
        # Bind before Telegram network calls so Render detects the port promptly.
        health_runner = await start_health_server()
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
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
